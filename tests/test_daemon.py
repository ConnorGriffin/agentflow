"""The daemon's public lifecycle: polling, snapshot publishing, and cycle isolation."""

import os
import signal
import subprocess
import sys
import threading
import time
from unittest import mock

from agentflow import daemon, live
from agentflow.daemon import _acquire_lock, _release_lock, cycle, main
from agentflow.loop import RepoConfig

A = RepoConfig("owner/a", "/tmp/a")
B = RepoConfig("owner/b", "/tmp/b")


def _wait_for_lock(lock, owner_pid: int, timeout: float = 2) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (lock / "pid").read_text().strip() == str(owner_pid):
                return
        except OSError:
            time.sleep(0.01)
    raise AssertionError("daemon did not acquire its lock")


def _start_daemon(state_dir):
    env = os.environ | {"AGENTFLOW_STATE": str(state_dir)}
    script = """
from agentflow import daemon

daemon.REPOS = []
daemon.POLL_SECONDS = 60
daemon.publish_snapshot = lambda repos: None  # hermetic: no pool-gate subprocesses
daemon.main()
"""
    return subprocess.Popen(
        [sys.executable, "-c", script],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def test_cycle_runs_every_repo_and_isolates_errors():
    seen, logs = [], []

    def run(cfg, _log=None):
        seen.append(cfg.repo)
        if cfg.repo == "owner/a":
            raise RuntimeError("boom")
        return "ok"

    cycle([A, B], run=run, _log=logs.append)
    assert seen == ["owner/a", "owner/b"]           # B still ran after A raised
    assert any("cycle error" in m and "owner/a" in m for m in logs)
    assert any("owner/b: ok" in m for m in logs)


def test_cycle_logs_result_per_repo():
    logs = []
    cycle([B], run=lambda cfg, _log=None: "no ready-for-agent issues", _log=logs.append)
    assert logs == ["owner/b: no ready-for-agent issues"]


def test_cycle_passes_log_into_run():
    """_log is forwarded into run so dispatch-start lines emitted inside pipeline_once
    use the same sink as the cycle's own per-repo result line."""
    emitted = []

    def run(cfg, _log=None):
        if _log:
            _log(f"{cfg.repo}: #5: routing → codex (build)")
        return "build: ok"

    cycle([B], run=run, _log=emitted.append)
    assert any("routing → codex" in m for m in emitted)   # dispatch-start line appeared
    assert any("build: ok" in m for m in emitted)          # result line also appeared


def test_main_once_runs_one_cycle_and_exits(tmp_path):
    """--once runs exactly one cycle without entering the poll loop."""
    events = []

    with (
        mock.patch("agentflow.daemon.STATE_DIR", tmp_path),
        mock.patch("agentflow.daemon.LOCK", tmp_path / "daemon.lock"),
        mock.patch("agentflow.daemon.REPOS", [A, B]),
        mock.patch("agentflow.daemon.recover_worktrees",
                   side_effect=lambda repos: events.append(("recover", list(repos)))),
        mock.patch("agentflow.daemon.dispatch_cycle",
                   side_effect=lambda repos: events.append(("cycle", list(repos)))),
        mock.patch("agentflow.daemon.publish_snapshot",
                   side_effect=lambda repos: events.append(("publish", list(repos)))),
        mock.patch("agentflow.daemon.log"),
        mock.patch.object(sys, "argv", ["daemon", "--once"]),
    ):
        main()

    assert events == [("recover", [A, B]), ("cycle", [A, B]), ("publish", [A, B])]
    assert not (tmp_path / "daemon.lock").exists()  # lock released on exit


def test_sigterm_releases_lock_so_a_fresh_daemon_can_start(tmp_path):
    """A supervised stop leaves the daemon ready to start again immediately."""
    lock = tmp_path / "daemon.lock"
    process = _start_daemon(tmp_path)
    replacement = None
    try:
        _wait_for_lock(lock, process.pid)
        process.send_signal(signal.SIGTERM)
        output, _ = process.communicate(timeout=2)

        assert process.returncode == 0, output
        assert not lock.exists()
        replacement = _start_daemon(tmp_path)
        _wait_for_lock(lock, replacement.pid)
    finally:
        for child in (process, replacement):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                child.wait(2)


def test_main_publishes_the_console_snapshot_every_tick(tmp_path):
    """The daemon is the console's only snapshot producer (ADR 0026): every tick —
    dormant included, because dormant is when the operator watches — publishes a
    snapshot that reflects the current dispatch state."""
    enabled = tmp_path / "enabled"
    dispatch_started = threading.Event()
    stop = threading.Event()

    class StopDaemon(Exception):
        pass

    def fake_cycle(_repos):
        dispatch_started.set()
        if stop.is_set():
            raise StopDaemon

    errors = []

    def run_daemon():
        try:
            main()
        except StopDaemon:
            pass
        except BaseException as exc:  # surfaced in the test thread below
            errors.append(exc)

    def wait_for_snapshot(predicate, timeout: float = 2) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = live.read_snapshot()
            if snap is not None and predicate(snap):
                return snap
            time.sleep(0.01)
        raise AssertionError("expected snapshot was not published")

    with (
        mock.patch("agentflow.daemon.STATE_DIR", tmp_path),
        mock.patch("agentflow.daemon.ENABLE_FLAG", enabled),
        mock.patch("agentflow.daemon.LOCK", tmp_path / "daemon.lock"),
        mock.patch("agentflow.daemon.POLL_SECONDS", 0.01),
        mock.patch("agentflow.daemon.REPOS", []),
        mock.patch("agentflow.daemon.recover_worktrees"),
        mock.patch("agentflow.daemon.dispatch_cycle", side_effect=fake_cycle),
        mock.patch("agentflow.daemon.log"),
        mock.patch("agentflow.dashboard_data.pools", return_value=[]),
        mock.patch.object(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json"),
        mock.patch.object(live, "LIVE_FILE", tmp_path / "live-sessions.json"),
        mock.patch.object(live, "DAEMON_FILE", tmp_path / "daemon-status.json"),
        mock.patch.object(sys, "argv", ["daemon"]),
    ):
        thread = threading.Thread(target=run_daemon)
        thread.start()
        try:
            dormant = wait_for_snapshot(lambda s: s["dispatch"] == {"enabled": False})
            assert not dispatch_started.is_set(), "dormant daemon claimed work"
            assert dormant["daemon"]["gh_fresh_at"], "snapshot carries its freshness stamp"

            enabled.touch()
            assert dispatch_started.wait(2), "poll loop did not observe the enable flag"
            wait_for_snapshot(lambda s: s["dispatch"] == {"enabled": True})

            enabled.unlink()
            wait_for_snapshot(lambda s: s["dispatch"] == {"enabled": False})
        finally:
            enabled.touch()
            stop.set()
            thread.join(3)

    assert not thread.is_alive()
    assert errors == []
    assert not (tmp_path / "daemon.lock").exists()


def test_stale_lock_reclaim_is_exclusive(tmp_path):
    """Many starters race a single stale lock — exactly one takes ownership."""
    lock = tmp_path / "daemon.lock"
    lock.mkdir()
    (lock / "pid").write_text("999999")  # a crashed run's pid
    old = time.time() - 4 * 3600  # older than the 3h stale threshold
    os.utime(lock, (old, old))

    results = []
    with (
        mock.patch("agentflow.daemon.STATE_DIR", tmp_path),
        mock.patch("agentflow.daemon.LOCK", lock),
        mock.patch("agentflow.daemon.log"),
    ):
        barrier = threading.Barrier(8)

        def race():
            barrier.wait()
            results.append(_acquire_lock())

        threads = [threading.Thread(target=race) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert results.count(True) == 1  # exactly one starter reclaimed and proceeded
    assert lock.exists() and (lock / "pid").read_text().strip() == str(os.getpid())


def test_fresh_lock_from_a_dead_daemon_is_reclaimed_on_startup(tmp_path):
    """A crashed daemon restarts without waiting for the lock to age out."""
    dead_owner = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_owner.wait()
    lock = tmp_path / "daemon.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(dead_owner.pid))
    process = _start_daemon(tmp_path)

    try:
        _wait_for_lock(lock, process.pid)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(2)


def test_lock_from_a_live_daemon_is_refused_on_startup(tmp_path):
    """A healthy owner keeps exclusive use of the daemon lock."""
    live_owner = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"]
    )
    lock = tmp_path / "daemon.lock"
    lock.mkdir()
    (lock / "pid").write_text(str(live_owner.pid))
    contender = _start_daemon(tmp_path)

    try:
        output, _ = contender.communicate(timeout=2)
        assert contender.returncode == 0, output
        assert "another daemon is running; exiting" in output
        assert (lock / "pid").read_text().strip() == str(live_owner.pid)
    finally:
        if contender.poll() is None:
            contender.kill()
            contender.wait()
        live_owner.terminate()
        live_owner.wait()


def test_release_leaves_another_pids_lock_alone(tmp_path):
    """Shutdown must not remove a lock owned by a different (live) daemon."""
    lock = tmp_path / "daemon.lock"
    lock.mkdir()
    (lock / "pid").write_text("999999")  # some other daemon owns it

    with (
        mock.patch("agentflow.daemon.STATE_DIR", tmp_path),
        mock.patch("agentflow.daemon.LOCK", lock),
    ):
        _release_lock()

    assert lock.exists()  # the other daemon's lock survived our shutdown


def test_heartbeat_survives_a_cycle_longer_than_the_stale_threshold(tmp_path, monkeypatch):
    """A long cycle can't make a healthy daemon look stale: the background heartbeat
    keeps the lock's mtime fresh, so a would-be second daemon still bows out."""
    lock = tmp_path / "daemon.lock"
    monkeypatch.setattr(daemon, "STATE_DIR", tmp_path)
    monkeypatch.setattr(daemon, "LOCK", lock)
    monkeypatch.setattr(daemon, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(daemon, "STALE_SECONDS", 0.2)
    monkeypatch.setattr(daemon, "log", lambda *a, **k: None)

    assert _acquire_lock() is True  # first daemon owns the lock
    stop = threading.Event()
    beat = threading.Thread(target=daemon._heartbeat, args=(stop,), daemon=True)
    beat.start()
    try:
        time.sleep(0.5)  # far past STALE_SECONDS — but the heartbeat keeps it fresh
        # A second starter tries to acquire; the lock is not stale, so it is refused.
        assert time.time() - lock.stat().st_mtime < daemon.STALE_SECONDS
        with mock.patch("agentflow.daemon.os.getpid", return_value=os.getpid() + 1):
            assert _acquire_lock() is False
    finally:
        stop.set()
        beat.join()
