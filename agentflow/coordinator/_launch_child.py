"""The provider-launch bootstrap child (ADR 0030).

Run as
``python -m agentflow.coordinator._launch_child <store_path> <identity> <token> <timeout>
[--build-lease <provider> <silent> <test> <absolute>] <working_dir> [argv...]``.

It double-forks so the provider family is reparented away from the daemon (and so an ended
provider never lingers as a zombie the daemon would misread as alive), then makes a *guarded*
durable ``started`` write with the detached supervisor's pid as the family — recorded only if
this reservation still holds ``token``. Recording the fact before provider spawn is the crash
boundary: the attempt is recoverable even if the provider exits immediately or the daemon
dies before observing it. The guard is the second half of that boundary: if the coordinator
already disowned this launch on a handshake timeout (rotating the token) or returned the
record to waiting, the write is refused and the child exits *without* becoming a provider — and
With no provider argv (the dormant slice) it exits after a successful start, which reconciliation
reads as a started-but-ended attempt.
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from agentflow.coordinator.session import events_path, write_result
from agentflow.coordinator.store import Store


def _head(working_dir: str) -> str | None:
    if not working_dir:
        return None
    try:
        return subprocess.run(["git", "-C", working_dir, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=2).stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


_TEST_PREFIXES = (("pytest",), ("uv", "run", "pytest"), ("npm", "test"),
                  ("npm", "run", "test"), ("pnpm", "test"), ("yarn", "test"),
                  ("cargo", "test"), ("go", "test"), ("make", "test"))


def _recognized_test(command: object, provider: str) -> bool:
    """Recognize only the provider's canonical, single test-command shape.

    Codex reports the shell wrapper it owns; Claude reports the Bash input itself. Shell
    composition is deliberately refused: a pipeline or chained command's eventual success is
    not proof that the named test succeeded, and supervising it would turn arbitrary commands
    beginning with ``pytest`` into test work.
    """
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if provider == "codex":
        if len(words) != 3 or words[:2] != ["/bin/zsh", "-lc"]:
            return False
        try:
            words = shlex.split(words[2])
        except ValueError:
            return False
    elif provider != "claude":
        return False
    # shlex preserves operators as words only when spaced; rejecting their characters as well
    # closes compact forms such as ``pytest|tail`` and redirections such as ``2>&1``.
    if not words or any(any(char in word for char in ";&|<>") for word in words):
        return False
    return any(tuple(words[:len(prefix)]) == prefix for prefix in _TEST_PREFIXES)


def _worktree_path(value: object, working_dir: str) -> bool:
    if not isinstance(value, str) or not value or not working_dir:
        return False
    root = Path(working_dir).resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        candidate.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


class _ProgressStream:
    """Incrementally decode one provider's durable stream into canonical Build facts."""

    def __init__(self, provider: str, working_dir: str) -> None:
        self.provider = provider
        self.working_dir = working_dir
        self.offset = 0
        self.partial = b""
        self.calls: dict[str, tuple[str, object] | None] = {}
        self.seen: set[str] = set()
        self.active_tests: dict[str, float] = {}

    def _remember(self, call_id: object, value: tuple[str, object], now: float) -> None:
        if not isinstance(call_id, str) or not call_id:
            return
        fact = f"{self.provider}:{value[0]}:{call_id}"
        if fact in self.seen:
            return
        if call_id not in self.calls:
            self.calls[call_id] = value
            if value[0] == "test":
                self.active_tests[call_id] = now
        elif self.calls[call_id] != value:
            # A provider id is canonical. Reusing it for a different action makes both
            # records ambiguous, so neither can renew or retain test supervision.
            self.calls[call_id] = None
            self.active_tests.pop(call_id, None)

    def _complete(self, call_id: object, expected: tuple[str, object], success: bool) -> bool:
        if not isinstance(call_id, str) or self.calls.get(call_id) != expected:
            return False
        self.active_tests.pop(call_id, None)
        fact = f"{self.provider}:{expected[0]}:{call_id}"
        if not success or fact in self.seen:
            return False
        self.seen.add(fact)
        return True

    def _codex(self, event: dict[str, object], now: float) -> bool:
        event_type = event.get("type")
        item = event.get("item")
        if event_type not in {"item.started", "item.completed"} or not isinstance(item, dict):
            return False
        call_id = item.get("id")
        item_type = item.get("type")
        if item_type == "command_execution":
            command = item.get("command")
            expected = ("test", command)
            if event_type == "item.started":
                if (item.get("status") == "in_progress" and item.get("exit_code") is None
                        and _recognized_test(command, "codex")):
                    self._remember(call_id, expected, now)
                return False
            if not isinstance(call_id, str) or self.calls.get(call_id) != expected:
                if isinstance(call_id, str):
                    self.active_tests.pop(call_id, None)
                return False
            exit_code = item.get("exit_code")
            valid = (item.get("status") == "completed"
                     and isinstance(exit_code, int) and not isinstance(exit_code, bool))
            if not valid:
                self.active_tests.pop(call_id, None)
                self.calls[call_id] = None
                return False
            return self._complete(call_id, expected, exit_code == 0)
        if event_type != "item.completed" or item_type != "file_change":
            return False
        changes = item.get("changes")
        valid = (item.get("status") == "completed" and isinstance(changes, list) and changes
                 and all(isinstance(change, dict)
                         and change.get("kind") in {"add", "update", "delete"}
                         and self._local_change(change.get("path")) for change in changes))
        if not valid:
            return False
        expected = ("edit", tuple((change["path"], change["kind"]) for change in changes))
        self._remember(call_id, expected, now)
        return self._complete(call_id, expected, True)

    def _local_change(self, value: object) -> bool:
        return _worktree_path(value, self.working_dir)

    def _claude(self, event: dict[str, object], now: float) -> bool:
        message = event.get("message")
        if not isinstance(message, dict) or message.get("type") != "message":
            return False
        event_type = event.get("type")
        role = message.get("role")
        if (event_type, role) not in {("assistant", "assistant"), ("user", "user")}:
            return False
        content = message.get("content")
        if not isinstance(content, list):
            return False
        renewed = False
        for block in content:
            if not isinstance(block, dict):
                continue
            if event_type == "assistant" and block.get("type") == "tool_use":
                name, payload = block.get("name"), block.get("input")
                if name == "Bash" and isinstance(payload, dict):
                    command = payload.get("command")
                    if _recognized_test(command, "claude"):
                        self._remember(block.get("id"), ("test", command), now)
                elif name in {"Edit", "Write", "NotebookEdit"} and isinstance(payload, dict):
                    path = payload.get("notebook_path" if name == "NotebookEdit" else "file_path")
                    if self._local_change(path):
                        self._remember(block.get("id"), ("edit", (name, path)), now)
            elif event_type == "user" and block.get("type") == "tool_result":
                call_id = block.get("tool_use_id")
                call = self.calls.get(call_id) if isinstance(call_id, str) else None
                if call is None:
                    continue
                if block.get("is_error") is not False:
                    self.active_tests.pop(call_id, None)
                    self.calls[call_id] = None
                    continue
                renewed = self._complete(call_id, call, True) or renewed
        return renewed

    def poll(self, events: Path, now: float) -> bool:
        try:
            with events.open("rb") as stream:
                stream.seek(self.offset)
                chunk = stream.read()
        except OSError:
            return False
        self.offset += len(chunk)
        parts = (self.partial + chunk).split(b"\n")
        self.partial = parts.pop()
        renewed = False
        decoder = {"codex": self._codex, "claude": self._claude}.get(self.provider)
        if decoder is None:
            return False
        for raw in parts:
            try:
                event = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(event, dict):
                renewed = decoder(event, now) or renewed
        return renewed


def _mark_active(working_dir: str) -> Path | None:
    """Mirror the detached supervisor pid into the legacy worktree-liveness marker.

    Startup recovery still consults this current-format marker before coordinator
    reconciliation. Keeping it for the supervisor's lifetime prevents that recovery pass from
    removing a clean coordinator-owned worktree while its provider is alive.
    """
    if not working_dir:
        return None
    from agentflow.runner import _active_marker
    marker = _active_marker(Path(working_dir))
    if marker is not None:
        marker.write_text(str(os.getpid()))
    return marker


def _clear_active(marker: Path | None) -> None:
    if marker is None:
        return
    try:
        if marker.read_text().strip() == str(os.getpid()):
            marker.unlink()
    except OSError:
        pass


def main(args: list[str]) -> None:
    store_path, identity, token, timeout, *tail = args
    build_lease: tuple[float, float, float] | None = None
    progress_provider = ""
    if tail[:1] == ["--build-lease"]:
        progress_provider, silent, test_grace, absolute, *tail = tail[1:]
        build_lease = (float(silent), float(test_grace), float(absolute))
    working_dir, *provider = tail
    # Double-fork: the intermediate exits immediately so the daemon reaps it at once, while
    # the detached supervisor is reparented to init and cannot zombie under the daemon.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    # Once child_start publishes this pid, reconciliation may signal it immediately. Install
    # the handler before that family becomes externally visible; it only records intent so a
    # request arriving inside store or process startup cannot strand an async exception there.
    signal.signal(signal.SIGTERM, request_stop)
    store = Store(store_path)
    won = store.child_start(identity, token, os.getpid())
    store.close()
    if not won:
        # Our reservation is gone; starting a provider now would be unreserved. Role-override
        # generation only ever happens further down, immediately before this supervisor's own
        # Popen call — nothing has been generated yet at this point, so there is nothing to
        # clean up here.
        os._exit(0)
    marker = _mark_active(working_dir)
    if not provider:
        _clear_active(marker)
        os._exit(0)  # dormant: no provider to become; a started-then-ended attempt
    # Remain as the recorded family supervisor while the provider runs in its own process
    # group. Output streams directly to its durable artifact, so partial output survives a
    # daemon crash. The supervisor records exit/signal/timeout facts after the whole provider
    # family ends; it can terminate that family without killing itself when the deadline fires.
    events = events_path(store_path, token)
    events.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with events.open("w") as output:
        try:
            process = subprocess.Popen(
                provider, cwd=working_dir or None, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            # No provider family ever came into existence for this attempt.
            write_result(store_path, token, exit_status=None, signal=None, timed_out=False)
            _clear_active(marker)
            os._exit(0)

        def stop_provider() -> int:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Do not claim this attempt ended while its provider may still be running.
                # The event artifact is the durable operator-facing record of why this
                # supervisor remains until it can observe the provider's real exit.
                print("agentflow: permission denied stopping provider process group; "
                      "waiting for provider exit", file=output, flush=True)
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    print("agentflow: permission denied force-stopping provider process group; "
                          "waiting for provider exit", file=output, flush=True)
                return process.wait()

        # Reconciliation signals this supervisor, not the provider's separate session. Turn that
        # request into the same orderly process-group shutdown the deadline path uses, then keep
        # the supervisor alive to write the provider's durable end facts.
        started_at = time.monotonic()
        deadline = started_at + float(timeout)
        silent_deadline = started_at + build_lease[0] if build_lease else deadline
        absolute_deadline = started_at + build_lease[2] if build_lease else deadline
        last_head = _head(working_dir) if build_lease else None
        head_poll_s = min(5.0, build_lease[0] / 4) if build_lease else 0
        next_head_poll = started_at + head_poll_s
        progress_stream = _ProgressStream(progress_provider, working_dir) if build_lease else None
        while True:
            if stop_requested:
                returncode = stop_provider()
                break
            now = time.monotonic()
            if build_lease:
                # A completion first observed after its already-active test cap cannot erase
                # that cap. Preserve the pre-poll fact and enforce it after consuming this
                # batch; provider events have no trustworthy monotonic timestamp of their own.
                active_test_deadline = (min(progress_stream.active_tests.values())
                                        + build_lease[1]
                                        if progress_stream.active_tests else None)
                test_expired = (active_test_deadline is not None
                                and now >= active_test_deadline)
                progressed = False
                if now >= next_head_poll:
                    head = _head(working_dir)
                    progressed = head is not None and head != last_head
                    if progressed:
                        last_head = head
                    next_head_poll = now + head_poll_s
                progressed = progress_stream.poll(events, now) or progressed
                if progressed:
                    silent_deadline = now + build_lease[0]
                test_deadline = min(progress_stream.active_tests.values(), default=0) + build_lease[1]
                deadline = (min(absolute_deadline, test_deadline)
                            if progress_stream.active_tests
                            else min(absolute_deadline, silent_deadline))
                if test_expired:
                    deadline = now
            remaining = deadline - now
            if remaining <= 0:
                timed_out = True
                returncode = stop_provider()
                break
            try:
                returncode = process.wait(timeout=min(remaining, 0.1))
                break
            except subprocess.TimeoutExpired:
                pass
        output.flush()
        os.fsync(output.fileno())
    ended_by_signal = -returncode if returncode < 0 else None
    exit_status = returncode if returncode >= 0 else None
    write_result(store_path, token, exit_status=exit_status,
                 signal=ended_by_signal, timed_out=timed_out)
    _clear_active(marker)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
