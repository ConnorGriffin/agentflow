"""Executable historical replay for the resilient-session policy (Wayfinder #94).

These cases drive the **production** session coordinator (ADR 0030) through its public
``submit_stage`` / ``cycle`` seam — never its private transitions. The admission matrix,
continuation ordering, attempt budget, permit ledger, capacity waits, and exhaustion holds
are exercised against the same code the daemon will run, over an isolated store per case.
Synthetic roots carry only the aggregate demand reported in
``docs/research/historical-session-demand.md``; the scenarios then replay the state,
admission, and outcome rules accepted in ADRs 0028--0030.

The provider lifecycle is scripted through the injected :class:`FakeSession` (a started and
alive family, then a specific ending); ``cycle`` returns only the terminal outcomes stage
orchestration consumes. The suite exercises continuation priority, gate composition, permit
conservation, tool-lineage rules (cross-pool review and the code-writing pin), descendant
reservation sharing, next-stage claim transfer, and fail-closed recovery. Only true cross-pool
*re-routing* of a read-only continuation onto the other pool mid-life remains a later slice.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from conftest import FakeSession, permits, record_of, starts_until_held

from agentflow.coordinator import Submission
from agentflow.coordinator.admission import (
    ADMISSION_MATRIX, CODE_WRITING, STAGE_NATIVE_HANDOFF, admission_demand)
from agentflow.coordinator.providers import ProviderCause


@dataclass(frozen=True)
class HistoricalRoot:
    """Privacy-safe representative of one aggregate historical session cell."""

    synthetic_id: str
    stage: str
    pool: str
    model: str
    complexity: str
    demand: int
    effort: str | None = None


HISTORICAL_ROOTS = {
    "claude-medium-build": HistoricalRoot(
        "H-BUILD-1", "build", "claude", "sonnet", "standard", 4, "medium"),
    "claude-deep-build": HistoricalRoot(
        "H-BUILD-2", "build", "claude", "opus", "deep", 5, "high"),
    "claude-intake": HistoricalRoot(
        "H-INTAKE-1", "intake", "claude", "opus", "deep", 1),
    "claude-review": HistoricalRoot(
        "H-REVIEW-1", "review", "claude", "opus", "deep", 1),
    "codex-review": HistoricalRoot(
        "H-REVIEW-2", "review", "codex", "sol", "deep", 2),
    "claude-revise": HistoricalRoot(
        "H-REVISE-1", "revise", "claude", "opus", "deep", 3),
    "codex-revise": HistoricalRoot(
        "H-REVISE-2", "revise", "codex", "sol", "deep", 4),
}


def submit_root(coord, key: str, *, suffix: str = "") -> str:
    """Submit one historical root through the public seam, asserting its reviewed demand."""
    root = HISTORICAL_ROOTS[key]
    assert admission_demand(
        root.stage, root.pool, root.model, root.complexity, root.effort) == root.demand
    return coord.submit_stage(Submission(
        repo="o/r", subject=root.synthetic_id + suffix, stage=root.stage, pool=root.pool,
        complexity=root.complexity, effort=root.effort))


def test_reviewed_matrix_is_monotone_conservative_and_pool_scoped():
    build_rows = [
        [ADMISSION_MATRIX[("build", pool, model, complexity, effort)]
         for effort in ("low", "medium", "high", "extra")]
        for pool, model, complexity in (
            ("claude", "sonnet", "standard"),
            ("claude", "opus", "deep"),
            ("codex", "terra", "standard"),
            ("codex", "sol", "deep"),
        )
    ]
    assert all(row == sorted(row) for row in build_rows)
    assert all(demand >= 3 for key, demand in ADMISSION_MATRIX.items()
               if key[0] in CODE_WRITING)
    assert admission_demand("new-stage", "claude", "new-model", "deep") == 5
    assert admission_demand("review", "unknown", "sol", "deep") is None


def test_same_snapshot_historical_stampede_is_bounded_atomically(make_coord):
    """The observed 4+1+5 launch burst becomes 4+1 admitted and 5 deferred."""
    fake = FakeSession()
    coord = make_coord(fake)
    medium = submit_root(coord, "claude-medium-build")   # demand 4
    intake = submit_root(coord, "claude-intake")         # demand 1
    deep = submit_root(coord, "claude-deep-build")       # demand 5

    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 5                  # 4 + 1 admitted; the 5 is deferred

    fake.end(medium, success=True)
    fake.end(intake, success=True)
    completed = {o.identity for o in coord.cycle("claude")}
    assert completed == {medium, intake}                 # exactly the two that were admitted
    assert permits(coord, "claude") == 5                  # now the deferred build (5) runs
    assert coord.cycle("claude") == []                   # deep is running, nothing terminal
    fake.kill(deep)                                       # (leave the store clean)


def test_two_writers_cannot_share_one_five_permit_pool(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    first = submit_root(coord, "claude-revise")               # demand 3
    second = submit_root(coord, "claude-revise", suffix="-2")  # demand 3

    coord.cycle("claude")
    assert permits(coord, "claude") == 3                       # only one writer fits (3 + 3 > 5)

    done: set[str] = set()
    for _ in range(4):
        fake.end(first, success=True)   # completes whichever writer is currently running
        fake.end(second, success=True)
        done.update(o.identity for o in coord.cycle("claude"))
        assert permits(coord, "claude") <= 3                   # never both writers at once
    assert done == {first, second}                            # each ran, one after the other


def test_near_exclusive_writer_keeps_useful_short_stage_concurrency(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    submit_root(coord, "claude-medium-build")                # demand 4
    submit_root(coord, "claude-review")                      # demand 1
    submit_root(coord, "claude-revise")                      # demand 3, cannot fit

    coord.cycle("claude")
    assert permits(coord, "claude") == 5   # a 4-permit writer still leaves room for a 1 review


def test_continuation_head_of_line_blocks_bypass_without_preempting_live_work(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    big = submit_root(coord, "claude-deep-build")            # demand 5
    coord.cycle("claude")                                    # big runs, pool full
    fake.end(big, cause=ProviderCause.CAPACITY, reset_at=100)  # deferred continuation @100

    live = submit_root(coord, "claude-intake")               # demand 1
    assert coord.cycle("claude", now=0) == []                # big not eligible yet; live runs
    assert permits(coord, "claude") == 1

    cold = submit_root(coord, "claude-review")               # demand 1, could fit beside live
    assert coord.cycle("claude", now=100) == []              # big is eligible but cannot fit
    assert permits(coord, "claude") == 1                      # cold did NOT bypass the blocked big

    fake.end(live, success=True)
    assert {o.identity for o in coord.cycle("claude", now=100)} == {live}
    assert permits(coord, "claude") == 5                      # big (continuation) admitted first
    assert coord.cycle("claude", now=100) == []              # cold still waits behind big
    fake.kill(big)


def test_future_capacity_reset_controls_eligibility_without_hot_looping(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    paused = submit_root(coord, "claude-review")
    coord.cycle("claude", now=0)
    fake.end(paused, cause=ProviderCause.CAPACITY, reset_at=50)

    cold = submit_root(coord, "claude-intake")
    assert coord.cycle("claude", now=49) == []               # paused deferred; cold admitted
    assert permits(coord, "claude") == 1                      # only the cold intake is running
    fake.end(cold, success=True)
    assert {o.identity for o in coord.cycle("claude", now=49)} == {cold}
    assert coord.cycle("claude", now=49) == []               # paused still not eligible
    assert permits(coord, "claude") == 0
    assert coord.cycle("claude", now=50) == []               # reset reached — paused restarts
    assert permits(coord, "claude") == 1


def test_a_closed_gate_defers_without_permits_or_attempts(make_coord):
    fake = FakeSession()
    fake.gate_open = False
    coord = make_coord(fake)
    identity = submit_root(coord, "claude-intake")

    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0                      # a failed gate reserves nothing

    fake.gate_open = True
    # No permit and no attempt were spent, so the full three-attempt budget remains.
    assert starts_until_held(coord, fake, identity, "claude") == 3


def test_permit_deferral_consumes_neither_permit_nor_attempt(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    live = submit_root(coord, "claude-medium-build")         # demand 4
    deferred = submit_root(coord, "claude-revise")           # demand 3, cannot fit beside live
    coord.cycle("claude")
    assert permits(coord, "claude") == 4                      # only the writer started

    fake.end(live, success=True)                             # free the pool
    # The deferred writer never spent an attempt while blocked, so it keeps its full budget.
    assert starts_until_held(coord, fake, deferred, "claude") == 3


@pytest.mark.parametrize("cause", [
    ProviderCause.CAPACITY, ProviderCause.SERVER, ProviderCause.TIMEOUT,
    ProviderCause.PROCESS, ProviderCause.UNKNOWN, ProviderCause.NONE])
def test_non_terminal_endings_wait_when_the_outcome_is_missing(make_coord, cause):
    fake = FakeSession()
    fake.gate_open = True
    coord = make_coord(fake)
    identity = submit_root(coord, "claude-review")
    coord.cycle("claude")
    fake.gate_open = False  # observe the wait itself, without an immediate re-admit
    fake.end(identity, cause=cause)

    assert coord.cycle("claude") == []                       # no terminal outcome
    assert permits(coord, "claude") == 0                      # reservation released, waiting


def test_a_verified_outcome_completes_the_stage(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = submit_root(coord, "claude-review")
    coord.cycle("claude")
    fake.end(identity, success=True)
    assert [(o.identity, o.status) for o in coord.cycle("claude")] == [(identity, "completed")]


def test_permanent_condition_holds_immediately_regardless_of_budget(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = submit_root(coord, "claude-review")
    coord.cycle("claude")
    fake.end(identity, cause=ProviderCause.PERMANENT)
    outcomes = coord.cycle("claude")
    assert [(o.identity, o.status) for o in outcomes] == [(identity, "held")]
    assert coord.cycle("claude") == []                       # a hold is not re-emitted


def test_exhausting_the_attempt_budget_holds_the_stage(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = submit_root(coord, "claude-review")
    # Three unknown interruptions exhaust the budget and hold the stage.
    assert starts_until_held(coord, fake, identity, "claude") == 3


@pytest.mark.parametrize(("stage", "expected"), STAGE_NATIVE_HANDOFF.items())
def test_exhaustion_creates_exactly_one_stage_native_handoff(make_coord, stage, expected):
    fake = FakeSession()
    coord = make_coord(fake)
    pool = "claude"
    identity = coord.submit_stage(Submission(
        repo="o/r", subject=f"HOLD-{stage}", stage=stage, pool=pool, complexity="deep"))
    starts = 0
    held = None
    for _ in range(12):
        for outcome in coord.cycle(pool):
            if outcome.identity == identity and outcome.status == "held":
                held = outcome
        if held is not None:
            break
        if permits(coord, pool) > 0:
            starts += 1
            fake.end(identity, cause=ProviderCause.UNKNOWN)
    assert held is not None and held.handoff == expected
    assert coord.cycle(pool) == []                            # exactly one handoff, not repeated


def test_submission_is_idempotent_so_one_logical_stage_cannot_duplicate_work(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    first = submit_root(coord, "claude-medium-build")
    duplicate = submit_root(coord, "claude-medium-build")
    assert duplicate == first
    coord.cycle("claude")
    assert permits(coord, "claude") == 4                      # one reservation, not two


def test_descendants_share_the_root_reservation_without_new_permits(make_coord):
    """A running root reserves once; its subagents inherit that reservation and never reserve
    independently, so nested work cannot push the pool past budget (ADR 0030). When the root
    completes, its descendants retire with it rather than lingering as waiting work."""
    fake = FakeSession()
    coord = make_coord(fake)
    root = submit_root(coord, "claude-medium-build")             # demand 4
    coord.cycle("claude")
    assert permits(coord, "claude") == 4

    for i in range(2):  # two subagents of the running root, each nominally demand 4
        coord.submit_stage(Submission(
            repo="o/r", subject=HISTORICAL_ROOTS["claude-medium-build"].synthetic_id,
            stage="build", pool="claude", complexity="standard", effort="medium",
            target=f"sub-{i}", descendant_of=root))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 4                        # descendants reserved nothing

    fake.end(root, success=True)
    assert {o.identity for o in coord.cycle("claude")} == {root}  # only the root is terminal
    assert permits(coord, "claude") == 0                        # released; descendants retired


def test_a_completed_stage_transfers_its_claim_to_the_next_stage(make_coord):
    """A completed stage keeps its GitHub claim until the next stage assumes it, then releases
    it — so a crash between stages can never drop ownership or double-claim (ADR 0030)."""
    fake = FakeSession()
    coord = make_coord(fake)
    review = coord.submit_stage(Submission(repo="o/r", subject="T", stage="review", pool="claude"))
    coord.cycle("claude")
    fake.end(review, success=True)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    assert record_of(coord, review).claim is True               # kept until the transfer lands

    revise = coord.submit_stage(Submission(
        repo="o/r", subject="T", stage="revise", pool="claude", transfer_from=review))
    assert record_of(coord, review).claim is False              # released to the next stage
    assert record_of(coord, revise).claim is True               # revise now owns the claim


def test_a_read_only_review_routes_cross_pool_and_lineage_governs_auto_merge(make_coord):
    """A read-only review is unpinned: it runs on either pool. Whether it may auto-merge turns
    on lineage — a review by the same tool that built the diff cannot, a cross-tool one can
    (ADR 0028)."""
    fake = FakeSession()
    coord = make_coord(fake)
    cross = coord.submit_stage(Submission(repo="o/r", subject="X", stage="review",
                                          pool="codex", builder_lineage="claude"))
    same = coord.submit_stage(Submission(repo="o/r", subject="Y", stage="review",
                                         pool="claude", builder_lineage="claude"))
    assert record_of(coord, cross).auto_merge_allowed is True    # a different tool reviewed it
    assert record_of(coord, same).auto_merge_allowed is False    # a same-tool review cannot merge

    # Both admit on their own pool — the read-only review was free to route cross-pool.
    assert coord.cycle("codex") == [] and permits(coord, "codex") == 2
    assert coord.cycle("claude") == [] and permits(coord, "claude") == 1
    fake.kill(cross)
    fake.kill(same)


def test_a_code_writing_stage_cannot_leave_its_builder_lineage(make_coord):
    """A code-writing stage is pinned to the tool that built its diff. A revise of a
    claude-built diff submitted onto codex is an illegal cross-pool move: it never admits and
    reserves nothing, failing closed rather than silently switching tools (ADR 0028)."""
    fake = FakeSession()
    coord = make_coord(fake)
    coord.submit_stage(Submission(repo="o/r", subject="Z", stage="revise", pool="codex",
                                  builder_lineage="claude", complexity="deep"))
    assert coord.cycle("codex") == []
    assert permits(coord, "codex") == 0
