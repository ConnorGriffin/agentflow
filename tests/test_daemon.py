"""The daemon's public lifecycle: two-clock polling, snapshot publishing, and cycle isolation."""

import os
import signal
import subprocess
import sys
import threading
import time
import types
from unittest import mock

import pytest

from agentflow import daemon, github, live
from agentflow.config import RuntimeConfig
from agentflow.daemon import PollLoop, _acquire_lock, _release_lock, cycle
from agentflow.loop import RepoConfig

A = RepoConfig("owner/a", "/tmp/a")
B = RepoConfig("owner/b", "/tmp/b")


def _loop(**kw):
    """A PollLoop wired for deterministic tests: a synchronous 'spawn' so a full pass runs
    inline, a stub clock, and recording sinks. Callers override what they're asserting on."""
    kw.setdefault("dispatch_pass", lambda repos, **kwargs: None)
    kw.setdefault("publish", lambda repos: None)
    kw.setdefault("enabled", lambda: True)
    kw.setdefault("local_complete", lambda: False)   # no local-completion wake unless asserted
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
from pathlib import Path

from agentflow import daemon
from agentflow.config import RuntimeConfig

daemon.FAST_TICK_SECONDS = 0.05
daemon.FULL_PASS_SECONDS = 0.05
daemon.publish_snapshot = lambda repos: None  # hermetic: no pool-gate subprocesses
daemon.run(RuntimeConfig((), (), Path("/tmp/test-agentflow-config.toml")))
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


def test_recheck_passes_the_daemon_log_into_the_repository_loop(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon, "recheck_once",
                        lambda cfg, _log=None: seen.append((cfg, _log)) or "loop result")

    log = seen.append
    assert daemon._recheck(B, _log=log) == "recheck: loop result"
    assert seen[0][0] == B
    assert seen[0][1] is log  # the same sink reaches the per-repository loop


def test_recheck_composed_coordinator_emits_sanitized_overlay_diagnostic(
        monkeypatch, tmp_path):
    """The daemon's real per-repository recheck path wires the composed coordinator's overlay
    diagnostic into the same log sink, without requiring a live GitHub or provider session."""
    from agentflow import (coordinated_intake, coordinated_review, coordinated_revise,
                           effective_policy, loop, pipeline)
    from agentflow.coordinator import Submission
    from agentflow.loop import RebaseResult

    cfg = RepoConfig("owner/repo", str(tmp_path / "repo"))
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))
    monkeypatch.setattr(github, "list_open_prs", lambda repo, limit: [
        github.PrRow(42, "agentflow/claude/issue-42-follow-up", "")])
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: [])
    monkeypatch.setattr(loop, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(loop, "_base_advanced_for", lambda workdir, branch: True)
    monkeypatch.setattr(loop, "_conflict_revise_owns_head", lambda cfg, n, branch: False)
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: RebaseResult.CLEAN)
    monkeypatch.setattr(loop, "remove_worktree_if_safe", lambda workdir, wt: True)
    monkeypatch.setattr(loop, "pick_reviewer", lambda tool, **kwargs: "codex")
    monkeypatch.setattr(loop, "_issue_acceptance", lambda cfg, number: "acceptance")
    monkeypatch.setattr(loop, "claim", lambda repo, number, label: True)
    monkeypatch.setattr(loop, "supersede_clean_review", lambda comments: True)
    monkeypatch.setattr(loop, "_run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=0, stdout="a" * 40 + "\n"))
    monkeypatch.setattr(
        coordinated_review, "survivor_review_submission",
        lambda *args, **kwargs: Submission(
            repo=cfg.repo, subject="42", stage="review", target="a" * 40,
            subject_revision="a" * 40, pool="codex", source=cfg.workdir,
            builder_lineage="claude", branch_lineage="claude"))
    monkeypatch.setattr(coordinated_review, "_resume_tainted_reviews", lambda coordinator: None)
    monkeypatch.setattr(coordinated_review, "_resettle_diverged_reviews", lambda coordinator: None)
    monkeypatch.setattr(coordinated_review, "_review_worktree_reset", lambda record: True)
    monkeypatch.setattr(coordinated_revise, "_retire_dead_revises", lambda coordinator: None)
    monkeypatch.setattr(coordinated_intake, "_retire_dead_intakes", lambda coordinator: None)
    monkeypatch.setattr(pipeline, "_production_gate", lambda: lambda record: True)
    monkeypatch.setattr(pipeline, "_capability_preflight", lambda record, materialize: None)

    def overlay_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"], output=b"secret")

    monkeypatch.setattr(effective_policy.subprocess, "run", overlay_run)
    logs = []

    cycle([cfg], run=daemon._recheck, _log=logs.append)

    diagnostics = [line for line in logs if "repository overlay read failed" in line]
    assert len(diagnostics) == 1
    assert "repository=owner/repo" in diagnostics[0]
    assert "phase=show" in diagnostics[0]
    assert "error_class=CLOSED" in diagnostics[0]
    assert cfg.workdir not in diagnostics[0]
    assert "secret" not in diagnostics[0]


def test_single_repository_composition_leaves_other_repository_waiting_record_untouched(
        monkeypatch, tmp_path):
    """A recheck helper may only admit the repository it configured for policy lookup."""
    from agentflow import pipeline
    from agentflow.coordinator import Coordinator, Submission
    from agentflow.coordinator.store import Store, default_store_path

    seed = Store(default_store_path())
    identity = Coordinator(store=seed).submit_stage(Submission(
        repo="owner/a", subject="680", stage="build", pool="claude", complexity="deep",
        subject_revision="a" * 40))
    seed.close()
    monkeypatch.setattr(pipeline, "_production_gate", lambda: lambda record: True)
    monkeypatch.setattr(pipeline, "_capability_preflight", lambda record, materialize: None)
    monkeypatch.setattr(pipeline, "worktree_ready", lambda record: True)
    logs = []

    coordinator = pipeline.build_coordinator(
        _log=logs.append, repositories={"owner/b": str(tmp_path / "repo-b")})
    pipeline.reconcile_and_project(coordinator)

    record = coordinator.stage_record(identity)
    assert record is not None and record.state == "waiting" and record.refusal == ""
    assert not any("invalid_overlay" in line for line in logs)


def test_dispatch_cycle_has_no_claim_reclaimer_and_forwards_pause(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon.dispatch, "run_cycle",
                        lambda repos, submit_new=True, _log=None:
                        seen.append((list(repos), submit_new)))
    monkeypatch.setattr(daemon, "recheck_once",
                        lambda cfg: pytest.fail("pause must not submit survivor reviews"))

    daemon.dispatch_cycle([A], _log=lambda _line: None, submit_new=False)

    assert seen == [([A], False)]


def test_main_once_runs_one_cycle_and_exits(tmp_path):
    """--once runs exactly one cycle without entering the poll loop."""
    events = []

    class RouteStore:
        def close(self):
            events.append(("routes-closed", None))

    with (
        mock.patch("agentflow.daemon.STATE_DIR", tmp_path),
        mock.patch("agentflow.daemon.LOCK", tmp_path / "daemon.lock"),
        mock.patch("agentflow.daemon.recover_worktrees",
                   side_effect=lambda repos: events.append(("recover", list(repos)))),
        mock.patch("agentflow.pipeline.production_store", return_value=RouteStore()),
        mock.patch("agentflow.routing.reconcile_route_cells",
                   side_effect=lambda config, store: events.append(("routes", config))),
        mock.patch("agentflow.daemon.dispatch_cycle",
                   side_effect=lambda repos: events.append(("cycle", list(repos)))),
        mock.patch("agentflow.daemon.publish_snapshot",
                   side_effect=lambda repos: events.append(("publish", list(repos)))),
        mock.patch("agentflow.daemon.log"),
    ):
        daemon.run(RuntimeConfig((A, B), (), tmp_path / "config.toml"), once=True)

    assert events == [
        ("routes", RuntimeConfig((A, B), (), tmp_path / "config.toml")),
        ("routes-closed", None),
        ("recover", [A, B]), ("cycle", [A, B]), ("publish", [A, B]),
    ]
    assert not (tmp_path / "daemon.lock").exists()  # lock released on exit


def test_once_production_path_reaches_provider_command_through_composed_admission(
        tmp_path, monkeypatch):
    """Exercise daemon -> dispatch -> production Coordinator -> Store -> provider argv."""
    from pathlib import Path

    from agentflow import coordinated_converse, dispatch, pipeline
    from agentflow.coordinator import Coordinator
    from agentflow.coordinator.launcher import LocalLauncher
    from agentflow.coordinator.store import Store, default_store_path

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"],
                   check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "AGENTS.md").write_text("profile: reviewed\nui-surfaces: none\n")
    source_skills = repo / ".agents" / "skills"
    source_skills.mkdir(parents=True)
    (source_skills / ".keep").write_text("capability source root\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)

    worktree = Path(coordinated_converse.ask_worktree(str(repo), "codex", "production-path"))
    worktree.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(worktree), "HEAD"],
        check=True, stdout=subprocess.DEVNULL)
    # Materialization is non-clobbering. Converse has no methodology requirements, so inert
    # existing destinations make this fixture independent of globally installed skills.
    for name in ("tdd", "codebase-design", "domain-modeling", "agentflow",
                 "ui-craft", "drive-local-webapp"):
        (worktree / ".agents" / "skills" / name).mkdir(parents=True, exist_ok=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    marker = tmp_path / "provider-argv.txt"
    fake_codex = fake_bin / "codex"
    fake_codex.write_text(
        "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$AGENTFLOW_PROVIDER_MARKER\"\nexit 0\n")
    fake_codex.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("AGENTFLOW_CODEX_BIN", str(fake_codex))
    monkeypatch.setenv("AGENTFLOW_PROVIDER_MARKER", str(marker))

    seed_store = Store(default_store_path())
    seed = Coordinator(store=seed_store)
    identity = seed.submit_stage(coordinated_converse.converse_submission(
        "octo/app", str(repo), "production-path", 0, "Summarize this repository.",
        pool="codex"))
    seed_store.close()

    # Model the concrete no-restart boundary: the launched handle becomes absent
    # before the pass's final reconciliation.  The command and admission remain
    # real; this hook only waits for the intentionally instant fake provider to
    # reach that observation point instead of depending on process scheduling.
    real_is_alive = LocalLauncher.is_alive

    def exited_during_final_observation(family):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if marker.exists() and not real_is_alive(family):
                return False
            time.sleep(0.01)
        return real_is_alive(family)

    monkeypatch.setattr(LocalLauncher, "is_alive", staticmethod(exited_during_final_observation))

    # Bound only external discovery, GitHub reconciliation, and publication. The named internal
    # production seams under test remain the real implementations.
    monkeypatch.setattr(daemon, "recover_worktrees", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        daemon,
        "recheck_once",
        lambda _cfg, _log=None: "bounded external recheck",
    )
    monkeypatch.setattr(daemon, "publish_snapshot", lambda _repos: None)
    monkeypatch.setattr(daemon, "log", lambda _line: None)
    monkeypatch.setattr(dispatch, "_refresh_claude_quota", lambda _log: None)
    monkeypatch.setattr(dispatch, "_submit_repo", lambda _cfg, _coord, _log: None)
    monkeypatch.setattr(pipeline, "reconcile_orphaned_claims", lambda *_args, **_kwargs: 0)

    cfg = RepoConfig("octo/app", str(repo))
    daemon.run(RuntimeConfig((cfg,), (), tmp_path / "config.toml"), once=True)

    deadline = time.monotonic() + 2
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists()
    argv = marker.read_text().splitlines()
    assert argv[0] == "exec" and "--ignore-user-config" in argv
    reopened = Store(default_store_path())
    receipt = reopened.read_admission_receipt(identity)
    record = reopened.record_of(identity)
    assert receipt is not None and record is not None
    # The real provider command is admitted and starts, then its instant exit is
    # reconciled as a continuation in this same --once cycle.
    assert record.state == "waiting" and record.start_fact == "started"
    assert record.continuation and record.claim and not record.process_alive
    assert receipt.route_cell_digest == record.route_cell_digest
    reopened.close()


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
    loop = _loop(probe=probe, dispatch_pass=lambda repos, **kw: passes.append((repos, kw)),
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
    feed = iter([[], [github.SearchHit(number=42, updated_at="2026-07-14T10:00:00Z")]])
    probe = ChangeProbe([A], search=lambda repos, since: next(feed),
                        now=lambda: "2026-07-14T09:59:00Z")
    passes = []
    clock = iter([0, 1, 16])   # startup pass, then two fast ticks well inside the heartbeat
    loop = _loop(probe=probe, dispatch_pass=lambda repos, **kw: passes.append("pass"),
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
    loop = _loop(probe=probe, dispatch_pass=lambda repos, **kw: passes.append("pass"),
                 clock=lambda: next(clock))

    loop.tick()   # t=0   heartbeat (startup)
    loop.tick()   # t=15  no change, heartbeat not due
    loop.tick()   # t=30  no change, heartbeat not due
    loop.tick()   # t=105 heartbeat due again → pass despite no change
    loop.tick()   # t=120 no change, heartbeat not due
    assert len(passes) == 2   # the two heartbeats, nothing from the (never-changing) probe


def test_dormant_fast_tick_only_reconciles_on_the_heartbeat(monkeypatch):
    """Paused: no probe or cold submissions; heartbeat still reconciles owned records."""
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 100)
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    probe_calls, passes, publishes = [], [], []
    probe = types.SimpleNamespace(changed=lambda: probe_calls.append(1) or True)
    clock = iter([0, 15, 30])
    loop = _loop(probe=probe, enabled=lambda: False,
                 dispatch_pass=lambda repos, **kw: passes.append(kw),
                 publish=lambda repos: publishes.append("snap"),
                 clock=lambda: next(clock))

    loop.tick()   # t=0   heartbeat due, paused → reconcile without cold submissions
    loop.tick()   # t=15  dormant fast tick → nothing at all
    loop.tick()   # t=30  dormant fast tick → nothing at all
    assert probe_calls == []      # the probe (its one API call) is never made while dormant
    assert passes == [{"submit_new": False}]
    assert publishes == ["snap"]  # one republish on the heartbeat, so the paused board stays fresh


def test_dead_provider_family_wakes_a_full_pass_within_a_fast_tick(monkeypatch):
    """The local-completion wake (issue #158): a running record whose provider family died after
    its last GitHub bump strands its permits until reconciliation. On a fast tick with the probe
    reporting no change and the heartbeat not due, that locally-durable completion still wakes
    exactly one full pass — so the finished stage advances and its permits release within ~one
    fast tick instead of waiting out the 300s heartbeat. This fails against a tick that only wakes
    on probe/heartbeat."""
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 10_000)   # heartbeat far away
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    passes = []
    probe = types.SimpleNamespace(changed=lambda: False)   # no GitHub change to react to
    # t=0 wakes on the startup heartbeat (local sweep not consulted); the sweep is consulted from
    # t=15 on: the family is now dead, then the triggered pass reconciles it so the next is quiet.
    dead = iter([True, False])
    clock = iter([0, 15, 30])
    loop = _loop(probe=probe, local_complete=lambda: next(dead),
                 dispatch_pass=lambda repos, **kw: passes.append(kw),
                 clock=lambda: next(clock))

    loop.tick()   # t=0   startup heartbeat consumes its slot (local sweep not consulted)
    assert len(passes) == 1
    loop.tick()   # t=15  probe no change, heartbeat not due, family now dead → wake a full pass
    assert len(passes) == 2
    assert passes[1] == {"submit_new": True}
    loop.tick()   # t=30  the pass reconciled the dead family off `running` → converged, no wake
    assert len(passes) == 2


def test_live_provider_family_does_not_wake_a_full_pass(monkeypatch):
    """The mirror case: a running record whose family is still alive (or whose liveness is
    unknown) reports no local completion, so a quiet fast tick runs no pass — the wake fires only
    on a *proven*-dead family, never on uncertainty."""
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 10_000)   # heartbeat far away
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    passes = []
    probe = types.SimpleNamespace(changed=lambda: False)
    clock = iter([0, 15, 30])
    loop = _loop(probe=probe, local_complete=lambda: False,   # family alive/unknown → no wake
                 dispatch_pass=lambda repos, **kw: passes.append(kw),
                 clock=lambda: next(clock))

    loop.tick()   # t=0   startup heartbeat
    loop.tick()   # t=15  nothing moved locally or on GitHub → no pass
    loop.tick()   # t=30  still nothing → no pass
    assert len(passes) == 1   # only the startup heartbeat


def test_local_completion_sweep_is_gated_off_while_dormant(monkeypatch):
    """Dormant mirrors the probe's gating: the local-completion sweep is not consulted while the
    daemon is paused (only the heartbeat reconciles), so a paused daemon stays free of any wake
    work between heartbeats — dormant is genuinely idle."""
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 10_000)   # heartbeat far away
    monkeypatch.setattr(live, "mark_cycle", lambda _s: None)
    sweeps, passes = [], []
    clock = iter([0, 15, 30])
    loop = _loop(enabled=lambda: False,
                 local_complete=lambda: sweeps.append(1) or True,
                 dispatch_pass=lambda repos, **kw: passes.append(kw),
                 clock=lambda: next(clock))

    loop.tick()   # t=0   heartbeat (startup) consumes its slot
    loop.tick()   # t=15  dormant fast tick → the local sweep is never consulted
    loop.tick()   # t=30  dormant fast tick → still never consulted
    assert sweeps == []
    assert len(passes) == 1   # only the startup heartbeat, cold submission disabled


def test_dead_family_running_ignores_unknown_liveness_and_bad_store(monkeypatch, tmp_path):
    """The wake's local signal (issue #158) proves completion the same way reconcile does: only a
    family `pid_family_alive` reports gone counts. An alive family, an unknown/permission-denied
    liveness, a record that never recorded a family, and an unreadable store all report 'nothing to
    wake' — so the sweep fails closed and never crashes the loop."""
    from agentflow.coordinator.record import RUNNING, WAITING, Record

    dead_pid, live_pid = "999999", str(os.getpid())
    calls = []

    def fake_alive(family):
        calls.append(family)
        return {dead_pid: False, live_pid: True}.get(family, True)  # unknown → True (fail closed)

    monkeypatch.setattr("agentflow.coordinator.launcher.pid_family_alive", fake_alive)

    def records(rs):
        monkeypatch.setattr("agentflow.coordinator.tracer.load_records", lambda *a, **k: rs)

    def rec(identity, state, family):
        return Record(identity=identity, pool="claude", stage="build", demand=1,
                      subject="1", repo="owner/a", state=state, family=family)

    records([rec("a", RUNNING, live_pid)])          # alive → no wake
    assert daemon._dead_family_running() is False
    records([rec("b", RUNNING, "42424242")])         # unknown pid → fail closed, no wake
    assert daemon._dead_family_running() is False
    records([rec("c", RUNNING, None)])               # never recorded a family → no wake
    assert daemon._dead_family_running() is False
    records([rec("d", WAITING, dead_pid)])           # not running → no wake
    assert daemon._dead_family_running() is False
    records([rec("e", RUNNING, live_pid), rec("f", RUNNING, dead_pid)])  # one proven dead → wake
    assert daemon._dead_family_running() is True

    def boom(*a, **k):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr("agentflow.coordinator.tracer.load_records", boom)
    assert daemon._dead_family_running(_log=lambda _m: None) is False   # swallowed, no crash


def test_change_probe_costs_one_call_per_tick_for_the_whole_fleet():
    """The probe's per-tick cost is a single cross-fleet search — the budget guarantee that makes
    a 15s cadence affordable (a full pass is dozens of calls). Bounded at ≤2, this is one."""
    from agentflow.probe import ChangeProbe

    calls = []

    def fake_search(repos, since):
        calls.append((tuple(repos), since))
        return [github.SearchHit(number=5, updated_at="2999-01-01T00:00:00Z")]

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
        [github.SearchHit(5, "2026-07-14T10:00:00Z")],   # a new update → change
        [github.SearchHit(5, "2026-07-14T10:00:00Z")],   # same update seen again → no change
        [github.SearchHit(6, "2026-07-14T10:05:00Z")],   # a newer update → change
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

        def slow_pass(_repos, **kwargs):
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


# --- bounded worktree reclamation on the pass clock (ADR 0050) --------------------------

def _swept(seen, archived=()):
    """A stand-in reclamation pass that records the repo and protected set it was handed."""
    from agentflow.runner import WorktreeRecovery

    def sweep(repo, workdir, protected):
        seen.append((repo, protected))
        return WorktreeRecovery((), (), archived)
    return sweep


def test_the_full_pass_reclaims_before_it_submits_and_a_paused_one_never_does(monkeypatch):
    """Reclamation reads which sources are owned and then spends minutes confirming completion.
    Running it inside the pass is what stops it archiving a checkout an admission is about to
    launch into. A paused daemon reclaims nothing at all — pause is the operator's stop signal."""
    order = []
    monkeypatch.setattr(daemon, "recover_worktrees",
                        lambda repos, _log=None: order.append("reclaim"))
    monkeypatch.setattr(daemon.dispatch, "run_cycle",
                        lambda repos, submit_new=True, _log=None:
                        order.append(f"dispatch:{submit_new}"))
    monkeypatch.setattr(daemon, "recheck_once", lambda cfg: "")

    daemon.dispatch_cycle([A], _log=lambda _line: None)
    assert order[:2] == ["reclaim", "dispatch:True"]

    order.clear()
    daemon.dispatch_cycle([A], _log=lambda _line: None, submit_new=False)
    assert order == ["dispatch:False"]


def test_reclamation_runs_at_most_once_per_interval_per_repository(monkeypatch):
    from agentflow import pipeline

    monkeypatch.setattr(daemon, "_LAST_SWEEP", {})
    monkeypatch.setattr(pipeline, "owned_worktrees", lambda cfg: {"/live/source"})
    seen = []

    daemon.recover_worktrees([A, B], sweep=_swept(seen), _log=lambda _line: None)
    daemon.recover_worktrees([A, B], sweep=_swept(seen), _log=lambda _line: None)
    assert seen == [("owner/a", {"/live/source"}), ("owner/b", {"/live/source"})]

    daemon._LAST_SWEEP["owner/a"] -= daemon.SWEEP_INTERVAL_SECONDS + 1
    daemon.recover_worktrees([A, B], sweep=_swept(seen), _log=lambda _line: None)
    assert [repo for repo, _protected in seen] == ["owner/a", "owner/b", "owner/a"]


def test_startup_and_the_pass_that_immediately_follows_it_reclaim_once(monkeypatch):
    from agentflow import pipeline

    monkeypatch.setattr(daemon, "_LAST_SWEEP", {})
    asked = []
    monkeypatch.setattr(pipeline, "owned_worktrees",
                        lambda cfg: asked.append(cfg.repo) or set())
    monkeypatch.setattr(daemon.dispatch, "run_cycle",
                        lambda repos, submit_new=True, _log=None: None)
    monkeypatch.setattr(daemon, "recheck_once", lambda cfg: "")
    seen = []

    daemon.recover_worktrees([A], sweep=_swept(seen), _log=lambda _line: None)
    daemon.dispatch_cycle([A], _log=lambda _line: None)

    assert seen == [("owner/a", set())]
    assert asked == ["owner/a"]  # the pass right after startup does not sweep again


def test_a_sweep_that_only_archives_still_reports_its_recovery_refs(monkeypatch):
    """The steady-state sweep removes nothing and retains nothing — it archives. If that case
    logged nothing, the only handle on the reclaimed work would never be printed."""
    from agentflow import pipeline

    monkeypatch.setattr(daemon, "_LAST_SWEEP", {})
    monkeypatch.setattr(pipeline, "owned_worktrees", lambda cfg: set())
    logs = []
    archived = (("/a/wt/issue-9-x", "refs/agentflow/stranded/issue-9-x/abc123abc123"),)

    daemon.recover_worktrees([A], sweep=_swept([], archived=archived), _log=logs.append)

    assert len(logs) == 1
    assert "archived 1 stranded" in logs[0]
    assert "refs/agentflow/stranded/issue-9-x/abc123abc123" in logs[0]


# --- publish_snapshot composes v1 + schema-v2 (ADR 0036) --------------------------------

def test_publish_snapshot_composes_v1_and_schema_v2(tmp_path, monkeypatch):
    """The daemon publishes one file carrying both the existing v1 fields and the additive
    schema-v2 Decision Map projection — exercised end-to-end through the real snapshot file,
    the way the console's endpoint reads it."""
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    monkeypatch.setattr(github, "decision_maps",
                        lambda repo, **kw: github.MapsRead(maps=(), total_count=0, cost=1,
                                                           remaining=4999))
    monkeypatch.setattr(github, "handoff_pr_links_read",
                        lambda repo, nums: github.HandoffLinksRead(links={}, cost=0,
                                                                   remaining=None))
    monkeypatch.setattr(github, "list_pipeline_prs", lambda repo, state: [])

    v1 = {"dispatch": {"enabled": True}, "daemon": {"gh_fresh_at": "2026-07-30T00:00:00+00:00"},
          "pools": [], "running": [], "repos": [
              {"repo": "owner/a", "profile": "reviewed", "recent_merges": [], "held": [],
               "parked": [], "ratchet": {"ready_to_loosen": False},
               "in_flight": [{"number": 7, "title": "a change", "builder": "claude",
                              "handed_off_at": "2026-07-29T00:00:00Z"}]}]}
    daemon.publish_snapshot([A], produce=lambda repos, dispatch_enabled: v1)

    published = live.read_snapshot()
    assert published["dispatch"] == {"enabled": True}, "v1 fields survive verbatim"
    assert published["schema_version"] == 2
    assert [r["name_with_owner"] for r in published["repositories"]] == ["owner/a"]
    assert published["fleet"] == {"recent_landed": []}
    # The attention queue needs facts from both halves — the open PR from v1, each
    # repository's freshness stamp from v2 — so it composes here (#373).
    assert published["attention"]["total"] == 1
    assert published["attention"]["rows"][0]["url"] == "https://github.com/owner/a/pull/7"


def test_publish_snapshot_skips_the_whole_publish_on_error(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")

    def boom(repos, dispatch_enabled):
        raise RuntimeError("gh outage")

    logs = []
    daemon.publish_snapshot([A], produce=boom, _log=logs.append)

    assert live.read_snapshot() is None
    assert len(logs) == 1
    assert "snapshot publish error: Traceback" in logs[0]
    assert "tests/test_daemon.py" in logs[0]
    assert "RuntimeError: gh outage" in logs[0]
