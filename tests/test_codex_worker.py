import errno
import io
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from agentflow import codex_worker
from agentflow.routing import RoutingConfigError


class PrivateInput(io.StringIO):
    def __init__(self, text, mode=0o600, regular=True):
        super().__init__(text)
        self._mode = mode | (stat.S_IFREG if regular else stat.S_IFIFO)

    def fileno(self):
        return 7


def _private_fstat(_fd):
    return os.stat_result((stat.S_IFREG | 0o600, 0, 0, 0, 0, 0, 4, 0, 0, 0))


def test_worker_argv_pins_model_effort_and_workspace_write_sandbox(tmp_path):
    argv = codex_worker.worker_argv("terra", "extra")
    assert argv[argv.index("-m") + 1] == "gpt-5.6-terra"
    assert "model_reasoning_effort=xhigh" in argv
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert "literal $() task" not in " ".join(argv)


def test_worker_argv_requires_single_flight_polling_of_yielded_commands():
    preamble = codex_worker.worker_argv("terra", "medium")[-1]

    assert "at most one long-running shell command at a time" in preamble
    assert "poll that exact handle until terminal" in preamble
    assert "Never reissue the same command while its prior handle may still be alive" in preamble
    assert "Never start a second test process" in preamble


def test_worker_rejects_unrouted_model(tmp_path):
    with pytest.raises(RoutingConfigError):
        codex_worker.worker_argv("fable", "medium")


def test_worker_rejects_noncanonical_effort_label(tmp_path):
    with pytest.raises(ValueError, match="unknown routed effort"):
        codex_worker.worker_argv("terra", "xhigh")


def test_worker_rejects_an_empty_task(monkeypatch, tmp_path):
    monkeypatch.setattr(codex_worker.os, "fstat", lambda _fd: os.stat_result(
        (stat.S_IFREG | 0o600, 0, 0, 0, 0, 0, 0, 0, 0, 0)))
    with pytest.raises(ValueError, match="must not be empty"):
        codex_worker.run("terra", "medium", 1, PrivateInput(""))


@pytest.mark.parametrize("mode,regular", [(0o644, True), (0o600, False)])
def test_worker_rejects_nonprivate_or_nonregular_stdin(monkeypatch, mode, regular):
    stream = PrivateInput("task", mode, regular)
    monkeypatch.setattr(codex_worker.os, "fstat", lambda _: os.stat_result(
        ((stat.S_IFREG if regular else stat.S_IFIFO) | mode,) + (0,) * 9))
    with pytest.raises(ValueError, match="private regular"):
        codex_worker._private_stdin(stream)


def test_worker_propagates_normal_nonzero_exit(monkeypatch, tmp_path, capsys):
    class Process:
        pid = 4321
        returncode = 7
        def communicate(self, timeout=None): return "worker failed", None
        def poll(self): return self.returncode
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex", "exec"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 7
    assert capsys.readouterr().out == "worker failed"


def test_worker_timeout_sends_sigterm_then_sigkill(monkeypatch, tmp_path, capsys):
    killed = []
    class Process:
        pid = 4321
        def __init__(self): self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1: raise subprocess.TimeoutExpired([], timeout, output="partial")
            return "after kill", None
        def poll(self): return None
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex", "exec"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.time, "monotonic", iter((0, 6)).__next__)
    monkeypatch.setattr(codex_worker.os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 124
    assert killed == [(4321, signal.SIGTERM), (4321, signal.SIGKILL)]
    assert "timed out" in capsys.readouterr().err


def test_worker_timeout_does_not_repeat_partial_output(monkeypatch, tmp_path, capsys):
    class Process:
        pid = 1
        def __init__(self): self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1: raise subprocess.TimeoutExpired([], timeout, output="once")
            return "once", None
        def poll(self): return 0
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg", lambda *a: None)
    codex_worker.run("terra", "medium", 1, PrivateInput("task"))
    assert capsys.readouterr().out == "once"


def test_worker_sigterm_before_popen_returns_stops_group_and_restores_handler(
        monkeypatch, capsys):
    killed = []
    handlers = []
    previous = object()

    class Process:
        pid = 4321
        returncode = -signal.SIGTERM
        def communicate(self, timeout=None): return "complete", None
        def poll(self): return self.returncode

    def install(signum, handler):
        handlers.append((signum, handler))
        return previous

    def launch(*_args, **kwargs):
        assert "preexec_fn" not in kwargs
        handlers[0][1](signal.SIGTERM, None)
        handlers[0][1](signal.SIGTERM, None)
        return Process()

    def killpg(pid, signum):
        killed.append((pid, signum))
        raise ProcessLookupError

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", launch)
    monkeypatch.setattr(codex_worker.os, "killpg", killpg)
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 143
    assert killed == [(4321, signal.SIGTERM)]
    assert handlers[-1] == (signal.SIGTERM, previous)
    captured = capsys.readouterr()
    assert captured.out == "complete"
    assert "interrupted" in captured.err


def test_worker_sigterm_handler_survives_nested_lock_free_invocation(monkeypatch):
    installed = None
    nested = False

    class Process:
        pid = 4321
        returncode = 0
        def communicate(self, timeout=None):
            def profile(frame, event, _arg):
                nonlocal nested
                if event == "call" and frame.f_code is installed.__code__ and not nested:
                    nested = True
                    installed(signal.SIGTERM, None)

            sys.setprofile(profile)
            try:
                installed(signal.SIGTERM, None)
            finally:
                sys.setprofile(None)
            return "once", None
        def poll(self): return 0

    def install(_signum, handler):
        nonlocal installed
        installed = handler
        return signal.SIG_DFL

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 143
    assert nested


def test_worker_sigterm_keeps_output_once(monkeypatch, capsys):
    installed = None
    class Process:
        pid = 4321
        returncode = -signal.SIGTERM
        def communicate(self, timeout=None):
            installed(signal.SIGTERM, None)
            return "partial", None
        def poll(self): return self.returncode

    def install(_signum, handler):
        nonlocal installed
        installed = handler
        return signal.SIG_DFL

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg", lambda *_args: None)
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 143
    assert capsys.readouterr().out == "partial"


def test_worker_sigterm_at_handler_restoration_cannot_bypass_cleanup(monkeypatch):
    current = None
    calls = 0

    class Process:
        pid = 4321
        returncode = 0
        def communicate(self, timeout=None): return "", None
        def poll(self): return 0

    def install(_signum, handler):
        nonlocal current, calls
        previous = current if current is not None else signal.SIG_DFL
        calls += 1
        if calls == 2:
            current(signal.SIGTERM, None)
            current(signal.SIGTERM, None)
        current = handler
        return previous

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 143
    assert current == signal.SIG_DFL


def test_worker_sigterm_immediately_before_teardown_helper_is_observed(monkeypatch):
    installed = None
    original_finish = codex_worker._finish_run

    class Process:
        pid = 4321
        returncode = 0
        def communicate(self, timeout=None): return "", None
        def poll(self): return 0

    def install(_signum, handler):
        nonlocal installed
        installed = handler
        return signal.SIG_DFL

    def interrupt_before_entry(*args, **kwargs):
        installed(signal.SIGTERM, None)
        return original_finish(*args, **kwargs)

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(codex_worker.signal, "signal", install)
    monkeypatch.setattr(codex_worker, "_finish_run", interrupt_before_entry)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 143


def test_worker_keeps_handler_through_cleanup_join_and_output(monkeypatch, capsys):
    events = []
    installed = None

    class Process:
        pid = 4321
        returncode = 0
        def communicate(self, timeout=None):
            events.append("communicate")
            return "complete", None

    class Stopper:
        def __init__(self, _process): pass
        def stop(self):
            events.append("cleanup")
            installed(signal.SIGTERM, None)

    class Watcher:
        def __init__(self, **_kwargs): pass
        def start(self): events.append("start")
        def join(self):
            events.append("join")
            installed(signal.SIGTERM, None)

    def install(_signum, handler):
        nonlocal installed
        if installed is None:
            installed = handler
            events.append("install")
        else:
            events.append("restore")
        return signal.SIG_DFL

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker, "_ProcessGroupStopper", Stopper)
    monkeypatch.setattr(codex_worker.threading, "Thread", Watcher)
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 143
    assert events == ["install", "start", "communicate", "cleanup", "join", "restore"]
    assert capsys.readouterr().out == "complete"


def test_worker_start_failure_restores_handler_without_joining_unstarted_watcher(monkeypatch):
    events = []

    class Process:
        pid = 4321
        returncode = None
        def poll(self): return None

    class Watcher:
        def __init__(self, **_kwargs): pass
        def start(self):
            events.append("start")
            raise RuntimeError("cannot start watcher")
        def join(self): events.append("join")

    def install(_signum, handler):
        events.append(("handler", handler))
        return signal.SIG_DFL

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.threading, "Thread", Watcher)
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    with pytest.raises(RuntimeError, match="cannot start watcher"):
        codex_worker.run("terra", "medium", 1, PrivateInput("task"))
    assert "join" not in events
    assert events[-1] == ("handler", signal.SIG_DFL)


def test_process_lookup_short_circuits_without_waiting(monkeypatch):
    killed = []

    class Process:
        pid = 4321
        def poll(self): raise AssertionError("a missing group needs no polling")

    def missing(_pid, signum):
        killed.append(signum)
        raise ProcessLookupError

    monkeypatch.setattr(codex_worker.os, "killpg", missing)
    started = time.monotonic()
    codex_worker._ProcessGroupStopper(Process()).stop()
    assert time.monotonic() - started < 0.1
    assert killed == [signal.SIGTERM]


@pytest.mark.parametrize(
    "term_failure",
    [PermissionError(errno.EPERM, "TERM denied"), OSError(errno.EIO, "TERM I/O failure")],
    ids=["permission-error", "unexpected-oserror"],
)
def test_term_failure_attempts_kill_and_preserves_first_failure(monkeypatch, term_failure):
    signals = []

    class Process:
        pid = 4321

    def signal_group(_pid, signum):
        signals.append(signum)
        if signum == signal.SIGTERM:
            raise term_failure
        raise ProcessLookupError

    stopper = codex_worker._ProcessGroupStopper(Process())
    monkeypatch.setattr(codex_worker.os, "killpg", signal_group)

    with pytest.raises(type(term_failure)) as owner_failure:
        stopper.stop()
    with pytest.raises(type(term_failure)) as observer_failure:
        stopper.stop()

    assert owner_failure.value is term_failure
    assert observer_failure.value is term_failure
    assert signals == [signal.SIGTERM, signal.SIGKILL]
    assert stopper._failures == (term_failure,)


def test_kill_failure_is_recorded_without_replacing_term_failure(monkeypatch):
    term_failure = PermissionError(errno.EPERM, "TERM denied")
    kill_failure = OSError(errno.EIO, "KILL I/O failure")

    class Process:
        pid = 4321

    def signal_group(_pid, signum):
        raise term_failure if signum == signal.SIGTERM else kill_failure

    stopper = codex_worker._ProcessGroupStopper(Process())
    monkeypatch.setattr(codex_worker.os, "killpg", signal_group)

    with pytest.raises(PermissionError) as raised:
        stopper.stop()

    assert raised.value is term_failure
    assert stopper._failures == (term_failure, kill_failure)


@pytest.mark.parametrize(
    "cleanup_failure",
    [PermissionError(errno.EPERM, "group denied"), OSError(errno.EIO, "group I/O failure")],
    ids=["permission-error", "unexpected-oserror"],
)
def test_watcher_cleanup_failure_preserves_valid_prefix_from_incomplete_utf8(
        monkeypatch, cleanup_failure, capsys):
    installed = None
    cleanup_attempted = threading.Event()
    handlers = []

    class Process:
        pid = 4321
        returncode = None
        stdout = type("Stdout", (), {"encoding": "utf-8", "errors": "strict"})()
        def __init__(self): self.communicate_calls = 0
        def communicate(self, timeout=None):
            self.communicate_calls += 1
            if self.communicate_calls > 1:
                raise AssertionError("cleanup failure must skip unbounded communicate retry")
            installed(signal.SIGTERM, None)
            assert cleanup_attempted.wait(1), "watcher did not own cleanup"
            raise subprocess.TimeoutExpired([], timeout, output=b"combined partial\n\xe2\x82")
        def poll(self): return None

    process = Process()

    def install(_signum, handler):
        nonlocal installed
        installed = handler
        handlers.append(handler)
        return signal.SIG_DFL

    def fail_cleanup(_pid, _signum):
        cleanup_attempted.set()
        raise cleanup_failure

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: process)
    monkeypatch.setattr(codex_worker.os, "killpg", fail_cleanup)
    monkeypatch.setattr(codex_worker.signal, "signal", install)
    monkeypatch.setattr(sys, "stdin", PrivateInput("task"))

    with pytest.raises(SystemExit) as raised:
        codex_worker.main(["--worker", "terra", "--effort", "medium", "--timeout", "30"])

    assert raised.value.code == 1
    assert process.communicate_calls == 1
    assert handlers[-1] == signal.SIG_DFL
    assert not any(t.name == "agentflow-codex-worker-cancellation" and t.is_alive()
                   for t in threading.enumerate())
    captured = capsys.readouterr()
    assert captured.out == "combined partial\n"
    assert captured.err == f"agentflow: Codex worker failed: {cleanup_failure}\n"


def test_worker_cancellation_wins_a_timeout_cleanup_race(monkeypatch, capsys):
    installed = None

    class Process:
        pid = 4321
        returncode = -signal.SIGTERM
        def __init__(self): self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                installed(signal.SIGTERM, None)
                raise subprocess.TimeoutExpired([], timeout, output="once")
            return "once", None
        def poll(self): return self.returncode

    def install(_signum, handler):
        nonlocal installed
        installed = handler
        return signal.SIG_DFL

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 143
    captured = capsys.readouterr()
    assert captured.out == "once"
    assert "interrupted" in captured.err and "timed out" not in captured.err


def test_worker_sigterm_during_real_pipe_communication_reaps_child_group_once(
        monkeypatch, tmp_path, capsys):
    task = tmp_path / "task"
    task.write_text("task")
    task.chmod(0o600)
    command = (
        "import os, signal, time; "
        "print('partial', flush=True); "
        "os.kill(os.getppid(), signal.SIGTERM); "
        "time.sleep(30)"
    )
    captured = {}
    real_popen = subprocess.Popen

    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        captured["process"] = process
        return process

    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: [sys.executable, "-c", command])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", launch)

    with task.open() as stdin:
        started = time.monotonic()
        assert codex_worker.run("terra", "medium", 30, stdin) == 143
    assert time.monotonic() - started < 5
    with pytest.raises(ProcessLookupError):
        os.killpg(captured["process"].pid, 0)
    captured_output = capsys.readouterr()
    assert captured_output.out == "partial\n"
    assert "interrupted" in captured_output.err


def _console_command(kind: str) -> list[str]:
    if kind == "installed":
        command = Path(sys.executable).with_name("agentflow-codex-worker")
        assert command.exists()
        return [str(command)]
    return [sys.executable, "-m", "agentflow.codex_worker"]


def _launch_console_worker(
        tmp_path: Path, kind: str, *, signal_on_start: bool = False,
        repeat_during_cleanup: bool = False):
    pid_file = tmp_path / "provider-pids"
    term_file = tmp_path / "provider-term"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, subprocess, sys, time\n"
        "ignore = os.environ.get('FAKE_REPEAT_DURING_CLEANUP') == '1'\n"
        "child_code = ('import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(30)' if ignore else 'import time; time.sleep(30)')\n"
        "child = subprocess.Popen([sys.executable, '-c', child_code])\n"
        "with open(os.environ['FAKE_PID_FILE'], 'w') as stream:\n"
        "    stream.write(f'{os.getpid()} {child.pid}')\n"
        "print('provider output', flush=True)\n"
        "if ignore:\n"
        "    def repeat(_signum, _frame):\n"
        "        with open(os.environ['FAKE_TERM_FILE'], 'w') as stream:\n"
        "            stream.write('term observed')\n"
        "        os.kill(os.getppid(), signal.SIGTERM)\n"
        "        os.kill(os.getppid(), signal.SIGTERM)\n"
        "    signal.signal(signal.SIGTERM, repeat)\n"
        "if os.environ.get('FAKE_SIGNAL_ON_START') == '1':\n"
        "    os.kill(os.getppid(), signal.SIGTERM)\n"
        "time.sleep(30)\n"
    )
    fake_codex.chmod(0o755)
    task = tmp_path / "task"
    task.write_text("bounded task")
    task.chmod(0o600)
    env = os.environ | {
        "AGENTFLOW_CODEX_BIN": str(fake_codex),
        "FAKE_PID_FILE": str(pid_file),
        "FAKE_TERM_FILE": str(term_file),
        "FAKE_SIGNAL_ON_START": "1" if signal_on_start else "0",
        "FAKE_REPEAT_DURING_CLEANUP": "1" if repeat_during_cleanup else "0",
    }
    stream = task.open()
    process = subprocess.Popen(
        _console_command(kind) + ["--worker", "terra", "--effort", "medium", "--timeout", "30"],
        stdin=stream, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    stream.close()
    return process, pid_file


def _wait_for_pids(pid_file: Path) -> list[int]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text():
            return [int(pid) for pid in pid_file.read_text().split()]
        time.sleep(0.01)
    raise AssertionError("fake provider did not start")


def _assert_group_gone(group_id: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    raise AssertionError(f"provider process group {group_id} survived cancellation")


@pytest.mark.parametrize("kind", ["installed", "module"])
def test_console_sigterm_during_communication_is_idempotent_and_reaps_child_group(
        tmp_path, kind):
    process, pid_file = _launch_console_worker(tmp_path, kind)
    provider_pid, _child_pid = _wait_for_pids(pid_file)

    os.kill(process.pid, signal.SIGTERM)
    os.kill(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143
    assert stdout == "provider output\n"
    assert stderr == "agentflow: Codex worker interrupted\n"
    _assert_group_gone(provider_pid)


@pytest.mark.parametrize("kind", ["installed", "module"])
def test_console_sigterm_at_spawn_handoff_returns_143_with_exact_output_and_no_child_group(
        tmp_path, kind):
    process, pid_file = _launch_console_worker(tmp_path, kind, signal_on_start=True)
    provider_pid, _child_pid = _wait_for_pids(pid_file)

    stdout, stderr = process.communicate(timeout=10)

    assert process.returncode == 143
    assert stdout == "provider output\n"
    assert stderr == "agentflow: Codex worker interrupted\n"
    _assert_group_gone(provider_pid)


def test_console_repeated_sigterm_during_term_to_kill_cleanup_reaps_ignoring_descendant(
        tmp_path):
    process, pid_file = _launch_console_worker(
        tmp_path, "module", repeat_during_cleanup=True,
    )
    provider_pid, _child_pid = _wait_for_pids(pid_file)

    os.kill(process.pid, signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=10)

    assert (tmp_path / "provider-term").read_text() == "term observed"
    assert process.returncode == 143
    assert stdout == "provider output\n"
    assert stderr == "agentflow: Codex worker interrupted\n"
    _assert_group_gone(provider_pid)


def test_worker_normal_run_restores_handler_without_leaking_watcher(monkeypatch):
    installed = []
    class Process:
        pid = 4321
        returncode = 0
        def communicate(self, timeout=None): return "", None
        def poll(self): return 0

    def install(_signum, handler):
        installed.append(handler)
        return signal.SIG_DFL

    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    monkeypatch.setattr(codex_worker.signal, "signal", install)

    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 0
    assert installed[-1] == signal.SIG_DFL
    assert not any(t.name == "agentflow-codex-worker-cancellation" and t.is_alive()
                   for t in threading.enumerate())


@pytest.mark.parametrize("signal_to_race", [signal.SIGTERM, signal.SIGKILL])
def test_worker_timeout_survives_an_exit_race(monkeypatch, tmp_path, signal_to_race):
    class Process:
        pid = 1
        def __init__(self): self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls == 1: raise subprocess.TimeoutExpired([], timeout)
            return "", None
        def poll(self): return None
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.time, "monotonic", iter((0, 6)).__next__)
    monkeypatch.setattr(codex_worker.os, "killpg", lambda _pid, sig:
                        (_ for _ in ()).throw(ProcessLookupError())
                        if sig in {signal_to_race, 0} else None)
    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 124


def test_worker_passes_the_original_stdin_and_current_cwd_to_popen(monkeypatch, tmp_path):
    captured = {}
    class Process:
        pid = 4321
        returncode = 0
        def communicate(self, timeout=None): return "", None
        def poll(self): return self.returncode
    stream = PrivateInput("sentinel task")
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker.os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex", "exec"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen",
                        lambda argv, **kwargs: captured.update(argv=argv, **kwargs) or Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()))
    codex_worker.run("terra", "medium", 1, stream)
    assert captured["stdin"] is stream and captured["cwd"] == str(tmp_path)
    assert "sentinel task" not in " ".join(captured["argv"])
