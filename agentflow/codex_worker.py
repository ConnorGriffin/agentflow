"""Bounded AgentFlow-owned Codex worker command (#555)."""

from __future__ import annotations

import argparse
import codecs
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from typing import Callable, TextIO

from agentflow.routing import RoutingConfigError, routing
from agentflow.runner import CodexRunner

MAX_TIMEOUT_S = 900
_EFFORT_LABELS = frozenset({"low", "medium", "high", "extra"})
# Codex's exec handles and any process groups it creates internally are opaque at this wrapper
# seam. There is no low-false-positive way to detect duplicate inner pytest launches here, so the
# concrete single-flight polling rule remains the enforceable control. Group cleanup below owns
# only the detached group created by this wrapper; it does not claim broader process-tree control.
_WORKER_PREAMBLE = (
    "You are an AgentFlow-routed Codex worker. Complete only the task supplied in the stdin "
    "block for this turn. Run at most one long-running shell command at a time. When an exec "
    "tool yields a running handle, cell, or session, poll that exact handle until terminal and "
    "use its eventual result. Never reissue the same command while its prior handle may still "
    "be alive. Never start a second test process to check whether the first finished."
)


def worker_argv(worker: str, effort: str) -> list[str]:
    """Build a routed worker invocation with model and effort as explicit CLI arguments."""
    if effort not in _EFFORT_LABELS:
        raise ValueError(f"unknown routed effort label: {effort!r}")
    model = routing.codex_worker_cli_identifier(worker)
    rung = routing.worker_reasoning(effort)
    argv = CodexRunner().structured_argv(_WORKER_PREAMBLE, model, os.getcwd())
    return argv[:-1] + ["-c", f"model_reasoning_effort={rung}", argv[-1]]


def _private_stdin(stream: TextIO) -> None:
    """Read a mode-0600 regular stdin file without accepting a path-bearing argument."""
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("stdin must be a private regular file (mode 0600)")
    if info.st_size == 0:
        raise ValueError("worker task must not be empty")


class _ProcessGroupStopper:
    """Idempotently stop the one process group the wrapper creates."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        self.process = process
        self._lock = threading.Lock()
        self._stopping = False
        self._failures: tuple[BaseException, ...] = ()
        self._done = threading.Event()

    def stop(self) -> None:
        with self._lock:
            owner = not self._stopping
            self._stopping = True
        if not owner:
            self._done.wait()
            self._raise_failure()
            return

        try:
            try:
                term_sent = self._signal(signal.SIGTERM)
            except BaseException as term_failure:
                # A failed TERM says nothing reliable about whether the group is alive. Preserve
                # that first cause, but still make one prompt best-effort KILL attempt.
                self._record_failure(term_failure)
                try:
                    self._signal(signal.SIGKILL)
                except BaseException as kill_failure:
                    self._record_failure(kill_failure)
                self._raise_failure()
            if not term_sent:
                return
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    break
                self._done.wait(0.05)
            # The group leader may exit before its descendants. KILL the same wrapper-created
            # group even after a graceful leader exit; ProcessLookupError is the clean no-op.
            self._signal(signal.SIGKILL)
        except BaseException as exc:
            self._record_failure(exc)
            raise
        finally:
            self._done.set()

    def _signal(self, signum: int) -> bool:
        try:
            os.killpg(self.process.pid, signum)
        except ProcessLookupError:
            return False
        return True

    def _raise_failure(self) -> None:
        with self._lock:
            failures = self._failures
        if failures:
            raise failures[0]

    def _record_failure(self, failure: BaseException) -> None:
        with self._lock:
            if not any(recorded is failure for recorded in self._failures):
                self._failures += (failure,)


def _timeout_output(process: subprocess.Popen[str], exc: subprocess.TimeoutExpired) -> str:
    """Return TimeoutExpired's partial combined stream in the wrapper's text encoding."""
    partial = exc.output or ""
    if isinstance(partial, bytes):
        stream = process.stdout
        encoding = getattr(stream, "encoding", None) or "utf-8"
        errors = getattr(stream, "errors", None) or "replace"
        # TimeoutExpired may expose raw bytes even in text mode, and the timeout can split a
        # multibyte character. An incremental decode emits the valid prefix while retaining the
        # incomplete tail; ``final=False`` prevents that tail from replacing cleanup failure.
        decoder = codecs.getincrementaldecoder(encoding)(errors=errors)
        return decoder.decode(partial, final=False)
    return partial


def _finish_run(
    cancelled: Callable[[], bool],
    previous_handler,
    stopper: _ProcessGroupStopper | None,
    watcher_finished: threading.Event,
    watcher: threading.Thread | None,
    watcher_started: bool,
    watcher_failure: Callable[[], BaseException | None],
    output: str,
    failure: BaseException | None,
    timed_out: bool,
    timeout: int,
) -> tuple[bool, BaseException | None]:
    """Finish every wrapper-owned lifecycle action, then restore the caller's handler last."""
    teardown_failure = failure
    try:
        try:
            # Always close the wrapper-created group. Besides making cancellation idempotent, this
            # catches same-group descendants whose leader exited and closed its output first.
            if stopper is not None:
                stopper.stop()
        except BaseException as exc:
            if teardown_failure is None:
                teardown_failure = exc

        watcher_finished.set()
        if watcher_started:
            assert watcher is not None
            try:
                watcher.join()
            except BaseException as exc:
                if teardown_failure is None:
                    teardown_failure = exc
        if teardown_failure is None and watcher_failure() is not None:
            teardown_failure = watcher_failure()

        # A SIGTERM immediately before helper entry or during cleanup/join only changes the plain
        # flag. Cleanup above is unconditional, so no repeated signal can strand this group.
        if output:
            try:
                sys.stdout.write(output)
            except BaseException as exc:
                if teardown_failure is None:
                    teardown_failure = exc
        interrupted = cancelled()
        if interrupted and teardown_failure is None:
            print("agentflow: Codex worker interrupted", file=sys.stderr)
        elif teardown_failure is None and timed_out:
            print(f"agentflow: Codex worker timed out after {timeout}s", file=sys.stderr)
    finally:
        # Ordering contract: group cleanup, watcher stop/join, final cancellation snapshot, and
        # provider-output ownership all end before the caller's handler is restored.
        signal.signal(signal.SIGTERM, previous_handler)

    # This read only closes the last instruction-boundary handoff: if SIGTERM ran immediately
    # before restoration, the temporary handler set the flag. The group is already irreversibly
    # stopped, so this classification requires no wrapper lifecycle work after restoration.
    return interrupted or cancelled(), teardown_failure


def run(worker: str, effort: str, timeout: int, stdin: TextIO) -> int:
    """Run one worker and return its result, terminating its complete process group if stopped."""
    if not 0 < timeout <= MAX_TIMEOUT_S:
        raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT_S} seconds")
    _private_stdin(stdin)
    argv = worker_argv(worker, effort)
    cancelled = False
    watcher_finished = threading.Event()
    process: subprocess.Popen[str] | None = None
    stopper: _ProcessGroupStopper | None = None
    watcher: threading.Thread | None = None
    watcher_started = False
    watcher_failure: BaseException | None = None
    output = ""
    timed_out = False
    failure: BaseException | None = None

    def cancel(_signum, _frame) -> None:
        nonlocal cancelled
        # One plain assignment is the entire handler contract. It takes no locks, allocates no
        # synchronization objects, raises nothing, and remains safe under nested SIGTERM delivery.
        cancelled = True

    previous_handler = signal.signal(signal.SIGTERM, cancel)
    try:
        process = subprocess.Popen(
            argv, cwd=os.getcwd(), stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True,
        )
        stopper = _ProcessGroupStopper(process)

        def watch_cancellation() -> None:
            nonlocal watcher_failure
            while not watcher_finished.is_set():
                if cancelled:
                    try:
                        stopper.stop()
                    except BaseException as exc:
                        # ``stop`` durably records the first cleanup failure before raising. The
                        # main thread observes that same object through its teardown ``stop`` call;
                        # this thread-local record also makes the joined watcher outcome explicit.
                        watcher_failure = exc
                    return
                watcher_finished.wait(0.01)

        watcher = threading.Thread(
            target=watch_cancellation, name="agentflow-codex-worker-cancellation",
        )
        watcher.start()
        watcher_started = True
        try:
            output, _ = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            # Keep this only as a cleanup-failure fallback. Successful cleanup replaces it with
            # the retry's complete stream, so TimeoutExpired bytes are never printed twice.
            output = _timeout_output(process, exc)
            stopper.stop()
            # The retry returns the complete buffered stream, including the first call's partial
            # bytes. Writing only this value preserves provider output exactly once.
            output, _ = process.communicate()
    except BaseException as exc:
        failure = exc
    finally:
        interrupted, failure = _finish_run(
            lambda: cancelled, previous_handler, stopper, watcher_finished, watcher,
            watcher_started, lambda: watcher_failure, output, failure, timed_out, timeout,
        )

    if failure is not None:
        raise failure
    if interrupted:
        return 143
    if timed_out:
        return 124
    assert process is not None
    return process.returncode


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentflow-codex-worker")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        raise SystemExit(run(args.worker, args.effort, args.timeout, sys.stdin))
    except (OSError, RoutingConfigError, ValueError) as exc:
        print(f"agentflow: Codex worker failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
