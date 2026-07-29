"""The composition root: eight coordinated stages wired into one live pipeline (ADR 0030).

Each ``coordinated_*`` module owns what *its* stage decides. This module owns everything that is
true of the pipeline as a whole and of no single stage:

- **the coordinator** — which stage module supplies which adapter collaborator, and the admission
  gate (stage enablement, provider headroom, machine/stage caps, operator pacing) they all pass
  through. Injecting the collaborators here is what keeps :mod:`agentflow.coordinator` free of
  GitHub, git worktrees and dispatch (ADR 0030).
- **the claim transfer** — a completed Build opens its Review, a blocking Review opens its
  Revise, a completed Revise opens its next Review, each before the projection so the change
  claim never has an ownership gap (ADR 0028).
- **claim reconciliation** — which issues and worktrees the durable store still owns, and the
  visible claim labels that reconciliation proves orphaned.

It is the one module that knows all eight stages, which is exactly why nothing else has to.
"""

from __future__ import annotations

import os
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from agentflow import (coordinated_build, coordinated_converse, coordinated_intake,
                       coordinated_mockup, coordinated_research, coordinated_respond,
                       coordinated_review, coordinated_revise, github)
from agentflow.balancer import BUILD_POOLS, pick_reviewer
from agentflow.coordinator import (BuildStageAdapter, ConverseStageAdapter, Coordinator,
                                   IntakeStageAdapter, MockupStageAdapter, ResearchStageAdapter,
                                   RespondStageAdapter, ReviewStageAdapter, ReviseStageAdapter,
                                   StageRouter, tracer)
from agentflow.coordinator.admission import MACHINE_CEILING, STAGE_CAPS
from agentflow.coordinator.store import ReservationLimits, StoreUnavailable, default_store_path
from agentflow.gate import MAX_REVISES, revise_round_budget_remains
from agentflow.labels import BUILDING, DRAWING, RESOLVING, TRIAGING
from agentflow.pr_park import park_pr
from agentflow.repo_facts import repo_profile
from agentflow.review_policy import CONFLICT_UNCERTAINTY_PREFIX
from agentflow.stage_worktree import worktree_ready
from agentflow.worktree_ref import source_facts


_ORPHAN_CLAIM_GRACE_SECONDS = 60 * 60


def owned_issues(cfg, *, store_path=None, lane=None) -> set[int]:
    """The issues in ``cfg.repo`` a coordinator record still owns. Empty when no store exists.

    ``lane`` scopes ownership to one claim type: ``"building"`` (Build/Review/Revise/Respond),
    ``"triaging"`` (Intake), or ``"drawing"`` (Mockup). Each reclamation pass supplies its lane
    so one claim type's live record never shields another type's stale claim."""
    path = Path(store_path or default_store_path())
    if not path.exists():
        return set()
    return tracer.owned_issues(tracer.load_records(path), cfg.repo, lane=lane)


def owned_worktrees(cfg, *, store_path=None) -> set[str]:
    """Coordinator-owned sources in ``cfg.repo`` that startup recovery must retain."""
    path = Path(store_path or default_store_path())
    if not path.exists():
        return set()
    return {
        os.path.realpath(record.source)
        for record in tracer.load_records(path)
        if record.repo == cfg.repo and record.source and not record.retired
    }


def reconcile_orphaned_claims(cfg, *, _log=None) -> int:
    """Clear visible claims only after coordinator reconciliation proves them orphaned.

    The durable store is read first and is authoritative. An unreadable store clears nothing.
    For each claim lane, any claim-owning continuation record for the issue keeps the label,
    including a waiting/completed record with no live process. Because every provider family is
    born from a running record, absence of such a record after ``Coordinator.cycle`` also proves
    there is no live family. A one-hour grace protects short deterministic interactive claim
    operations. GitHub listing or verification failures likewise clear nothing.
    """
    from agentflow.coordinator.record import RUNNING

    _log = _log or (lambda _line: None)
    try:
        records = tracer.load_records()
    except StoreUnavailable as exc:
        _log(f"{cfg.repo}: claim reconciliation deferred — coordinator state unreadable: {exc}")
        return 0

    lane_labels = (("building", BUILDING), ("triaging", TRIAGING), ("drawing", DRAWING),
                   ("resolving", RESOLVING))
    cleared = 0
    for lane, label in lane_labels:
        claimed = github.claimed_issues(cfg.repo, label)
        if claimed is None:
            _log(f"{cfg.repo}: {lane} claim reconciliation deferred — GitHub unreadable")
            continue
        for issue in claimed:
            number = issue.number
            try:
                updated = datetime.fromisoformat(
                    issue.updated_at.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if time.time() - updated < _ORPHAN_CLAIM_GRACE_SECONDS:
                continue  # protect short deterministic by-hand claim operations
            related = [
                record for record in records
                if record.repo == cfg.repo and str(record.subject) == str(number)
                and tracer.CLAIM_LANE.get(record.stage) == lane
            ]
            if any((not record.retired and record.claim) or record.state == RUNNING
                   for record in related):
                continue
            if not github.remove_label(cfg.repo, number, label):
                continue
            labels = github.issue_labels(cfg.repo, number)
            if labels is None:
                continue
            if label not in labels:
                cleared += 1
                _log(f"{cfg.repo}: #{number}: reclaimed orphaned {lane} claim — "
                     "no live family or continuation record")
    return cleared


# --- production wiring (live orchestration; not unit-tested, ADR 0020) -------------------
def build_coordinator(_log=None) -> Coordinator:
    """The daemon's coordinator for all eight logical stages (issues #103–#108).
    Its Build adapter verifies the real PR outcome and reuses the retained worktree; its Review
    adapter verifies a durable starting/final-head verdict and retains the detached bounded-fix
    checkout; its Revise adapter verifies a pushed revision on the same branch and reuses that
    retained worktree; its Respond adapter verifies the marked reply plus any pushed change on that
    same branch and releases the change claim on completion; its Mockup adapter verifies one
    pushed visual round and releases its drawing claim at the human-pick boundary. One
    :class:`StageRouter` dispatches each adapter call on the record's stage."""
    intake = IntakeStageAdapter(
        worktree_reset=coordinated_intake.reset_worktree,
        apply_route=coordinated_intake.apply_route,
        claim_ready=coordinated_intake.intake_claim_ready,
        worktree_dispose=coordinated_intake.dispose_worktree,
        handoff=coordinated_intake.hold_intake)
    build = BuildStageAdapter(
        pr_exists=coordinated_build._pr_exists, worktree_ready=worktree_ready,
        handoff=coordinated_build._hold_build,
        integration_collision=coordinated_build._integration_collision,
        main_head=coordinated_build._main_head)
    review = ReviewStageAdapter(
        verdict_ready=coordinated_review._verdict_ready,
        worktree_reset=lambda record: coordinated_review._review_worktree_reset(record, _log=_log),
        handoff=park_pr,
        settle=coordinated_review._settle_review,
        prepare_settle=coordinated_review._prepare_review_settlement)
    revise = ReviseStageAdapter(
        revision_ready=coordinated_revise._revision_ready, worktree_ready=worktree_ready,
        handoff=park_pr,
        uncertainty=coordinated_revise._conflict_uncertainty_outcome)
    respond = RespondStageAdapter(
        reply_ready=coordinated_respond._reply_ready, worktree_ready=worktree_ready,
        handoff=coordinated_respond._park_respond,
        settle=coordinated_respond._settle_respond)
    mockup = MockupStageAdapter(
        outcome_ready=coordinated_mockup._mockup_outcome_ready,
        worktree_ready=lambda record: (coordinated_mockup._mockup_claim_ready(record)
                                       and worktree_ready(record)),
        missing_context=coordinated_mockup._mockup_missing_context,
        handoff=coordinated_mockup._hold_mockup,
        settle=coordinated_mockup._settle_mockup)
    converse = ConverseStageAdapter(
        reply_ready=coordinated_converse._reply_ready,
        adopt=coordinated_converse._adopt_turn,
        park=coordinated_converse._park_ask,
        worktree_ready=coordinated_converse._ask_worktree_ready)
    research = ResearchStageAdapter(
        findings_ready=coordinated_research._findings_ready,
        resolve=coordinated_research.resolve,
        release=coordinated_research.release,
        worktree_ready=coordinated_research._research_worktree_ready)
    router = StageRouter({"intake": intake, "build": build, "review": review, "revise": revise,
                          "respond": respond, "mockup": mockup, "converse": converse,
                          "research": research})
    return Coordinator(adapter=router, gate=_production_gate(),
                       disabled_cold_stages=frozenset({"mockup"}),
                       log=_log or (lambda _line: None))


class _ProductionGate:
    """One dispatch cycle's composed durable admission policy."""

    def __init__(self, running_permits=None) -> None:
        from collections import Counter
        self._paced = Counter()
        self._active: dict[str, bool] = {}
        # How many permits are already running on a pool, from the durable ledger. Injected so the
        # in-flight Claude reservation is exercised without a live store; production reads the same
        # running rows the reservation itself charges.
        self._running_permits = running_permits or _durable_running_permits

    def __call__(self, record) -> bool:
        from agentflow import balancer
        if not tracer.build_review_revise_gate(record):
            return False
        # An interactive turn (an operator-present Ask) is a real-time conversation: it is exempt
        # from the recent-session cooldown, the spend ceiling, and the active-pacing budget (ADR
        # 0034/0025 as amended by #162). Only the reservation ledger in `_begin_start` — true zero
        # capacity — may still defer it. Background stages keep the full clear + pacing gate.
        if record.interactive:
            return True
        try:
            # Claude admission reserves conservative five-hour headroom for work already running on
            # the pool before another session starts (#305): its provider quota fact only updates
            # after a session ends, so several launches must not all pass one stale below-ceiling
            # reading. The reservation scales with running *permits* (the ledger's SUM(demand)), not
            # session count, so a heavier session reserves proportionally more — see
            # balancer.CLAUDE_INFLIGHT_RESERVE_PCT for the intent and calibration. Codex reports live
            # per-window facts, so it needs no such reservation.
            reserved_pct = (self._running_permits(record.pool) * balancer.CLAUDE_INFLIGHT_RESERVE_PCT
                            if record.pool == "claude" else 0.0)
            status = balancer._query_pool(record.pool, reserved_pct=reserved_pct)
            # Launch must honor the same unattended weekly pacing that `pick_pair` applies at
            # submission: raw `_query_pool` only checks the short/five-hour ceiling, so a stage
            # queued while weekly headroom existed would otherwise launch after that weekly budget
            # is exhausted. Both pools now carry a paced weekly allowance (#315), so each recheck is
            # pool-specific — Codex via `_codex_dispatch_status`, Claude via `_claude_dispatch_status`.
            if status is not None and record.pool == "codex":
                status = balancer._codex_dispatch_status(status, time.time())
            elif status is not None and record.pool == "claude":
                status = balancer._claude_dispatch_status(status, time.time())
        except Exception:
            return False
        if not status or not status.clear:
            return False
        self._active[record.pool] = status.active
        return not (status.active and self._paced[record.pool] >= balancer.ACTIVE_PACE)

    @staticmethod
    def reservation_limits(record) -> ReservationLimits:
        """The global limits the store enforces with the running-row reservation."""
        lane = {"intake": "triage", "build": "build", "review": "build", "revise": "build",
                "respond": "respond", "mockup": "mockup", "research": "research"}
        stage_lane = lane.get(record.stage, record.stage)
        return ReservationLimits(
            machine_ceiling=MACHINE_CEILING,
            stage_cap=STAGE_CAPS.get(stage_lane, 1),
            stage_lane=stage_lane,
            lane_by_stage=lane,
        )

    def started(self, record) -> None:
        """Charge operator pacing only after the provider start is durable. An interactive turn
        never consumes the background pace budget — even when a background record already marked
        the pool active this cycle, its start is exempt from pacing (ADR 0034/0025 as amended)."""
        if record.interactive:
            return
        if self._active.get(record.pool, False):
            self._paced[record.pool] += 1


def _durable_running_permits(pool: str) -> int:
    """The permits already running on ``pool``, read from the durable ledger. A read failure
    reserves nothing (the hard permit ledger in ``_begin_start`` still caps concurrency), so a
    transient store hiccup never wedges dispatch."""
    from agentflow.coordinator.store import Store

    try:
        store = Store(default_store_path())
        try:
            return store.permits_used(pool)
        finally:
            store.close()
    except (StoreUnavailable, OSError):
        return 0


def _production_gate():
    """Compose stage enablement, headroom, machine/stage caps, and operator pacing.

    Running durable records are the concurrency ledger. The closure lasts one daemon dispatch
    cycle, so its active-pool counter is exactly the per-cycle pacing budget.
    """
    return _ProductionGate()


def _open_review_on_completed_build(coord: Coordinator, build_identity: str) -> None:
    """A completed Build opens exactly one waiting Review for the exact PR head SHA and transfers
    the change claim before the Build record retires — no ownership gap (ADR 0028). Submission is
    idempotent on the review identity (repo, subject, review, head SHA), so a repeat or restart
    never opens a second review; a new head SHA is a genuinely new stage. Live — its mapping is
    covered through :func:`coordinated_review.review_submission`, and the re-drive after a crash
    or transient failure through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    build = records.get(build_identity)
    if build is None or build.stage != "build":
        return
    facts = source_facts(build)
    if facts is None:
        return
    _workdir, branch, _wt = facts
    pr = github.open_pr_for_branch(build.repo, branch)
    if pr is None:
        return
    context = coordinated_review._review_context(build)
    if context is None:
        return
    acceptance, surfaces = context
    from agentflow.review_policy import ReviewState
    profile = repo_profile(_workdir)
    assignment, _changed_files = coordinated_review._review_assignment_facts(
        build.repo, pr.number, profile=profile)
    reviewer_tool = (pick_reviewer(build.pool, allow_same_tool=False)
                     if profile == "autonomous" else pick_reviewer(build.pool))
    if reviewer_tool is None:
        return  # ADR 0020: no tool free to review this cycle — post nothing; the completed
                # build keeps its claim and this opener re-drives next cycle.
    submission = coordinated_review.review_submission(
        build, pr.head_ref_oid, reviewer_tool, pr.number,
        acceptance=acceptance, surfaces=surfaces,
        review=ReviewState(assignment=assignment, change_author_tool=build.pool))
    if submission is not None:
        coord.submit_stage(submission)


def _open_revise_on_blocking_review(coord: Coordinator, review_identity: str) -> None:
    """A completed Review whose verdict blocks opens exactly one waiting Revise on the builder's
    retained branch/worktree and transfers the change claim before the Review record retires — no
    ownership gap (ADR 0028). A clean verdict is the merge path, not a revise. The auto-revise
    product cap (``MAX_REVISES`` rounds, ADR 0004) is unchanged: once it is spent, a further
    blocking review parks on its own exhaustion rather than looping. Submission is idempotent on
    the revise identity (repo, subject, revise, reviewed SHA, round), so a repeat or restart never
    opens a second revise. Live — its mapping is covered through
    :func:`coordinated_revise.revise_submission`, and the park and re-drive paths through
    ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    review = records.get(review_identity)
    if review is None or review.stage != "review" or not review.target:
        return
    verdict = coordinated_review._review_verdict(review)
    if verdict.change_author_tool:
        # Structured ADR 0047 reviews stay inside the private review chain. A reviewer push is
        # never self-approved; Full separates product and standards before one fix pass; genuine
        # uncertainty gets one narrow other-tool handoff. Only the final other-tool unchanged pass
        # reaches settlement.
        if verdict.pushed_sha:
            if review.review_passes + 1 >= 3:
                coord.park_completed(review_identity)
                return
            successor = coordinated_review.review_successor_submission(review, verdict)
            if successor is not None:
                coord.submit_stage(successor)
            # No eligible reviewer is an availability hold: the completed record keeps the claim
            # and this opener retries without consuming a session or publishing a park.
            return
        if coordinated_review._review_depth_escalated(review, verdict):
            successor = coordinated_review.review_axis_successor_submission(
                review, verdict, axis="product")
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if verdict.uncertainty is not None:
            if review.uncertainty_handoffs:
                coord.park_completed(review_identity)
                return
            successor = coordinated_review.review_axis_successor_submission(
                review, verdict, axis=review.review_axis, uncertainty=True)
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if review.review_axis == "decision":
            successor = coordinated_revise.conflict_decision_revise_submission(review, verdict)
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if review.review_axis == "product":
            successor = coordinated_review.review_axis_successor_submission(review, verdict)
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if any(item.action.value == "fix_before_completion" for item in verdict.actions):
            if review.review_axis == "fix":
                coord.park_completed(review_identity)
                return
            successor = coordinated_review.review_axis_successor_submission(
                review, verdict, axis="fix")
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if not verdict.clean:
            coord.park_completed(review_identity)
        return
    if verdict.clean or not verdict.blocking:
        return  # a clean (or non-blocking) verdict is the merge path, not a revise
    if (review.round >= MAX_REVISES
            or not revise_round_budget_remains(records.values(), review.repo, review.subject)):
        # The auto-revise rounds are spent and the review still blocks: no revise, review, or
        # merge stage will ever consume this outcome, so park the PR for a human exactly once and
        # release the review's retained claim rather than leaving the PR owned forever (ADR 0028).
        coord.park_completed(review_identity)
        return
    facts = coordinated_revise._revise_builder_source(review)
    # The revise runs at the *original builder* complexity, carried durably on the review record
    # since the build opened it (ADR 0018). Re-reading the issue's live label here would let a
    # changed, removed, or unreadable label alter or block the revise; the stage chain owns it.
    complexity = review.builder_complexity
    if facts is None or not complexity:
        # Missing lineage facts — a pre-#105 review record with no durable builder complexity, or
        # an unreadable builder source — are a permanent condition: no revise can ever open from
        # this record, so park the PR for a human exactly once instead of silently stranding the
        # claim (issue #105: a permanent condition creates exactly one parked-PR handoff).
        coord.park_completed(review_identity)
        return
    findings = "\n".join(f"- {f.summary}" for f in verdict.blocking)
    submission = coordinated_revise.revise_submission(
        review, complexity, findings, target_sha=verdict.final_sha or review.target)
    if submission is not None:
        coord.submit_stage(submission)


def _open_review_on_completed_revise(coord: Coordinator, revise_identity: str) -> None:
    """A completed Revise opens exactly one waiting Review bound to the current PR head SHA and
    transfers the change claim before the Revise record retires — no ownership gap (ADR 0028). The
    new review carries the revise round in its identity and starts a fresh review budget, so the
    prior review's record is never reused — even for an evidence-only revision whose head SHA never
    moved. Submission is idempotent on that identity, so a repeat or restart never opens a second
    review. Live — its mapping is covered through :func:`coordinated_review.review_submission`,
    and the evidence-only and re-drive paths through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    revise = records.get(revise_identity)
    if revise is None or revise.stage != "revise":
        return
    facts = source_facts(revise)  # revise reuses the builder worktree, so this parses its branch
    if facts is None:
        return
    _workdir, branch, _wt = facts
    pr = github.open_pr_for_branch(revise.repo, branch)
    if pr is None:
        return
    context = coordinated_review._review_context(revise)
    if context is None:
        return
    acceptance, surfaces = context
    if revise.outcome and revise.outcome.startswith(CONFLICT_UNCERTAINTY_PREFIX):
        submission = coordinated_review.conflict_decision_review_submission(
            revise, head_sha=pr.head_ref_oid, pr_number=pr.number,
            acceptance=acceptance, surfaces=surfaces)
        if submission is not None:
            coord.submit_stage(submission)
        return
    conflict_resolution = bool(revise.conflict_round)
    from agentflow.review_policy import ReviewState
    profile = repo_profile(_workdir)
    inherited = ReviewState.from_record(revise)
    if conflict_resolution and revise.uncertainty_handoffs and inherited is not None:
        # A private decision resolution is a Full product change. The PR-body proposal cannot
        # downgrade the required product+standards pass when the resolved Revise completes.
        review = replace(
            inherited, reviewed_from_sha=revise.target,
            cross_tool_covered=False, sequence=inherited.sequence + 1)
    else:
        assignment, _changed_files = coordinated_review._review_assignment_facts(
            revise.repo, pr.number, conflict_resolution=conflict_resolution, profile=profile)
        review = ReviewState(assignment=assignment, change_author_tool=revise.pool,
                             reviewed_from_sha=revise.target)
    reviewer_tool = (pick_reviewer(revise.builder_lineage, allow_same_tool=False)
                     if profile == "autonomous" else pick_reviewer(revise.builder_lineage))
    if reviewer_tool is None:
        return  # ADR 0020: no tool free to review this cycle — post nothing; the completed
                # revise keeps its claim and this opener re-drives next cycle.
    submission = coordinated_review.review_submission(
        revise, pr.head_ref_oid, reviewer_tool, pr.number,
        acceptance=acceptance, surfaces=surfaces,
        conflict_resolution=conflict_resolution,
        review=review)  # ADR 0038: add discard-check lens
    if submission is not None:
        coord.submit_stage(submission)

# Each completed stage's claim-transfer opener, keyed by the stage it consumes.
_OPENERS = {"build": _open_review_on_completed_build,
            "review": _open_revise_on_blocking_review,
            "revise": _open_review_on_completed_revise}


def reconcile_and_project(coord: Coordinator, *, _log=None) -> list:
    """Reconcile every Build/Review/Revise pool and republish the live board as a projection of the
    running records (ADR 0030). A completed Build opens its Review, a blocking Review opens its
    Revise, and a completed Revise opens its next Review — each before the projection, so the claim
    transfers with no ownership gap. The openers are driven from the *durable records*, not this
    cycle's outcomes: any completed record still holding the change claim has no successor yet —
    whether it completed just now, the daemon died between completion and its opener, or a prior
    opener failed on a transient read — so every pass re-drives the transfer idempotently rather
    than stranding the chain (ADR 0028). Returns the terminal outcomes settled this cycle."""
    from agentflow import live
    from agentflow.coordinator.record import COMPLETED
    outcomes = []
    now = int(time.time())
    # Before any attempt is charged, retire a Review whose PR head has moved off its immutable
    # target (or whose PR is gone) rather than burning the budget re-reviewing a superseded head
    # and wrongly parking a merged PR (#208).
    coordinated_review._resume_tainted_reviews(coord)
    coordinated_review._resettle_diverged_reviews(coord)
    for pool in BUILD_POOLS:
        outcomes.extend(coord.cycle(pool, now=now))
    # Handoffs are driven from durable state, not only this process's outcomes. A daemon may
    # die after a stage completion is committed but before it consumes the returned outcome.
    # A completed stage keeps its claim until its successor is atomically persisted.
    for record in tracer.load_records():
        if (record.state == COMPLETED and not record.retired and record.claim
                and not record.hold_pending):  # a pending park is already retried by reconcile
            opener = _OPENERS.get(record.stage)
            if opener is not None:
                try:
                    opener(coord, record.identity)
                except StoreUnavailable:
                    # The fail-closed transactional submit refused the transfer — e.g. another
                    # process moved the claim between our durable snapshot and this submit. The
                    # store is the truth; skip and let the next pass re-read it.
                    continue
    records = tracer.load_records()
    live.replace_projection(tracer.live_projection(records))
    return outcomes
