"""The deep session coordinator (ADR 0030) — one owner for one logical stage session.

Stage orchestration ``submit_stage``s the facts for one logical stage and later ``cycle``s a
pool to collect the completed stage outcomes and human holds that reconciliation produced.
Those two calls are the whole public surface. Everything hard lives behind them: the
continuation record and its four states, the waiting queue and ADR 0028 ordering, the
attempt budget, the reviewed five-permit admission matrix, the atomic permit reservation on
the running-record ledger, the crash-safe provider start handshake, outcome-first
classification, and reconciliation. SQLite, admission demand, attempt numbers, gates, and
provider observations are private implementation details.

This slice is intentionally dormant: no production pipeline stage submits work here yet, so
the current legacy pipeline behavior cannot change. It makes the later Build tracer small
enough to review while proving the interface and the crash boundaries with an injected
launcher, gate, and observer.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from agentflow.coordinator.admission import (
    ATTEMPT_BUDGET, CODE_WRITING, MODEL_FOR, PERMIT_BUDGET, STAGE_NATIVE_HANDOFF,
    admission_demand, normalize_stage)
from agentflow.coordinator.launcher import (
    NOT_STARTED, STARTED, LocalLauncher, pid_family_alive)
from agentflow.coordinator.providers import ProviderObservation
from agentflow.coordinator.record import COMPLETED, HELD, RUNNING, WAITING, Record
from agentflow.coordinator.store import Store, default_store_path


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


@dataclass(frozen=True)
class StageOutcome:
    """A terminal fact ``cycle`` returns for stage orchestration to consume: a stage that
    completed or a human hold. Nothing about the record's private state crosses the seam."""

    identity: str
    stage: str
    status: str                    # completed | held
    handoff: str | None = None     # the stage-native human handoff kind, for a hold


class Coordinator:
    """One logical-stage session owner backed by a private, versioned SQLite store under the
    agentflow state directory (``AGENTFLOW_STATE``).

    A fresh instance over the same state directory reconstructs the working set, which is how
    the crash boundaries are recovered. ``submit_stage`` is idempotent for a stage identity.
    ``cycle`` reconciles first — returning the completed outcomes and holds that reconciliation
    settled — then admits eligible continuations ahead of cold work with strict head-of-line
    blocking, starting each through the crash-safe launcher. If the store is unreadable the
    constructor raises and the caller starts nothing and clears no claim (fail-closed).

    The launcher, admission gate, provider observer, and family-liveness probe are injected so
    the crash boundaries can be exercised with fakes; production uses the real spawning
    launcher and pid liveness.
    """

    def __init__(self, *, launcher=None, gate=None, observe=None, verify=None,
                 is_alive=pid_family_alive) -> None:
        self._store = Store(default_store_path())
        self._launcher = launcher or LocalLauncher()
        self._gate = gate or (lambda record: True)
        self._observe = observe or (lambda record: ProviderObservation())
        self._verify = verify or (lambda record, obs: False)
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
        with self._lock:
            existing = self.records.setdefault(identity, record)
            if existing is record:
                self._persist(record)
        return identity

    def cycle(self, pool: str, *, now: int = 0) -> list[StageOutcome]:
        """Reconcile, returning the stage outcomes and holds settled this cycle, then admit
        eligible continuations first with strict head-of-line blocking, starting each through
        the launcher. Newly started attempts run beyond this cycle and surface as outcomes in
        a later cycle's reconciliation."""
        with self._lock:
            outcomes = self._reconcile()
            waiting = [r for r in self.records.values()
                       if r.pool == pool and r.state == WAITING and not r.hold_pending]
            continuations = sorted(
                (r for r in waiting if r.continuation and r.eligible_at <= now),
                key=lambda r: (r.eligible_at, r.created_at, r.identity))
            cold = sorted((r for r in waiting if not r.continuation),
                          key=lambda r: r.identity)
            for record in continuations:
                if not self._admit(record):
                    return outcomes  # first blocked continuation stops the pool this cycle
            for record in cold:
                self._admit(record)
            return outcomes

    def permits(self, pool: str) -> int:
        """Read-only projection of the permits in use on ``pool`` (the running-record ledger).
        Like the live board, this is a projection of running records, never a control knob."""
        return self._store.permits_used(pool)

    # --- reconciliation -----------------------------------------------------------------

    def _reconcile(self) -> list[StageOutcome]:
        """Resolve every ambiguous running record from its durable start fact and family
        liveness (ADR 0028/0030), returning the outcomes that terminated this cycle. The
        working set is reloaded first so a child's cross-process ``started`` write and any
        concurrent instance's writes are observed. A committed reservation with no durable
        start whose family is dead returns to ``waiting`` without consuming an attempt; a
        ``started`` family always counts and is classified once it is no longer alive. An
        unresolved reservation fails closed — it keeps its permits until the process ends."""
        self.records = self._store.load()
        outcomes: list[StageOutcome] = []
        for record in list(self.records.values()):
            if record.state != RUNNING:
                continue
            if record.start_fact == NOT_STARTED:
                self._release(record)
                record.state = WAITING
                self._persist(record)
            elif record.start_fact is None:
                # The launcher never durably recorded a start, so no provider family can
                # exist (the child records `started` before replacing itself). If nothing is
                # alive, this is not-started: release and preserve the attempt count.
                if not self._is_alive(record.family):
                    self._release(record)
                    record.state = WAITING
                    self._persist(record)
            elif record.start_fact == STARTED and record.family is not None:
                # A durable `started` always consumes its one attempt, even if the daemon
                # died before the live commit could count it. Then only a proven-dead family
                # is classified; an unknown-liveness family fails closed and keeps its permits.
                was_committed = record.attempt_committed
                self._consume_attempt(record)
                if not self._is_alive(record.family):
                    outcome = self._finalize(record)
                    if outcome is not None:
                        outcomes.append(outcome)
                elif not was_committed:
                    self._persist(record)
        return outcomes

    # --- admission ----------------------------------------------------------------------

    def _admit(self, record: Record) -> bool:
        if not self._begin_start(record):
            return False
        result = self._launcher.start(record, self._store)
        self._commit_start(record, result.fact, result.family)
        return result.fact == STARTED

    def _begin_start(self, record: Record) -> bool:
        if (record.state != WAITING or record.hold_pending
                or record.attempts >= ATTEMPT_BUDGET):
            return False
        if record.pool not in {"claude", "codex"}:
            return False  # no permit ledger to charge an unknown pool (ADR 0029)
        if record.stage in CODE_WRITING and record.pool != record.lineage:
            return False  # a code-writing stage may not silently leave its pinned lineage
        if not self._gate(record):
            return False  # an independent admission gate (headroom, ceiling, cap, pacing)
        # Flip to a reservation and atomically claim the demand on the ledger; the store
        # reads availability and writes the running row under one lock, so concurrent
        # instances cannot push a pool past its five-permit budget (ADR 0029/0030).
        record.state = RUNNING
        record.start_fact = None
        record.family = None
        record.process_alive = False
        record.attempt_committed = False  # a fresh attempt has not been consumed yet
        if not self._store.reserve(record, PERMIT_BUDGET):
            record.state = WAITING  # the pool cannot fit this demand right now
            record.start_fact = None
            return False
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

    # --- outcome-first classification ---------------------------------------------------

    def _finalize(self, record: Record) -> StageOutcome | None:
        """Classify an ended provider family and release its reservation atomically (ADR
        0028 precedence): a verified stage outcome completes it; a permanent provider
        condition holds it; a recoverable, incomplete, or unknown ending waits when budget
        remains and holds when it is exhausted. Returns the terminal outcome, if any."""
        obs = self._observe(record)
        self._release(record)
        if self._verify(record, obs):
            record.state = COMPLETED
            self._persist(record)
            return StageOutcome(record.identity, record.stage, "completed")
        label = obs.classification()
        if label == "permanent":
            self._hold(record)
        elif record.attempts < ATTEMPT_BUDGET:
            record.state = WAITING
            record.continuation = True
            record.eligible_at = obs.reset_at or 0
            self._persist(record)
            return None
        else:
            self._hold(record)
        # The dormant slice owns the human handoff itself; a real stage adapter proves it in
        # the live pipeline. Finalizing it here is idempotent and crash-safe.
        return self._finalize_hold(record)

    def _finalize_hold(self, record: Record) -> StageOutcome | None:
        if not record.hold_pending:
            return None
        if record.handoff_proof is None:
            record.handoffs = 1
            record.handoff_kind = STAGE_NATIVE_HANDOFF[record.stage]
            record.handoff_proof = f"proof:{record.identity}:{record.handoff_kind}"
            record.notifications = 1
            self._persist(record)
        record.state = HELD
        record.hold_pending = False
        record.claim = False
        self._persist(record)
        return StageOutcome(record.identity, record.stage, "held", record.handoff_kind)

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
