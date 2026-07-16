"""The coordinator's public seam (ADR 0030): idempotent logical-stage submission and pool
cycling, with the admission matrix, continuation priority, atomic permit reservation, and
provider observations kept private. Everything here is driven through ``submit_stage`` and
``cycle`` — the only two calls stage orchestration makes. Also asserts the dormant guarantee:
nothing here is wired into the daemon yet.
"""

from __future__ import annotations

from conftest import FakeSession, NeverStartsLauncher, permits

from agentflow.coordinator import Coordinator, StageOutcome, Submission
from agentflow.coordinator.providers import ProviderCause


def test_submit_stage_is_idempotent_on_the_logical_stage_identity(make_coord):
    coord = make_coord(FakeSession())
    first = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    again = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    assert first == again


def test_legacy_lane_alias_never_turns_revise_into_build(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    # A revise reported on the ambiguous `building` lane must charge revise, not build.
    build = coord.submit_stage(Submission(repo="o/r", subject="9", stage="building",
                                          pool="claude", complexity="deep"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise",
                                           pool="claude", complexity="deep"))
    # Build (deep, no effort) reserves the exclusive five; revise reserves three. If the alias
    # had collapsed revise into build the pool could not have fit both — it fits exactly.
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 5  # build 5 admitted; revise deferred, pool full
    fake.end(build, success=True)
    assert [o.stage for o in coord.cycle("claude")] == ["build"]
    assert permits(coord, "claude") == 3  # now revise (3) admitted — proving it stayed revise


def test_cycle_admits_intake_and_charges_one_permit(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="1", stage="intake",
                                             pool="claude"))
    assert coord.cycle("claude") == []       # admitted; its outcome is not terminal this cycle
    assert permits(coord, "claude") == 1
    assert coord.cycle("codex") == []        # the other pool has no work
    fake.end(identity, success=True)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    assert permits(coord, "claude") == 0


def test_unknown_pool_submission_is_inadmissible(make_coord):
    coord = make_coord(FakeSession())
    coord.submit_stage(Submission(repo="o/r", subject="2", stage="review", pool="gemini"))
    # No ledger to charge an unknown pool, so it never starts and never yields an outcome.
    assert coord.cycle("gemini") == []
    assert permits(coord, "gemini") == 0


def test_capacity_reset_defers_a_continuation_until_it_is_eligible(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="3", stage="review",
                                             pool="claude"))
    assert coord.cycle("claude", now=0) == []
    assert permits(coord, "claude") == 1

    # A capacity interruption with a future reset returns the stage to waiting and defers it.
    fake.end(identity, cause=ProviderCause.CAPACITY, reset_at=50)
    assert coord.cycle("claude", now=49) == []          # reconciled to waiting, permits freed
    assert permits(coord, "claude") == 0
    assert coord.cycle("claude", now=49) == []          # still not eligible, not restarted
    assert permits(coord, "claude") == 0
    assert coord.cycle("claude", now=50) == []          # reset reached, restarted
    assert permits(coord, "claude") == 1                 # a second attempt is now running


def test_never_started_launch_consumes_no_permit(make_coord):
    coord = make_coord(FakeSession(), launcher=NeverStartsLauncher())
    coord.submit_stage(Submission(repo="o/r", subject="4", stage="review", pool="claude"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0  # a launch that never started reserves nothing


def test_permit_ledger_is_shared_across_coordinator_instances(make_coord):
    """Two coordinator instances over one store draw from the same durable permit ledger, so
    a second instance sees the first's reservations and cannot push a pool past its budget —
    two demand-2 reviews fit, a third does not (ADR 0029/0030)."""
    fake = FakeSession()
    a = make_coord(fake)
    b = make_coord(fake)
    a.submit_stage(Submission(repo="o/r", subject="a1", stage="review", pool="codex"))
    a.submit_stage(Submission(repo="o/r", subject="a2", stage="review", pool="codex"))
    b.submit_stage(Submission(repo="o/r", subject="b1", stage="review", pool="codex"))

    a.cycle("codex")                     # a reserves two (four permits)
    assert permits(a, "codex") == 4
    b.cycle("codex")                     # b sees the shared ledger is full and reserves none
    assert permits(b, "codex") == 4


def test_only_build_is_wired_behind_the_coordinator():
    """Guardrail for issue #103: Build — and only Build — has moved behind the coordinator.
    Dispatch routes it through the rollout; the legacy provider surface (`runner`) and the other
    five logical stages' orchestration (`loop`) still never import the coordinator, so nothing
    else submits work there."""
    import agentflow.dispatch
    import agentflow.loop
    import agentflow.runner
    dispatch_source = agentflow.dispatch.__loader__.get_source("agentflow.dispatch") or ""
    assert "coordinated_build" in dispatch_source  # Build is wired
    for module in (agentflow.loop, agentflow.runner):
        source = module.__loader__.get_source(module.__name__) or ""
        assert "agentflow.coordinator" not in source


def test_stage_outcome_is_the_only_terminal_fact_that_crosses_the_seam():
    """cycle returns typed terminal outcomes, not the mutable record and not started ids."""
    assert StageOutcome("id", "review", "completed").status == "completed"
    assert not hasattr(Coordinator, "reconcile")  # reconciliation is private to cycle


def test_public_surface_keeps_completed_boundary_settlement_private(make_coord):
    """Completed boundary settlement stays behind cycle, preserving ADR 0030's deep seam."""
    coord = make_coord(FakeSession())
    public = {name for name in dir(coord)
              if not name.startswith("_") and callable(getattr(coord, name))}
    assert public == {"submit_stage", "cycle", "park_completed"}
    assert not hasattr(coord, "permits")      # permit accounting is an internal invariant
    assert not hasattr(coord, "records")      # the working set is private (_records)
