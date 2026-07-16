"""The dispatch governor and cycle — the concurrency/pacing decision surface (ADR 0023 / 0025)."""

import json
import subprocess
import threading
import time
from collections import Counter

import pytest

from agentflow import dispatch, gate, loop
from agentflow.dispatch import BUILD_CONCURRENCY, STAGE_CAPS, TRIAGE_CONCURRENCY, Governor
from agentflow.loop import RepoConfig


class _Tracker:
    """Records how many sessions of each kind are in flight at once, across threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.current = Counter()
        self.peak = Counter()
        self.total_current = 0
        self.total_peak = 0

    def enter(self, stage):
        with self.lock:
            self.current[stage] += 1
            self.total_current += 1
            self.peak[stage] = max(self.peak[stage], self.current[stage])
            self.total_peak = max(self.total_peak, self.total_current)

    def leave(self, stage):
        with self.lock:
            self.current[stage] -= 1
            self.total_current -= 1


def test_named_config_lets_triage_outrun_builds():
    """Grounding sessions are cheap and the intake queue should drain fast, so triage is
    allowed more parallelism than builds — the load-bearing asymmetry in the ADR."""
    assert TRIAGE_CONCURRENCY > BUILD_CONCURRENCY
    assert STAGE_CAPS["triage"] == TRIAGE_CONCURRENCY
    assert STAGE_CAPS["build"] == BUILD_CONCURRENCY


def test_machine_ceiling_bounds_total_sessions_across_kinds():
    """No more than the machine ceiling run at once, counting every kind together."""
    gov = Governor(machine_ceiling=3, stage_caps={"triage": 5, "build": 5})
    assert gov.admit("triage", "claude") is True
    assert gov.admit("build", "codex") is True
    assert gov.admit("triage", "claude") is True
    assert gov.admit("build", "codex") is False   # 4th session over the ceiling of 3
    gov.release("triage")
    assert gov.admit("build", "codex") is True     # a freed slot reopens capacity


def test_per_stage_cap_limits_builds_while_triage_still_flows():
    """With both pools clear, builds are capped lower than triage: several triages run
    concurrently while builds stay capacity-bound (the ADR's deep-queue scenario)."""
    gov = Governor(machine_ceiling=10, stage_caps={"triage": 3, "build": 2})
    assert [gov.admit("build", "claude") for _ in range(3)] == [True, True, False]
    assert [gov.admit("triage", "codex") for _ in range(4)] == [True, True, True, False]


def test_active_pool_paces_to_one_new_session_per_cycle():
    """When the operator is active on a pool, only ACTIVE_PACE new sessions start on it per
    cycle — while the other, idle pool keeps dispatching at full concurrency."""
    gov = Governor(machine_ceiling=10, stage_caps={"build": 5}, pace=1)
    assert gov.admit("build", "claude", active=True) is True
    assert gov.admit("build", "claude", active=True) is False   # paced: one per cycle
    assert gov.admit("build", "codex", active=False) is True    # idle pool unaffected
    assert gov.admit("build", "codex", active=False) is True


def test_pace_budget_refreshes_each_cycle_but_slots_do_not():
    """A new cycle refreshes the pace budget for an active pool; live slots persist until
    their sessions actually release (they are not per-cycle)."""
    gov = Governor(machine_ceiling=10, stage_caps={"build": 5}, pace=1)
    assert gov.admit("build", "claude", active=True) is True
    assert gov.admit("build", "claude", active=True) is False
    gov.begin_cycle()
    assert gov.admit("build", "claude", active=True) is True    # pace budget back
    assert gov.live == 2                                        # both prior slots still held


def test_admission_is_thread_safe_under_a_race():
    """Concurrent chains racing the last slot: exactly the ceiling get in, never more."""
    gov = Governor(machine_ceiling=5, stage_caps={"build": 100})
    admitted = []
    barrier = threading.Barrier(20)

    def race():
        barrier.wait()
        if gov.admit("build", "claude"):
            admitted.append(1)

    threads = [threading.Thread(target=race) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(admitted) == 5


# --- the concurrent dispatch cycle, through the public `run_cycle` interface -----------
# These drive `run_cycle` with instrumented session bodies so the concurrency, ceiling,
# and pacing DECISIONS are exercised end to end — not just the governor's counters.

def _idle_pools(monkeypatch, active=None):
    """Make every pool read idle (or active per `active`) without touching the gate."""
    active = active or {}
    monkeypatch.setattr(dispatch, "_pool_activity",
                        lambda _log: {"claude": active.get("claude", False),
                                      "codex": active.get("codex", False)})


def _no_triage_no_mockup_no_respond(monkeypatch, tracker=None, build_tool=None):
    """Stub the non-build stages to no-ops so a test can isolate build concurrency."""
    monkeypatch.setattr(loop, "_next_intake_candidate", lambda cfg, reserved=frozenset(): None)
    monkeypatch.setattr(loop, "produce_once", lambda cfg, _log=None, slot=None: "no mockups")
    monkeypatch.setattr(loop, "respond_once", lambda cfg, _log=None, slot=None: "no replies")


def _build_body(tracker, tool_for):
    def fake_run_once(cfg, _log=None, slot=None):
        tool = tool_for(cfg.repo)
        if slot is not None and not slot.admit("build", tool):
            return f"{cfg.repo}: build deferred"
        try:
            tracker.enter("build")
            time.sleep(0.05)
            return f"{cfg.repo}: built"
        finally:
            tracker.leave("build")
            if slot is not None:
                slot.release("build")
    return fake_run_once


def test_multiple_builds_run_concurrently_across_repos_up_to_the_build_cap(monkeypatch):
    """With ready issues across repos and both pools clear, more than one build runs at once —
    up to the per-stage build cap, never beyond the machine ceiling."""
    tracker = _Tracker()
    _idle_pools(monkeypatch)
    _no_triage_no_mockup_no_respond(monkeypatch)
    monkeypatch.setattr(loop, "run_once", _build_body(tracker, lambda repo: "claude"))

    repos = [RepoConfig(f"o/r{i}", f"/tmp/{i}") for i in range(5)]
    gov = Governor(machine_ceiling=4, stage_caps={"build": 2, "triage": 3,
                                                  "mockup": 1, "respond": 1})
    dispatch.run_cycle(repos, gov, _log=lambda _m: None)

    assert tracker.peak["build"] == 2        # more than one build concurrent, capped at 2
    assert tracker.total_peak <= 4           # never over the machine ceiling


def test_several_triages_run_concurrently_when_the_queue_is_deep(monkeypatch):
    """A deep intake queue on one repo drains fast: several grounding sessions run at once,
    up to the triage cap (higher than builds), and the fan-out never re-picks a claimed issue."""
    tracker = _Tracker()
    _idle_pools(monkeypatch)
    monkeypatch.setattr(loop, "produce_once", lambda cfg, _log=None, slot=None: "no mockups")
    monkeypatch.setattr(loop, "respond_once", lambda cfg, _log=None, slot=None: "no replies")
    monkeypatch.setattr(loop, "run_once", lambda cfg, _log=None, slot=None: "no builds")

    queue = list(range(1, 8))   # 7 issues waiting
    reserved_seen = []

    def next_candidate(cfg, reserved=frozenset()):
        reserved_seen.append(set(reserved))
        remaining = [n for n in queue if n not in reserved]
        return ({"number": remaining[0], "labels": [], "title": "t"}, "") if remaining else None

    monkeypatch.setattr(loop, "_next_intake_candidate", next_candidate)
    monkeypatch.setattr(loop, "_claim_triage", lambda repo, n: None)

    class _Builder:
        tool = "claude"
    monkeypatch.setattr(dispatch, "pick_pair", lambda *a, **k: (_Builder(), None, ""))

    def fake_session(cfg, issue, extra, builder):
        tracker.enter("triage")
        time.sleep(0.05)
        tracker.leave("triage")
        return f"#{issue['number']}: triaged"
    monkeypatch.setattr(loop, "_run_intake_session", fake_session)

    gov = Governor(machine_ceiling=10, stage_caps={"triage": 3, "build": 2,
                                                   "mockup": 1, "respond": 1})
    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], gov, _log=lambda _m: None)

    assert tracker.peak["triage"] == 3            # several triages concurrent, capped at 3
    assert tracker.current["triage"] == 0         # all released
    # The fan-out selected exactly the cap-many distinct issues, reserving as it went.
    assert any(len(r) == 3 for r in reserved_seen)


def test_active_pool_paces_while_the_idle_pool_runs_free(monkeypatch):
    """Operator active on claude: at most one new claude session starts this cycle, while the
    idle codex pool keeps dispatching. The active pool yields — it does not hard-stop."""
    tracker = _Tracker()
    _idle_pools(monkeypatch, active={"claude": True})
    _no_triage_no_mockup_no_respond(monkeypatch)
    # Even repos build on claude (active/paced), odd on codex (idle/free).
    tool_for = lambda repo: "claude" if int(repo[-1]) % 2 == 0 else "codex"
    started = Counter()

    def fake_run_once(cfg, _log=None, slot=None):
        tool = tool_for(cfg.repo)
        if slot is not None and not slot.admit("build", tool):
            return "deferred"
        try:
            started[tool] += 1
            time.sleep(0.02)
            return "built"
        finally:
            slot.release("build")
    monkeypatch.setattr(loop, "run_once", fake_run_once)

    repos = [RepoConfig(f"o/r{i}", f"/tmp/{i}") for i in range(6)]
    gov = Governor(machine_ceiling=10, stage_caps={"build": 5, "triage": 3,
                                                   "mockup": 1, "respond": 1}, pace=1)
    dispatch.run_cycle(repos, gov, _log=lambda _m: None)

    assert started["claude"] == 1     # active pool paced to one new session per cycle
    assert started["codex"] >= 2      # idle pool unaffected


def test_yield_decision_is_logged_before_the_dashboard_shows_it(monkeypatch):
    """The operator-yield is observable in the daemon log (ADR 0025) — driven off the real
    activity read, not a stubbed one, so the log line reflects the actual pool fact."""
    from agentflow.balancer import PoolStatus
    logs = []
    monkeypatch.setattr(dispatch.balancer, "_query_pool",
                        lambda tool: PoolStatus(tool, True, 20.0, active=(tool == "claude"),
                                                ceiling=50.0 if tool == "claude" else 85.0))
    _no_triage_no_mockup_no_respond(monkeypatch)
    monkeypatch.setattr(loop, "run_once", lambda cfg, _log=None, slot=None: "no builds")
    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], Governor(), _log=logs.append)
    assert any("claude yielding to operator" in m and "ceiling 50%" in m for m in logs)


def test_coordinated_phase_submits_to_the_coordinator_and_skips_the_legacy_build(monkeypatch):
    """In coordinated phase Build goes to the session coordinator, never the legacy launcher —
    so one cycle can never launch both legacy and coordinator-owned Build work (issue #103).
    Afterward the live board is republished as a projection of the running records."""
    from agentflow.coordinator import COORDINATED, Phase
    _idle_pools(monkeypatch)
    _no_triage_no_mockup_no_respond(monkeypatch)
    monkeypatch.setattr(loop, "_next_intake_candidate", lambda *a, **k: None)
    monkeypatch.setattr(loop, "produce_once", lambda *a, **k: pytest.fail(
        "legacy Mockup orchestration must not run in coordinated mode"))
    monkeypatch.setattr(loop, "respond_once", lambda *a, **k: pytest.fail(
        "Respond must stay queued while Build is coordinated"))
    monkeypatch.setattr(loop, "run_once", lambda *a, **k: pytest.fail(
        "legacy build must not launch while Build is coordinated"))
    monkeypatch.setattr(dispatch.coordinated_build, "resolve_phase",
                        lambda rollout, repos, sessions, **k: Phase(COORDINATED))
    issue = {"number": 7, "title": "add a thing",
             "labels": [{"name": "agentflow:complexity:deep"}]}
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: issue)
    builder = type("B", (), {"tool": "claude"})()
    monkeypatch.setattr(dispatch, "pick_pair", lambda *a, **k: (builder, None, ""))
    monkeypatch.setattr(loop, "_claim", lambda repo, number: True)
    submitted, projected = [], []
    coord = type("C", (), {"submit_stage": lambda self, sub: submitted.append(sub)})()
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project",
                        lambda coordinator, phase, _log=None: projected.append(coordinator) or [])

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], Governor(), coordinator=coord,
                       _log=lambda _m: None)

    assert len(submitted) == 1
    assert submitted[0].stage == "build" and submitted[0].subject == "7"
    assert projected  # running records were projected back onto the live board


def test_coordinated_phase_claims_and_submits_intake_before_build(monkeypatch):
    from agentflow.coordinator import COORDINATED, Phase
    _idle_pools(monkeypatch)
    monkeypatch.setattr(dispatch.coordinated_build, "resolve_phase",
                        lambda rollout, repos, sessions, **k: Phase(COORDINATED))
    intake = {"number": 3, "title": "vague", "body": "help", "labels": []}
    monkeypatch.setattr(loop, "_next_intake_candidate",
                        lambda cfg, reserved: None if reserved else (intake, ""))
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: None)
    builder = type("B", (), {"tool": "claude"})()
    monkeypatch.setattr(dispatch, "pick_pair", lambda *a, **k: (builder, None, ""))
    events = []
    monkeypatch.setattr(loop, "_claim_triage",
                        lambda repo, number: events.append(("claim", repo, number)) or True)
    monkeypatch.setattr(loop, "_run", lambda cmd: subprocess.CompletedProcess(
        cmd, 0, "source-sha\n", ""))
    submitted = []
    coord = type("C", (), {"submit_stage": lambda self, sub: (
        submitted.append(sub), events.append(("submit", sub.subject)))})()
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project", lambda *a, **k: [])

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], Governor(), coordinator=coord,
                       _log=lambda _m: None)

    assert events == [("submit", "3"), ("claim", "o/r", 3)]
    assert len(submitted) == 1 and submitted[0].stage == "intake"


def test_coordinated_run_cycle_discovers_claims_and_submits_one_respond(monkeypatch):
    """The public dispatch interface turns one pending comment into its durable Respond."""
    from agentflow.coordinator import COORDINATED, Phase
    _idle_pools(monkeypatch)
    monkeypatch.setattr(dispatch.coordinated_build, "resolve_phase",
                        lambda rollout, repos, sessions, **k: Phase(COORDINATED))
    monkeypatch.setattr(loop, "_next_intake_candidate", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: None)
    monkeypatch.setattr(loop, "respond_once", lambda *a, **k: pytest.fail(
        "legacy Respond must not launch in coordinated mode"))

    def github(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps([
                {"number": 42,
                 "headRefName": "agentflow/claude/issue-7-fix-thing",
                 "headRefOid": "head-42"}]), "")
        if argv[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"comments": [
                {"body": "> *agentflow: parked for human review.*"},
                {"body": "please tweak it", "id": "IC_42"},
            ]}), "")
        return subprocess.CompletedProcess(argv, 1, "", "unexpected")

    monkeypatch.setattr(loop, "_run", github)
    events = []
    monkeypatch.setattr(loop, "_claim",
                        lambda repo, number: events.append(("claim", number)) or True)
    coord = type("C", (), {"submit_stage": lambda self, sub: events.append(("submit", sub))})()
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project", lambda *a, **k: [])

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], Governor(), coordinator=coord,
                       _log=lambda _m: None)

    assert events[0] == ("claim", 7)
    submitted = events[1][1]
    assert submitted.stage == "respond" and submitted.target == "IC_42"
    assert submitted.pool == "claude" and "please tweak it" in submitted.input_ptr
    assert "agentflow-respond-baseline:head-42" in submitted.input_ptr


def test_coordinated_run_cycle_defers_next_comment_until_prior_respond_settles(monkeypatch):
    """One issue-level building claim cannot be released underneath a later Respond."""
    from agentflow.coordinator import COORDINATED, Phase
    _idle_pools(monkeypatch)
    monkeypatch.setattr(dispatch.coordinated_build, "resolve_phase",
                        lambda rollout, repos, sessions, **k: Phase(COORDINATED))
    monkeypatch.setattr(loop, "_next_intake_candidate", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: None)
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix-thing", "second question", "IC_2", "head-42"))
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues",
                        lambda cfg, lane=None: {7})
    claims, submitted = [], []
    monkeypatch.setattr(loop, "_claim", lambda *a: claims.append(a) or True)
    coord = type("C", (), {"submit_stage": lambda self, sub: submitted.append(sub)})()
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project", lambda *a, **k: [])

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], Governor(), coordinator=coord,
                       _log=lambda _m: None)
    assert claims == [] and submitted == []


def test_coordinated_run_cycle_submits_and_claims_one_mockup_round(monkeypatch):
    from agentflow.coordinator import COORDINATED, Phase
    _idle_pools(monkeypatch)
    monkeypatch.setattr(dispatch.coordinated_build, "resolve_phase",
                        lambda rollout, repos, sessions, **k: Phase(COORDINATED))
    monkeypatch.setattr(loop, "_next_intake_candidate", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: None)
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: None)
    issue = {"number": 11, "title": "A screen", "body": "Draw variants",
             "labels": [{"name": "agentflow:needs-mockup"}]}
    monkeypatch.setattr(loop, "_next_mockup_issue", lambda cfg: issue)
    monkeypatch.setattr(loop, "produce_once", lambda *a, **k: pytest.fail(
        "legacy Mockup must not launch in coordinated mode"))
    builder = type("B", (), {"tool": "claude"})()
    monkeypatch.setattr(dispatch, "pick_pair", lambda *a, **k: (builder, None, ""))
    events = []
    monkeypatch.setattr(loop, "_claim_mockup",
                        lambda repo, number: events.append(("claim", number)) or True)
    coord = type("C", (), {"submit_stage": lambda self, sub: events.append(("submit", sub))})()
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project", lambda *a, **k: [])

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], Governor(), coordinator=coord,
                       _log=lambda _m: None)

    assert events[0][0] == "submit" and events[0][1].stage == "mockup"
    assert events[0][1].pool == "claude" and events[0][1].subject == "11"
    assert events[1] == ("claim", 11)


def test_draining_phase_launches_no_new_provider_stage(monkeypatch):
    """A drain stops every new legacy provider stage and makes no coordinated submission, while
    the coordinator keeps reconciling whatever it already owns (issue #103)."""
    from agentflow.coordinator import DRAINING, Phase
    _idle_pools(monkeypatch)
    _no_triage_no_mockup_no_respond(monkeypatch)
    monkeypatch.setattr(loop, "_next_intake_candidate", lambda *a, **k: pytest.fail(
        "no new Intake during a drain"))
    monkeypatch.setattr(loop, "produce_once", lambda *a, **k: pytest.fail(
        "no new Mockup during a drain"))
    monkeypatch.setattr(loop, "respond_once", lambda *a, **k: pytest.fail(
        "no new Respond during a drain"))
    monkeypatch.setattr(loop, "run_once", lambda *a, **k: pytest.fail(
        "no new legacy build during a drain"))
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: pytest.fail(
        "no new coordinated submission during a drain"))
    monkeypatch.setattr(dispatch.coordinated_build, "resolve_phase",
                        lambda rollout, repos, sessions, **k: Phase(DRAINING))
    reconciled = []
    coord = object()
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project",
                        lambda coordinator, phase, _log=None: reconciled.append(coordinator) or [])

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], Governor(), coordinator=coord,
                       _log=lambda _m: None)

    assert reconciled == [coord]  # existing records still reconcile; nothing new launches


def test_unreadable_rollout_evidence_fails_closed_into_a_named_drain(monkeypatch):
    monkeypatch.setattr(dispatch.live, "running_strict",
                        lambda: (_ for _ in ()).throw(ValueError("partial live board")))
    logs = []

    phase = dispatch._resolve_phase(None, [RepoConfig("o/r", "/tmp")], logs.append)

    assert phase.name == "draining" and not phase.launch_legacy
    assert "partial live board" in phase.blocked_by[0]
    assert any("draining" in line for line in logs)


def test_coordinated_submission_requires_the_visible_build_claim(monkeypatch):
    issue = {"number": 7, "title": "add a thing",
             "labels": [{"name": "agentflow:complexity:deep"}]}
    monkeypatch.setattr(loop, "_next_ready_issue", lambda cfg, _log=None: issue)
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda *a, **k: (type("B", (), {"tool": "claude"})(), None, ""))
    monkeypatch.setattr(dispatch.coordinated_build, "build_submission", lambda *a: object())
    monkeypatch.setattr(loop, "_claim", lambda repo, number: False)
    submitted = []
    coord = type("C", (), {"submit_stage": lambda self, value: submitted.append(value)})()

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", "/tmp"), coord, None)

    assert "could not claim" in result
    assert submitted == []


def test_merges_never_overlap_under_concurrent_builds(monkeypatch):
    """Concurrent build chains landing at once must not overlap: the merge lock serializes the
    squash-merge across all of them (ADR 0009 collision floor)."""
    overlap = _Tracker()

    def fake_run(cmd, cwd=None, timeout=None):
        import subprocess
        if cmd[:3] == ["gh", "pr", "view"]:
            overlap.enter("merge")
            time.sleep(0.03)
            overlap.leave("merge")
            return subprocess.CompletedProcess(cmd, 0, '{"isDraft": false}', "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(gate, "_run", fake_run)

    threads = [threading.Thread(target=gate.squash_merge, args=("o/r", pr)) for pr in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlap.total_peak == 1   # exactly one merge in flight at any instant
