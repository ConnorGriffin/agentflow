"""The deep session coordinator (ADR 0030) — one owner for one logical stage session.

Stage orchestration ``submit_stage``s the facts for one logical stage and later ``cycle``s a
pool to collect the completed stage outcomes and human holds that reconciliation produced.
``park_completed`` is the one deliberate addition to that surface: it turns a completed stage
the product policy leaves with no successor into the same idempotent human hold a budget
exhaustion produces. Everything hard lives behind them: the
continuation record and its four states, the waiting queue and ADR 0028 ordering, the
attempt budget, the reviewed five-permit admission matrix, the atomic permit reservation on
the running-record ledger, the crash-safe provider start handshake, outcome-first
classification, and reconciliation. SQLite, admission demand, attempt numbers, gates, and
provider observations are private implementation details.

All six logical stages are production stages behind this coordinator (issues #103–#108),
including Mockup's durable visual round. Review
is read-only, so an eligible continuation may move to the other pool when its home pool cannot
fit it. The interface and crash boundaries remain exercised with injected launcher, gate, and
observer collaborators.
"""

from __future__ import annotations

import os
import threading
import time
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
# A daemon restart/reboot that kills a running family costs no attempt — the same attempt resumes in
# place. That resume is bounded per stage identity so a family that keeps dying with no provider end
# fact still parks eventually instead of spinning forever at zero budget cost.
RESTART_RESUME_CAP = 5

# The required-outcome noun each stage proves, for the completion log line (ADR 0028).
_OUTCOME_LABEL = {
    "intake": "route parsed", "build": "pr opened", "review": "verdict recorded",
    "revise": "revision pushed", "mockup": "mockup committed", "respond": "reply posted",
    "converse": "reply appended", "research": "findings recorded"}


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
    builder_complexity: str | None = None  # the original builder complexity, carried to a Revise
    round: int = 0                  # completed auto-revise rounds behind this stage — part of the
                                    # identity, so a re-review at an unchanged head SHA is still a
                                    # genuinely new stage with a fresh budget
    descendant_of: str | None = None  # a subagent shares this root stage's one reservation
    transfer_from: str | None = None  # the completed prior stage whose GitHub claim this assumes
    supersede: bool = False           # the ``transfer_from`` predecessor is a still-in-flight Review
                                      # stranded at a moved head (#208), not a completed stage
    interactive: bool = False         # operator-present (Ask) turn: admission priority over
                                      # background pipeline work (ADR 0034)


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
    public operation — the public surface is ``submit_stage``, ``cycle``, and ``park_completed``.
    """

    def __init__(self, *, launcher=None, gate=None, adapter=None, log=None,
                 daemon_generation: str | None = None) -> None:
        self._store = Store(default_store_path())
        self._launcher = launcher or LocalLauncher()
        self._gate = gate or _admit_everything
        self._adapter = adapter or _DefaultAdapter()
        # This process's daemon-lifecycle identity, stamped on every attempt it admits. A restart
        # is a new process with a new pid, so an attempt found dead under a *different* generation
        # — and leaving no supervisor end fact — was taken down with the daemon, not by the
        # provider. The daemon writes this same pid under STATE_DIR/daemon.lock (ADR 0030).
        self._daemon_generation = daemon_generation or str(os.getpid())
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
        identity = _identity(submission.repo, submission.subject, stage, submission.target,
                             submission.round)
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
            builder_complexity=submission.builder_complexity, round=submission.round,
            source=submission.source, input_ptr=submission.input_ptr, lineage=lineage,
            auto_merge_allowed=auto_merge, root=submission.descendant_of,
            interactive=submission.interactive, created_at=int(time.time()))
        with self._lock:
            successor, prior, transferred, root = self._store.submit(
                record, submission.transfer_from, supersede=submission.supersede)
            self._records[identity] = successor
            if prior is not None:
                self._records[prior.identity] = prior
            if root is not None:
                self._records[root.identity] = root
            if transferred and prior is not None:
                self._emit(prior, f"attempt {prior.attempts}/{ATTEMPT_BUDGET} completed — "
                           f"{_OUTCOME_LABEL.get(prior.stage, prior.stage)}; "
                           f"claim transferred to {successor.stage}")
        return identity

    def park_completed(self, identity: str) -> "StageOutcome | None":
        """Terminally park a completed stage the product policy leaves with no next stage to take
        over its claim (ADR 0028) — the third public operation beside ``submit_stage`` and
        ``cycle``, added deliberately: the completed-outcome consumer sometimes learns there is no
        successor (a blocking Review whose auto-revise rounds are spent, a record missing the
        lineage facts a successor needs), and without this the retained claim would keep the PR
        owned forever. It stays within ADR 0030's seam: the hold still flows through the stage
        adapter's own ``finalize_hold`` handoff, exactly as a budget exhaustion does. Returns the
        ``held`` outcome, or ``None`` when the record is missing, already retired, or not a
        completed stage awaiting a transfer. Idempotent and crash-safe: a repeat re-observes the
        durable handoff and neither re-notifies nor double-releases the claim."""
        with self._lock:
            record = self._records.get(identity)
            if record is None or record.retired or record.state != COMPLETED:
                return None
            if not record.hold_pending:
                record.hold_pending = True
                record.hold_reason = "completed stage has no successor"
                if not self._persist(record):
                    return None
                self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} completed but no "
                                  f"next stage remains — parking for human; claim retained "
                                  f"pending durable handoff")
            return self._finalize_hold(record)

    def retire_stale_review(self, identity: str) -> bool:
        """Silently retire a Review whose PR is gone — merged or closed — before any further attempt
        is charged against a head there is no longer anything to review (#208). The record releases
        its claim with no park comment and no notification. Idempotent: a repeat finds it already
        retired and does nothing."""
        with self._lock:
            record = self._store.record_of(identity)
            if (record is None or record.retired or record.stage != "review"
                    or not record.claim):
                return False
            self._release(record)
            record.state = COMPLETED
            record.claim = False
            record.retired = True
            if not self._persist(record, retire_descendants=True):
                return False
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} — PR merged or closed; "
                              "nothing left to review; retired silently, claim released")
            return True

    def park_stale_review(self, identity: str) -> "StageOutcome | None":
        """Park a Review whose PR head moved off its immutable target once the auto-revise rounds are
        spent: the stranded record can open no bounded successor, so the PR is handed to a human
        through the same Review-native exhaustion handoff a budget exhaustion uses (#208). Only ever
        called for an open PR. Idempotent and crash-safe: a repeat re-observes the durable park and
        neither re-notifies nor double-releases the claim."""
        with self._lock:
            record = self._store.record_of(identity)
            if (record is None or record.retired or record.stage != "review"
                    or not record.claim):
                return None
            self._records[identity] = record
            if not record.hold_pending:
                self._release(record)
                record.state = COMPLETED
                record.hold_pending = True
                record.hold_reason = "PR head moved off the reviewed SHA and revise rounds are spent"
                if not self._persist(record):
                    return None
                self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} — PR head moved and "
                                  "revise rounds spent; parking for human; claim retained pending "
                                  "durable handoff")
            return self._finalize_hold(record)

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
            # An operator's interactive turn (an Ask) outranks background pipeline work at
            # admission (ADR 0034): it sorts to the head of each queue. For an interactive turn the
            # gate is also exempt from headroom/pacing/ceiling (ADR 0034/0025 as amended by #162),
            # so only true zero capacity — no permit obtainable on the reservation ledger — can
            # defer it. Background starts still face the full budget/gate/pool checks below.
            continuations = sorted(
                (r for r in waiting if r.continuation and r.eligible_at <= now),
                key=lambda r: (not r.interactive, r.eligible_at, r.created_at, r.identity))
            cold = sorted((r for r in waiting if not r.continuation),
                          key=lambda r: (not r.interactive, r.identity))
            for record in continuations:
                # An admission (permit/gate) refusal or a launch that never started blocks the
                # pool head-of-line (ADR 0029); only a preparation miss is skipped, since it
                # reserved nothing and can retry next cycle without holding capacity hostage.
                if self._admit(record, now) not in ("started", "unprepared"):
                    return outcomes
            for record in cold:
                self._admit(record, now)
            # A read-only Review whose home pool cannot launch it may move here (ADR 0028/0020) —
            # a fresh review as well as a continuation, since a pool that lost its launch capacity
            # would otherwise freeze it forever. It is best-effort and never blocks the pool
            # head-of-line: a move that cannot reserve reverts, leaving the record on its home pool
            # for that pool's own cycle.
            for record in self._migratable_reviews(pool, now):
                self._admit_migration(record, pool, now)
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
            if record.state == COMPLETED and not record.retired:
                self._settle_completed(record)
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
                    if self._resume_after_restart(record):
                        continue  # a daemon restart killed it — resumed in place, uncharged
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

    def _resume_after_restart(self, record: Record) -> bool:
        """Resume, without charging the budget, an attempt whose family a daemon restart killed.

        A durable ``started`` family proven dead that (a) left no supervisor end fact and (b) was
        admitted under a *different* daemon generation than the one now reconciling was taken down
        with the daemon — a restart or reboot — not by the provider. That is not an attempt
        failure, so the same attempt re-runs in place: its permits are released and re-reserved on
        the next admission, its attempt count stays flat (the up-front charge is refunded so the
        resumed start re-charges to the same number), and it is *not* marked a consumed
        continuation. A family that left an end fact (a clean-but-incomplete exit, or any typed
        provider failure) keeps consuming attempts exactly as today, so keying is on end-fact
        absence, never on the provider cause. The resume is bounded per identity so a session that
        keeps dying with no end fact still parks. Returns whether it took the resume path.
        """
        generation = record.daemon_generation
        if generation is None or generation == self._daemon_generation:
            return False  # same daemon still up (or a pre-generation record): a genuine interruption
        if record.restart_resumes >= RESTART_RESUME_CAP:
            return False  # bounded — hand a persistent no-end-fact crash-loop to the normal park path
        obs = self._adapter.observe(record)
        if getattr(obs, "has_end_fact", False):
            return False  # the provider recorded an end — a real failure, charged like any other
        attempt_no = record.attempts
        self._release(record)
        record.attempts -= 1               # refund the up-front charge; the resume re-charges it
        record.attempt_committed = False
        record.state = WAITING
        record.continuation = False        # a restart resume is not one of the two continuations
        record.restart_resumes += 1
        if not self._persist(record):
            return True  # another instance advanced this identity; either way our pass stops here
        self._emit(record, f"attempt {attempt_no}/{ATTEMPT_BUDGET} interrupted by daemon restart "
                          f"— resuming in place (resume {record.restart_resumes}/"
                          f"{RESTART_RESUME_CAP}); attempt not charged; claim retained")
        return True

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

    def _migratable_reviews(self, pool: str, now: int) -> list[Record]:
        """Eligible read-only Reviews whose home pool cannot currently launch them and that
        review safety lets move onto ``pool`` (ADR 0028/0020). Ordered like any continuation
        queue. A review freezes if its home pool loses launch capacity *after* the record was
        created — the permit ledger fills, or the launcher's own admission gate now refuses it
        (e.g. codex weekly-budget pacing) — so both are re-placement triggers, and a *fresh*
        review (``attempts=0``, never a continuation) may move too, not only continuations:
        without launching it could never become one, so requiring a continuation would freeze
        it forever. A code-writing stage is pinned to its builder lineage and never appears
        here; a review whose home pool can still launch it is left for that pool's cycle."""
        candidates = [
            r for r in self._records.values()
            if r.state == WAITING and not r.hold_pending and r.root is None
            and r.eligible_at <= now
            and r.pool != pool and self._review_may_move(r)
            and self._pool_cannot_fit(r)]
        return sorted(candidates, key=lambda r: (r.eligible_at, r.created_at, r.identity))

    def _pool_cannot_fit(self, record: Record) -> bool:
        """Whether ``record``'s home pool cannot launch it this cycle for a durable reason: its
        permit ledger cannot seat the demand, or the launcher's own admission gate refuses it.
        The gate is the very check ``_begin_start`` applies (codex weekly-budget pacing included),
        reused here rather than re-derived, so a review pinned to a pool that lost its launch
        capacity is re-placed instead of freezing at zero attempts (ADR 0020)."""
        if self._store.permits_used(record.pool) + record.demand > PERMIT_BUDGET:
            return True
        return not self._gate(record)

    @staticmethod
    def _review_may_move(record: Record) -> bool:
        """Review safety for a cross-pool move: a read-only review is unpinned (no code-writing
        lineage), so it may run on either pool. A same-tool review that moves onto the builder's
        pool may still finish, but the coordinator strips its auto-merge eligibility (ADR 0028)."""
        return record.stage == "review" and record.lineage is None

    def _admit_migration(self, record: Record, dest_pool: str, now: int) -> None:
        """Move a Review continuation to ``dest_pool``, recomputing its admission demand and
        model for the destination and re-deriving auto-merge (a review by the builder's own tool
        can finish but never auto-merges — ADR 0028). A move that does not start reverts every
        moved field, so a record that could not reserve is never stranded off its home pool."""
        home = (record.pool, record.model, record.demand, record.auto_merge_allowed)
        record.pool = dest_pool
        record.model = MODEL_FOR.get((dest_pool, record.complexity), "opus")
        demand = admission_demand(
            record.stage, dest_pool, record.model, record.complexity, record.effort)
        record.demand = demand if demand is not None else PERMIT_BUDGET
        record.auto_merge_allowed = not (record.builder_lineage is not None
                                         and dest_pool == record.builder_lineage)
        if self._admit(record, now) != STARTED:
            (record.pool, record.model, record.demand, record.auto_merge_allowed) = home
            record.state = WAITING
            self._persist(record)

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
        # Flip to a reservation and atomically claim demand plus any global admission limits
        # on the ledger; concurrent instances cannot push a pool, machine, or stage lane past
        # its reviewed budget (ADR 0029/0030). The fresh
        # launch token binds this reservation to exactly one bootstrap child: only a child
        # holding it may record `started`, so a timed-out launch disowned back to waiting can
        # never be adopted by an uncancelled child (ADR 0030 handshake boundary).
        expected_launch_token = record.launch_token
        expected_revision = record.revision
        record.state = RUNNING
        record.start_fact = None
        record.launch_token = uuid4().hex
        record.family = None
        record.process_alive = False
        record.attempt_committed = False  # a fresh attempt has not been consumed yet
        record.daemon_generation = self._daemon_generation  # who admitted this attempt (restart marker)
        record.started_at = now
        record.deadline = now + SUPERVISOR_WINDOW  # observe-until, for the recovered-running log
        reservation_limits = getattr(self._gate, "reservation_limits", None)
        limits = reservation_limits(record) if reservation_limits is not None else None
        if not self._store.reserve(
                record, PERMIT_BUDGET, limits,
                expected_launch_token=expected_launch_token,
                expected_revision=expected_revision):
            record.state = WAITING  # the pool cannot fit this demand right now
            record.start_fact = None
            return False
        return True

    def _commit_start(self, record: Record, fact: str, family: str | None = None) -> None:
        # The bootstrap child may have advanced the durable row while the parent waited for its
        # handshake. Continue from that exact revision; never overwrite the child's start fact
        # with the older reservation snapshot.
        attempted_token = record.launch_token
        durable = self._store.record_of(record.identity)
        if durable is None or durable.state != RUNNING:
            return
        assert fact in {STARTED, NOT_STARTED}
        if fact == STARTED:
            if (durable.start_fact != STARTED
                    or durable.launch_token != attempted_token):
                return
        elif not (durable.start_fact == NOT_STARTED
                  or durable.launch_token == attempted_token):
            # A delayed result from an older launch may not release a newer running attempt.
            return
        record = durable
        self._records[record.identity] = record
        record.start_fact = fact
        if fact == NOT_STARTED:
            self._release(record)
            record.state = WAITING
            self._persist(record)
            return
        record.family = family or record.family
        was_continuation = record.continuation
        self._consume_attempt(record)
        if not self._persist(record):
            return
        started = getattr(self._gate, "started", None)
        if started is not None:
            started(record)
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

    def _settle_completed(self, record: Record) -> bool:
        """Project a completed stage at its durable boundary behind the ``cycle`` seam."""
        prepare = getattr(self._adapter, "prepare_completed", None)
        if prepare is not None and not prepare(record):
            return False

        def settle(current: Record) -> bool:
            if current.state != COMPLETED or current.retired:
                return False
            finalize = getattr(self._adapter, "finalize_completed", None)
            proof = finalize(current) if finalize is not None else None
            if proof is None:
                return False
            current.handoff_proof = proof
            current.claim = False
            current.retired = True
            return True

        settled = self._store.transition(record, settle)
        if settled is None:
            return False
        self._records[settled.identity] = settled
        self._emit(settled, f"attempt {settled.attempts}/{ATTEMPT_BUDGET} settled — "
                          f"{_OUTCOME_LABEL.get(settled.stage, settled.stage)}; claim released")
        return True

    def _finalize(self, record: Record) -> StageOutcome | None:
        """Classify an ended provider family and release its reservation atomically (ADR
        0028 precedence): a verified stage outcome completes it; a permanent provider
        condition holds it; a recoverable, incomplete, or unknown ending waits when budget
        remains and holds when it is exhausted. Returns the terminal outcome, if any."""
        obs = self._adapter.observe(record)
        self._release(record)
        capture = getattr(self._adapter, "capture", None)
        outcome = capture(record, obs) if capture is not None else None
        if outcome is not None:
            record.outcome = outcome
            if not self._persist(record):  # parsed outcome precedes any external projection
                return None
        if outcome is not None or self._adapter.verify(record, obs):
            record.state = COMPLETED
            if not self._persist(record, retire_descendants=True):
                return None
            # A completed stage keeps its claim until the next stage transfers it (ADR 0028);
            # the transfer line is emitted when that next stage is submitted.
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} completed — "
                              f"{_OUTCOME_LABEL.get(record.stage, record.stage)}; claim retained")
            return StageOutcome(record.identity, record.stage, "completed")
        label = obs.classification()
        cause = obs.cause.value
        if label == "permanent":
            record.hold_reason = f"permanent provider condition ({cause})"
            if not self._hold(record):
                return None
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} held ({cause}) — "
                              f"permanent; handoff pending; claim retained")
        elif record.attempts < ATTEMPT_BUDGET:
            record.state = WAITING
            record.continuation = True
            record.eligible_at = obs.reset_at or 0
            if not self._persist(record):
                return None
            done = record.attempts  # continuations begun after this interruption
            when = f"at {record.eligible_at}" if record.eligible_at else "next cycle"
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} interrupted "
                              f"({cause}) — continuation {done}/{CONTINUATION_BUDGET} eligible "
                              f"{when}; claim retained")
            return None
        else:
            record.hold_reason = "continuation budget exhausted"
            if not self._hold(record):
                return None
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} interrupted "
                              f"({cause}) — continuation budget exhausted; handoff pending; "
                              f"claim retained")
        # The stage adapter proves a live external handoff; the coordinator then finalizes it
        # idempotently and crash-safely.
        return self._finalize_hold(record)

    def _finalize_hold(self, record: Record) -> StageOutcome | None:
        if not record.hold_pending:
            return None

        def hold(current: Record) -> bool:
            if not current.hold_pending or current.state not in {WAITING, COMPLETED}:
                return False
            if current.handoff_proof is None:
                finalize = getattr(self._adapter, "finalize_hold", None)
                proof = finalize(current) if finalize is not None else (
                    f"proof:{current.identity}:{STAGE_NATIVE_HANDOFF[current.stage]}")
                if proof is None:
                    return False
                current.handoffs = 1
                current.handoff_kind = STAGE_NATIVE_HANDOFF[current.stage]
                current.handoff_proof = proof
                current.notifications = 1
            current.state = HELD
            current.hold_pending = False
            current.claim = False
            return True

        held = self._store.transition(record, hold, retire_descendants=True)
        if held is None:
            return None
        self._records[held.identity] = held
        self._emit(held, f"attempt {held.attempts}/{ATTEMPT_BUDGET} held for human — "
                         "durable handoff proved; claim released")
        return StageOutcome(held.identity, held.stage, "held", held.handoff_kind)

    # --- internal helpers ---------------------------------------------------------------

    def _hold(self, record: Record) -> bool:
        record.state = WAITING
        record.hold_pending = True
        return self._persist(record)

    def _release(self, record: Record) -> None:
        record.process_alive = False

    def _persist(self, record: Record, *, retire_descendants: bool = False) -> bool:
        if self._store.upsert(record, retire_descendants=retire_descendants):
            self._records[record.identity] = record
            return True
        # Another coordinator advanced this identity. Refresh the working set and let the
        # current pass stop at the lost compare-and-set instead of overwriting the winner.
        durable = self._store.record_of(record.identity)
        if durable is not None:
            self._records[record.identity] = durable
        return False

    def _emit(self, record: Record, tail: str) -> None:
        """One stable ADR 0028 operational line: ``{repo}: {subject}: {stage}: {tail}``. The
        prefix identifies the logical stage; the tail carries the attempt, cause, and claim
        disposition. Provider prose and secrets never reach here — only typed causes do."""
        self._log(f"{record.repo}: {record.subject}: {record.stage}: {tail}")


def _identity(repo: str, subject: str, stage: str, target: str | None, round: int = 0) -> str:
    # The auto-revise round joins the identity once one exists, so an evidence-only revision —
    # whose re-review binds to the *same* head SHA — still opens a genuinely new stage rather
    # than colliding with the retired prior review's record.
    parts = [repo, str(subject), stage, target or "-"]
    if round:
        parts.append(f"r{round}")
    return "|".join(parts)


def _admit_everything(record: Record) -> bool:
    """The bare coordinator's default gate. Live tracers supply their stage gate; tests and
    direct construction use only the private permit ledger (ADR 0030)."""
    return True
