"""Executable historical replay for the resilient-session policy (Wayfinder #94).

This is deliberately a verification model, not the ADR 0030 production coordinator.
Synthetic root identifiers carry only the aggregate demand and duration reported in
``docs/research/historical-session-demand.md``.  Explicit interruption cases then replay
the state, admission, and crash rules accepted in ADRs 0028--0030.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field

import pytest


PERMIT_BUDGET = 5
ATTEMPT_BUDGET = 3
CODE_WRITING = {"build", "revise", "mockup", "respond"}
STAGE_NATIVE_HANDOFF = {
    "intake": "issue:needs-grilling",
    "build": "issue:needs-grilling",
    "mockup": "issue:needs-mockup",
    "review": "pr:parked",
    "revise": "pr:parked",
    "respond": "pr:parked",
}

# Exact reviewed rows from ADR 0029. Unknown rows on a known pool fall back to five;
# an unknown pool is inadmissible because there is no permit ledger to charge.
ADMISSION_MATRIX = {
    ("intake", "claude", "opus", "deep", None): 1,
    ("intake", "codex", "sol", "deep", None): 1,
    ("review", "claude", "opus", "deep", None): 1,
    ("review", "codex", "sol", "deep", None): 2,
    ("revise", "claude", "sonnet", "standard", None): 3,
    ("revise", "claude", "opus", "deep", None): 3,
    ("revise", "codex", "terra", "standard", None): 4,
    ("revise", "codex", "sol", "deep", None): 4,
    ("respond", "claude", "opus", "deep", None): 3,
    ("respond", "codex", "sol", "deep", None): 5,
    ("mockup", "claude", "opus", "deep", None): 5,
    ("mockup", "codex", "sol", "deep", None): 5,
}
for pool, model, complexity, demands in (
    ("claude", "sonnet", "standard", (3, 4, 5, 5)),
    ("claude", "opus", "deep", (4, 4, 5, 5)),
    ("codex", "terra", "standard", (4, 5, 5, 5)),
    ("codex", "sol", "deep", (5, 5, 5, 5)),
):
    for effort, demand in zip(("low", "medium", "high", "extra"), demands, strict=True):
        ADMISSION_MATRIX[("build", pool, model, complexity, effort)] = demand


def admission_demand(stage, pool, model, complexity, effort=None):
    if pool not in {"claude", "codex"}:
        return None
    return ADMISSION_MATRIX.get((stage, pool, model, complexity, effort), PERMIT_BUDGET)


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


@dataclass
class Record:
    identity: str
    stage: str
    pool: str
    demand: int
    continuation: bool = False
    eligible_at: int = 0
    created_at: int = 0
    model: str = "opus"
    complexity: str = "deep"
    effort: str | None = None
    attempts: int = 0
    state: str = "waiting"
    claim: bool = True
    lineage: str | None = None
    start_fact: str | None = None
    process_alive: bool = False
    descendants: set[str] = field(default_factory=set)
    handoffs: int = 0
    handoff_kind: str | None = None
    notifications: int = 0
    handoff_proof: str | None = None
    hold_pending: bool = False
    retired: bool = False
    builder_lineage: str | None = None
    auto_merge_allowed: bool = True


@dataclass(frozen=True)
class Gates:
    """The independent preconditions that must all pass for one provider start."""

    headroom: bool = True
    machine: bool = True
    stage: bool = True
    pace: bool = True


class ReplayCoordinator:
    """Policy oracle with explicit controls at crash and durable-boundary transitions."""

    def __init__(self) -> None:
        self.records: dict[str, Record] = {}
        self.running: dict[str, Record] = {}

    def submit(self, record: Record) -> Record:
        existing = self.records.setdefault(record.identity, record)
        if existing.state == "running":
            self.running[existing.identity] = existing
        return existing

    def permits_used(self, pool: str) -> int:
        return sum(record.demand for record in self.running.values() if record.pool == pool)

    def begin_start(self, record: Record, gates: Gates = Gates()) -> bool:
        if (record.state != "waiting" or record.hold_pending
                or record.attempts >= ATTEMPT_BUDGET):
            return False
        if record.stage in CODE_WRITING and record.pool != record.lineage:
            return False
        if not all((gates.headroom, gates.machine, gates.stage, gates.pace)):
            return False
        if self.permits_used(record.pool) + record.demand > PERMIT_BUDGET:
            return False
        record.state = "running"
        record.start_fact = None
        self.running[record.identity] = record
        return True

    def commit_start(self, record: Record, fact: str) -> None:
        assert record.state == "running"
        assert fact in {"started", "not_started"}
        record.start_fact = fact
        if fact == "not_started":
            self._release(record)
            record.state = "waiting"
            return
        record.attempts += 1
        record.process_alive = True

    def recover_start(self, record: Record, fact: str, *, process_alive: bool) -> None:
        """Replay the crash-recoverable provider start handshake from ADR 0030."""

        if record.start_fact is None:
            self.commit_start(record, fact)
        if fact == "not_started":
            return
        record.process_alive = process_alive
        if not process_alive:
            self.finish(record, provider="incomplete", outcome=False)

    def add_descendant(self, record: Record, synthetic_id: str) -> None:
        record.descendants.add(synthetic_id)

    def finish(self, record: Record, *, provider: str, outcome: bool,
               eligible_at: int | None = None) -> None:
        """Apply outcome-first classification, then release the provider family."""

        assert record.state == "running"
        self._release(record)
        if outcome:
            record.state = "completed"
            return
        if provider in {"bail", "permanent"}:
            self._hold(record)
            return
        if record.attempts < ATTEMPT_BUDGET:
            record.state = "waiting"
            record.continuation = True
            record.eligible_at = eligible_at or 0
            return
        self._hold(record)

    def route(self, record: Record, available: dict[str, bool], safe: set[str]) -> str | None:
        """Choose an allowed pool and recalculate demand at the destination row."""

        if record.stage in CODE_WRITING:
            candidates = [record.lineage] if record.lineage else []
        else:
            candidates = [record.pool, *sorted(set(available) - {record.pool})]
        for pool in candidates:
            if pool not in safe or not available.get(pool, False):
                continue
            model = {("claude", "deep"): "opus", ("codex", "deep"): "sol",
                     ("claude", "standard"): "sonnet",
                     ("codex", "standard"): "terra"}[(pool, record.complexity)]
            demand = admission_demand(
                record.stage, pool, model, record.complexity, record.effort)
            if demand is None:
                continue
            record.pool = pool
            record.model = model
            record.demand = demand
            if record.stage == "review":
                record.auto_merge_allowed = record.builder_lineage != pool
            return pool
        return None

    def cycle(self, pool: str, *, now: int,
              gates_by_id: dict[str, Gates] | None = None) -> list[str]:
        """Admit eligible continuations first, with strict head-of-line blocking."""

        gates_by_id = gates_by_id or {}
        waiting = [r for r in self.records.values()
                   if r.pool == pool and r.state == "waiting" and not r.hold_pending]
        continuations = sorted(
            (r for r in waiting if r.continuation and r.eligible_at <= now),
            key=lambda r: (r.eligible_at, r.created_at, r.identity),
        )
        cold = sorted((r for r in waiting if not r.continuation), key=lambda r: r.identity)
        admitted: list[str] = []
        for record in continuations:
            if not self.begin_start(record, gates_by_id.get(record.identity, Gates())):
                return admitted
            self.commit_start(record, "started")
            admitted.append(record.identity)
        for record in cold:
            if self.begin_start(record, gates_by_id.get(record.identity, Gates())):
                self.commit_start(record, "started")
                admitted.append(record.identity)
        return admitted

    def finalize_hold(self, record: Record, *, crash_after_proof: bool = False) -> None:
        """Persist one external handoff proof before entering ``held``."""

        if not record.hold_pending:
            return
        if record.handoff_proof is None:
            record.handoffs = 1
            record.handoff_kind = STAGE_NATIVE_HANDOFF[record.stage]
            record.handoff_proof = f"proof:{record.identity}:{record.handoff_kind}"
            record.notifications = 1
        if crash_after_proof:
            return
        record.state = "held"
        record.hold_pending = False
        record.claim = False

    def finalize_completion(self, record: Record, *, next_record: Record | None = None,
                            external_boundary_proven: bool = False) -> None:
        """Transfer ownership or release it only after a durable boundary exists."""

        if record.state != "completed" or record.retired:
            return
        if next_record is not None:
            next_record.claim = True
            self.submit(next_record)
        elif not external_boundary_proven:
            return
        record.claim = False
        record.retired = True

    def _hold(self, record: Record) -> None:
        record.state = "waiting"
        record.hold_pending = True

    def _release(self, record: Record) -> None:
        self.running.pop(record.identity, None)
        record.process_alive = False


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


def test_same_snapshot_historical_stampede_is_bounded_atomically():
    """The observed 4+1+5 launch burst becomes 4+1 admitted and 5 deferred."""

    replay = ReplayCoordinator()
    roots = [
        record_from(HISTORICAL_ROOTS["claude-medium-build"]),
        record_from(HISTORICAL_ROOTS["claude-intake"]),
        record_from(HISTORICAL_ROOTS["claude-deep-build"]),
    ]
    for root in roots:
        replay.submit(root)

    admitted = replay.cycle("claude", now=0)

    assert admitted == ["H-BUILD-1", "H-INTAKE-1"]
    assert replay.permits_used("claude") == 5
    assert roots[2].state == "waiting" and roots[2].attempts == 0


def test_two_writers_cannot_share_one_five_permit_pool():
    replay = ReplayCoordinator()
    first = replay.submit(record_from(HISTORICAL_ROOTS["claude-revise"]))
    second = replay.submit(record_from(HISTORICAL_ROOTS["claude-revise"], suffix="-2"))

    assert replay.begin_start(first)
    replay.commit_start(first, "started")
    assert not replay.begin_start(second)
    assert replay.permits_used("claude") == 3
    assert second.attempts == 0


def test_near_exclusive_writer_keeps_useful_short_stage_concurrency():
    replay = ReplayCoordinator()
    writer = replay.submit(record_from(HISTORICAL_ROOTS["claude-medium-build"]))
    short = replay.submit(record_from(HISTORICAL_ROOTS["claude-review"]))
    other_writer = replay.submit(record_from(HISTORICAL_ROOTS["claude-revise"]))

    for record in (writer, short):
        assert replay.begin_start(record)
        replay.commit_start(record, "started")

    assert replay.permits_used("claude") == 5
    assert not replay.begin_start(other_writer)


def test_continuation_head_of_line_blocks_bypass_without_preempting_live_work():
    replay = ReplayCoordinator()
    live_short = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"], suffix="-live"))
    assert replay.begin_start(live_short)
    replay.commit_start(live_short, "started")

    oldest = replay.submit(Record("C-OLD", "build", "claude", 5, True, 1, 1, lineage="claude"))
    later = replay.submit(Record("C-LATER", "review", "claude", 1, True, 2, 2))
    cold = replay.submit(Record("COLD", "intake", "claude", 1))

    assert replay.cycle("claude", now=10) == []
    assert live_short.state == "running"
    assert later.state == cold.state == "waiting"

    replay.finish(live_short, provider="success", outcome=True)
    assert replay.cycle("claude", now=10) == [oldest.identity]
    assert later.state == cold.state == "waiting"


def test_code_lineage_is_pinned_while_read_only_continuation_can_move():
    replay = ReplayCoordinator()
    pinned = replay.submit(Record(
        "PINNED", "build", "codex", 5, True,
        model="sol", complexity="deep", effort="medium", lineage="codex"))
    available = {"claude": True, "codex": False}

    assert replay.route(pinned, available, safe={"claude", "codex"}) is None
    assert (pinned.pool, pinned.demand, pinned.lineage) == ("codex", 5, "codex")
    pinned.pool = "claude"  # even a malformed caller cannot bypass the lineage gate
    assert not replay.begin_start(pinned)
    pinned.pool = "codex"

    movable = replay.submit(Record(
        "MOVABLE", "review", "codex", 2, True, model="sol", complexity="deep",
        builder_lineage="codex"))
    assert replay.route(movable, available, safe={"claude"}) == "claude"
    assert (movable.pool, movable.model, movable.demand, movable.lineage,
            movable.auto_merge_allowed) == ("claude", "opus", 1, None, True)

    same_tool = replay.submit(Record(
        "SAME-TOOL", "review", "claude", 1, True, model="opus", complexity="deep",
        builder_lineage="claude"))
    assert replay.route(same_tool, {"claude": True}, safe={"claude"}) == "claude"
    assert same_tool.auto_merge_allowed is False
    assert replay.begin_start(same_tool)
    replay.commit_start(same_tool, "started")
    replay.finish(same_tool, provider="success", outcome=True)
    assert (same_tool.state, same_tool.auto_merge_allowed) == ("completed", False)


def test_future_capacity_reset_controls_eligibility_without_hot_looping():
    replay = ReplayCoordinator()
    paused = replay.submit(record_from(HISTORICAL_ROOTS["claude-review"]))
    assert replay.begin_start(paused)
    replay.commit_start(paused, "started")
    replay.finish(paused, provider="recoverable", outcome=False, eligible_at=50)
    cold = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"]))

    assert replay.cycle("claude", now=49) == [cold.identity]
    replay.finish(cold, provider="success", outcome=True)
    assert (paused.state, paused.attempts, paused.claim) == ("waiting", 1, True)
    assert replay.cycle("claude", now=50) == [paused.identity]


@pytest.mark.parametrize("failed_gate", ["headroom", "machine", "stage", "pace"])
def test_every_independent_gate_defers_without_permits_or_attempts(failed_gate):
    replay = ReplayCoordinator()
    record = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"]))
    gates = Gates(**{failed_gate: False})

    assert not replay.begin_start(record, gates)
    assert record.state == "waiting"
    assert record.attempts == replay.permits_used("claude") == 0


def test_permit_deferral_also_consumes_neither_permit_nor_attempt():
    replay = ReplayCoordinator()
    live = replay.submit(record_from(HISTORICAL_ROOTS["claude-medium-build"]))
    deferred = replay.submit(record_from(HISTORICAL_ROOTS["claude-review"], suffix="-2"))
    assert replay.begin_start(live)
    replay.commit_start(live, "started")
    full = replay.submit(record_from(HISTORICAL_ROOTS["claude-intake"], suffix="-full"))
    assert replay.begin_start(full)
    replay.commit_start(full, "started")
    used_before = replay.permits_used("claude")

    assert not replay.begin_start(deferred)
    assert deferred.attempts == 0
    assert replay.permits_used("claude") == used_before == 5


def test_crash_recovery_distinguishes_not_started_from_started_atomically():
    before_crash = ReplayCoordinator()
    reserved = before_crash.submit(record_from(HISTORICAL_ROOTS["codex-review"]))
    assert before_crash.begin_start(reserved)

    after_crash = ReplayCoordinator()
    not_started = after_crash.submit(deepcopy(reserved))
    assert after_crash.permits_used("codex") == 2  # ambiguous running state fails closed
    after_crash.recover_start(not_started, "not_started", process_alive=False)
    assert (not_started.state, not_started.attempts, after_crash.permits_used("codex")) == (
        "waiting", 0, 0)

    before_crash = ReplayCoordinator()
    started = before_crash.submit(record_from(HISTORICAL_ROOTS["codex-review"], suffix="-2"))
    assert before_crash.begin_start(started)
    before_crash.commit_start(started, "started")

    after_crash = ReplayCoordinator()
    recovered = after_crash.submit(deepcopy(started))
    after_crash.recover_start(recovered, "started", process_alive=True)
    after_crash.recover_start(recovered, "started", process_alive=True)
    assert (recovered.state, recovered.attempts, after_crash.permits_used("codex")) == (
        "running", 1, 2)


def test_live_recovered_family_keeps_one_root_reservation_and_dead_family_releases_it():
    before_crash = ReplayCoordinator()
    root = before_crash.submit(record_from(HISTORICAL_ROOTS["codex-review"]))
    assert before_crash.begin_start(root)
    before_crash.commit_start(root, "started")
    for descendant in ("D-1", "D-2", "D-3", "D-4"):
        before_crash.add_descendant(root, descendant)

    after_crash = ReplayCoordinator()
    recovered = after_crash.submit(deepcopy(root))
    after_crash.recover_start(recovered, "started", process_alive=True)
    assert after_crash.permits_used("codex") == 2
    assert len(recovered.descendants) == 4

    after_crash.recover_start(recovered, "started", process_alive=False)
    assert after_crash.permits_used("codex") == 0
    assert (recovered.state, recovered.attempts, recovered.claim) == ("waiting", 1, True)


@pytest.mark.parametrize("provider", ["recoverable", "incomplete", "unknown", "success"])
def test_non_terminal_endings_wait_when_the_outcome_is_missing(provider):
    replay = ReplayCoordinator()
    record = replay.submit(Record(f"END-{provider}", "review", "claude", 1))
    assert replay.begin_start(record)
    replay.commit_start(record, "started")
    replay.finish(record, provider=provider, outcome=False)
    assert (record.state, record.attempts, record.claim) == ("waiting", 1, True)


def test_outcome_precedence_permanent_hold_and_exhaustion_all_terminate_safely():
    replay = ReplayCoordinator()

    success = replay.submit(Record("OUTCOME", "review", "claude", 1))
    assert replay.begin_start(success)
    replay.commit_start(success, "started")
    replay.finish(success, provider="permanent", outcome=True)
    assert success.state == "completed"

    permanent = replay.submit(Record("PERMANENT", "review", "claude", 1))
    assert replay.begin_start(permanent)
    replay.commit_start(permanent, "started")
    replay.finish(permanent, provider="permanent", outcome=False)
    assert (permanent.state, permanent.hold_pending, permanent.claim) == (
        "waiting", True, True)
    replay.finalize_hold(permanent)
    assert (permanent.state, permanent.handoffs, permanent.claim) == ("held", 1, False)

    exhausted = replay.submit(Record("EXHAUSTED", "build", "claude", 3, lineage="claude"))
    for expected_attempt in range(1, ATTEMPT_BUDGET + 1):
        assert replay.begin_start(exhausted)
        replay.commit_start(exhausted, "started")
        replay.finish(exhausted, provider="unknown", outcome=False)
        assert exhausted.attempts == expected_attempt
    assert (exhausted.state, exhausted.hold_pending, exhausted.claim) == (
        "waiting", True, True)
    replay.finalize_hold(exhausted)
    assert (exhausted.state, exhausted.handoffs, exhausted.claim) == ("held", 1, False)
    assert not replay.begin_start(exhausted)
    replay.finalize_hold(exhausted)
    assert exhausted.handoffs == 1


@pytest.mark.parametrize(
    ("stage", "expected"),
    STAGE_NATIVE_HANDOFF.items(),
)
def test_exhaustion_creates_exactly_one_stage_native_handoff(stage, expected):
    replay = ReplayCoordinator()
    record = replay.submit(Record(f"HOLD-{stage}", stage, "claude", 1,
                                  attempts=ATTEMPT_BUDGET - 1,
                                  lineage="claude" if stage in CODE_WRITING else None))
    assert replay.begin_start(record)
    replay.commit_start(record, "started")
    replay.finish(record, provider="unknown", outcome=False)
    replay.finalize_hold(record)

    assert (record.state, record.handoffs, record.handoff_kind, record.claim) == (
        "held", 1, expected, False)


def test_handoff_proof_survives_crash_without_duplicate_handoff_or_notification():
    before_crash = ReplayCoordinator()
    record = before_crash.submit(Record(
        "CRASH-HOLD", "review", "claude", 1, attempts=ATTEMPT_BUDGET - 1))
    assert before_crash.begin_start(record)
    before_crash.commit_start(record, "started")
    before_crash.finish(record, provider="unknown", outcome=False)
    before_crash.finalize_hold(record, crash_after_proof=True)
    assert (record.state, record.hold_pending, record.claim, record.handoffs,
            record.notifications, record.handoff_proof is not None) == (
        "waiting", True, True, 1, 1, True)

    after_crash = ReplayCoordinator()
    recovered = after_crash.submit(deepcopy(record))
    after_crash.finalize_hold(recovered)
    assert (recovered.state, recovered.claim, recovered.handoffs,
            recovered.notifications, recovered.handoff_proof) == (
        "held", False, 1, 1, record.handoff_proof)


def test_completed_stage_transfers_claim_before_retirement_or_releases_at_boundary():
    replay = ReplayCoordinator()
    build = replay.submit(Record(
        "BUILD-STAGE", "build", "claude", 4, lineage="claude"))
    assert replay.begin_start(build)
    replay.commit_start(build, "started")
    replay.finish(build, provider="success", outcome=True)
    assert (build.state, build.claim, build.retired) == ("completed", True, False)

    review = Record("REVIEW-STAGE", "review", "codex", 2, model="sol")
    replay.finalize_completion(build, next_record=review)
    assert (review.state, review.claim) == ("waiting", True)
    assert (build.claim, build.retired) == (False, True)

    assert replay.begin_start(review)
    replay.commit_start(review, "started")
    replay.finish(review, provider="success", outcome=True)
    replay.finalize_completion(review, external_boundary_proven=True)
    assert (review.state, review.claim, review.retired) == ("completed", False, True)


def test_submission_is_idempotent_so_one_logical_stage_cannot_duplicate_work():
    replay = ReplayCoordinator()
    first = replay.submit(Record("SAME-STAGE", "build", "claude", 4, lineage="claude"))
    duplicate = replay.submit(Record("SAME-STAGE", "build", "claude", 4, lineage="claude"))

    assert duplicate is first
    assert len(replay.records) == 1
