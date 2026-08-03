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
including Mockup's durable visual round. Review may ship bounded fixes and its selected tool is an
independence decision, so it stays pinned like every other code-writing stage. The interface and crash boundaries remain exercised with
injected launcher, gate, and observer collaborators.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from uuid import uuid4

from agentflow.coordinator.admission import (
    ATTEMPT_BUDGET, CODE_WRITING, ISSUE_BOUND, LINEAGE_PINNED, MODEL_FOR, PERMIT_BUDGET, PR_BOUND,
    STAGE_NATIVE_HANDOFF, admission_demand, normalize_stage)
from agentflow.coordinator.launcher import NOT_STARTED, STARTED, LocalLauncher
from agentflow.coordinator.providers import EndingReason, ProviderCause
from agentflow.coordinator.providers import ProviderObserver as _DefaultAdapter
from agentflow.coordinator.record import COMPLETED, HELD, RUNNING, WAITING, Record
from agentflow.coordinator.recovery import PROGRESS, REPAIR
from agentflow.coordinator.stage_router import StageCalls
from agentflow.coordinator.store import Store, default_store_path
from agentflow.coordinator.telemetry import AttemptTelemetry, AttemptUsage, record_attempt
from agentflow.coordinator.verification import VERIFIED, miss_summary
from agentflow.review_policy import ReviewState

# The observe-until window a recovered running attempt is logged against (ADR 0028's
# supervisor deadline). Stored on the record at admission so a fresh coordinator reports a
# stable deadline after a restart.
CONTINUATION_BUDGET = ATTEMPT_BUDGET - 1  # the two automatic continuations after the first try
SUPERVISOR_WINDOW = 2 * 3600              # observe-until horizon stamped at admission, for the log
# A daemon restart/reboot that kills a running family costs no attempt — the same attempt resumes in
# place. That resume is bounded per stage identity so a family that keeps dying with no provider end
# fact still parks eventually instead of spinning forever at zero budget cost.
RESTART_RESUME_CAP = 5
# How many targeted repairs a clean exit with a missing required outcome earns before the stage is
# parked for a human instead of replaying an identical session (issue #225). One repair — naming the
# exact missing proof — then no more blind replays.
REPAIR_BUDGET = 1

# The durable ``hold_reason`` prefix a permanent provider condition stamps. Stage handoffs read it
# to tell an infrastructure/auth failure apart from a hold the model actually reasoned its way into
# (issue #328), so it is a shared constant rather than a string repeated at both ends.
PERMANENT_HOLD_REASON = "permanent provider condition"


def permanent_hold_reason(reason: EndingReason) -> str:
    """The durable ``hold_reason`` for a permanent park, naming *which* condition fired.

    The category alone told every parked issue the same story, so a rejected request drew
    re-authenticate advice for a healthy sign-in (issue #342). The reason is stamped into the
    record before the handoff runs, so a crash-resumed handoff reads the same reason and
    composes byte-identical copy. The prefix is unchanged, so the permanent-vs-generic
    predicate (issue #328) keeps working as the string grows this suffix."""
    return f"{PERMANENT_HOLD_REASON} ({reason.value})"


def parse_permanent_hold_reason(hold_reason: str) -> EndingReason:
    """The permanent reason a durable ``hold_reason`` names, or ``UNSPECIFIED``. Fail-safe:
    a reason written before this suffix existed, or one this build doesn't know, reads as
    unspecified rather than as a wrong remediation."""
    inner = (hold_reason or "").partition("(")[2].rpartition(")")[0].strip()
    try:
        return EndingReason(inner)
    except ValueError:
        return EndingReason.UNSPECIFIED


# The durable ``hold_reason`` clause a stage stamps when the attempt that spent its last try was
# cut off at the per-stage turn cap rather than reasoning its way to the end of the budget (#411).
# Stage handoffs read it so a maintainer can tell "cut off at its ceiling" from "ran out of tries":
# those two want opposite responses, and until now both read as a spent budget.
TURN_CAP_HOLD_CLAUSE = " — the last attempt was cut off at its turn cap"


def ended_at_turn_cap(hold_reason: str | None) -> bool:
    """Whether a durable hold reason records the turn cap as what stopped its last attempt."""
    return TURN_CAP_HOLD_CLAUSE in (hold_reason or "")


def _cut_off_at_turn_cap(obs) -> bool:
    """Whether an observation says the per-stage turn cap is what stopped this attempt."""
    return getattr(obs, "ending_reason", EndingReason.UNSPECIFIED) is EndingReason.TURN_CAP

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
    conflict_round: int = 0         # which conflict Revise this is in the PR's lifetime (ADR 0038):
                                    # >0 joins the identity so each conflict resolution is a fresh
                                    # stage, budgeted separately from the finding-driven `round`
    resume: int = 0                 # which deliberate maintainer resume of an exhausted `held` Build
                                    # this is (#245): >0 joins the identity so the resume opens a
                                    # fresh bounded execution instead of colliding with the terminal
                                    # held record still living at the base identity
    descendant_of: str | None = None  # a subagent shares this root stage's one reservation
    transfer_from: str | None = None  # the completed prior stage whose GitHub claim this assumes
    supersede: bool = False           # the ``transfer_from`` predecessor is a still-in-flight Review
                                      # stranded at a moved head (#208), not a completed stage
    interactive: bool = False         # operator-present (Ask) turn: admission priority over
                                      # background pipeline work (ADR 0034)
    floodgates: bool = False          # per-record floodgates override (ADR 0025 amendment) —
                                      # see `Record.floodgates`
    continuation: bool = False        # admit ahead of cold work — a conflict Revise is a
                                      # continuation of nearly-merged work, not new build (ADR 0038)
    review: ReviewState | None = None


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
                 daemon_generation: str | None = None,
                 disabled_cold_stages: frozenset[str] = frozenset()) -> None:
        self._store = Store(default_store_path())
        self._launcher = launcher or LocalLauncher()
        self._gate = gate or _admit_everything
        # Every adapter hook is optional; StageCalls owns the default for each one, so the
        # coordinator never carries a second copy of them (ADR 0030).
        self._adapter = StageCalls(adapter or _DefaultAdapter())
        # This process's daemon-lifecycle identity, stamped on every attempt it admits. A restart
        # is a new process with a new pid, so an attempt found dead under a *different* generation
        # — and leaving no supervisor end fact — was taken down with the daemon, not by the
        # provider. The daemon writes this same pid under STATE_DIR/daemon.lock (ADR 0030).
        self._daemon_generation = daemon_generation or str(os.getpid())
        # Product policy may stop unattended admission for a stage without abandoning work that
        # already started. The coordinator owns the distinction: only truly cold, never-started
        # records are discarded; continuations and restart-resumes remain eligible.
        self._disabled_cold_stages = disabled_cold_stages
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
        review = submission.review or ReviewState(
            change_author_tool=submission.builder_lineage)
        model = MODEL_FOR.get((submission.pool, submission.complexity), "opus")
        demand = admission_demand(
            stage, submission.pool, model, submission.complexity, submission.effort)
        identity = _identity(submission.repo, submission.subject, stage, submission.target,
                             submission.round, submission.conflict_round, submission.resume,
                             review.assignment.axis.value, review.passes,
                             review.sequence, review.uncertainty_handoffs)
        # A code-writing stage is pinned to its writing tool and cannot silently cross pools.
        # Build-family stages use the builder lineage; Review uses its independent reviewer pool.
        # A review by the same tool that built the diff cannot auto-merge
        # (ADR 0028 lineage rules).
        lineage = (submission.pool if stage == "review"
                   else (submission.builder_lineage or submission.pool
                         if stage in LINEAGE_PINNED else None))
        current_author = review.change_author_tool or submission.builder_lineage
        auto_merge = not (current_author is not None and submission.pool == current_author)
        record = Record(
            identity=identity, stage=stage, pool=submission.pool,
            repo=submission.repo, subject=str(submission.subject), target=submission.target,
            demand=demand if demand is not None else PERMIT_BUDGET,
            model=model, complexity=submission.complexity, effort=submission.effort,
            claim=submission.claim, builder_lineage=submission.builder_lineage,
            builder_complexity=submission.builder_complexity, round=submission.round,
            conflict_round=submission.conflict_round, resume=submission.resume,
            source=submission.source, input_ptr=submission.input_ptr, lineage=lineage,
            auto_merge_allowed=auto_merge, root=submission.descendant_of,
            interactive=submission.interactive, continuation=submission.continuation,
            floodgates=submission.floodgates,
            **review.record_fields(),
            created_at=int(time.time()))
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

    def stage_record(self, identity: str) -> "Record | None":
        """Public read of one durable stage record by its identity — fresh from the store — so the
        dispatch layer can check whether a submission actually produced work eligible to run before
        it claims the issue and reports success (#245). An ordinary resubmission of a terminal `held`
        Build reuses that held record unchanged; the caller reads it here and neither claims nor
        reports a launch. ``None`` when the identity is absent."""
        with self._lock:
            return self._store.record_of(identity)

    def withdraw_stage(self, identity: str) -> bool:
        """Roll back a freshly-submitted stage the dispatcher never managed to guard on GitHub, so no
        orphaned ``waiting`` record is left for a later cycle to launch without the ownership claim
        (#245). Withdraws only a never-started record — one that has consumed no attempt and holds no
        live family — so a genuine in-flight Build is untouched; a subsequent cycle can never admit it.

        The withdrawal *frees the identity* rather than leaving a terminal tombstone (#251): a
        never-run rollback must not permanently occupy the issue's build slot, so the record is
        deleted and the next cold ``build <N>`` — by hand or by the daemon — opens a genuinely
        runnable build exactly as if the failed attempt had never happened. The delete is a durable
        compare-and-set on the never-started row, so a concurrent cycle that just admitted the record
        (or a completed transfer) is never freed or revived. Idempotent and a no-op for any started
        or absent record: a repeat finds the slot already free."""
        with self._lock:
            record = self._store.record_of(identity)
            if (record is None or record.retired or record.state != WAITING
                    or record.attempts != 0 or record.start_fact not in {None, NOT_STARTED}
                    or record.process_alive):
                return False
            if not self._store.discard(record):
                return False
            self._records.pop(identity, None)
            return True

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

    def retire_stale_revise(self, identity: str) -> bool:
        """Silently retire a Revise whose PR is gone — merged, closed, or opened from a branch that
        no longer carries any PR — before another attempt or continuation is charged against a
        change nothing can land (#432): the Revise-native counterpart of ``retire_stale_review``.
        The record releases its claim with no park comment and no notification. A pending park
        handoff is withdrawn too: a Revise park resolves its PR number by branch, so a gone PR
        would leave that handoff pending and silently retrying forever. Idempotent: a repeat finds
        it already retired and does nothing."""
        with self._lock:
            record = self._store.record_of(identity)
            if (record is None or record.retired or record.stage != "revise"
                    or not record.claim):
                return False
            self._release(record)
            record.state = COMPLETED
            record.claim = False
            record.hold_pending = False
            record.retired = True
            if not self._persist(record, retire_descendants=True):
                return False
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} — PR merged, closed or "
                              "gone; nothing left to revise; retired silently, claim released")
            return True

    def retire_stale_intake(self, identity: str) -> bool:
        """Silently retire an Intake or attack round whose issue is closed — before any session is
        charged triaging or arguing a subject nothing can act on (#438): the triage-side
        counterpart of ``retire_stale_review``/``retire_stale_revise``. The record releases its
        claim with no comment and no notification; the caller has already stripped the GitHub
        triaging label, so the operator's label view agrees. A pending hold handoff is withdrawn
        too — a held triage of a closed issue asks a human for a decision nobody can act on.
        Idempotent: a repeat finds it already retired and does nothing."""
        with self._lock:
            record = self._store.record_of(identity)
            if (record is None or record.retired or record.stage not in ("intake", "attack")
                    or not record.claim):
                return False
            self._release(record)
            record.state = COMPLETED
            record.claim = False
            record.hold_pending = False
            record.retired = True
            if not self._persist(record, retire_descendants=True):
                return False
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} — issue closed; "
                              "nothing left to triage; retired silently, claim released")
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
            begin_cycle = getattr(self._gate, "begin_cycle", None)
            if begin_cycle is not None:
                begin_cycle(pool)
            outcomes = self._reconcile()
            self._discard_disabled_cold_stages()
            waiting = [r for r in self._records.values()
                       if r.pool == pool and r.state == WAITING and not r.hold_pending
                       and r.root is None]  # descendants share the root's reservation, never admit
            # Admission ranks each queue in three tiers (ADR 0039). An operator's interactive turn
            # (an Ask) outranks all background pipeline work (ADR 0034): it sorts to the head of
            # each queue, and its gate stays exempt from headroom/pacing/ceiling (ADR 0034/0025 as
            # amended by #162), so only true zero capacity — no permit obtainable on the reservation
            # ledger — can defer it. Below that, PR-bound stages (review/revise/respond) drain ahead
            # of issue-bound work: an open PR is the #1 thing to get over the finish line, and
            # starting new issues while finished work decays only creates more conflicts (ADR 0039).
            # Within a tier the existing tie-breakers hold. Background starts still face the full
            # budget/gate/pool checks below.
            continuations = sorted(
                (r for r in waiting if r.continuation and r.eligible_at <= now),
                key=lambda r: (not r.interactive, r.stage not in PR_BOUND,
                               r.eligible_at, r.created_at, r.identity))
            cold = sorted((r for r in waiting if not r.continuation),
                          key=lambda r: (not r.interactive, r.stage not in PR_BOUND, r.identity))
            for record in continuations:
                # An admission (permit/gate) refusal or a launch that never started blocks the
                # pool head-of-line (ADR 0029); only a preparation miss is skipped, since it
                # reserved nothing and can retry next cycle without holding capacity hostage.
                if self._admit(record, now) not in ("started", "unprepared", "deferred"):
                    return outcomes
            for record in cold:
                self._admit(record, now)
            # A stage whose home pool cannot launch it may move here (ADR 0028/0020) — a read-only
            # Review (fresh or continuation) whose review safety allows either pool, and a
            # *never-started* Build (attempts=0, no branch/worktree/PR yet, so its lineage pin is
            # vacuous), since a pool that lost its launch capacity would otherwise freeze it forever
            # (#273). It is best-effort and never blocks the pool head-of-line: a move that cannot
            # reserve reverts, leaving the record on its home pool for that pool's own cycle.
            for record in self._migratable(pool, now):
                self._admit_migration(record, pool, now)
            return outcomes

    def _discard_disabled_cold_stages(self) -> None:
        """Discard disabled stages that have never owned a provider family.

        Run after reconciliation so a reservation crash first becomes ``waiting``. Durable
        restart evidence keeps a previously-started attempt eligible even when its refunded
        attempt count is zero and capacity delays its restart.
        """
        for record in list(self._records.values()):
            if (record.stage not in self._disabled_cold_stages or record.state != WAITING
                    or record.continuation or record.attempts != 0 or record.restart_resumes != 0
                    or record.start_fact not in {None, NOT_STARTED} or record.process_alive):
                continue
            if self._store.discard(record):
                self._records.pop(record.identity, None)

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
        admission — no permit, no attempt), ``deferred`` (an issue-bound stage yielded the pool to
        a waiting PR-bound stage — no permit, non-blocking, #293), ``blocked`` (admission/permits
        refused), ``started`` (a provider family exists and an attempt was consumed), or
        ``not_started`` (admitted but no provider came into existence — no attempt consumed)."""
        # A stage adapter that owns branch/worktree recovery may reject admission before it
        # happens; a preparation failure consumes neither a permit nor an attempt (ADR 0028).
        if not self._adapter.prepare(record):
            # Do not let an unpreparable PR-bound record keep the pool's issue work behind the
            # PR-priority barrier. It owns no permit and gets another preparation attempt next
            # cycle; until then, other useful work may use the idle pool.
            if record.stage in PR_BOUND:
                record.eligible_at = max(record.eligible_at, now + 1)
                self._persist(record)
            return "unprepared"
        if not self._begin_start(record, now):
            # An issue-bound stage held back so a waiting PR-bound stage can take the pool is a
            # deliberate yield, not a capacity refusal: it must not block the pool head-of-line, or
            # the very review it is yielding to (processed later in the cycle) is never reached (#293).
            if record.stage in ISSUE_BOUND and self._pr_bound_waiting(record.pool, now):
                return "deferred"
            return "blocked"
        result = self._launcher.start(record, self._store)
        self._started_here.add(record.identity)
        self._commit_start(record, result.fact, result.family)
        return STARTED if result.fact == STARTED else "not_started"

    def _migratable(self, pool: str, now: int) -> list[Record]:
        """Eligible stages whose home pool cannot currently launch them and that safety lets move
        onto ``pool`` (ADR 0028/0020, #273). Ordered like any continuation queue. A stage freezes
        if its home pool loses launch capacity *after* the record was created — the permit ledger
        fills, or the launcher's own admission gate now refuses it (e.g. codex weekly-budget
        pacing) — so both are re-placement triggers, and a *fresh* record (``attempts=0``, never a
        continuation) may move too, not only continuations: without launching it could never become
        one, so requiring a continuation would freeze it forever. See ``_may_migrate`` for which
        stages are safe to move; a stage whose home pool can still launch it is left for that
        pool's cycle."""
        candidates = [
            r for r in self._records.values()
            if r.state == WAITING and not r.hold_pending and r.root is None
            and r.eligible_at <= now
            and r.pool != pool and self._may_migrate(r)
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
    def _may_migrate(record: Record) -> bool:
        """Whether a stage is safe to move off its home pool. Review never moves: its selected tool
        is part of the cross-tool independence policy, not only worktree placement. A *never-started*
        Build (``attempts=0``) has no branch/worktree/PR, so its
        lineage pin is vacuous and the move is safe (#273): once it launches, the pin is
        re-established on the destination pool. A Build that already made a real attempt, and
        ``revise``/``respond`` continuations bound to an existing PR on a specific lineage, stay
        pinned; only a genuinely fresh Build moves."""
        return record.stage == "build" and record.attempts == 0

    def _admit_migration(self, record: Record, dest_pool: str, now: int) -> None:
        """Move a migratable stage to ``dest_pool``, recomputing its admission demand and model
        for the destination and re-deriving auto-merge (a review by the builder's own tool can
        finish but never auto-merges — ADR 0028). A code-writing Build also carries a pool-keyed
        lineage and worktree source, so its move re-pins the lineage to ``dest_pool`` and rewrites
        the tool segment of the source path — without both, ``_source_facts`` rejects the moved
        record and it never admits (#273). A move that does not start reverts every moved field, so
        a record that could not reserve is never stranded off its home pool."""
        home = (record.pool, record.model, record.demand, record.auto_merge_allowed,
                record.lineage, record.source)
        record.pool = dest_pool
        record.model = MODEL_FOR.get((dest_pool, record.complexity), "opus")
        demand = admission_demand(
            record.stage, dest_pool, record.model, record.complexity, record.effort)
        record.demand = demand if demand is not None else PERMIT_BUDGET
        current_author = record.change_author_tool or record.builder_lineage
        record.auto_merge_allowed = not (current_author is not None and dest_pool == current_author)
        if record.stage in LINEAGE_PINNED:
            record.lineage = dest_pool
            record.source = _repool_source(record.source, dest_pool)
        if self._admit(record, now) != STARTED:
            (record.pool, record.model, record.demand, record.auto_merge_allowed,
             record.lineage, record.source) = home
            record.state = WAITING
            self._persist(record)

    def _pr_bound_waiting(self, pool: str, now: int) -> bool:
        """Whether a PR-bound stage (review/revise/respond) is waiting and eligible to start on
        ``pool`` right now. Issue-bound new work (build/mockup/intake) defers behind it at the
        reservation ledger so one high-effort build cannot reserve all five permits while a
        review that needs a single permit waits (#293, ADR 0039). Scoped to the same pool only —
        permits are per-pool. Coupling pools here would invite a cross-pool deadlock. A gate-blocked
        autonomous review deliberately stays waiting for its required independent tool without
        consuming permits; reviewed-profile same-tool fallback is selected before submission."""
        return any(
            r.stage in PR_BOUND and r.pool == pool and r.state == WAITING
            and not r.hold_pending and r.root is None
            and r.attempts < ATTEMPT_BUDGET and r.eligible_at <= now
            for r in self._records.values())

    def _begin_start(self, record: Record, now: int) -> bool:
        if (record.state != WAITING or record.hold_pending
                or record.attempts >= ATTEMPT_BUDGET):
            return False
        if record.pool not in {"claude", "codex"}:
            return False  # no permit ledger to charge an unknown pool (ADR 0029)
        if record.stage in LINEAGE_PINNED and record.pool != record.lineage:
            return False  # a code-writing stage may not silently leave its pinned lineage
        if record.stage in ISSUE_BOUND and self._pr_bound_waiting(record.pool, now):
            return False  # new issue work defers while a PR-bound stage waits to start (#293)
        if not self._gate(record):
            # An independent admission gate refusal (headroom, ceiling, cap, pacing) used to be
            # silent: a record pinned to a pool the weekly ratchet blocks sat `waiting` with its
            # claim held for days with zero log lines while intake logged its own deferral every
            # cycle (#436). Name the pool, the reason, and how long the record has waited — one
            # line per record per cycle, the cadence intake's `no pool has headroom` line gets.
            self._emit_deferral(record, now)
            return False
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

    def _emit_deferral(self, record: Record, now: int) -> None:
        """Log why the admission gate refused this waiting record (#436), through the gate's
        optional ``deferral_reason`` hook — the same optional-hook seam as ``begin_cycle`` and
        ``started``. A gate without the hook, or a refusal with nothing durable to report (the
        per-cycle pace budget), stays silent."""
        reason_of = getattr(self._gate, "deferral_reason", None)
        reason = reason_of(record) if reason_of is not None else None
        if not reason:
            return
        waited = ""
        if now > record.created_at > 0:
            waited = f" for {(now - record.created_at) / 3600:.1f}h"
        self._emit(record, f"waiting{waited} — no headroom on {record.pool} ({reason}); "
                           "deferring this cycle; claim retained")

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
        if not self._adapter.prepare_completed(record):
            return False

        def settle(current: Record) -> bool:
            if current.state != COMPLETED or current.retired:
                return False
            proof = self._adapter.finalize_completed(current)
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
        condition holds it; a dispatched environment failure still keeps its attempt, so the
        durable handoff and per-attempt telemetry describe the same session (#454). A
        recoverable interruption waits within budget. An incomplete or
        unknown ending waits only when a fresh attempt would have new recovery state to act on —
        retained partial work continues, a clean exit missing its outcome earns one targeted
        repair — and otherwise holds rather than replaying an identical session (issue #225).
        Returns the terminal outcome, if any."""
        obs = self._adapter.observe(record)
        self._release(record)
        outcome = self._adapter.capture(record, obs)
        verification = VERIFIED if outcome is not None else self._adapter.verify(record, obs)
        verified = bool(verification)
        # An unverified attempt keeps which conjunct stopped it (when the verifier is typed), so
        # the recovery envelope, hold reason, telemetry, and park comment all name the exact miss
        # instead of leaving each park to be re-diagnosed from session transcripts.
        record.verify_miss = "" if verified else miss_summary(verification)
        # Every ended family — completed, superseded, or held — records its spend exactly once,
        # keyed by this attempt's launch token (ADR 0040 per-attempt telemetry).
        self._record_telemetry(record, obs, outcome=outcome, verified=verified)
        if outcome is not None:
            record.outcome = outcome
            if not self._persist(record):  # parsed outcome precedes any external projection
                return None
        if verified:
            record.state = COMPLETED
            if not self._persist(record, retire_descendants=True):
                return None
            # A completed stage keeps its claim until the next stage transfers it (ADR 0028);
            # the transfer line is emitted when that next stage is submitted.
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} completed — "
                              f"{_OUTCOME_LABEL.get(record.stage, record.stage)}; claim retained")
            return StageOutcome(record.identity, record.stage, "completed")
        collision = self._adapter.integration_collision(record)
        if collision is not None:
            return self._settle_collision(record, collision)
        label = obs.classification()
        # Two ceilings now end in the clock class — the supervisor's wall-clock deadline and the
        # per-stage turn cap (#411) — and only one of them is answered by giving the session more
        # room, so the log names which one stopped it rather than saying "timeout" for both.
        cause = "turn cap" if _cut_off_at_turn_cap(obs) else obs.cause.value
        if label == "permanent":
            reason = getattr(obs, "ending_reason", EndingReason.UNSPECIFIED)
            record.hold_reason = permanent_hold_reason(reason)
            environment = reason is EndingReason.ENVIRONMENT
            if not self._hold(record):
                return None
            if environment:
                self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} held (environment) — "
                                  "the session never got a working shell and never reached the "
                                  "work; handoff pending; claim retained")
            else:
                self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} held ({cause}) — "
                                  f"permanent; handoff pending; claim retained")
            # The stage adapter proves a live external handoff; the coordinator finalizes it
            # idempotently and crash-safely.
            return self._finalize_hold(record)
        # A non-permanent ending only earns a fresh session when that session has new recovery
        # state to act on (issue #225). A genuine capacity/server/timeout interruption always does
        # — the limit lifts or the transport recovers — so it continues automatically within the
        # budget. For every other ending the stage adapter classifies the durable world it owns:
        # retained partial work or a changed input keeps continuing behind a recovery envelope; a
        # clean exit that only left its required outcome missing earns one targeted repair naming
        # that exact proof; nothing new stops the replay instead of burning a session on an
        # identical prompt.
        recovery = self._adapter.recover(record, obs)
        if obs.cause is ProviderCause.CAPACITY:
            # A provider-declared five-hour capacity interruption is an automatic reset wait, not a
            # spent attempt or a durable human hold (#305). It refunds the attempt and requeues
            # eligible at the reset, so a pure quota pause resumes on its own without draining the
            # bounded continuation budget. Non-capacity recoverable ends keep the budgeted path.
            return self._capacity_wait(record, obs, recovery.envelope)
        if label == "recoverable" or recovery.kind == PROGRESS:
            return self._continue(record, obs, recovery.envelope, cause)
        if recovery.kind == REPAIR and record.repairs < REPAIR_BUDGET:
            record.repairs += 1
            return self._continue(record, obs, recovery.envelope, cause, repair=True)
        record.hold_reason = ("no new recovery state to act on"
                              + (f" — last unverified check: {record.verify_miss}"
                                 if record.verify_miss else ""))
        if not self._hold(record):
            return None
        self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} ended ({cause}) with no "
                          "new recovery state — not replaying an identical session; handoff "
                          "pending; claim retained")
        return self._finalize_hold(record)

    def _continue(self, record: Record, obs, envelope: str, cause: str,
                  *, repair: bool = False) -> "StageOutcome | None":
        """Requeue one continuation when the attempt budget allows, stamping the bounded recovery
        envelope so the fresh session resumes from durable facts rather than replaying an identical
        prompt (issue #225). At the budget the stage holds for a human, exactly as an exhausted
        continuation always has."""
        if record.attempts >= ATTEMPT_BUDGET:
            record.hold_reason = ("continuation budget exhausted"
                                  + (TURN_CAP_HOLD_CLAUSE if _cut_off_at_turn_cap(obs) else "")
                                  + (f" — last unverified check: {record.verify_miss}"
                                     if record.verify_miss else ""))
            if not self._hold(record):
                return None
            self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} interrupted "
                              f"({cause}) — continuation budget exhausted; handoff pending; "
                              f"claim retained")
            return self._finalize_hold(record)
        record.state = WAITING
        record.continuation = True
        record.eligible_at = obs.reset_at or 0
        record.recovery_envelope = envelope or None
        if not self._persist(record):
            return None
        done = record.attempts  # continuations begun after this interruption
        when = f"at {record.eligible_at}" if record.eligible_at else "next cycle"
        kind = "targeted repair" if repair else f"continuation {done}/{CONTINUATION_BUDGET}"
        self._emit(record, f"attempt {record.attempts}/{ATTEMPT_BUDGET} interrupted "
                          f"({cause}) — {kind} eligible {when}; claim retained")
        return None

    def _capacity_wait(self, record: Record, obs, envelope: str) -> "StageOutcome | None":
        """Requeue a provider-declared five-hour capacity interruption as a free automatic reset
        wait (#305). It refunds the up-front attempt charge and sets ``eligible_at`` to the reset,
        so a pure quota pause never drains the bounded continuation budget or hardens into a
        durable human hold — it simply resumes once the provider window rolls over. The bounded
        recovery envelope is still stamped so the fresh session resumes from durable facts."""
        attempt_no = record.attempts
        if record.attempt_committed:
            record.attempts -= 1
            record.attempt_committed = False
        record.state = WAITING
        record.continuation = True
        record.eligible_at = obs.reset_at or 0
        record.recovery_envelope = envelope or None
        if not self._persist(record):
            return None
        when = f"at {record.eligible_at}" if record.eligible_at else "next cycle"
        self._emit(record, f"attempt {attempt_no}/{ATTEMPT_BUDGET} interrupted (capacity) — "
                          f"automatic reset wait eligible {when}; attempt refunded; claim retained")
        return None

    def _settle_collision(self, record: Record, main_sha: str) -> StageOutcome | None:
        """A reported integration collision is a durable outcome, not a retryable failure (issue
        #209). The first collision against an unchanged main records that main head and defers the
        continuation without charging the attempt — the retry would be provably identical until
        main moves (the stage adapter's ``prepare`` holds it back while main is unchanged). A
        second collision (main moved, the granted retry collided again) or a collision that arrives
        with the budget already exhausted hands the issue off visibly instead of retrying blind."""
        repeat = record.collision_main_sha is not None
        attempt_no = record.attempts
        record.collision_main_sha = main_sha
        if repeat or record.attempts >= ATTEMPT_BUDGET:
            record.hold_reason = "integration collision"
            if not self._hold(record):
                return None
            self._emit(record, f"attempt {attempt_no}/{ATTEMPT_BUDGET} interrupted (integration "
                              "collision) — unresolved against a moved main; handoff pending; "
                              "claim retained")
            return self._finalize_hold(record)
        record.attempts -= 1               # refund the up-front charge; an identical retry is doomed
        record.attempt_committed = False
        record.state = WAITING
        record.continuation = True
        record.eligible_at = 0
        if not self._persist(record):
            return None
        self._emit(record, f"attempt {attempt_no}/{ATTEMPT_BUDGET} hit an integration collision on "
                          f"main {main_sha[:7]} — deferring retry until main moves; attempt "
                          "refunded; claim retained")
        return None

    def _finalize_hold(self, record: Record) -> StageOutcome | None:
        if not record.hold_pending:
            return None

        def hold(current: Record) -> bool:
            if not current.hold_pending or current.state not in {WAITING, COMPLETED}:
                return False
            if current.handoff_proof is None:
                proof = self._adapter.finalize_hold(current)
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

    def _record_telemetry(self, record: Record, obs, *, outcome, verified: bool) -> None:
        """Stamp this ended attempt's durable spend entry, keyed by its launch token (ADR 0040).

        Every attempt that ends is recorded — a completed stage, a superseded retry, a held
        exhaustion — so no spend is lost. A telemetry write never fails a cycle: the durable
        session artifact still carries the raw usage to re-derive later if the write is lost."""
        from agentflow.coordinator.profiles import profile_for

        entry = AttemptTelemetry(
            token=record.launch_token or "",
            identity=record.identity, repo=record.repo, subject=record.subject,
            stage=record.stage, pool=record.pool, model=record.model,
            complexity=record.complexity, effort=record.effort,
            # The reasoning effort actually set at launch (ADR 0046) — the mapped rung for
            # build/revise, explicit ``None`` for every other stage (provider default).
            reasoning_effort=profile_for(record).reasoning_effort,
            attempt=record.attempts, continuation=record.continuation,
            restart_resumes=record.restart_resumes, round=record.round,
            conflict_round=record.conflict_round,
            verified=verified, outcome=outcome or "",
            verify_miss=record.verify_miss,
            cause=obs.cause.value, classification=obs.classification(),
            started_at=record.started_at, finalized_at=int(time.time()),
            usage=getattr(obs, "usage", AttemptUsage()))
        record_attempt(self._store.path, entry)
        # The provider's own five-hour quota fact, when this attempt's stream reported one, is the
        # persisted Claude dispatch authority (#305). Writing it here — on every observed attempt —
        # keeps the freshest reading durable so the balancer sizes headroom (and honors the reset)
        # from the provider, not a trailing transcript proxy. A missing fact leaves the prior one.
        quota = getattr(obs, "quota", None)
        if quota is not None:
            from agentflow.coordinator.quota import record_quota
            record_quota(self._store.path, quota)

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


def _identity(repo: str, subject: str, stage: str, target: str | None, round: int = 0,
              conflict_round: int = 0, resume: int = 0, review_axis: str = "combined",
              review_passes: int = 0, review_sequence: int = 0,
              uncertainty_handoffs: int = 0) -> str:
    # The auto-revise round joins the identity once one exists, so an evidence-only revision —
    # whose re-review binds to the *same* head SHA — still opens a genuinely new stage rather
    # than colliding with the retired prior review's record. A conflict Revise's own round joins
    # it too (ADR 0038), so each conflict resolution is a fresh stage that never collides with a
    # finding-driven revise on the same head SHA. A deliberate maintainer resume of an exhausted
    # Build joins its resume number the same way (#245): the terminal `held` record keeps the base
    # identity live, so the successor must carry a distinct dimension or it would silently reuse it.
    parts = [repo, str(subject), stage, target or "-"]
    if round:
        parts.append(f"r{round}")
    if conflict_round:
        parts.append(f"c{conflict_round}")
    if resume:
        parts.append(f"s{resume}")
    if stage == "review" and review_axis != "combined":
        parts.append(f"a{review_axis}")
    if stage == "review" and review_passes:
        parts.append(f"p{review_passes}")
    if stage == "review" and review_sequence:
        parts.append(f"q{review_sequence}")
    if uncertainty_handoffs:
        parts.append(f"u{uncertainty_handoffs}")
    return "|".join(parts)


_WORKTREES_MARKER = "/.agentflow/worktrees/"


def _repool_source(source: str | None, dest_pool: str) -> str | None:
    """Rewrite the tool segment of a Build's worktree source path to ``dest_pool`` when a
    never-started Build migrates pools (#273). The path is ``<workdir>/.agentflow/worktrees/
    <tool>/issue-<n>-<slug>``; only the ``<tool>`` segment changes. A pure string rewrite local to
    the coordinator — the loop layer that first built the path must not be imported here (ADR 0030
    layering). A source with no worktree marker is left untouched (nothing to re-pin)."""
    if not source or _WORKTREES_MARKER not in source:
        return source
    head, tail = source.split(_WORKTREES_MARKER, 1)
    name = tail.split("/", 1)[1] if "/" in tail else ""
    return f"{head}{_WORKTREES_MARKER}{dest_pool}/{name}" if name else source


def _admit_everything(record: Record) -> bool:
    """The bare coordinator's default gate. Live tracers supply their stage gate; tests and
    direct construction use only the private permit ledger (ADR 0030)."""
    return True
