"""The deep session coordinator (ADR 0030) — one owner for one logical stage session.

Stage orchestration submits the facts for one logical stage and later consumes its
completed outcome or human hold. Everything hard lives behind that seam: the continuation
record and its four states, the waiting queue and ADR 0028 ordering, the attempt budget,
the reviewed five-permit admission matrix, atomic permit accounting on the running-record
ledger, the crash-safe provider start handshake, outcome-first classification, and
reconciliation. SQLite, admission demand, attempt numbers, and provider observations are
private implementation details — the public control surface is only ``submit`` /
``submit_stage`` and ``cycle``.

This slice is intentionally dormant: no production pipeline stage submits work here yet. It
makes the later Build tracer small enough to review while proving the interface and the
crash boundaries with executable fake providers. The internal transitions
(``_begin_start`` … ``_finish``) are the mechanics ``cycle`` composes; the historical replay
harness drives them white-box to prove the policy, but production callers never touch them.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

from agentflow.coordinator.admission import (
    ATTEMPT_BUDGET, CODE_WRITING, MODEL_FOR, PERMIT_BUDGET, STAGE_NATIVE_HANDOFF,
    admission_demand, normalize_stage)
from agentflow.coordinator.launcher import (
    NOT_STARTED, STARTED, LocalLauncher, pid_family_alive)
from agentflow.coordinator.record import COMPLETED, HELD, RUNNING, WAITING, Record
from agentflow.coordinator.store import Store


@dataclass(frozen=True)
class Gates:
    """The independent preconditions that must all pass for one provider start (ADR 0029):
    current headroom, the machine ceiling, the per-stage cap, and operator pacing. Permits
    compose *with* these, they do not replace them."""

    headroom: bool = True
    machine: bool = True
    stage: bool = True
    pace: bool = True


@dataclass(frozen=True)
class Submission:
    """The minimal facts stage orchestration hands across the seam for one logical stage
    (ADR 0030). Live-board aliases, raw provider messages, admission demand, attempt numbers,
    and continuation decisions are deliberately absent — they are derived behind the seam."""

    repo: str
    subject: str
    stage: str
    target: str | None = None       # immutable target (head SHA / comment id) — part of identity
    source: str | None = None       # durable worktree/source pointer
    claim: bool = True
    complexity: str = "deep"
    effort: str | None = None
    pool: str = "claude"            # allowed pool or pinned lineage
    input_ptr: str | None = None
    builder_lineage: str | None = None


class Coordinator:
    """One logical-stage session owner backed by a private, versioned SQLite store.

    Construct it against a store path; a fresh instance over the same path reconstructs the
    working set, which is how the crash boundaries are recovered. ``submit`` is idempotent
    for a stage identity; ``cycle`` reconciles, then admits eligible continuations ahead of
    cold work with strict head-of-line blocking, starting each through the crash-safe
    launcher. If the store is unreadable it raises ``StoreUnavailable`` and the caller starts
    nothing and clears no claim (fail-closed).
    """

    def __init__(self, store_path: Path | str, *, launcher: LocalLauncher | None = None,
                 is_alive=pid_family_alive) -> None:
        self._store = Store(store_path)
        self._launcher = launcher or LocalLauncher()
        self._is_alive = is_alive
        self._lock = threading.RLock()
        self.records: dict[str, Record] = self._store.load()

    # --- public interface ---------------------------------------------------------------

    def submit_stage(self, submission: Submission) -> str:
        """Submit one logical stage's facts; returns its stable identity. Idempotent — a
        repeated submission for the same identity never duplicates work."""
        stage = normalize_stage(submission.stage)
        model = MODEL_FOR.get((submission.pool, submission.complexity), "opus")
        demand = admission_demand(
            stage, submission.pool, model, submission.complexity, submission.effort)
        identity = _identity(submission.repo, submission.subject, stage, submission.target)
        record = Record(
            identity=identity, stage=stage, pool=submission.pool,
            demand=demand if demand is not None else PERMIT_BUDGET,
            model=model, complexity=submission.complexity, effort=submission.effort,
            claim=submission.claim, builder_lineage=submission.builder_lineage,
            lineage=submission.pool if stage in CODE_WRITING else None)
        self.submit(record)
        return identity

    def cycle(self, pool: str, *, now: int = 0,
              gates_by_id: dict[str, Gates] | None = None) -> list[str]:
        """Reconcile, then admit eligible continuations first with strict head-of-line
        blocking, starting each through the launcher. Returns the identities started this
        cycle; terminal outcomes are read from the reconciled records."""
        with self._lock:
            gates_by_id = gates_by_id or {}
            self.reconcile()
            waiting = [r for r in self.records.values()
                       if r.pool == pool and r.state == WAITING and not r.hold_pending]
            continuations = sorted(
                (r for r in waiting if r.continuation and r.eligible_at <= now),
                key=lambda r: (r.eligible_at, r.created_at, r.identity))
            cold = sorted((r for r in waiting if not r.continuation),
                          key=lambda r: r.identity)
            admitted: list[str] = []
            for record in continuations:
                if not self._start(record, gates_by_id.get(record.identity, Gates())):
                    return admitted  # first blocked continuation stops the pool this cycle
                admitted.append(record.identity)
            for record in cold:
                if self._start(record, gates_by_id.get(record.identity, Gates())):
                    admitted.append(record.identity)
            return admitted

    def reconcile(self) -> None:
        """Resolve every ambiguous running record from its durable start fact and family
        liveness (ADR 0028/0030). A committed reservation with ``not_started`` returns to
        ``waiting`` without consuming an attempt; a ``started`` family always counts and is
        classified incomplete once it is no longer alive. An unresolved reservation fails
        closed — it keeps its permits until the process is proven ended."""
        with self._lock:
            for record in list(self.records.values()):
                if record.state != RUNNING:
                    continue
                if record.start_fact == NOT_STARTED:
                    self._release(record)
                    record.state = WAITING
                    self._persist(record)
                elif record.start_fact is None:
                    # The launcher never durably recorded a start, so no provider family can
                    # exist (it records `started` before replacing itself). If nothing is
                    # alive, this is `not_started`: release and preserve the attempt count. A
                    # still-alive launcher family fails closed and keeps its permits.
                    if not self._is_alive(record.family):
                        self._release(record)
                        record.state = WAITING
                        self._persist(record)
                elif record.start_fact == STARTED and record.family is not None:
                    # A durable `started` always consumes its one attempt, even if the daemon
                    # died before the live commit could count it. Then only a proven-dead
                    # family may be downgraded; an unknown-liveness family fails closed and
                    # keeps its permits until the process is proven ended.
                    was_committed = record.attempt_committed
                    self._consume_attempt(record)
                    alive = self._is_alive(record.family)
                    if not alive:
                        record.process_alive = False
                        self._finish(record, provider="incomplete", outcome=False)
                    elif not was_committed:
                        self._persist(record)

    def permits(self, pool: str) -> int:
        """Read-only projection of the permits in use on ``pool`` (the running-record ledger).
        Like the live board, this is a projection of running records, never a control knob."""
        return self._store.permits_used(pool)

    # --- white-box mechanics (private; the replay harness drives these) -----------------

    def submit(self, record: Record) -> Record:
        with self._lock:
            existing = self.records.setdefault(record.identity, record)
            if existing is record:
                self._persist(record)
            return existing

    def _permits_used(self, pool: str) -> int:
        return self._store.permits_used(pool)

    def _start(self, record: Record, gates: Gates) -> bool:
        if not self._begin_start(record, gates):
            return False
        result = self._launcher.start(record, self._persist)
        self._commit_start(record, result.fact, result.family)
        return result.fact == STARTED

    def _begin_start(self, record: Record, gates: Gates = Gates()) -> bool:
        with self._lock:
            if (record.state != WAITING or record.hold_pending
                    or record.attempts >= ATTEMPT_BUDGET):
                return False
            if record.pool not in {"claude", "codex"}:
                return False  # no permit ledger to charge an unknown pool (ADR 0029)
            if record.stage in CODE_WRITING and record.pool != record.lineage:
                return False
            if not all((gates.headroom, gates.machine, gates.stage, gates.pace)):
                return False
            if self._permits_used(record.pool) + record.demand > PERMIT_BUDGET:
                return False
            record.state = RUNNING
            record.start_fact = None
            record.family = None
            record.process_alive = False
            record.attempt_committed = False  # a fresh attempt has not been consumed yet
            self._persist(record)
            return True

    def _commit_start(self, record: Record, fact: str, family: str | None = None) -> None:
        assert record.state == RUNNING
        assert fact in {STARTED, NOT_STARTED}
        record.start_fact = fact
        if fact == NOT_STARTED:
            self._release(record)
            record.state = WAITING
            self._persist(record)
            return
        record.family = family or record.family
        self._consume_attempt(record)
        self._persist(record)

    def _consume_attempt(self, record: Record) -> None:
        """Consume exactly one attempt for a durable ``started``. Idempotent: a recovery that
        re-reads the same ``started`` fact never frees or duplicates the attempt (ADR 0030)."""
        if not record.attempt_committed:
            record.attempts += 1
            record.attempt_committed = True
        record.process_alive = True

    def _recover_start(self, record: Record, fact: str, *, process_alive: bool) -> None:
        if record.start_fact is None:
            self._commit_start(record, fact)
        if fact == NOT_STARTED:
            return
        record.process_alive = process_alive
        self._persist(record)
        if not process_alive:
            self._finish(record, provider="incomplete", outcome=False)

    def _add_descendant(self, record: Record, family_id: str) -> None:
        record.descendants.add(family_id)
        self._persist(record)

    def _finish(self, record: Record, *, provider: str, outcome: bool,
                eligible_at: int | None = None) -> None:
        assert record.state == RUNNING
        self._release(record)
        if outcome:
            record.state = COMPLETED
            self._persist(record)
            return
        if provider in {"bail", "permanent"}:
            self._hold(record)
            return
        if record.attempts < ATTEMPT_BUDGET:
            record.state = WAITING
            record.continuation = True
            record.eligible_at = eligible_at or 0
            self._persist(record)
            return
        self._hold(record)

    def _route(self, record: Record, available: dict[str, bool], safe: set[str]) -> str | None:
        if record.stage in CODE_WRITING:
            candidates = [record.lineage] if record.lineage else []
        else:
            candidates = [record.pool, *sorted(set(available) - {record.pool})]
        for pool in candidates:
            if pool not in safe or not available.get(pool, False):
                continue
            model = MODEL_FOR[(pool, record.complexity)]
            demand = admission_demand(
                record.stage, pool, model, record.complexity, record.effort)
            if demand is None:
                continue
            record.pool = pool
            record.model = model
            record.demand = demand
            if record.stage == "review":
                record.auto_merge_allowed = record.builder_lineage != pool
            self._persist(record)
            return pool
        return None

    def _finalize_hold(self, record: Record, *, crash_after_proof: bool = False) -> None:
        if not record.hold_pending:
            return
        if record.handoff_proof is None:
            record.handoffs = 1
            record.handoff_kind = STAGE_NATIVE_HANDOFF[record.stage]
            record.handoff_proof = f"proof:{record.identity}:{record.handoff_kind}"
            record.notifications = 1
            self._persist(record)
        if crash_after_proof:
            return
        record.state = HELD
        record.hold_pending = False
        record.claim = False
        self._persist(record)

    def _finalize_completion(self, record: Record, *, next_record: Record | None = None,
                             external_boundary_proven: bool = False) -> None:
        if record.state != COMPLETED or record.retired:
            return
        if next_record is not None:
            next_record.claim = True
            self.submit(next_record)
        elif not external_boundary_proven:
            return
        record.claim = False
        record.retired = True
        self._persist(record)

    # --- internal helpers ---------------------------------------------------------------

    def _hold(self, record: Record) -> None:
        record.state = WAITING
        record.hold_pending = True
        self._persist(record)

    def _release(self, record: Record) -> None:
        record.process_alive = False

    def _persist(self, record: Record) -> None:
        self._store.upsert(record)


def _identity(repo: str, subject: str, stage: str, target: str | None) -> str:
    return "|".join((repo, str(subject), stage, target or "-"))
