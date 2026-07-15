"""Executable historical replay for the resilient-session policy (Wayfinder #94).

These 28 cases now drive the **production** session coordinator (ADR 0030) rather than a
throwaway oracle: the admission matrix, continuation ordering, attempt budget, permit
ledger, lineage rules, claim transfer, and crash-safe recovery are exercised against the
same code the daemon will run, over an isolated SQLite store per case. Synthetic root
identifiers carry only the aggregate demand and duration reported in
``docs/research/historical-session-demand.md``; explicit interruption cases then replay the
state, admission, and crash rules accepted in ADRs 0028--0030.

``submit`` and ``cycle`` are the coordinator's public seam; the fine-grained transition
methods below are its private mechanics, driven here white-box because this is a policy
verification harness, not a production caller.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from agentflow.coordinator import Coordinator, Gates, Record
from agentflow.coordinator.admission import (
    ADMISSION_MATRIX, ATTEMPT_BUDGET, CODE_WRITING, STAGE_NATIVE_HANDOFF, admission_demand)


def new_coordinator(tmp_path, name: str = "store.db", **kwargs) -> Coordinator:
    """A coordinator over an isolated store. Reopening the same ``name`` replays a crash."""
    return Coordinator(tmp_path / name, **kwargs)


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


def record_from(root: HistoricalRoot, *, suffix: str = "") -> Record:
    expected = admission_demand(
        root.stage, root.pool, root.model, root.complexity, root.effort)
    assert expected == root.demand
    return Record(
        root.synthetic_id + suffix,
        root.stage,
        root.pool,
        root.demand,
        model=root.model,
        complexity=root.complexity,
        effort=root.effort,
        lineage=root.pool if root.stage in CODE_WRITING else None,
    )


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


def test_same_snapshot_historical_stampede_is_bounded_atomically(tmp_path):
    """The observed 4+1+5 launch burst becomes 4+1 admitted and 5 deferred."""

    replay = new_coordinator(tmp_path)
    roots = [
        record_from(HISTORICAL_ROOTS["claude-medium-build"]),
        record_from(HISTORICAL_ROOTS["claude-intake"]),
        record_from(HISTORICAL_ROOTS["claude-deep-build"]),
    ]
    for root in roots:
        replay.submit(root)

    admitted = replay.cycle("claude", now=0)

    assert admitted == ["H-BUILD-1", "H-INTAKE-1"]
    assert replay.permits("claude") == 5
    assert roots[2].state == "waiting" and roots[2].attempts == 0


def test_two_writers_cannot_share_one_five_permit_pool(tmp_path):
    replay = new_coordinator(tmp_path)
    first = replay.submit(record_from(HISTORICAL_ROOTS["claude-revise"]))
    second = replay.submit(record_from(HISTORICAL_ROOTS["claude-revise"], suffix="-2"))

    assert replay._begin_start(first)
    replay._commit_start(first, "started")
    assert not replay._begin_start(second)
    assert replay.permits("claude") == 3
    assert second.attempts == 0


def test_near_exclusive_writer_keeps_useful_short_stage_concurrency(tmp_path):
    replay = new_coordinator(tmp_path)
    writer = replay.submit(record_from(HISTORICAL_ROOTS["claude-medium-build"]))
    short = replay.submit(record_from(HISTORICAL_ROOTS["claude-review"]))
    other_writer = replay.submit(record_from(HISTORICAL_ROOTS["claude-revise"]))

    for record in (writer, short):
        assert replay._begin_start(record)
        replay._commit_start(record, "started")

    assert replay.permits("claude") == 5
    assert not replay._begin_start(other_writer)


def test_continuation_head_of_line_blocks_bypass_without_preempting_live_work(tmp_path):
    replay = new_coordinator(tmp_path)
    live_short = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"], suffix="-live"))
    assert replay._begin_start(live_short)
    replay._commit_start(live_short, "started")

    oldest = replay.submit(Record("C-OLD", "build", "claude", 5, True, 1, 1, lineage="claude"))
    later = replay.submit(Record("C-LATER", "review", "claude", 1, True, 2, 2))
    cold = replay.submit(Record("COLD", "intake", "claude", 1))

    assert replay.cycle("claude", now=10) == []
    assert live_short.state == "running"
    assert later.state == cold.state == "waiting"

    replay._finish(live_short, provider="success", outcome=True)
    assert replay.cycle("claude", now=10) == [oldest.identity]
    assert later.state == cold.state == "waiting"


def test_code_lineage_is_pinned_while_read_only_continuation_can_move(tmp_path):
    replay = new_coordinator(tmp_path)
    pinned = replay.submit(Record(
        "PINNED", "build", "codex", 5, True,
        model="sol", complexity="deep", effort="medium", lineage="codex"))
    available = {"claude": True, "codex": False}

    assert replay._route(pinned, available, safe={"claude", "codex"}) is None
    assert (pinned.pool, pinned.demand, pinned.lineage) == ("codex", 5, "codex")
    pinned.pool = "claude"  # even a malformed caller cannot bypass the lineage gate
    assert not replay._begin_start(pinned)
    pinned.pool = "codex"

    movable = replay.submit(Record(
        "MOVABLE", "review", "codex", 2, True, model="sol", complexity="deep",
        builder_lineage="codex"))
    assert replay._route(movable, available, safe={"claude"}) == "claude"
    assert (movable.pool, movable.model, movable.demand, movable.lineage,
            movable.auto_merge_allowed) == ("claude", "opus", 1, None, True)

    same_tool = replay.submit(Record(
        "SAME-TOOL", "review", "claude", 1, True, model="opus", complexity="deep",
        builder_lineage="claude"))
    assert replay._route(same_tool, {"claude": True}, safe={"claude"}) == "claude"
    assert same_tool.auto_merge_allowed is False
    assert replay._begin_start(same_tool)
    replay._commit_start(same_tool, "started")
    replay._finish(same_tool, provider="success", outcome=True)
    assert (same_tool.state, same_tool.auto_merge_allowed) == ("completed", False)


def test_future_capacity_reset_controls_eligibility_without_hot_looping(tmp_path):
    replay = new_coordinator(tmp_path)
    paused = replay.submit(record_from(HISTORICAL_ROOTS["claude-review"]))
    assert replay._begin_start(paused)
    replay._commit_start(paused, "started")
    replay._finish(paused, provider="recoverable", outcome=False, eligible_at=50)
    cold = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"]))

    assert replay.cycle("claude", now=49) == [cold.identity]
    replay._finish(cold, provider="success", outcome=True)
    assert (paused.state, paused.attempts, paused.claim) == ("waiting", 1, True)
    assert replay.cycle("claude", now=50) == [paused.identity]


@pytest.mark.parametrize("failed_gate", ["headroom", "machine", "stage", "pace"])
def test_every_independent_gate_defers_without_permits_or_attempts(tmp_path, failed_gate):
    replay = new_coordinator(tmp_path)
    record = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"]))
    gates = Gates(**{failed_gate: False})

    assert not replay._begin_start(record, gates)
    assert record.state == "waiting"
    assert record.attempts == replay.permits("claude") == 0


def test_permit_deferral_also_consumes_neither_permit_nor_attempt(tmp_path):
    replay = new_coordinator(tmp_path)
    live = replay.submit(record_from(HISTORICAL_ROOTS["claude-medium-build"]))
    deferred = replay.submit(record_from(HISTORICAL_ROOTS["claude-review"], suffix="-2"))
    assert replay._begin_start(live)
    replay._commit_start(live, "started")
    full = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"], suffix="-full"))
    assert replay._begin_start(full)
    replay._commit_start(full, "started")
    used_before = replay.permits("claude")

    assert not replay._begin_start(deferred)
    assert deferred.attempts == 0
    assert replay.permits("claude") == used_before == 5


def test_crash_recovery_distinguishes_not_started_from_started_atomically(tmp_path):
    before_crash = new_coordinator(tmp_path, "a.db")
    reserved = before_crash.submit(record_from(HISTORICAL_ROOTS["codex-review"]))
    assert before_crash._begin_start(reserved)

    after_crash = new_coordinator(tmp_path, "a.db")
    assert after_crash.permits("codex") == 2  # ambiguous running state fails closed
    not_started = after_crash.records[reserved.identity]
    after_crash._recover_start(not_started, "not_started", process_alive=False)
    assert (not_started.state, not_started.attempts, after_crash.permits("codex")) == (
        "waiting", 0, 0)

    before_crash = new_coordinator(tmp_path, "b.db")
    started = before_crash.submit(record_from(HISTORICAL_ROOTS["codex-review"], suffix="-2"))
    assert before_crash._begin_start(started)
    before_crash._commit_start(started, "started")

    after_crash = new_coordinator(tmp_path, "b.db")
    recovered = after_crash.records[started.identity]
    after_crash._recover_start(recovered, "started", process_alive=True)
    after_crash._recover_start(recovered, "started", process_alive=True)
    assert (recovered.state, recovered.attempts, after_crash.permits("codex")) == (
        "running", 1, 2)


def test_live_recovered_family_keeps_one_root_reservation_and_dead_family_releases_it(tmp_path):
    before_crash = new_coordinator(tmp_path)
    root = before_crash.submit(record_from(HISTORICAL_ROOTS["codex-review"]))
    assert before_crash._begin_start(root)
    before_crash._commit_start(root, "started")
    for descendant in ("D-1", "D-2", "D-3", "D-4"):
        before_crash._add_descendant(root, descendant)

    after_crash = new_coordinator(tmp_path)
    recovered = after_crash.records[root.identity]
    after_crash._recover_start(recovered, "started", process_alive=True)
    assert after_crash.permits("codex") == 2
    assert len(recovered.descendants) == 4

    after_crash._recover_start(recovered, "started", process_alive=False)
    assert after_crash.permits("codex") == 0
    assert (recovered.state, recovered.attempts, recovered.claim) == ("waiting", 1, True)


@pytest.mark.parametrize("provider", ["recoverable", "incomplete", "unknown", "success"])
def test_non_terminal_endings_wait_when_the_outcome_is_missing(tmp_path, provider):
    replay = new_coordinator(tmp_path)
    record = replay.submit(Record(f"END-{provider}", "review", "claude", 1))
    assert replay._begin_start(record)
    replay._commit_start(record, "started")
    replay._finish(record, provider=provider, outcome=False)
    assert (record.state, record.attempts, record.claim) == ("waiting", 1, True)


def test_outcome_precedence_permanent_hold_and_exhaustion_all_terminate_safely(tmp_path):
    replay = new_coordinator(tmp_path)

    success = replay.submit(Record("OUTCOME", "review", "claude", 1))
    assert replay._begin_start(success)
    replay._commit_start(success, "started")
    replay._finish(success, provider="permanent", outcome=True)
    assert success.state == "completed"

    permanent = replay.submit(Record("PERMANENT", "review", "claude", 1))
    assert replay._begin_start(permanent)
    replay._commit_start(permanent, "started")
    replay._finish(permanent, provider="permanent", outcome=False)
    assert (permanent.state, permanent.hold_pending, permanent.claim) == (
        "waiting", True, True)
    replay._finalize_hold(permanent)
    assert (permanent.state, permanent.handoffs, permanent.claim) == ("held", 1, False)

    exhausted = replay.submit(Record("EXHAUSTED", "build", "claude", 3, lineage="claude"))
    for expected_attempt in range(1, ATTEMPT_BUDGET + 1):
        assert replay._begin_start(exhausted)
        replay._commit_start(exhausted, "started")
        replay._finish(exhausted, provider="unknown", outcome=False)
        assert exhausted.attempts == expected_attempt
    assert (exhausted.state, exhausted.hold_pending, exhausted.claim) == (
        "waiting", True, True)
    replay._finalize_hold(exhausted)
    assert (exhausted.state, exhausted.handoffs, exhausted.claim) == ("held", 1, False)
    assert not replay._begin_start(exhausted)
    replay._finalize_hold(exhausted)
    assert exhausted.handoffs == 1


@pytest.mark.parametrize(
    ("stage", "expected"),
    STAGE_NATIVE_HANDOFF.items(),
)
def test_exhaustion_creates_exactly_one_stage_native_handoff(tmp_path, stage, expected):
    replay = new_coordinator(tmp_path)
    record = replay.submit(Record(f"HOLD-{stage}", stage, "claude", 1,
                                  attempts=ATTEMPT_BUDGET - 1,
                                  lineage="claude" if stage in CODE_WRITING else None))
    assert replay._begin_start(record)
    replay._commit_start(record, "started")
    replay._finish(record, provider="unknown", outcome=False)
    replay._finalize_hold(record)

    assert (record.state, record.handoffs, record.handoff_kind, record.claim) == (
        "held", 1, expected, False)


def test_handoff_proof_survives_crash_without_duplicate_handoff_or_notification(tmp_path):
    before_crash = new_coordinator(tmp_path)
    record = before_crash.submit(Record(
        "CRASH-HOLD", "review", "claude", 1, attempts=ATTEMPT_BUDGET - 1))
    assert before_crash._begin_start(record)
    before_crash._commit_start(record, "started")
    before_crash._finish(record, provider="unknown", outcome=False)
    before_crash._finalize_hold(record, crash_after_proof=True)
    assert (record.state, record.hold_pending, record.claim, record.handoffs,
            record.notifications, record.handoff_proof is not None) == (
        "waiting", True, True, 1, 1, True)

    after_crash = new_coordinator(tmp_path)
    recovered = after_crash.records[record.identity]
    after_crash._finalize_hold(recovered)
    assert (recovered.state, recovered.claim, recovered.handoffs,
            recovered.notifications, recovered.handoff_proof) == (
        "held", False, 1, 1, record.handoff_proof)


def test_completed_stage_transfers_claim_before_retirement_or_releases_at_boundary(tmp_path):
    replay = new_coordinator(tmp_path)
    build = replay.submit(Record(
        "BUILD-STAGE", "build", "claude", 4, lineage="claude"))
    assert replay._begin_start(build)
    replay._commit_start(build, "started")
    replay._finish(build, provider="success", outcome=True)
    assert (build.state, build.claim, build.retired) == ("completed", True, False)

    review = Record("REVIEW-STAGE", "review", "codex", 2, model="sol")
    replay._finalize_completion(build, next_record=review)
    assert (review.state, review.claim) == ("waiting", True)
    assert (build.claim, build.retired) == (False, True)

    assert replay._begin_start(review)
    replay._commit_start(review, "started")
    replay._finish(review, provider="success", outcome=True)
    replay._finalize_completion(review, external_boundary_proven=True)
    assert (review.state, review.claim, review.retired) == ("completed", False, True)


def test_submission_is_idempotent_so_one_logical_stage_cannot_duplicate_work(tmp_path):
    replay = new_coordinator(tmp_path)
    first = replay.submit(Record("SAME-STAGE", "build", "claude", 4, lineage="claude"))
    duplicate = replay.submit(Record("SAME-STAGE", "build", "claude", 4, lineage="claude"))

    assert duplicate is first
    assert len(replay.records) == 1
