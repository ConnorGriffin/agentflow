"""The dispatch governor and cycle — the concurrency/pacing decision surface (ADR 0023 / 0025)."""

import threading
import time
from collections import Counter

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
