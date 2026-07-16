"""The deep session coordinator (ADR 0030) — one owner for one logical stage session.

Stage orchestration ``submit_stage``s the facts for one logical stage and later ``cycle``s a
pool to collect the completed stage outcomes and human holds that reconciliation produced.
Those two calls are the whole public surface. Everything hard lives behind them: the
continuation record and its four states, the waiting queue and ADR 0028 ordering, the
attempt budget, the reviewed five-permit admission matrix, the atomic permit reservation on
the running-record ledger, the crash-safe provider start handshake, outcome-first
classification, and reconciliation. SQLite, admission demand, attempt numbers, gates, and
provider observations are private implementation details.

Build is the first production stage behind this coordinator (issue #103); every other logical
stage remains queued behind the admission gate until its own tracer lands. The interface and
crash boundaries remain exercised with injected launcher, gate, and observer collaborators.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

from agentflow.coordinator.admission import (
    ATTEMPT_BUDGET, CODE_WRITING, MODEL_FOR, PERMIT_BUDGET, STAGE_NATIVE_HANDOFF,
    admission_demand, normalize_stage)
from agentflow.coordinator.launcher import NOT_STARTED, STARTED, LocalLauncher
from agentflow.coordinator.providers import ProviderObserver as _DefaultAdapter
from agentflow.coordinator.record import COMPLETED, HELD, RUNNING, WAITING, Record
from agentflow.coordinator.store import Store, default_store_path

# The observe-until window a recovered running attempt is logged against (ADR 0028's
# supervisor deadline). Stored on the record at admission so a fresh coordinator reports a
# stable deadline after a restart.
CONTINUATION_BUDGET = ATTEMPT_BUDGET - 1  # the two automatic continuations after the first try
SUPERVISOR_WINDOW = 2 * 3600              # observe-until horizon stamped at admission, for the log

# The required-outcome noun each stage proves, for the completion log line (ADR 0028).
_OUTCOME_LABEL = {
    "intake": "route parsed", "build": "pr opened", "review": "verdict recorded",
    "revise": "revision pushed", "mockup": "mockup committed", "respond": "reply posted"}


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
    descendant_of: str | None = None  # a subagent shares this root stage's one reservation
    transfer_from: str | None = None  # the completed prior stage whose GitHub claim this assumes


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

    It depends on three cohesive collaborators, injected only so the crash boundaries can be
    exercised with fakes: a **launcher** that starts a provider family and reports its
    liveness, an admission **gate** (the composed headroom/ceiling/cap/pacing check), and a
    stage **adapter** that observes an ended family and verifies its stage outcome. Production
    uses the real spawning launcher with pid liveness; a bare coordinator keeps permissive/
    never-verified defaults, while the live Build tracer supplies its gate and adapter. None is a
    public operation — the only public surface is ``submit_stage`` and ``cycle``.
    """

    def __init__(self, *, launcher=None, gate=None, adapter=None, log=None) -> None:
        self._store = Store(default_store_path())
        self._launcher = launcher or LocalLauncher()
        self._gate = gate or _admit_everything
        self._adapter = adapter or _DefaultAdapter()
        # A stage adapter that owns branch/worktree recovery may reject admission before it
        # happens; a preparation failure consumes neither a permit nor an attempt (ADR 0028).
        # An adapter with no prepare (the read-only default) is always ready.
        self._prepare = getattr(self._adapter, "prepare", None) or (lambda _record: True)
        self._log = log or (lambda _line: None)
        self._lock = threading.RLock()
        self._records: dict[str, Record] = self._store.load()
        # In-memory bookkeeping (reset on restart, which is correct): identities this process
        # started, so reconciliation only logs "recovered running" for a family it did not just
        # launch — i.e. one found alive after a fresh coordinator reloaded the durable store.
        self._started_here: set[str] = set()
        self._recovered_logged: set[str] = set()

    # --- public interface ---------------------------------------------------------------

    def submit_stage(self, submission: Submission) -> str:
        """Submit one logical stage's facts; returns its stable identity. Idempotent — a
        repeated submission for the same identity never duplicates work."""
        stage = normalize_stage(submission.stage)
        model = MODEL_FOR.get((submission.pool, submission.complexity), "opus")
        demand = admission_demand(
            stage, submission.pool, model, submission.complexity, submission.effort)
        identity = _identity(submission.repo, submission.subject, stage, submission.target)
        # A code-writing stage is pinned to the tool that built its diff (or, first time, its
        # own pool) and cannot silently cross pools; a read-only stage is unpinned and may run
        # on either pool. A review by the same tool that built the diff cannot auto-merge
        # (ADR 0028 lineage rules).
        lineage = (submission.builder_lineage or submission.pool
                   if stage in CODE_WRITING else None)
        auto_merge = not (submission.builder_lineage is not None
                          and submission.pool == submission.builder_lineage)
        record = Record(
            identity=identity, stage=stage, pool=submission.pool,
            repo=submission.repo, subject=str(submission.subject), target=submission.target,
            demand=demand if demand is not None else PERMIT_BUDGET,
            model=model, complexity=submission.complexity, effort=submission.effort,
            claim=submission.claim, builder_lineage=submission.builder_lineage,
            source=submission.source, input_ptr=submission.input_ptr, lineage=lineage,
            auto_merge_allowed=auto_merge, root=submission.descendant_of)
        with self._lock:
            existing = self._records.setdefault(identity, record)
            if existing is record:
                self._register_descendant(record)
                self._transfer_claim(record, submission.transfer_from)
                self._persist(record)
        return identity

    def _register_descendant(self, record: Record) -> None:
        """A descendant/subagent shares its root's single reservation and is never admitted or
        reserved independently (ADR 0030). Recording the lineage on the root lets the root's
        terminal outcome retire it, so nested work can never push a pool past its budget."""
        if record.root is None:
            return
        root = self._records.get(record.root)
        if root is not None:
            root.descendants.add(record.identity)
            self._persist(root)

    def _transfer_claim(self, record: Record, prior_identity: str | None) -> None:
        """Assume the GitHub claim from a completed prior stage (ADR 0030 claim transfer). A
        completed record keeps its claim until this transfer is durable — so a crash between
        stages cannot drop ownership — then releases it and retires as the next stage takes over."""
        if prior_identity is None:
            return
        prior = self._records.get(prior_identity)
        if prior is not None and prior.state == COMPLETED:
            prior.claim = False
            prior.retired = True
            self._persist(prior)
            self._emit(prior, f"attempt {prior.attempts}/{ATTEMPT_BUDGET} completed — "
                              f"{_OUTCOME_LABEL.get(prior.stage, prior.stage)}; "
                              f"claim transferred to {record.stage}")

    def cycle(self, pool: str, *, now: int = 0) -> list[StageOutcome]:
        """Reconcile, returning the stage outcomes and holds settled this cycle, then admit
        eligible continuations first with strict head-of-line blocking, starting each through
        the launcher. Newly started attempts run beyond this cycle and surface as outcomes in
        a later cycle's reconciliation."""
        with self._lock:
            outcomes = self._reconcile()
            waiting = [r for r in self._records.values()
                       if r.pool == pool and r.state == WAITING and not r.hold_pending
                       and r.root is None]  # descendants share the root's reservation, never admit
            continuations = sorted(
                (r for r in waiting if r.continuation and r.eligible_at <= now),
                key=lambda r: (r.eligible_at, r.created_at, r.identity))
            cold = sorted((r for r in waiting if not r.continuation),
                          key=lambda r: r.identity)
            for record in continuations:
                # An admission (permit/gate) refusal or a launch that never started blocks the
                # pool head-of-line (ADR 0029); only a preparation miss is skipped, since it
                # reserved nothing and can retry next cycle without holding capacity hostage.
                if self._admit(record, now) not in ("started", "unprepared"):
                    return outcomes
            for record in cold:
                self._admit(record, now)
            return outcomes

    # --- reconciliation -----------------------------------------------------------------

    def _reconcile(self) -> list[StageOutcome]:
        """Resolve every ambiguous running record from its durable start fact and family
        liveness (ADR 0028/0030), returning the outcomes that terminated this cycle. The
        working set is reloaded first so a child's cross-process ``started`` write and any
        concurrent instance's writes are observed. A committed reservation with no durable
        start whose family is dead returns to ``waiting`` without consuming an attempt; a
        ``started`` family always counts and is classified once it is no longer alive. An
        unresolved reservation fails closed — it keeps its permits until the process ends."""
        self._records = self._store.load()
        outcomes: list[StageOutcome] = []
        for record in list(self._records.values()):
            if record.hold_pending:
                outcome = self._finalize_hold(record)
                if outcome is not None:
                    outcomes.append(outcome)
                continue
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
                if not self._launcher.is_alive(record.family):
                    self._release(record)
                    record.state = WAITING
                    self._persist(record)
            elif record.start_fact == STARTED and record.family is not None:
                # A durable `started` always consumes its one attempt, even if the daemon
                # died before the live commit could count it. Then only a proven-dead family
                # is classified; an unknown-liveness family fails closed and keeps its permits.
                was_committed = record.attempt_committed
                self._consume_attempt(record)
                if not self._launcher.is_alive(record.family):
                    record.process_alive = False
                    outcome = self._finalize(record)
                    if outcome is not None:
                        outcomes.append(outcome)
                else:
                    # A family found alive that this process did not itself launch is a
                    # recovered running attempt — observed, never re-launched, claim retained.
                    if (record.identity not in self._started_here
                            and record.identity not in self._recovered_logged):
                        self._recovered_logged.add(record.identity)
                        self._emit(record, f"recovered running attempt {record.attempts}/"
                                          f"{ATTEMPT_BUDGET} pid {record.family} — observing "
                                          f"until {record.deadline}; claim retained")
                    if not was_committed:
                        self._persist(record)
        return outcomes

    # --- admission ----------------------------------------------------------------------

    def _admit(self, record: Record, now: int) -> str:
        """Try to start one attempt. Returns ``unprepared`` (the stage adapter refused before
        admission — no permit, no attempt), ``blocked`` (admission/permits refused), ``started``
        (a provider family exists and an attempt was consumed), or ``not_started`` (admitted but
        no provider came into existence — no attempt consumed)."""
        if not self._prepare(record):
            return "unprepared"
        if not self._begin_start(record, now):
            return "blocked"
        result = self._launcher.start(record, self._store)
        self._started_here.add(record.identity)
        self._commit_start(record, result.fact, result.family)
        return STARTED if result.fact == STARTED else "not_started"

    def _begin_start(self, record: Record, now: int) -> bool:
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
        # instances cannot push a pool past its five-permit budget (ADR 0029/0030). The fresh
        # launch token binds this reservation to exactly one bootstrap child: only a child
        # holding it may record `started`, so a timed-out launch disowned back to waiting can
        # never be adopted by an uncancelled child (ADR 0030 handshake boundary).
        record.state = RUNNING
        record.start_fact = None
        record.launch_token = uuid4().hex
        record.family = None
        record.process_alive = False
        record.attempt_committed = False  # a fresh attempt has not been consumed yet
        record.started_at = now
        record.deadline = now + SUPERVISOR_WINDOW  # observe-until, for the recovered-running log
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
        was_continuation = record.continuation
        self._consume_attempt(record)
        self._persist(record)
        if was_continuation:
            done = record.attempts - 1  # continuations completed before this one
            self._emit(record, f"continuation {done}/{CONTINUATION_BUDGET} "
                              f"(attempt {record.attempts}/{ATTEMPT_BUDGET}) → {record.pool}")
        else:
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} → {record.pool}")

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
        obs = self._adapter.observe(record)
        self._release(record)
        if self._adapter.verify(record, obs):
            record.state = COMPLETED
            self._persist(record)
            self._retire_descendants(record)
            # A completed stage keeps its claim until the next stage transfers it (ADR 0028);
            # the transfer line is emitted when that next stage is submitted.
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} completed — "
                              f"{_OUTCOME_LABEL.get(record.stage, record.stage)}; claim retained")
            return StageOutcome(record.identity, record.stage, "completed")
        label = obs.classification()
        cause = obs.cause.value
        if label == "permanent":
            self._hold(record)
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} held ({cause}) — "
                              f"permanent; held for human; claim released")
        elif record.attempts < ATTEMPT_BUDGET:
            record.state = WAITING
            record.continuation = True
            record.eligible_at = obs.reset_at or 0
            self._persist(record)
            done = record.attempts  # continuations begun after this interruption
            when = f"at {record.eligible_at}" if record.eligible_at else "next cycle"
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} interrupted "
                              f"({cause}) — continuation {done}/{CONTINUATION_BUDGET} eligible "
                              f"{when}; claim retained")
            return None
        else:
            self._hold(record)
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} interrupted "
                              f"({cause}) — continuation budget exhausted; held for human; "
                              f"claim released")
        # The stage adapter proves a live external handoff; the coordinator then finalizes it
        # idempotently and crash-safely.
        return self._finalize_hold(record)

    def _finalize_hold(self, record: Record) -> StageOutcome | None:
        if not record.hold_pending:
            return None
        if record.handoff_proof is None:
            finalize = getattr(self._adapter, "finalize_hold", None)
            proof = finalize(record) if finalize is not None else (
                f"proof:{record.identity}:{STAGE_NATIVE_HANDOFF[record.stage]}")
            if proof is None:
                return None  # the external handoff is not durable yet; retry next cycle
            record.handoffs = 1
            record.handoff_kind = STAGE_NATIVE_HANDOFF[record.stage]
            record.handoff_proof = proof
            record.notifications = 1
            self._persist(record)
        record.state = HELD
        record.hold_pending = False
        record.claim = False
        self._persist(record)
        self._retire_descendants(record)
        return StageOutcome(record.identity, record.stage, "held", record.handoff_kind)

    # --- internal helpers ---------------------------------------------------------------

    def _retire_descendants(self, record: Record) -> None:
        """A root's terminal outcome retires its subagents: they shared its one reservation, so
        they are done when it is and never linger as waiting work or a second outcome."""
        for identity in record.descendants:
            child = self._records.get(identity)
            if child is not None and not child.retired:
                child.state = COMPLETED
                child.retired = True
                child.claim = False
                self._persist(child)

    def _hold(self, record: Record) -> None:
        record.state = WAITING
        record.hold_pending = True
        self._persist(record)

    def _release(self, record: Record) -> None:
        record.process_alive = False

    def _persist(self, record: Record) -> None:
        self._store.upsert(record)

    def _emit(self, record: Record, tail: str) -> None:
        """One stable ADR 0028 operational line: ``{repo}: {subject}: {stage}: {tail}``. The
        prefix identifies the logical stage; the tail carries the attempt, cause, and claim
        disposition. Provider prose and secrets never reach here — only typed causes do."""
        self._log(f"{record.repo}: {record.subject}: {record.stage}: {tail}")


def _identity(repo: str, subject: str, stage: str, target: str | None) -> str:
    return "|".join((repo, str(subject), stage, target or "-"))


def _admit_everything(record: Record) -> bool:
    """The bare coordinator's default gate. Live tracers supply their stage gate; tests and
    direct construction use only the private permit ledger (ADR 0030)."""
    return True
