import io
import os
import signal
import stat
import subprocess

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
        returncode = 7
        def communicate(self, timeout=None): return "worker failed", None
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex", "exec"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 7
    assert capsys.readouterr().out == "worker failed"


def test_worker_timeout_sends_sigterm_then_sigkill(monkeypatch, tmp_path, capsys):
    killed = []
    class Process:
        pid = 4321
        def __init__(self): self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls < 3: raise subprocess.TimeoutExpired([], timeout, output="partial")
            return "after kill", None
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex", "exec"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
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
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg", lambda *a: None)
    codex_worker.run("terra", "medium", 1, PrivateInput("task"))
    assert capsys.readouterr().out == "once"


@pytest.mark.parametrize("signal_to_race", [signal.SIGTERM, signal.SIGKILL])
def test_worker_timeout_survives_an_exit_race(monkeypatch, tmp_path, signal_to_race):
    class Process:
        pid = 1
        def __init__(self): self.calls = 0
        def communicate(self, timeout=None):
            self.calls += 1
            if self.calls <= 2: raise subprocess.TimeoutExpired([], timeout)
            return "", None
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(codex_worker.os, "killpg",
                        lambda _pid, sig: (_ for _ in ()).throw(ProcessLookupError())
                        if sig == signal_to_race else None)
    assert codex_worker.run("terra", "medium", 1, PrivateInput("task")) == 124


def test_worker_passes_the_original_stdin_and_current_cwd_to_popen(monkeypatch, tmp_path):
    captured = {}
    class Process:
        returncode = 0
        def communicate(self, timeout=None): return "", None
    stream = PrivateInput("sentinel task")
    monkeypatch.setattr(codex_worker.os, "fstat", _private_fstat)
    monkeypatch.setattr(codex_worker.os, "getcwd", lambda: str(tmp_path))
    monkeypatch.setattr(codex_worker, "worker_argv", lambda *a: ["codex", "exec"])
    monkeypatch.setattr(codex_worker.subprocess, "Popen",
                        lambda argv, **kwargs: captured.update(argv=argv, **kwargs) or Process())
    codex_worker.run("terra", "medium", 1, stream)
    assert captured["stdin"] is stream and captured["cwd"] == str(tmp_path)
    assert "sentinel task" not in " ".join(captured["argv"])
