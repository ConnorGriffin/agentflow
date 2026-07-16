"""The daemon's public lifecycle: two-clock polling, snapshot publishing, and cycle isolation."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
from types import SimpleNamespace
from unittest import mock

import pytest

from agentflow import daemon, live
from agentflow.daemon import PollLoop, _acquire_lock, _release_lock, cycle, main
from agentflow.loop import RepoConfig

A = RepoConfig("owner/a", "/tmp/a")
B = RepoConfig("owner/b", "/tmp/b")


def _loop(**kw):
    """A PollLoop wired for deterministic tests: a synchronous 'spawn' so a full pass runs
    inline, a stub clock, and recording sinks. Callers override what they're asserting on."""
    kw.setdefault("dispatch_pass", lambda repos: None)
    kw.setdefault("publish", lambda repos: None)
    kw.setdefault("enabled", lambda: True)
    kw.setdefault("spawn", lambda fn: fn())
    return PollLoop([A], **kw)


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
daemon.FAST_TICK_SECONDS = 0.05
daemon.FULL_PASS_SECONDS = 0.05
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


def test_reclaim_cycle_clears_a_stranded_triaging_claim(monkeypatch):
    # The daemon's reclaim pass runs at the top of every cycle. A claim left by a daemon killed
    # mid-grounding — carrying no intake state label — must be cleared here and logged distinctly
    # from a build reclaim, so the stalled issue re-enters the intake queue (issue #74).
    from agentflow import loop

    stranded = {"number": 69, "labels": [{"name": "agentflow:triaging"}]}

    def fake_run(cmd):
        if "--label" in cmd and "agentflow:triaging" in cmd:
            return SimpleNamespace(stdout=json.dumps([stranded]), returncode=0)
        return SimpleNamespace(stdout="[]", returncode=0)   # no build claims

    released = []
    monkeypatch.setattr(loop, "_run", fake_run)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    monkeypatch.setattr(loop, "_issues_with_live_session", lambda repo: set())
    monkeypatch.setattr(loop, "_release_triage", lambda repo, n: released.append(n))

    logs = []
    cycle([A], run=daemon._reclaim, _log=logs.append)

    assert released == [69]
    assert any("reclaimed 1 stale triaging claim(s)" in m for m in logs)


def test_reclaim_cycle_strips_nothing_on_github_error(monkeypatch):
    # Fail closed: a `gh` blip must reclaim no build, triaging, or drawing claim.
    from agentflow import loop

    released = []
    monkeypatch.setattr(loop, "_run", lambda cmd: SimpleNamespace(stdout="", returncode=1))
    monkeypatch.setattr(loop, "_release", lambda repo, n: released.append(("build", n)))
    monkeypatch.setattr(loop, "_release_triage", lambda repo, n: released.append(("triage", n)))
    monkeypatch.setattr(loop, "_release_mockup", lambda repo, n: released.append(("drawing", n)))

    logs = []
    cycle([A], run=daemon._reclaim, _log=logs.append)

    assert released == []
    assert any("no stale claims" in m for m in logs)


def test_reclaim_pass_preserves_coordinator_owned_drawing_claim(monkeypatch):
    from agentflow import coordinated_build

    lanes = []
    monkeypatch.setattr(
        coordinated_build, "owned_issues",
        lambda cfg, lane=None: lanes.append(lane) or ({108} if lane == "drawing" else set()))
    monkeypatch.setattr(daemon, "reclaim_claims", lambda cfg, owned: 0)
    monkeypatch.setattr(daemon, "reclaim_triage_claims", lambda cfg, owned: 0)
    seen = []
    monkeypatch.setattr(daemon, "reclaim_mockup_claims",
                        lambda cfg, owned: seen.append(owned) or 0)

    assert daemon._reclaim(A) == "no stale claims"
    assert lanes == ["building", "triaging", "drawing"]
    assert seen == [{108}]


def test_unreadable_coordinator_store_fails_closed_before_drawing_reclaim(monkeypatch):
    from agentflow import coordinated_build

    monkeypatch.setattr(daemon, "reclaim_claims", lambda cfg, owned: 0)
    monkeypatch.setattr(daemon, "reclaim_triage_claims", lambda cfg, owned: 0)
    monkeypatch.setattr(coordinated_build, "owned_issues",
                        lambda cfg, lane=None: (_ for _ in ()).throw(RuntimeError("store unreadable"))
                        if lane == "drawing" else set())
    monkeypatch.setattr(daemon, "reclaim_mockup_claims", lambda *a: pytest.fail(
        "an unreadable continuation store must preserve drawing claims"))

    logs = []
    cycle([A], run=daemon._reclaim, _log=logs.append)
    assert any("store unreadable" in line for line in logs)


def test_forward_rollout_preserves_build_claims_for_drain_evidence(tmp_path, monkeypatch):
    """A coordinated request inspects legacy Build claims; the old stale-claim pass must not
    erase them first and turn an ambiguous forward drain into a false all-clear."""
    from agentflow.coordinator import Rollout

    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    Rollout().request_coordinated()
    monkeypatch.setattr(daemon, "reclaim_claims", lambda *a, **k: pytest.fail(
        "forward rollout must preserve Build claims"))
    monkeypatch.setattr(daemon, "reclaim_triage_claims", lambda *a, **k: pytest.fail(
        "forward rollout must preserve Intake claims"))
    monkeypatch.setattr(daemon, "reclaim_mockup_claims", lambda *a, **k: pytest.fail(
        "forward rollout must preserve Mockup claims"))
    monkeypatch.setattr(daemon, "recheck_once", lambda cfg: "nothing")
    seen = []
    monkeypatch.setattr(daemon.dispatch, "run_cycle",
                        lambda repos, rollout=None, rollout_mode=None, _log=None:
                        seen.append((rollout.mode, rollout_mode)))

    daemon.dispatch_cycle([A], _log=lambda _line: None)

    assert seen == [("coordinated", "coordinated")]


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


def _named_config_is_two_env_overridable_clocks():
    """The fast tick and the heartbeat are both named, env-overridable config, with the fast
    clock the shorter of the two — a single 300s clock can't satisfy this (issue #80)."""
    import importlib

    with mock.patch.dict(os.environ, {"AGENTFLOW_FAST_TICK_SECONDS": "7",
                                      "AGENTFLOW_HEARTBEAT_SECONDS": "480"}):
        reloaded = importlib.reload(daemon)
        try:
            assert reloaded.FAST_TICK_SECONDS == 7
            assert reloaded.FULL_PASS_SECONDS == 480
            assert reloaded.FAST_TICK_SECONDS < reloaded.FULL_PASS_SECONDS
        finally:
            importlib.reload(daemon)   # restore module-level defaults for other tests


def test_fast_and_heartbeat_intervals_are_named_env_overridable_config():
    _named_config_is_two_env_overridable_clocks()


def test_probe_no_change_runs_no_full_pass_but_change_does(monkeypatch):
    """Through the loop interface: a fast tick whose probe reports no change runs no dispatch
    pass; a tick whose probe reports change runs exactly one. This is the whole point of the
    cheap clock — react to real work, stay idle otherwise (issue #80 acceptance)."""
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 10_000)   # heartbeat far away
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    passes, publishes = [], []
    answers = iter([True, False, False, True])   # startup heartbeat, then probe verdicts
    probe = types.SimpleNamespace(changed=lambda: next(answers))
    clock = iter([0, 1, 2, 3, 4])
    loop = _loop(probe=probe, dispatch_pass=lambda repos: passes.append(repos),
                 publish=lambda repos: publishes.append("snap"), clock=lambda: next(clock))

    loop.tick()                       # t=0: startup heartbeat → one pass (probe not consulted)
    assert len(passes) == 1
    loop.tick()                       # t=1: probe True  → pass
    loop.tick()                       # t=2: probe False → no pass
    loop.tick()                       # t=3: probe False → no pass
    loop.tick()                       # t=4: probe True  → pass
    assert len(passes) == 3           # exactly the two change ticks plus the startup heartbeat
    # Snapshot production rides the full pass, never the cheap no-change tick (issue #80).
    assert len(publishes) == 3


def test_newly_ready_issue_dispatches_within_a_fast_tick(monkeypatch):
    """The reaction-latency guarantee (issue #80): a freshly-actionable issue surfaces in the
    probe's search, so the very next fast tick runs a dispatch pass — and the fast clock is bound
    well under the ~30s SLA, unlike the old single 300s clock."""
    assert daemon.FAST_TICK_SECONDS <= 30                      # bound the reaction latency itself
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 10_000)   # heartbeat far off — probe alone
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    from agentflow.probe import ChangeProbe

    # A fleet that was quiet, then a new ready-for-agent issue appears (its update is newer).
    feed = iter([[], [{"number": 42, "updatedAt": "2026-07-14T10:00:00Z"}]])
    probe = ChangeProbe([A], search=lambda repos, since: next(feed),
                        now=lambda: "2026-07-14T09:59:00Z")
    passes = []
    clock = iter([0, 1, 16])   # startup pass, then two fast ticks well inside the heartbeat
    loop = _loop(probe=probe, dispatch_pass=lambda repos: passes.append("pass"),
                 clock=lambda: next(clock))

    loop.tick()               # startup heartbeat consumes its slot; probe not consulted
    loop.tick()               # t=1:  fleet still quiet → probe no change → no pass
    assert passes == ["pass"]
    loop.tick()               # t=16: the new issue is now visible → probe change → dispatch
    assert passes == ["pass", "pass"]


def test_heartbeat_runs_a_full_pass_even_when_the_probe_sees_no_change(monkeypatch):
    """The slow clock is the backstop: a full pass runs on its own interval even while the probe
    keeps reporting no change (covers search-index lag / probe blind spots)."""
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 100)
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    passes = []
    probe = types.SimpleNamespace(changed=lambda: False)   # probe never reports change
    clock = iter([0, 15, 30, 105, 120])
    loop = _loop(probe=probe, dispatch_pass=lambda repos: passes.append("pass"),
                 clock=lambda: next(clock))

    loop.tick()   # t=0   heartbeat (startup)
    loop.tick()   # t=15  no change, heartbeat not due
    loop.tick()   # t=30  no change, heartbeat not due
    loop.tick()   # t=105 heartbeat due again → pass despite no change
    loop.tick()   # t=120 no change, heartbeat not due
    assert len(passes) == 2   # the two heartbeats, nothing from the (never-changing) probe


def test_dormant_fast_tick_makes_zero_calls_and_never_dispatches(monkeypatch):
    """Dormant (no enable flag): the probe is never even asked and no dispatch runs, so a paused
    fast tick costs zero network calls. Only the slow heartbeat republishes the console snapshot."""
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 100)
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    probe_calls, passes, publishes = [], [], []
    probe = types.SimpleNamespace(changed=lambda: probe_calls.append(1) or True)
    clock = iter([0, 15, 30])
    loop = _loop(probe=probe, enabled=lambda: False,
                 dispatch_pass=lambda repos: passes.append("pass"),
                 publish=lambda repos: publishes.append("snap"),
                 clock=lambda: next(clock))

    loop.tick()   # t=0   heartbeat due, dormant → publish only, no probe, no dispatch
    loop.tick()   # t=15  dormant fast tick → nothing at all
    loop.tick()   # t=30  dormant fast tick → nothing at all
    assert probe_calls == []      # the probe (its one API call) is never made while dormant
    assert passes == []           # no dispatch pass while dormant
    assert publishes == ["snap"]  # one republish on the heartbeat, so the paused board stays fresh


def test_change_probe_costs_one_call_per_tick_for_the_whole_fleet():
    """The probe's per-tick cost is a single cross-fleet search — the budget guarantee that makes
    a 15s cadence affordable (a full pass is dozens of calls). Bounded at ≤2, this is one."""
    from agentflow.probe import ChangeProbe

    calls = []

    def fake_search(repos, since):
        calls.append((tuple(repos), since))
        return [{"number": 5, "updatedAt": "2999-01-01T00:00:00Z"}]

    probe = ChangeProbe([A, B], search=fake_search, now=lambda: "2000-01-01T00:00:00Z")
    assert probe.changed() is True
    assert len(calls) == 1                       # one search call for BOTH repos
    assert calls[0][0] == ("owner/a", "owner/b")


def test_change_probe_reports_change_only_when_something_moved():
    """A fresh update past the watermark is a change; the same state seen again is not — so a
    single burst of work triggers one pass, then the fleet converges back to quiet."""
    from agentflow.probe import ChangeProbe

    feed = iter([
        [],                                                  # nothing new
        [{"number": 5, "updatedAt": "2026-07-14T10:00:00Z"}],  # a new update → change
        [{"number": 5, "updatedAt": "2026-07-14T10:00:00Z"}],  # same update seen again → no change
        [{"number": 6, "updatedAt": "2026-07-14T10:05:00Z"}],  # a newer update → change
    ])
    probe = ChangeProbe([A], search=lambda repos, since: next(feed),
                        now=lambda: "2026-07-14T09:00:00Z")
    assert probe.changed() is False
    assert probe.changed() is True
    assert probe.changed() is False
    assert probe.changed() is True


def test_change_probe_treats_a_search_failure_as_no_change():
    """A `gh` blip is unknown, not change: reporting change on failure would run a full pass every
    tick through an outage — the opposite of the point. The heartbeat still backstops."""
    from agentflow.probe import ChangeProbe

    probe = ChangeProbe([A], search=lambda repos, since: None)
    assert probe.changed() is False


def test_fast_tick_returns_without_blocking_on_an_in_flight_pass():
    """Dispatch-and-return: a full pass runs off the fast clock, so a long-running pass never
    stalls the next probe tick — and a single-flight guard means an overlapping tick does not
    launch a second pass (no double-dispatch, serial bookends preserved)."""
    with mock.patch.object(live, "mark_cycle", lambda _s: None):
        started = threading.Event()
        release = threading.Event()
        passes = []

        def slow_pass(_repos):
            passes.append("start")
            started.set()
            release.wait(2)

        loop = _loop(probe=types.SimpleNamespace(changed=lambda: True),
                     dispatch_pass=slow_pass, clock=lambda: 0,
                     spawn=lambda fn: threading.Thread(target=fn, daemon=True).start())

        t0 = time.monotonic()
        loop.tick()                       # launches the slow pass in a worker
        assert started.wait(2)
        first_tick_and_second = time.monotonic()
        loop.tick()                       # must NOT block on the in-flight pass, nor double it
        assert time.monotonic() - first_tick_and_second < 1, "fast tick blocked on the pass"
        release.set()
        time.sleep(0.05)
        assert passes == ["start"]        # single-flight: the second tick launched no second pass
        assert time.monotonic() - t0 < 2


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
