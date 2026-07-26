"""All six logical stages behind the session coordinator, wired into dispatch
(issues #103–#108).

Every provider stage enters one durable submission. Build, Review, and Revise transfer one change
claim through their convergence loop; Intake, Mockup, and Respond each own their stage-native
boundary and claim. The coordinator owns continuation, admission, and completion, and the live
board is generated from its running records. There is no legacy provider path or bypass mode.

The pure parts — mapping stage inputs to submissions, the Build/Review/Revise transfers, the
``MAX_REVISES``-capped
auto-revise product policy (ADR 0004) that continuation attempts never expand; deriving the phase
and projecting running records — are exercised directly. The production factory wires the
coordinator's stage adapters to the real GitHub PR check, verdict parse, branch head, and
worktrees, following the same stage-native completion contracts the earlier pipeline established.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agentflow import github
from agentflow.coordinator import (BuildStageAdapter, ConverseStageAdapter, Coordinator,
                                   IntakeStageAdapter, MockupStageAdapter, ResearchStageAdapter,
                                   RespondStageAdapter, ReviewStageAdapter, ReviseStageAdapter,
                                   StageRouter, tracer)
from agentflow.balancer import pick_reviewer
from agentflow.coordinator.store import ReservationLimits, StoreUnavailable, default_store_path
from agentflow.gate import MAX_REVISES
from agentflow.worktree_ref import WorktreeKind, WorktreeRef

BUILD_POOLS = ("claude", "codex")
_ORPHAN_CLAIM_GRACE_SECONDS = 60 * 60
_REVIEW_CI_OBSERVED: dict[str, bool] = {}

# The one extra lens a re-review gains when a conflict Revise produced the head under review (ADR
# 0038): the reviewer must confirm it kept every compatible behavior and did not silently choose a
# winner for genuinely competing product intent.
CONFLICT_REVIEW_LENS = (
    "\n\nThis head was produced by resolving a merge conflict against `main`. One extra lens: "
    "verify the resolution preserves both sides wherever their behavior is compatible. If the "
    "sides encode genuinely competing product intent, verify that the private second-opinion path "
    "resolved it; never accept a silent choice based on which side is newer.")


def build_submission(cfg, issue: dict, tool: str):
    """Translate one ready issue and its chosen tool into a single Build stage submission — the
    minimal facts the coordinator needs (ADR 0030). The durable input pointer is the full build
    brief the provider session runs, so a recovered attempt rebuilds the same prompt. Pure: the
    issue→submission mapping is the test surface. Returns ``None`` when the issue lacks the
    complexity gate a build requires (ADR 0018), so a mis-labelled issue never becomes an
    attempt."""
    from agentflow.coordinator import Submission
    from agentflow.loop import (BUILD_PROMPT, _builder_worktree, _surfaces_phrase,
                                complexity_from_labels, effort_from_labels, slug, ui_surfaces)
    n = issue["number"]
    labels = [lbl["name"] for lbl in issue.get("labels", [])]
    complexity = complexity_from_labels(labels)
    if complexity is None:
        return None
    sl = slug(issue["title"])
    brief = BUILD_PROMPT.format(
        repo=cfg.repo, n=n, title=issue.get("title", ""), body=issue.get("body") or "",
        effort=effort_from_labels(labels).value,
        surfaces=_surfaces_phrase(ui_surfaces(cfg.workdir)))
    return Submission(
        repo=cfg.repo, subject=str(n), stage="build", pool=tool,
        complexity=complexity.value, effort=effort_from_labels(labels).value,
        source=_builder_worktree(cfg, tool, n, sl), claim=True, input_ptr=brief)


def resume_if_held(submission, records):
    """Turn a deliberate maintainer `build <N>` into an explicit, durable resume when the issue's
    latest Build is a budget-exhausted ``held`` record (#245).

    A ``held`` Build is terminal but never retired, so its stable identity (``repo|issue|build|-``)
    stays live: an ordinary resubmission reuses it unwritten and no provider ever launches. When the
    latest Build for this issue is that held record, this bumps the submission to the next resume
    dimension, whose fresh identity opens a genuinely new bounded execution (a fresh
    ``ATTEMPT_BUDGET``) that still reuses the same issue, brief, builder lineage, and retained
    worktree ``source``. Otherwise the submission is returned unchanged, so an ordinary duplicate
    stays idempotent and a repeated resume — whose successor is already live — never opens a second
    concurrent Build. Pure: the resume decision is the test surface."""
    from dataclasses import replace
    from agentflow.coordinator.record import HELD

    builds = [r for r in records
              if r.repo == submission.repo and str(r.subject) == str(submission.subject)
              and r.stage == "build"]
    live = [r for r in builds if not r.retired]
    if not live:
        return submission                       # no live Build — an ordinary cold submission
    latest = max(live, key=lambda r: r.resume)
    if latest.state != HELD:
        return submission                       # a live or completed Build — nothing to resume
    # The next resume dimension is one past *every* Build ever opened for this issue — retired
    # successors included — so a resume can never collide with a prior successor's identity.
    next_resume = max(r.resume for r in builds) + 1
    # Reuse the held builder's pinned pool, retained worktree, and durable brief so the resume
    # *recovers* the same branch/worktree the stage adapter left on disk — and re-runs the same
    # build brief — rather than re-deriving a fresh path from a possibly re-picked tool (#245).
    return replace(submission, resume=next_resume, pool=latest.pool,
                   source=latest.source, builder_lineage=latest.builder_lineage,
                   input_ptr=latest.input_ptr)


def resume_in_flight(submission, records) -> bool:
    """True when a resume of this issue's Build is already live — a non-retired successor at a resume
    dimension past the original held record, still running or queued (#245). A repeated maintainer
    `build <N>` while that resume runs is correctly non-duplicating (``resume_if_held`` leaves it
    unchanged, so it idempotently reuses the terminal held record), but the caller should acknowledge
    the running resume rather than report the record as merely 'still held'. Pure."""
    from agentflow.coordinator.record import HELD

    return any(r.repo == submission.repo and str(r.subject) == str(submission.subject)
               and r.stage == "build" and not r.retired and r.resume >= 1 and r.state != HELD
               for r in records)


def mockup_submission(cfg, issue: dict, tool: str):
    """Translate one eligible held issue into its single durable Mockup variant round.

    The stable identity is ``(repo, issue, mockup)``: repeated discovery returns the same record,
    while the pinned pool and owned branch/worktree preserve tool lineage and local progress across
    fresh-session continuations. The durable prompt reconstructs the exact same visual-design job.
    """
    from agentflow.coordinator import Submission
    from agentflow.loop import (PRODUCE_PROMPT, _MOCKUP_DISCLAIMER, _SCOPE_GUIDANCE,
                                _surfaces_phrase, mockup_scope_from_labels, slug, ui_surfaces)

    n = int(issue["number"])
    sl = slug(issue.get("title", ""))
    ref = WorktreeRef.for_mockup(cfg.workdir, tool, n, sl)
    branch = ref.branch
    source = ref.path
    scope = mockup_scope_from_labels([lbl["name"] for lbl in issue.get("labels", [])])
    prompt = PRODUCE_PROMPT.format(
        repo=cfg.repo, n=n, title=issue.get("title", ""), body=issue.get("body") or "",
        branch=branch, surfaces=_surfaces_phrase(ui_surfaces(cfg.workdir)),
        scope_guidance=_SCOPE_GUIDANCE[scope], disclaimer=_MOCKUP_DISCLAIMER)
    return Submission(
        repo=cfg.repo, subject=str(n), stage="mockup", pool=tool, complexity="deep",
        source=source, claim=True, input_ptr=prompt, builder_lineage=tool)


def _build_source_parts(record):
    """The ``(workdir, slug)`` behind a Build record's owned worktree, or ``None``. The slug is
    reused to name the review worktree so both stages of one issue read as a pair on disk."""
    ref = WorktreeRef.parse(record.source)
    if ref is None:
        return None
    return ref.workdir, ref.slug


def review_submission(build_record, head_sha, reviewer_tool, pr_number,
                      *, acceptance="", surfaces="", conflict_resolution=False,
                      review=None):
    """Translate a completed Build (or completed Revise) and its PR head SHA into one Review stage
    submission — the minimal facts the coordinator needs (ADR 0030). The review is bound to the
    *exact* head SHA (its immutable target, so a new head SHA starts a fresh review stage), assumes
    the prior stage's change claim, records the builder's lineage so a same-tool review can finish
    but never auto-merges, and carries the *original builder complexity* forward so a later Revise
    reads it from the durable record instead of a mutable issue label (ADR 0018). A review that
    follows a Revise carries that revise round in its identity, so an evidence-only revision — same
    head SHA, new durable proof — still opens a genuinely new review with a fresh budget, never the
    retired prior review's record. It points at a detached writable review worktree starting at
    that SHA. Cross-tool review is always the deep safety net.

    ``conflict_resolution`` marks a re-review whose head a conflict Revise produced (ADR 0038): it
    adds the discard-check lens to the prompt and leaves the finding-driven ``round`` untouched — a
    conflict resolution is not one of the auto-revise rounds, so it neither advances nor spends that
    budget. Pure: the mapping is the test surface (ADR 0020). Returns ``None`` if the Build worktree
    or head SHA is unreadable."""
    from agentflow.coordinator import Submission
    from agentflow.review_policy import ReviewState
    from agentflow.reviewer import REVIEW_PROMPT, review_worktree, with_review_assignment
    parts = _build_source_parts(build_record)
    if parts is None or not head_sha:
        return None
    workdir, slug = parts
    brief = REVIEW_PROMPT.format(
        pr=pr_number, starting_sha=head_sha, acceptance=acceptance or "(none provided)",
        surfaces=surfaces or "any user-facing surface")
    state = review or ReviewState(change_author_tool=build_record.pool)
    assignment = state.assignment
    author = state.change_author_tool or build_record.pool
    brief = with_review_assignment(
        brief,
        depth=assignment.depth, reason=assignment.reason, axis=assignment.axis,
        change_author_tool=author, handoff=state.handoff or "")
    if conflict_resolution:
        brief += CONFLICT_REVIEW_LENS
    # A conflict Revise did not complete a finding-driven round, so the re-review it opens carries
    # the same finding-driven round it inherited — only a finding-driven Revise advances it.
    completed_rounds = (build_record.round + 1
                        if build_record.stage == "revise" and not build_record.conflict_round
                        else build_record.round)
    state = replace(
        state, change_author_tool=author,
        reviewed_from_sha=state.reviewed_from_sha or getattr(build_record, "target", None),
        cross_tool_covered=reviewer_tool != author)
    return Submission(
        repo=build_record.repo, subject=build_record.subject, stage="review",
        target=head_sha, pool=reviewer_tool, complexity="deep",
        source=str(review_worktree(workdir, reviewer_tool, pr_number, slug)),
        claim=True, input_ptr=brief, builder_lineage=build_record.pool,
        builder_complexity=build_record.complexity, round=completed_rounds,
        conflict_round=build_record.conflict_round,
        review=state,
        transfer_from=build_record.identity)


def conflict_decision_review_submission(revise_record, *, head_sha: str, pr_number: int,
                                        acceptance: str, surfaces: str):
    """Open the one private other-tool decision pass for genuine conflict ambiguity."""
    import json
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState, Uncertainty, other_tool)

    if (not revise_record.conflict_round or not revise_record.outcome
            or not revise_record.outcome.startswith(_CONFLICT_UNCERTAINTY_PREFIX)):
        return None
    try:
        uncertainty = json.loads(
            revise_record.outcome[len(_CONFLICT_UNCERTAINTY_PREFIX):])
    except json.JSONDecodeError:
        return None
    reviewer_tool = other_tool(revise_record.pool)
    if reviewer_tool is None:
        return None
    handoff = (
        f"Conflict resolver recorded exactly two options: {uncertainty.get('options')}. "
        f"Missing guidance: {uncertainty.get('missing_guidance')}. "
        f"Recommendation: {uncertainty.get('recommendation')}. Verify and decide independently."
    )
    try:
        uncertainty_value = Uncertainty(
            tuple(uncertainty["options"]), uncertainty["missing_guidance"],
            uncertainty["recommendation"])
    except (KeyError, TypeError):
        return None
    return review_submission(
        revise_record, head_sha, reviewer_tool, pr_number,
        acceptance=acceptance, surfaces=surfaces, conflict_resolution=True,
        review=ReviewState(
            assignment=ReviewAssignment(
                ReviewDepth.FULL, "competing product behaviors in a conflict",
                ReviewAxis.DECISION),
            change_author_tool=revise_record.pool, handoff=handoff,
            uncertainty=uncertainty_value, uncertainty_handoffs=1))


def conflict_decision_revise_submission(review_record, verdict):
    """Resume the same retained conflict after the other tool supplies a grounded decision."""
    from agentflow.coordinator import Submission
    from agentflow.loop import REVISE_PROMPT
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState)

    facts = _revise_builder_source(review_record)
    if (facts is None or review_record.review_axis != "decision"
            or not review_record.conflict_round or not verdict.decision):
        return None
    build_worktree, pr_number = facts
    decision = (
        "The other tool resolved the private conflict decision. Apply this choice while preserving "
        f"all compatible behavior: {verdict.decision}"
    )
    prompt = REVISE_PROMPT.format(
        n=pr_number, repo=review_record.repo, findings=f"- {decision}",
        surfaces="any user-facing surface")
    prior = ReviewState.from_record(review_record)
    if prior is None:
        return None
    review = replace(
        prior,
        assignment=ReviewAssignment(
            ReviewDepth.FULL, review_record.depth_reason, ReviewAxis.PRODUCT),
        change_author_tool=review_record.builder_lineage, handoff=decision)
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="revise",
        target=review_record.target, pool=review_record.builder_lineage,
        complexity=review_record.builder_complexity or "deep", source=build_worktree,
        claim=True, input_ptr=prompt, builder_lineage=review_record.builder_lineage,
        builder_complexity=review_record.builder_complexity or "deep",
        round=review_record.round, conflict_round=review_record.conflict_round,
        transfer_from=review_record.identity, continuation=True,
        review=review)


def review_successor_submission(review_record, verdict):
    """Map a reviewer-authored pushed head to the other tool's exact-head review.

    This is ADR 0047's independence boundary: the mutating reviewer becomes the current change
    author, the opposite tool receives a private bounded handoff, and the stale builder checkout is
    never reused. Three consecutive mutating passes have no successor; the caller parks once.
    """
    from agentflow.coordinator import Submission
    from agentflow.loop import repo_profile
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState)
    from agentflow.reviewer import review_worktree, with_review_assignment

    facts = _review_source_facts(review_record)
    if (facts is None or not verdict.pushed_sha or verdict.pushed_sha != verdict.final_sha
            or verdict.final_sha == review_record.target):
        return None
    passes = review_record.review_passes + 1
    if passes >= 3:
        return None
    workdir, pr = facts
    next_tool = pick_reviewer(
        review_record.pool, allow_same_tool=repo_profile(workdir) != "autonomous")
    if next_tool is None:
        return None
    prior = ReviewState.from_record(review_record)
    if prior is None:
        return None
    follow_ups = prior.follow_ups + verdict.follow_ups
    fixes = prior.fixes + verdict.fixes
    all_checks = tuple(dict.fromkeys(prior.checks + verdict.checks))
    checks = "; ".join(all_checks) or "No checks were recorded; verify independently."
    changed = "; ".join(verdict.fixes) or "Reviewer pushed a changed head."
    handoff = (
        f"Review {review_record.target}..{verdict.final_sha}. "
        f"Changed: {changed} Why: grounded fixes from the prior pass. "
        f"Completed proof: {checks} Unresolved concerns: none recorded."
    )
    depth = max(
        (ReviewDepth(review_record.review_depth), verdict.depth),
        key=lambda value: ("focused", "targeted", "full").index(value.value))
    axis = ReviewAxis.PRODUCT if depth is ReviewDepth.FULL else ReviewAxis.COMBINED
    reason = (verdict.depth_reason if verdict.depth != prior.assignment.depth
              else prior.assignment.reason)
    prompt = (review_record.input_ptr or "").replace(
        review_record.target or "", verdict.final_sha)
    prompt = with_review_assignment(
        prompt,
        depth=depth,
        reason=reason,
        axis=axis,
        change_author_tool=review_record.pool, handoff=handoff)
    state = ReviewState(
        assignment=ReviewAssignment(depth, reason, axis),
        change_author_tool=review_record.pool, reviewed_from_sha=review_record.target,
        passes=passes, cross_tool_covered=next_tool != review_record.pool,
        tainted=prior.tainted, handoff=handoff,
        # A Full mutation invalidates both previous axes. Product and standards must inspect the
        # entire new head before any assigned fix ledger can be considered complete.
        findings=(), fixes=fixes, follow_ups=follow_ups, checks=all_checks,
        uncertainty_handoffs=prior.uncertainty_handoffs)
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="review",
        target=verdict.final_sha, pool=next_tool, complexity="deep",
        source=str(review_worktree(workdir, next_tool, pr, _review_slug(review_record))),
        claim=True, input_ptr=prompt, builder_lineage=review_record.builder_lineage,
        builder_complexity=review_record.builder_complexity, round=review_record.round,
        transfer_from=review_record.identity, review=state)


def review_axis_successor_submission(review_record, verdict, *, axis=None, tool=None,
                                     uncertainty=False):
    """Open a serialized same-head Full axis, fix pass, or uncertainty handoff."""
    import json
    from agentflow.coordinator import Submission
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState, merge_findings, other_tool)
    from agentflow.reviewer import review_worktree, with_review_assignment

    facts = _review_source_facts(review_record)
    if facts is None or verdict.pushed_sha:
        return None
    if axis is None:
        if review_record.review_depth == "full" and review_record.review_axis == "product":
            axis = ReviewAxis.STANDARDS
        else:
            return None
    else:
        axis = ReviewAxis(axis)
    workdir, pr = facts
    next_tool = tool or (other_tool(review_record.pool) if uncertainty else review_record.pool)
    if next_tool is None:
        return None
    prior = ReviewState.from_record(review_record)
    if prior is None:
        return None
    follow_ups = prior.follow_ups + verdict.follow_ups
    checks = tuple(dict.fromkeys(prior.checks + verdict.checks))
    combined_actions = merge_findings(prior.findings, verdict.actions)
    actions = [
        {"action": item.action.value, "summary": item.summary, "grounding": item.grounding,
         "file": item.file, "line": item.line}
        for item in combined_actions]
    uncertainty_value = verdict.uncertainty
    handoff = (
        f"Reviewed {review_record.target} at {review_record.review_axis} axis. "
        f"Findings: {json.dumps(actions)}. Checks: {json.dumps(checks)}. "
        f"Unresolved uncertainty: {uncertainty_value or 'none'}. Verify these facts yourself."
    )
    depth = max(
        (ReviewDepth(review_record.review_depth), verdict.depth),
        key=lambda value: ("focused", "targeted", "full").index(value.value))
    reason = (verdict.depth_reason if verdict.depth != prior.assignment.depth
              else prior.assignment.reason)
    author = prior.change_author_tool or review_record.builder_lineage
    prompt = with_review_assignment(
        review_record.input_ptr or "",
        depth=depth,
        reason=reason,
        axis=axis,
        change_author_tool=author or "",
        handoff=handoff)
    state = replace(
        prior, assignment=ReviewAssignment(depth, reason, axis),
        change_author_tool=author,
        reviewed_from_sha=prior.reviewed_from_sha or review_record.target,
        sequence=prior.sequence + 1,
        cross_tool_covered=next_tool != author, handoff=handoff,
        findings=combined_actions, follow_ups=follow_ups, checks=checks,
        uncertainty=uncertainty_value,
        uncertainty_handoffs=prior.uncertainty_handoffs + (1 if uncertainty else 0))
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="review",
        target=review_record.target, pool=next_tool, complexity="deep",
        source=str(review_worktree(workdir, next_tool, pr, _review_slug(review_record))),
        claim=True, input_ptr=prompt, builder_lineage=review_record.builder_lineage,
        builder_complexity=review_record.builder_complexity, round=review_record.round,
        transfer_from=review_record.identity, review=state)


def tainted_review_submission(review_record, reviewer_tool: str):
    """Reopen a human-merge-only same-tool result when the independent tool returns."""
    from agentflow.coordinator import Submission
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState)
    from agentflow.reviewer import review_worktree, with_review_assignment

    facts = _review_source_facts(review_record)
    author = review_record.change_author_tool or review_record.builder_lineage
    if facts is None or not review_record.target or reviewer_tool == author:
        return None
    prior = ReviewState.from_record(review_record)
    if prior is None:
        return None
    workdir, pr = facts
    depth = ReviewDepth(review_record.review_depth)
    axis = ReviewAxis.COMBINED if depth is not ReviewDepth.FULL else ReviewAxis.PRODUCT
    handoff = (
        "A same-tool review completed while the independent tool was unavailable. "
        "Verify the current exact head independently; a clean unchanged result clears the "
        "human-merge-only taint."
    )
    prompt = with_review_assignment(
        review_record.input_ptr or "",
        depth=depth, reason=review_record.depth_reason or "taint-clearing independent review",
        axis=axis, change_author_tool=author or "", handoff=handoff)
    state = replace(
        prior,
        assignment=ReviewAssignment(
            depth, review_record.depth_reason or "taint-clearing independent review", axis),
        change_author_tool=author, sequence=prior.sequence + 1,
        cross_tool_covered=True, tainted=True, taint_cleared=False, handoff=handoff)
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="review",
        target=review_record.target, pool=reviewer_tool, complexity="deep",
        source=str(review_worktree(workdir, reviewer_tool, pr, _review_slug(review_record))),
        claim=True, input_ptr=prompt, builder_lineage=review_record.builder_lineage,
        builder_complexity=review_record.builder_complexity, round=review_record.round,
        conflict_round=review_record.conflict_round, review=state)


def survivor_review_submission(cfg, *, issue: int, slug: str, builder_tool: str,
                               head_sha: str, reviewer_tool: str, pr_number: int,
                               acceptance: str, review=None,
                               transfer_from: str | None = None,
                               supersede: bool = False):
    """Submit a fresh exact-head Review for an already-open autonomous survivor.

    A survivor has no completed coordinator predecessor to transfer from: its earlier chain has
    already reached an external PR boundary. This mapping therefore creates a cold Review that
    owns the newly-established visible claim directly, while preserving builder lineage and the
    retained branch/worktree naming needed by any later Revise.
    """
    from agentflow.coordinator import Submission
    from agentflow.loop import _surfaces_phrase, ui_surfaces
    from agentflow.review_policy import ReviewState
    from agentflow.reviewer import REVIEW_PROMPT, review_worktree, with_review_assignment

    if not head_sha or builder_tool not in BUILD_POOLS or reviewer_tool not in BUILD_POOLS:
        return None
    prompt = REVIEW_PROMPT.format(
        pr=pr_number, starting_sha=head_sha, acceptance=acceptance or "(none provided)",
        surfaces=_surfaces_phrase(ui_surfaces(cfg.workdir)))
    state = review or ReviewState(change_author_tool=builder_tool)
    assignment = state.assignment
    author = state.change_author_tool or builder_tool
    prompt = with_review_assignment(
        prompt,
        depth=assignment.depth, reason=assignment.reason, axis=assignment.axis,
        change_author_tool=author, handoff=state.handoff or "")
    state = replace(
        state, change_author_tool=author,
        reviewed_from_sha=state.reviewed_from_sha or head_sha,
        cross_tool_covered=reviewer_tool != author)
    return Submission(
        repo=cfg.repo, subject=str(issue), stage="review", target=head_sha,
        pool=reviewer_tool, complexity="deep",
        source=str(review_worktree(cfg.workdir, reviewer_tool, pr_number, slug)),
        claim=True, input_ptr=prompt, builder_lineage=builder_tool,
        review=state, transfer_from=transfer_from, supersede=supersede)


# ADR 0038: rebase, resolve, stay green, and preserve every compatible behavior. Genuine product
# competition is a decision handoff, not a reason to privilege whichever side merged first.
_CONFLICT_REVISE_FINDING = (
    "Rebase this PR's branch onto the current `origin/main` and resolve the merge conflicts, then "
    "keep the full test suite green. Preserve both sides wherever their intended behavior is "
    "compatible; neither side wins merely because it is newer. If they encode incompatible product "
    "intent, return the private two-option conflict uncertainty instead of choosing silently.")


def survivor_conflict_revise_submission(cfg, *, issue: int, slug: str, builder_tool: str,
                                        head_sha: str, pr_number: int, conflict_round: int):
    """Submit a conflict Revise for an open survivor whose re-rebase no longer applies (ADR 0038).

    Like a survivor re-review, a survivor has no completed coordinator predecessor to transfer from —
    its chain already reached the PR boundary — so this creates a Revise that owns the visible claim
    directly. It runs on the builder's own tool lineage in the retained PR-branch worktree (recovered
    the way any Revise recovers it), bound to the conflicting head SHA it must supersede, carrying the
    conflict round so it is budgeted apart from the finding-driven revise rounds and admitted ahead of
    cold build work. Returns ``None`` when the tool is unknown or the head SHA is missing.
    """
    from agentflow.coordinator import Submission
    from agentflow.loop import REVISE_PROMPT, _builder_worktree
    if not head_sha or builder_tool not in BUILD_POOLS:
        return None
    brief = REVISE_PROMPT.format(
        n=pr_number, repo=cfg.repo, findings=f"- {_CONFLICT_REVISE_FINDING}",
        surfaces="any user-facing surface")
    return Submission(
        repo=cfg.repo, subject=str(issue), stage="revise", target=head_sha,
        pool=builder_tool, complexity="deep", conflict_round=conflict_round,
        source=_builder_worktree(cfg, builder_tool, issue, slug), claim=True, input_ptr=brief,
        builder_lineage=builder_tool, builder_complexity="deep", continuation=True)


def _revise_builder_source(review_record):
    """The ``(build_worktree, pr_number)`` a Revise adopts from a blocking Review record. The
    revise reuses the *builder's* retained branch/worktree — ``.../<builder_lineage>/issue-<subject>
    -<slug>`` — which the review source (``.../<tool>-review/pr-<pr>-<slug>``) and the record's
    builder lineage together recover, so no second durable field is needed. ``None`` if unreadable."""
    review = WorktreeRef.parse(review_record.source)
    if review is None or review.kind is not WorktreeKind.REVIEW or not review_record.builder_lineage:
        return None
    build = WorktreeRef.for_build(
        review.workdir, review_record.builder_lineage, int(review_record.subject), review.slug)
    return build.path, review.number


def revise_submission(review_record, complexity, findings="", *, surfaces="", target_sha=""):
    """Translate a blocking Review into one Revise stage submission — the minimal facts the
    coordinator needs (ADR 0030). The revise adopts the original builder's retained PR branch and
    worktree, stays pinned to the builder's tool lineage and its original complexity, is bound to
    the reviewed head SHA it must supersede (its immutable target, together with the review's
    revise round — so a later blocking review, even one re-reviewing an unchanged head SHA, is a
    genuinely fresh revise stage), and assumes the Review's change claim. Pure: the mapping is the
    test surface (ADR 0020). Returns ``None`` if the builder worktree cannot be reconstructed or
    the reviewed SHA is missing."""
    from agentflow.coordinator import Submission
    from agentflow.loop import REVISE_PROMPT
    facts = _revise_builder_source(review_record)
    reviewed_head = target_sha or review_record.target
    if facts is None or not reviewed_head:
        return None
    build_worktree, pr_number = facts
    brief = REVISE_PROMPT.format(
        n=pr_number, repo=review_record.repo, findings=findings or "- (see review)",
        surfaces=surfaces or "any user-facing surface")
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="revise",
        target=reviewed_head, pool=review_record.builder_lineage, complexity=complexity,
        source=build_worktree, claim=True, input_ptr=brief,
        builder_lineage=review_record.builder_lineage, builder_complexity=complexity,
        round=review_record.round, transfer_from=review_record.identity)


def respond_submission(cfg, pr_number, branch, comment, target, baseline):
    """Translate one unanswered maintainer comment on an existing agentflow PR into a single
    Respond stage submission — the minimal facts the coordinator needs (ADR 0030). Respond adopts
    the change's *original tool lineage* and its retained PR branch/worktree — both recovered from
    the branch name (``agentflow/<tool>/issue-<n>-<slug>``), so capacity on the other pool can never
    silently switch this code-writing continuation — is bound to the maintainer comment it answers
    (its immutable ``target``, so a later comment opens a genuinely new Respond with a fresh budget),
    and holds the ``building`` change claim while it waits. Pure: the mapping is the test surface
    (ADR 0020). The durable prompt carries the PR head observed before Respond so completion can
    verify a requested push actually advanced that baseline. Returns ``None`` when the branch is
    not an agentflow PR branch or either immutable target is missing."""
    from agentflow.coordinator import Submission
    from agentflow.gate import respond_reply_disclaimer
    from agentflow.loop import _BRANCH_RE, RESPOND_PROMPT, _builder_worktree
    m = _BRANCH_RE.match(branch or "")
    if m is None or not target or not baseline:
        return None
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    brief = RESPOND_PROMPT.format(
        n=pr_number, comment=comment, baseline=baseline,
        disclaimer=respond_reply_disclaimer(str(target)))
    return Submission(
        repo=cfg.repo, subject=str(n), stage="respond", target=str(target),
        pool=tool, complexity="deep", source=_builder_worktree(cfg, tool, n, sl),
        claim=True, input_ptr=brief, builder_lineage=tool)


def revise_round_budget_remains(records, repo, subject) -> bool:
    """Whether the auto-revise product cap (ADR 0004's revise round, relaxed to ``MAX_REVISES``
    rounds by ADR 0020's convergence bail) still has room for this issue — fewer than
    ``MAX_REVISES`` *logical* Revise records exist for it, regardless of how many continuation
    attempts each one used. This keeps the per-stage continuation budget separate from the product
    loop: continuation attempts never reset or expand the round cap. Conflict Revises (ADR 0038) are
    counted apart and never spend this one; each conflicting head gets its own bounded stage. Pure —
    the test surface (ADR 0020)."""
    rounds = sum(1 for r in records
                 if r.stage == "revise" and not r.conflict_round
                 and r.repo == repo and str(r.subject) == str(subject))
    return rounds < MAX_REVISES


def conflict_revises_used(records, repo, subject) -> list:
    """The conflict Revise records already opened for this PR in its lifetime (ADR 0038), oldest
    first. They determine the next stable conflict-round identity but do not impose a lifetime cap:
    every genuinely new conflicting head gets another bounded Revise attempt. Includes retired
    records and stays separate from the finding-driven revise rounds. Pure."""
    conflicts = [r for r in records
                 if r.stage == "revise" and r.conflict_round
                 and r.repo == repo and str(r.subject) == str(subject)]
    return sorted(conflicts, key=lambda r: r.conflict_round)


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
    from agentflow.loop import BUILDING, DRAWING, RESOLVING, TRIAGING

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
        # The per-label listing reads number+updatedAt in one call, which the typed surface does
        # not offer, so it goes through the module's escape hatch. An unreadable listing (None)
        # defers this lane rather than clearing anything (ADR 0040).
        claimed = github.api(["issue", "list", "--repo", cfg.repo, "--state", "open",
                              "--label", label, "--json", "number,updatedAt", "--limit", "100"],
                             parse_json=True)
        if not isinstance(claimed, list):
            _log(f"{cfg.repo}: {lane} claim reconciliation deferred — GitHub unreadable")
            continue
        for issue in claimed:
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            try:
                updated = datetime.fromisoformat(
                    str(issue.get("updatedAt", "")).replace("Z", "+00:00")).timestamp()
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
    """The daemon's coordinator for all six logical stages (issues #103–#108).
    Its Build adapter verifies the real PR outcome and reuses the retained worktree; its Review
    adapter verifies a durable starting/final-head verdict and retains the detached bounded-fix
    checkout; its Revise adapter verifies a pushed revision on the same branch and reuses that
    retained worktree; its Respond adapter verifies the marked reply plus any pushed change on that
    same branch and releases the change claim on completion; its Mockup adapter verifies one
    pushed visual round and releases its drawing claim at the human-pick boundary. One
    :class:`StageRouter` dispatches each adapter call on the record's stage."""
    from agentflow import coordinated_intake
    intake = IntakeStageAdapter(
        worktree_reset=coordinated_intake.reset_worktree,
        apply_route=coordinated_intake.apply_route,
        claim_ready=coordinated_intake.intake_claim_ready,
        worktree_dispose=coordinated_intake.dispose_worktree,
        handoff=coordinated_intake.hold_intake)
    build = BuildStageAdapter(
        pr_exists=_pr_exists, worktree_ready=_worktree_ready, handoff=_hold_build,
        integration_collision=_integration_collision, main_head=_main_head)
    review = ReviewStageAdapter(
        verdict_ready=_verdict_ready,
        worktree_reset=lambda record: _review_worktree_reset(record, _log=_log),
        handoff=_park_pr,
        settle=_settle_review, prepare_settle=_prepare_review_settlement)
    revise = ReviseStageAdapter(
        revision_ready=_revision_ready, worktree_ready=_worktree_ready, handoff=_park_pr,
        uncertainty=_conflict_uncertainty_outcome)
    respond = RespondStageAdapter(
        reply_ready=_reply_ready, worktree_ready=_worktree_ready, handoff=_park_respond,
        settle=_settle_respond)
    mockup = MockupStageAdapter(
        outcome_ready=_mockup_outcome_ready,
        worktree_ready=lambda record: (_mockup_claim_ready(record)
                                       and _worktree_ready(record)),
        missing_context=_mockup_missing_context,
        handoff=_hold_mockup,
        settle=_settle_mockup)
    from agentflow import coordinated_converse
    converse = ConverseStageAdapter(
        reply_ready=coordinated_converse._reply_ready,
        adopt=coordinated_converse._adopt_turn,
        park=coordinated_converse._park_ask,
        worktree_ready=coordinated_converse._ask_worktree_ready)
    from agentflow import coordinated_research
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
        from agentflow import dispatch
        lane = {"intake": "triage", "build": "build", "review": "build", "revise": "build",
                "respond": "respond", "mockup": "mockup", "research": "research"}
        stage_lane = lane.get(record.stage, record.stage)
        return ReservationLimits(
            machine_ceiling=dispatch.MACHINE_CEILING,
            stage_cap=dispatch.STAGE_CAPS.get(stage_lane, 1),
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


def _pr_exists(record) -> bool:
    """Whether the expected PR is open for the record's owned branch (the Build outcome)."""
    parsed = _source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, _wt = parsed
    # A PR opened for this branch in *any* state (open/closed/merged) is the Build outcome, which
    # the typed open-only listing cannot express, so this goes through the module's escape hatch. An
    # unreadable read stays unknown — raise rather than mistake it for an absent PR (ADR 0040).
    data = github.api(["pr", "list", "--repo", record.repo, "--head", branch,
                       "--state", "all", "--json", "headRefName,url", "--limit", "1"],
                      parse_json=True)
    if not isinstance(data, list):
        raise RuntimeError(f"cannot verify Build PR outcome for {record.repo}:{branch}")
    return any(pr.get("headRefName") == branch for pr in data)


_COLLISION_MARK = "INTEGRATION-COLLISION"


def _main_head(record) -> str | None:
    """The current `origin/main` head SHA in the record's checkout, or None if unreadable. The
    coordinator compares it to the head a collision was recorded against to tell an identical
    retry (defer) from a main that has moved (one retry is warranted — issue #209)."""
    from agentflow.loop import _run
    parsed = _source_facts(record)
    if parsed is None:
        return None
    workdir, _branch, _wt = parsed
    _run(["git", "-C", workdir, "fetch", "--quiet", "origin", "main"])
    head = _run(["git", "-C", workdir, "rev-parse", "origin/main"])
    return head.stdout.strip() if head.returncode == 0 else None


def _integration_collision(record) -> str | None:
    """The `origin/main` head this Build reported an integration collision against this attempt, or
    None. The builder rebases onto `origin/main` before opening a PR and, on conflict, stops without
    resolving and posts a comment prefixed ``INTEGRATION-COLLISION`` (ADR 0009). A comment carrying
    that marker at its start and created after this attempt was admitted is the durable outcome; the
    recorded main head is what makes a subsequent identical retry detectable (issue #209)."""
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    comments = github.issue_comments(record.repo, number)
    if comments is None:
        return None
    if not any(_collision_comment(comment, record.started_at) for comment in comments):
        return None
    return _main_head(record)


def _collision_comment(comment: github.Comment, admitted_at: int) -> bool:
    """Whether one issue comment is this attempt's integration-collision report: its body starts
    with the ``INTEGRATION-COLLISION`` marker and it was created after the attempt was admitted, so
    a collision comment from an earlier attempt can never stand in for this one. A record from
    before admission times were stamped carries ``started_at == 0`` and keeps the unanchored
    behavior; an unparseable timestamp cannot be proven fresh and fails closed."""
    if not (comment.body or "").lstrip().startswith(_COLLISION_MARK):
        return False
    if not admitted_at:
        return True
    try:
        created = datetime.fromisoformat(
            (comment.created_at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return created > admitted_at


def _source_facts(record):
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.tool != record.pool or record.lineage != record.pool:
        return None
    if record.stage == "mockup":
        if ref.kind is not WorktreeKind.MOCKUP or str(ref.number) != str(record.subject):
            return None
    elif ref.kind is not WorktreeKind.BUILD:
        return None
    return ref.workdir, ref.branch, Path(record.source)


def _worktree_ready(record) -> bool:
    """Prepare the record's owned branch/worktree before admission (ADR 0030). An existing
    worktree is reused *as it is* — a continuation must keep its local changes, so it is never
    rebuilt. An absent Build worktree may start a new branch from ``origin/main``; a continuation
    stage may only recover the existing branch from its local or remote PR ref. Any git failure
    returns False, so admission is skipped with no permit and no attempt consumed."""
    from agentflow.loop import _run
    from agentflow.runner import ClaudeRunner, CodexRunner, _worktree_is_registered
    parsed = _source_facts(record)
    if parsed is None:
        return False
    workdir, branch, wt = parsed
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    if wt.exists():
        if not _worktree_is_registered(workdir, wt):
            return False
        current = _run(["git", "-C", str(wt), "branch", "--show-current"])
        if current.returncode != 0 or current.stdout.strip() != branch:
            return False
        try:
            runner.provision(wt)
        except subprocess.CalledProcessError:
            return False
        return True  # retained worktree — reuse across the continuation, never recreate it
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    have = _run(["git", "-C", workdir, "show-ref", "--quiet",
                 f"refs/heads/{branch}"]).returncode == 0
    add = ["git", "-C", workdir, "worktree", "add"]
    if have:
        add += [str(wt), branch]
    else:
        remote = _run(["git", "-C", workdir, "show-ref", "--quiet",
                       f"refs/remotes/origin/{branch}"]).returncode == 0
        if remote:
            add += ["-b", branch, str(wt), f"origin/{branch}"]
        elif record.stage in {"build", "mockup"}:
            add += ["-b", branch, str(wt), "origin/main"]
        else:
            return False
    if _run(add).returncode != 0:
        return False
    try:
        runner.provision(wt)
    except subprocess.CalledProcessError:
        return False
    return True


def _mockup_outcome_ready(record, obs) -> bool:
    """Prove one pushed variant round: committed artifacts/screenshots and one marked comment.

    The worktree is continuation state, never outcome authority. Completion requires its clean
    head to equal the remote branch, at least three branch-only HTML variants and screenshots,
    and exactly one durable issue comment that embeds every committed screenshot. A
    MISSING-CONTEXT comment is a human hold, not a completed visual round.
    """
    from agentflow.loop import MOCKUP_MARK, _issue_comments, _run

    parsed = _source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, wt = parsed
    if not wt.exists():
        return False
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    marked = [comment for comment in _issue_comments(record.repo, number)
              if MOCKUP_MARK in comment.get("body", "")]
    if len(marked) != 1 or "MISSING-CONTEXT:" in marked[0].get("body", ""):
        return False
    fetched = _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", "main", branch])
    if fetched.returncode != 0:
        return False
    local = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    remote = _run(["git", "-C", str(wt), "rev-parse", f"origin/{branch}"])
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if (local.returncode != 0 or remote.returncode != 0 or status.returncode != 0
            or not local.stdout.strip() or local.stdout.strip() != remote.stdout.strip()
            or status.stdout.strip()):
        return False
    changed = _run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=ACMRT",
                    "origin/main...HEAD"])
    if changed.returncode != 0:
        return False
    paths = [path for path in changed.stdout.splitlines() if path.startswith("mockups/")]
    variants = [path for path in paths if path.lower().endswith((".html", ".htm"))]
    screenshots = [path for path in paths
                   if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    body = marked[0].get("body", "")
    return (len(variants) >= 3 and len(screenshots) >= 3
            and all(path in body for path in screenshots))


def _mockup_missing_context(record) -> bool:
    """Whether this issue carries Mockup's deliberate durable MISSING-CONTEXT boundary."""
    from agentflow.loop import MOCKUP_MARK, _issue_comments

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    return any(MOCKUP_MARK in comment.get("body", "")
               and "MISSING-CONTEXT:" in comment.get("body", "")
               for comment in _issue_comments(record.repo, number))


def _mockup_claim_ready(record) -> bool:
    """Prove Mockup's visible drawing claim immediately before admission."""
    from agentflow.loop import DRAWING

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    labels = github.issue_labels(record.repo, number)
    if labels is None:
        return False
    return DRAWING in labels and "agentflow:needs-mockup" in labels


def _settle_mockup(record) -> str | None:
    """Retire one completed visual round at the human-pick boundary.

    The durable comment and pushed artifacts were already verified by the adapter. Settlement
    removes and proves the drawing claim, keeps ``needs-mockup`` in place for the maintainer's
    choice, and disposes the clean pushed worktree before coordinator ownership disappears.
    Every step is idempotent; an unreadable label or stubborn worktree retries next cycle.
    """
    from agentflow.loop import DRAWING
    from agentflow.runner import remove_worktree_if_safe

    parsed = _source_facts(record)
    if parsed is None:
        return None
    workdir, _branch, wt = parsed
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    github.remove_label(record.repo, number, DRAWING)
    # The proof read spans labels+url in one snapshot, which the typed surface does not offer, so
    # it goes through the module's escape hatch; an unreadable read (None) retries next cycle.
    state = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "labels,url"], parse_json=True)
    if not isinstance(state, dict):
        return None
    labels = {label.get("name") for label in state.get("labels", [])}
    if DRAWING in labels or "agentflow:needs-mockup" not in labels:
        return None
    if wt.exists() and not remove_worktree_if_safe(workdir, wt):
        return None
    if wt.exists():
        return None
    return state.get("url") or f"https://github.com/{record.repo}/issues/{number}"


def _hold_mockup(record) -> str | None:
    """Create Mockup's one issue-native handoff while preserving unfinished local work.

    MISSING-CONTEXT already is the durable stage-native handoff; exhaustion posts one stable
    marked comment. Both leave ``needs-mockup`` in place, release and prove the drawing claim,
    retain the worktree, and use a stable notification sequence across crash retries.
    """
    from agentflow.loop import DRAWING, MOCKUP_MARK, _MOCKUP_DISCLAIMER
    from agentflow.notify import notify

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    # Labels+comments+url in one snapshot doesn't fit the typed surface, so this read (and the
    # matching proof read below) goes through the module's escape hatch; None means unreadable.
    issue = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "labels,comments,url"], parse_json=True)
    if not isinstance(issue, dict):
        return None
    comments = issue.get("comments", [])
    marked = next((comment for comment in comments
                   if MOCKUP_MARK in comment.get("body", "")), None)
    missing = next((comment for comment in comments
                    if MOCKUP_MARK in comment.get("body", "")
                    and "MISSING-CONTEXT:" in comment.get("body", "")), None)
    proof = "<!-- agentflow-mockup-hold:" + hashlib.sha256(
        record.identity.encode()).hexdigest()[:24] + " -->"
    explanation = ("Mockup exhausted its continuation budget before completing the visual round. "
                   "The branch and local worktree are retained for a human to continue.")
    existing = marked or next((comment for comment in comments
                               if proof in comment.get("body", "")), None)
    if existing is None:
        body = f"{_MOCKUP_DISCLAIMER}\n{proof}\n\n{explanation}"
        if not github.comment(record.repo, number, body):
            return None
    elif missing is None and proof not in existing.get("body", ""):
        comment_id = existing.get("id")
        if not comment_id:
            return None
        body = f"{existing.get('body', '').rstrip()}\n\n{proof}\n\n{explanation}"
        mutation = ("mutation($id:ID!,$body:String!){updateIssueComment("
                    "input:{id:$id,body:$body}){issueComment{id}}}")
        # A GraphQL comment edit is one of the escape hatch's named exotic cases (ADR 0040).
        if github.api(["api", "graphql", "-f", f"query={mutation}",
                       "-f", f"id={comment_id}", "-f", f"body={body}"]) is None:
            return None
    # A single edit that both adds and removes a label is not on the typed surface, so the write
    # goes through the escape hatch; the proof read below is authoritative either way.
    github.api(["issue", "edit", str(number), "--repo", record.repo,
                "--add-label", "agentflow:needs-mockup", "--remove-label", DRAWING])
    state = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "labels,comments,url"], parse_json=True)
    if not isinstance(state, dict):
        return None
    labels = {label.get("name") for label in state.get("labels", [])}
    final_comments = state.get("comments", [])
    has_proof = any(
        proof in comment.get("body", "")
        or (MOCKUP_MARK in comment.get("body", "")
            and "MISSING-CONTEXT:" in comment.get("body", ""))
        for comment in final_comments)
    if DRAWING in labels or "agentflow:needs-mockup" not in labels or not has_proof:
        return None
    url = state.get("url") or f"https://github.com/{record.repo}/issues/{number}"
    sequence = "mockup-" + hashlib.sha256(record.identity.encode()).hexdigest()[:24]
    reason = ("missing context" if missing is not None
              else "continuation budget exhausted")
    if not notify("agentflow needs you", f"{record.repo} #{number}: Mockup held — {reason}",
                  url, sequence_id=sequence):
        return None
    return str(url)


def _hold_build(record) -> str | None:
    """Create and prove Build's exhaustion handoff without touching its worktree.

    The issue comment and ``needs-grilling`` label are the durable proof. A repeat after a
    daemon crash observes the same comment and does not notify again; the visible building
    claim is released only after the hold exists.
    """
    from agentflow.intake import apply_intake
    from agentflow.loop import BUILDING, held_build_result
    from agentflow.notify import notify

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    # Title+labels+comments in one snapshot (and labels+comments+url for the proof read below) does
    # not fit the typed surface, so both go through the module's escape hatch; None is unreadable.
    issue = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "title,labels,comments"], parse_json=True)
    if not isinstance(issue, dict):
        return None
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    status = ("could not rebase past a collision with newer changes on the main branch and stopped "
              "without resolving it" if record.hold_reason == "integration collision"
              else "continuation budget exhausted")
    result = held_build_result(status, f"the retained worktree `{record.source}`")
    already_posted = any(
        comment.get("body", "").strip() == result.body.strip()
        for comment in issue.get("comments", [])
    )
    apply_intake(record.repo, number, issue.get("title", ""), labels, result)
    github.remove_label(record.repo, number, BUILDING)

    state = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "labels,comments,url"], parse_json=True)
    if not isinstance(state, dict):
        return None
    final_labels = {label.get("name") for label in state.get("labels", [])}
    has_comment = any(
        comment.get("body", "").strip() == result.body.strip()
        for comment in state.get("comments", [])
    )
    if "agentflow:needs-grilling" not in final_labels or BUILDING in final_labels or not has_comment:
        return None
    url = state.get("url") or f"https://github.com/{record.repo}/issues/{number}"
    headline = ("Build hit an integration collision" if record.hold_reason == "integration collision"
                else "Build continuation budget exhausted")
    if not already_posted:
        notify("agentflow needs you", f"{record.repo} #{number}: {headline}", url)
    return str(url)


# --- Review stage: verdict outcome, bounded-fix checkout, PR park (live; ADR 0020/0047) ---

def _verdict_ready(record, obs) -> bool:
    """The Review outcome is a parsed verdict anchored to its exact starting SHA.

    A reviewer may push bounded fixes, but the captured final message must still name
    ``record.target`` as ``reviewed_sha`` and name the fully re-reviewed post-fix head as
    ``final_sha``. Settlement independently requires the live PR head to equal that final SHA.
    """
    from agentflow.reviewer import parse_verdict
    if not record.target:
        return False
    verdict = parse_verdict(
        obs.final_message or "", expected_sha=record.target,
        expected_depth=record.review_depth, expected_axis=record.review_axis,
        expected_author=record.change_author_tool)
    if not verdict.parsed:
        return False
    if record.review_axis == "fix" and not verdict.pushed_sha:
        from agentflow.review_policy import ReviewAction, ReviewState
        review = ReviewState.from_record(record)
        if review is None or any(item.action is ReviewAction.FIX for item in review.findings):
            return False
    if not _review_follow_ups_valid(record, verdict):
        return False
    if (record.review_tainted and not record.review_taint_cleared
            and record.cross_tool_covered and not verdict.pushed_sha
            and record.review_axis in {"combined", "standards"}):
        from agentflow.review_policy import ReviewAction, ReviewState, merge_findings
        review = ReviewState.from_record(record)
        actions = (() if review is None
                   else merge_findings(review.findings, verdict.actions))
        if (review is not None and verdict.clean
                and not any(item.action in {ReviewAction.FIX, ReviewAction.ASK}
                            for item in actions)):
            # This mutation is persisted by the coordinator with the verified completed record.
            # Product, fix, pushed, or unresolved passes keep taint until the final clean
            # independent combined/standards result.
            record.review_taint_cleared = True
    return True


def _review_follow_ups_valid(record, verdict) -> bool:
    """Validate structured follow-up evidence against this repository's live issue tracker."""
    from agentflow.review_policy import validate_follow_ups

    if not verdict.follow_ups:
        return True

    def view(number):
        return github.api(["issue", "view", str(number), "--repo", record.repo,
                           "--json", "number,url"], parse_json=True)

    def search(query):
        return github.api(["issue", "list", "--repo", record.repo, "--state", "all",
                           "--search", query, "--json", "number", "--limit", "100"],
                          parse_json=True)

    return validate_follow_ups(
        record.repo, verdict.follow_ups, issue_view=view, issue_search=search)


# Consecutive review-prepare failures per source path, so a genuinely stuck
# review (one that never checks out) is surfaced periodically instead of silently
# no-op'ing admission every cycle. Process-local — a daemon restart re-arms it.
_REVIEW_PREPARE_FAILURES: dict[str, int] = {}


def _review_worktree_reset(record, _log=None) -> bool:
    """Prepare Review's detached, writable exact-head checkout (ADR 0030, amended).

    The first attempt starts at the immutable target. A continuation reuses the registered
    checkout exactly as it is so an interrupted review keeps its fixes; it is never reset or
    cleaned. A fresh logical review may reuse a clean prior checkout and reset it to its own
    target. Any git failure skips admission without consuming a permit or attempt.
    """
    from agentflow.loop import _run
    from agentflow.runner import ClaudeRunner, CodexRunner
    facts = _review_source_facts(record)
    if facts is None or not record.target:
        return False
    workdir, _pr = facts
    wt = Path(record.source)
    from agentflow.runner import _worktree_is_registered
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    try:
        if wt.exists() and _worktree_is_registered(workdir, wt) and getattr(record, "attempts", 0):
            runner.provision(wt)
            _REVIEW_PREPARE_FAILURES.pop(record.source, None)
            return True  # continuation: preserve committed, dirty, and untracked review work
        wt.parent.mkdir(parents=True, exist_ok=True)
        runner.prepare_worktree_detached(workdir, record.target, wt)
        runner.provision(wt)
    except subprocess.CalledProcessError:
        fails = _REVIEW_PREPARE_FAILURES[record.source] = \
            _REVIEW_PREPARE_FAILURES.get(record.source, 0) + 1
        # Surface on the 2nd consecutive failure, then re-remind every 10th, so a
        # long-stuck review keeps a periodic breadcrumb instead of a single line.
        if _log is not None and fails >= 2 and (fails - 2) % 10 == 0:
            _log(f"{record.repo}: review checkout keeps failing at {record.source} — "
                 "admission is stuck; the PR will not be reviewed until it is cleared")
        return False
    _REVIEW_PREPARE_FAILURES.pop(record.source, None)
    return True


def _review_source_facts(record):
    """The ``(workdir, pr_number)`` a review worktree encodes, or ``None``. The review source is
    ``.../<tool>-review/pr-<pr>-<slug>``, so the PR number is recoverable for the park handoff
    without a second durable field."""
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.kind is not WorktreeKind.REVIEW:
        return None
    return ref.workdir, ref.number


def _review_slug(record) -> str:
    """The slug in a Review record's detached checkout path (``.../pr-<pr>-<slug>``), reused to
    name the finished worktree so a review reads as the same issue's pair on disk. ``""`` when the
    source is not a well-formed review path."""
    ref = WorktreeRef.parse(record.source)
    return ref.slug if ref is not None else ""


def _park_pr_number(record) -> int | None:
    """The PR number to park for a Review or a Revise record. A Review encodes it directly in its
    detached review worktree path (``.../<tool>-review/pr-<pr>-<slug>``); a Revise instead owns the
    *builder's* branch/worktree (``.../<tool>/issue-<subject>-<slug>``, no PR number), so the open PR
    for that branch is looked up from GitHub. Returns ``None`` when it cannot be resolved, so the
    park handoff stays pending and retries rather than proving a park it never made."""
    facts = _review_source_facts(record)
    if facts is not None:
        return facts[1]
    parsed = _source_facts(record)
    if parsed is None:
        return None
    _workdir, branch, _wt = parsed
    # A Revise's builder branch PR may already be closed/merged, so this spans all states — not the
    # typed open-only listing — and goes through the module's escape hatch; None stays unresolved.
    prs = github.api(["pr", "list", "--repo", record.repo, "--head", branch, "--state", "all",
                      "--json", "number", "--limit", "1"], parse_json=True)
    if not isinstance(prs, list):
        return None
    return prs[0].get("number") if prs else None


def _park_context(record, verdict, *, reason: str, missing: str):
    """Build the concrete two-section park contract from durable stage state."""
    from agentflow.gate import ParkContext

    actions = tuple(verdict.actions) if verdict is not None else ()
    uncertainty = verdict.uncertainty if verdict is not None else None
    options = (tuple(uncertainty.options) if uncertainty is not None else (
        "Clarify the affected behavior and resume this retained stage on the same PR.",
        "Close the PR and leave the currently shipped application behavior unchanged.",
    ))
    locations = tuple(dict.fromkeys(
        f"{item.file}:{item.line}" if item.line else item.file
        for item in actions if item.file))
    if not locations:
        locations = (f"PR #{_park_pr_number(record) or '?'} exact head {record.target or 'unknown'}",)
    checks = tuple(verdict.checks) if verdict is not None and verdict.checks else (
        "No completed check proof was recorded before the stage stopped.",)
    conflicts = (
        f"Missing guidance: {uncertainty.missing_guidance}. "
        f"Agent recommendation: {uncertainty.recommendation}."
        if uncertainty is not None else missing)
    return ParkContext(
        behavior=f"The requested PR behavior {reason}.",
        options=options,
        consequences=(
            "Resuming can ship the intended change after the named uncertainty is resolved; "
            "closing preserves the application's current behavior."),
        recommendation=(uncertainty.recommendation if uncertainty is not None
                        else "Resolve the named uncertainty, then resume the retained stage."),
        locations=locations, conflicts=conflicts, checks=checks,
        retained_work=f"`{record.source}` at `{record.target or 'unknown head'}`",
        next_action=(
            "Record the chosen behavior, then resume this exact retained stage against the same PR."))


def _park_proof_marker(record, reason: str) -> str:
    """One low-noise current-park proof scoped to this durable stage decision."""
    digest = hashlib.sha256(f"{record.identity}:{reason}".encode()).hexdigest()[:20]
    return f"agentflow-park:{digest}"


def _park_pr(record) -> str | None:
    """Park the reviewed PR for a human and notify once (ADR 0028's exhaustion table). Serves both
    the Review-native park and the Revise-native park — Revise owns a builder worktree, so the PR is
    resolved by branch (:func:`_park_pr_number`). The crash-safe post-once → prove → notify-once
    recipe is the shared :class:`DurableHandoff` envelope (ADR 0042): the park comment is the durable
    proof, so a repeat after a daemon crash observes the same comment and neither parks nor pings
    again. Live orchestration; exercised with faked GitHub reads in ``tests/test_revise_tracer.py``."""
    from agentflow.gate import park
    from agentflow.handoff import DurableHandoff, Notification, Subject
    pr = _park_pr_number(record)
    if pr is None:
        return None
    if record.stage == "review" and record.review_axis == "decision":
        reason = "needs the maintainer to choose between competing product behaviors"
        missing = "Both tools remain unsure. " + (
            f"Exact recorded decision: {record.review_uncertainty}"
            if record.review_uncertainty else "The private decision record is unavailable.")
        notice = "conflict decision needs your judgment"
    elif record.stage == "review":
        reason = "exhausted its review budget without a durable verdict"
        missing = "No review was completed — do not treat this as a clean review."
        notice = "review parked for your action"
    elif record.conflict_round:
        reason = "could not safely complete and verify the merge-conflict resolution"
        missing = "The conflict resolution did not reach a verified pushed revision."
        notice = "conflict resolution needs your judgment"
    else:
        reason = "could not complete and verify the requested revision"
        missing = "The requested revision did not reach a verified pushed outcome."
        notice = "revision parked for your action"
    marker = _park_proof_marker(record, reason)
    return DurableHandoff().hand_off(
        Subject(repo=record.repo, number=pr, kind="pr"),
        identity=record.identity, stage=record.stage,
        marker=marker,
        action=lambda: park(
            record.repo, pr, None, reason=reason, missing_outcome=missing,
            context=_park_context(record, None, reason=reason, missing=missing),
            proof_marker=marker),
        notification=Notification(
            "agentflow needs you", f"{record.repo} PR #{pr}: {notice}"))


def _review_pr_facts(record) -> dict | None:
    """The PR's current head and state, or ``None`` when GitHub is unreadable."""
    facts = _review_source_facts(record)
    if facts is None:
        return None
    _workdir, pr = facts
    # Head SHA + state in one snapshot doesn't fit the typed surface, so this read goes through the
    # module's escape hatch; None means GitHub was unreadable (ADR 0040).
    data = github.api(["pr", "view", str(pr), "--repo", record.repo,
                       "--json", "headRefOid,state"], parse_json=True)
    if not isinstance(data, dict):
        return None
    head, state = data.get("headRefOid"), data.get("state")
    if not isinstance(head, str) or not head or state not in {"OPEN", "CLOSED", "MERGED"}:
        return None
    return {"head": head, "state": state}


def _review_pr_head(record) -> str | None:
    facts = _review_pr_facts(record)
    return facts["head"] if facts is not None else None


def _review_depth_escalated(record, verdict) -> bool:
    from agentflow.review_policy import ReviewDepth

    order = {ReviewDepth.FOCUSED: 0, ReviewDepth.TARGETED: 1, ReviewDepth.FULL: 2}
    return order[verdict.depth] > order[ReviewDepth(record.review_depth)]


def _prepare_review_settlement(record) -> bool:
    """Perform slow CI observation outside the coordinator store transaction.

    Only an exact-head, independent, clean autonomous review can merge and therefore needs the
    bounded CI wait. Every park/revise path is immediately ready for its short transactional
    finalization. Settlement rechecks both head and CI immediately before the merge.
    """
    from agentflow.gate import ci_is_green
    from agentflow.loop import repo_profile

    facts = _review_source_facts(record)
    if facts is None:
        return False
    workdir, _pr = facts
    verdict = _review_verdict(record)
    if (verdict.change_author_tool
            and (_review_depth_escalated(record, verdict)
                 or verdict.pushed_sha or verdict.uncertainty is not None
                 or record.review_axis == "product"
                 or any(item.action.value == "fix_before_completion"
                        for item in verdict.actions))):
        return True  # the private successor opener owns this non-terminal pass
    if (not verdict.clean or repo_profile(workdir) != "autonomous"
            or not record.auto_merge_allowed):
        return True
    if _review_pr_head(record) != (verdict.final_sha or record.target):
        return True  # short settlement parks the stale exact-head verdict
    _REVIEW_CI_OBSERVED[record.identity] = ci_is_green(record.repo, facts[1])
    return True


def _park_review_settlement(record, verdict, workdir: str, pr: int,
                            *, reason: str, autonomous: bool) -> str | None:
    """Idempotently park, prove, clean up, and notify one completed Review.

    The park-comment-plus-notify-once envelope is the shared :class:`DurableHandoff` recipe (ADR
    0042); the Review-specific bookkeeping — disposing the finished detached checkout and recording
    the parked ratchet — stays here and runs after the handoff confirms (returns non-``None``).
    """
    from agentflow.gate import park
    from agentflow.handoff import DurableHandoff, Notification, Subject
    from agentflow.loop import _finish_review
    from agentflow import ratchet

    marker = _park_proof_marker(record, reason)
    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=pr, kind="pr"),
        identity=record.identity, stage="review",
        marker=marker,
        action=lambda: park(
            record.repo, pr, verdict, reason=reason,
            context=_park_context(
                record, verdict, reason=reason,
                missing=verdict.detail or "Grounded review actions remain unresolved."),
            proof_marker=marker),
        notification=Notification(
            "agentflow needs you", f"{record.repo} PR #{pr}: reviewed — your action"))
    if url is None:
        return None
    slug = _review_slug(record)
    _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir), record.pool, pr, slug)
    if autonomous:
        ratchet.record_once(record.repo, "parked", record.identity)
    return url


def _settle_review(record) -> str | None:
    """Consume a parsed exact-head verdict through the established repository merge policy."""
    from agentflow import ratchet
    from agentflow.gate import (MergeDecision, ci_is_green, decide_merge,
                                post_clean_review_summary, reply_pending, squash_merge,
                                ui_evidence_gap)
    from agentflow.loop import (_UI_GAP_REASON, _finish_review, _pr_comments,
                                repo_profile, ui_surfaces)

    facts = _review_source_facts(record)
    if facts is None:
        return None
    workdir, pr = facts
    verdict = _review_verdict(record)
    if (verdict.change_author_tool
            and (_review_depth_escalated(record, verdict)
                 or verdict.pushed_sha or verdict.uncertainty is not None
                 or record.review_axis == "product"
                 or any(item.action.value == "fix_before_completion"
                        for item in verdict.actions))):
        return None  # reviewer-authored/axis/decision work must transfer privately first
    if verdict.blocking:
        return None  # durable opener transfers this claim to Revise
    comments = _pr_comments(record.repo, pr)
    if comments is None:
        return None
    profile = repo_profile(workdir)
    autonomous = profile == "autonomous"
    pr_facts = _review_pr_facts(record)
    if pr_facts is None:
        return None
    head = pr_facts["head"]
    if pr_facts["state"] == "MERGED":
        if (verdict.change_author_tool
                and not post_clean_review_summary(record.repo, pr, verdict)):
            return None
        slug = _review_slug(record)
        _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir),
                       record.pool, pr, slug, merged=True)
        ratchet.record_once(
            record.repo, ratchet.CLEAN_MERGE if record.round == 0 else "merge_after_revise",
            record.identity)
        github.remove_label(record.repo, record.subject, "ready-for-agent")
        return f"https://github.com/{record.repo}/pull/{pr}"
    reviewed_head = verdict.final_sha or record.target
    if head != reviewed_head:
        # The head moved after this clean verdict was recorded (a maintainer rebase, a manual push,
        # a conflict fix). Do not park a superseded head: leave the completed record in place so the
        # diverged-review reconciler opens one bounded successor Review at the live head, or parks
        # once when the auto-revise rounds are spent (#208).
        return None

    surfaces = ui_surfaces(workdir)
    ui_gap = ui_evidence_gap(record.repo, pr, surfaces)
    if not autonomous:
        if verdict.clean and not ui_gap:
            if not post_clean_review_summary(record.repo, pr, verdict):
                return None
            slug = _review_slug(record)
            _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir),
                           record.pool, pr, slug)
            return f"https://github.com/{record.repo}/pull/{pr}"
        reason = (_UI_GAP_REASON if ui_gap
                  else f"has unresolved review actions in a `{profile}` repository")
        return _park_review_settlement(
            record, verdict, workdir, pr, reason=reason, autonomous=False)
    if record.review_tainted and not record.review_taint_cleared:
        if verdict.clean and not ui_gap:
            if not post_clean_review_summary(record.repo, pr, verdict):
                return None
            slug = _review_slug(record)
            _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir),
                           record.pool, pr, slug)
            return f"https://github.com/{record.repo}/pull/{pr}"
        reason = _UI_GAP_REASON if ui_gap else "forced same-tool review remains unresolved"
        return _park_review_settlement(
            record, verdict, workdir, pr, reason=reason, autonomous=True)
    if not verdict.clean:
        return _park_review_settlement(
            record, verdict, workdir, pr,
            reason="review did not produce an actionable clean verdict", autonomous=True)
    pending_reply = reply_pending(comments)
    if not record.auto_merge_allowed or ui_gap or pending_reply:
        reason = _UI_GAP_REASON if ui_gap else "could not be auto-merged after review"
        return _park_review_settlement(
            record, verdict, workdir, pr, reason=reason, autonomous=True)

    # CI already completed in prepare_completed, outside SQLite's write transaction. Recheck it
    # once without polling, together with the exact head, immediately before merge.
    ci_green = _REVIEW_CI_OBSERVED.pop(record.identity, None)
    if ci_green is None:
        return None
    if not ci_green:
        return _park_review_settlement(
            record, verdict, workdir, pr,
            reason="CI did not complete successfully within the review settlement window",
            autonomous=True)
    decision = decide_merge(
        verdict=verdict, ci_green=True, reviewer_tool=record.pool,
        builder_tool=record.change_author_tool or record.builder_lineage or "",
        revises_used=record.round,
        ui_evidence_missing=False, reply_pending=False)
    if decision is not MergeDecision.MERGE:
        return _park_review_settlement(
            record, verdict, workdir, pr,
            reason="could not be auto-merged after review", autonomous=True)
    if _review_pr_head(record) != reviewed_head:
        return None
    if not ci_is_green(record.repo, pr, timeout=0, interval=0):
        return None
    if not squash_merge(record.repo, pr):
        return _park_review_settlement(
            record, verdict, workdir, pr,
            reason="could not be squash-merged (branch protection, conflict, or transient error)",
            autonomous=True)
    if (verdict.change_author_tool
            and not post_clean_review_summary(record.repo, pr, verdict)):
        return None
    slug = _review_slug(record)
    _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir),
                   record.pool, pr, slug, merged=True)
    ratchet.record_once(
        record.repo, ratchet.CLEAN_MERGE if record.round == 0 else "merge_after_revise",
        record.identity)
    github.remove_label(record.repo, record.subject, "ready-for-agent")
    return f"https://github.com/{record.repo}/pull/{pr}"


def _park_respond(record) -> str | None:
    """Create Respond's record-specific park proof and idempotent phone notification.

    A generic Review park may already be present on the PR, so this uses its own target-scoped
    marker to prove that this exact maintainer-comment target exhausted its Respond budget. The
    crash-safe post-once → prove → notify-once recipe is the shared :class:`DurableHandoff` envelope
    (ADR 0042): it derives the stable ntfy sequence id, so a replay across the window between posting
    the durable comment and recording completion locally replaces the same notification rather than
    multiplying it.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject

    pr = _park_pr_number(record)
    if pr is None or not record.target:
        return None
    proof = f"<!-- agentflow-respond-park-target:{record.target} -->"
    body = ("> *agentflow: parked for human review (Respond).*\n"
            f"{proof}\n\n"
            "## Maintainer decision needed\n\n"
            f"Affected behavior: the requested application change in maintainer comment "
            f"`{record.target}` was not fully answered and proved.\n\n"
            "Options:\n"
            "- Restate the requested behavior and resume this retained Respond stage.\n"
            "- Withdraw the request and keep the PR's current behavior.\n\n"
            "Consequences: resuming may change the PR; withdrawing leaves its current pushed "
            "behavior unchanged.\n\n"
            "Recommendation: restate the exact desired behavior, then resume this same target.\n\n"
            "## Agent handoff\n\n"
            f"Code locations: PR #{pr} at `{record.target}`; inspect the retained branch diff.\n\n"
            "Conflicting changes or unresolved facts: the targeted reply/pushed outcome was not "
            "proved before the continuation budget ended.\n\n"
            "Checks: no complete targeted reply + exact pushed-head proof was recorded.\n\n"
            f"Retained work: `{record.source}` at `{record.target}`.\n\n"
            "Exact next action: record the chosen behavior, then resume this Respond target on "
            "the same PR.")
    return DurableHandoff().hand_off(
        Subject(repo=record.repo, number=pr, kind="pr"),
        identity=record.identity, stage="respond",
        marker=proof,
        action=lambda: github.pr_comment(record.repo, pr, body),
        notification=Notification(
            "agentflow needs you",
            f"{record.repo} PR #{pr}: Respond parked for maintainer comment {record.target}"))


# --- Revise stage: pushed-revision outcome on the retained branch (live; ADR 0020) ------

_CONFLICT_UNCERTAINTY_PREFIX = "conflict-uncertainty:"


def _conflict_uncertainty_outcome(record, obs) -> str | None:
    """Turn a resolver's private structured ambiguity into the Revise stage outcome."""
    import json
    from agentflow.review_policy import conflict_uncertainty_from_message

    if not record.conflict_round:
        return None
    value = conflict_uncertainty_from_message(obs.final_message or "")
    if value is None:
        return None
    return _CONFLICT_UNCERTAINTY_PREFIX + json.dumps({
        "options": list(value.options), "missing_guidance": value.missing_guidance,
        "recommendation": value.recommendation}, sort_keys=True)

def _worktree_owns_head(wt, head: str) -> bool:
    """Whether the retained worktree durably owns the pushed remote ``head``: its checked-out
    ``HEAD`` equals that SHA and its tree is clean (no dirty tracked file, staged change, or
    untracked new file). Read after fetching the branch, this proves the reviser's own local state
    and the pushed branch agree — a stale or third-party push cannot satisfy it. Any failed read
    fails closed."""
    from agentflow.loop import _run
    local = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    if local.returncode != 0 or local.stdout.strip() != head:
        return False
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    return status.returncode == 0 and not status.stdout.strip()


def _revision_ready(record, obs) -> bool:
    """The Revise outcome is a verified pushed revision **or** the required durable non-code proof,
    read from GitHub independently of how the reviser exited (ADR 0028, issue #105):

    - a pushed revision: the PR branch head SHA has moved off the reviewed SHA the revise was opened
      against (``record.target``). A head that *descends from* the reviewed SHA is such a revision;
      so is a rewritten head (e.g. a rebase a finding demanded) that the retained builder worktree
      owns — its ``HEAD`` equals the pushed head and its tree is clean after fetching the branch. A
      force-push back to a commit that is an *ancestor* of the reviewed SHA is a rewind and never
      counts, even when the worktree sits on it; or
    - the required non-code proof: a durable agentflow-marked PR comment carrying attached evidence
      (e.g. a before/after screenshot), the way a finding that asks to *show* something is answered
      without a code change. The comment must be *created after this revise record was submitted*
      (its durable ``created_at``, which survives a restart) — a marked screenshot left during the
      Build or a prior revise round predates this round and cannot complete it (issue #118).

    A branch whose head still equals the reviewed SHA and carries no such evidence comment pushed
    and proved nothing, so it stays incomplete and continues. Live orchestration; exercised with
    faked GitHub reads in ``tests/test_revise_tracer.py``."""
    from agentflow.loop import _pr_comments, _run
    parsed = _source_facts(record)
    if parsed is None or not record.target:
        return False
    _workdir, branch, wt = parsed
    prs = github.list_open_prs(record.repo, head=branch)
    if prs is None:
        raise RuntimeError(f"cannot verify Revise outcome for {record.repo}:{branch}")
    if not prs:
        return False
    head = prs[0].head_ref_oid
    if head and head != record.target:
        # A moved head is the pushed revision when it descends from the reviewed SHA, or — when the
        # history was rewritten (a rebase the finding asked for) — when the retained builder worktree
        # proves the reviser owns that exact head. The worktree answers both (fetching the branch so
        # the remote head is local). A rewind to an *ancestor* of the reviewed SHA still never counts,
        # and with no worktree to ask, the head comparison stands alone.
        if not wt.exists():
            return True
        _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", branch])
        if _run(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                 record.target, head]).returncode == 0:
            return True
        rewound = _run(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                        head, record.target]).returncode == 0
        if not rewound and _worktree_owns_head(wt, head):
            return True
    # No new code, but an evidence-only revision still completes on its durable non-code proof: an
    # agentflow-authored PR comment (our marker, never the maintainer's) that attaches evidence
    # and postdates this revise round.
    comments = _pr_comments(record.repo, prs[0].number)
    if comments is None:
        return False
    return any(_round_evidence(c, record.created_at) for c in comments)


def _round_evidence(comment: dict, opened_at: int) -> bool:
    """Whether one PR comment is the current revise round's durable non-code proof: agentflow-
    marked, carrying attached image evidence, and created strictly after the revise record's
    durable submission time — so evidence left before this round opened can never complete it,
    however many times it is re-observed (issue #118). A record from before submission times were
    stamped carries ``created_at == 0`` and keeps the unanchored behavior; a comment whose
    ``createdAt`` is missing or unparseable cannot be proven to postdate the round, so it fails
    closed."""
    from agentflow.gate import PR_MARK, has_image_evidence
    body = comment.get("body", "") or ""
    if PR_MARK not in body or not has_image_evidence(body):
        return False
    if not opened_at:
        return True
    try:
        created = datetime.fromisoformat(
            str(comment.get("createdAt", "") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        created = None
    return created is not None and created > opened_at


# --- Respond stage: posted-reply outcome on the retained PR branch (live; ADR 0020) ------

def _untracked_respond_scratch(path: str) -> bool:
    """Classify conventional temporary artifacts that are outside PR state.

    This is path semantics, not a one-filename exception: nested screenshot runners, test reports,
    cache/temp directories, editor swap files, logs, and trace artifacts are disposable local
    proof machinery. Unknown untracked files remain relevant because they may be requested source.
    """
    normalized = path.strip().strip('"')
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts:
        return False
    basename = parts[-1].lower()
    temporary_dirs = {
        "tmp", "temp", ".tmp", ".cache", "test-results", "playwright-report",
        "coverage", ".coverage", "__pycache__",
    }
    return (
        any(part.lower() in temporary_dirs for part in parts[:-1])
        or re.fullmatch(r"run-shot(?:-[^/]*)?\.(?:sh|js|mjs|json)", basename) is not None
        or basename in {".ds_store", "thumbs.db"}
        or basename.endswith((".tmp", ".temp", ".log", ".trace", ".swp", ".swo"))
    )


def _reply_ready(record, obs) -> bool:
    """The Respond outcome is the marked agentflow reply to the maintainer comment this record
    answers, plus any branch change verified pushed (ADR 0028, issue #107) — read from GitHub
    independently of how the responder exited:

    - the reply: a marked agentflow comment names this record's immutable maintainer-comment
      target, so another reply or generic agentflow comment cannot satisfy it; and
    - verified pushed: when the reply marks a change, its SHA must be the current pushed head (not the
      baseline — a no-op push cannot count), and the retained PR-branch worktree holds no commit
      absent from that head *and* no uncommitted change at all. A responder that committed a small fix
      but never pushed it left the remote branch unchanged; one that edited a file but never committed
      it (a modified tracked file, a staged change, or an untracked new file) never turned that change
      into a pushed commit either. Both leave the stage incomplete so it continues on that same
      retained worktree. History rewritten by a rebase (the fix a conflict demands) is accepted: the
      marked head plus the clean owned worktree sitting on it prove the reviser's work either way, so
      the baseline need not be an ancestor of the pushed head.

    A record without that exact targeted reply stays incomplete. Live orchestration; exercised
    through the Coordinator/Respond adapter seam in ``tests/test_respond_tracer.py``."""
    from agentflow.gate import respond_reply_change, respond_reply_posted
    from agentflow.loop import _pr_comments, _run
    parsed = _source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, wt = parsed
    pr = _open_pr_for_branch(record.repo, branch)
    if pr is None:
        return False
    comments = _pr_comments(record.repo, pr.number)
    if comments is None or not respond_reply_posted(comments, record.target or ""):
        return False   # no durable reply bound to this record's maintainer-comment target
    change = respond_reply_change(comments, record.target or "")
    baseline_match = re.search(r"agentflow-respond-baseline:([^\s>]+)", record.input_ptr or "")
    baseline = baseline_match.group(1) if baseline_match is not None else ""
    if not change or not baseline:
        return False
    # A reply exists. The owned worktree is mandatory evidence: without it there is no way to
    # prove that a requested branch change was either pushed or never left locally. Fail closed and
    # let preparation recover the PR branch before another attempt.
    if not wt.exists():
        return False
    head = pr.head_ref_oid or ""
    if change != "none" and (change == baseline or change != head):
        return False
    fetched = _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", branch])
    if fetched.returncode != 0:
        return False
    ahead = _run(["git", "-C", str(wt), "rev-list", "--count", f"{head}..HEAD"])
    if not head or ahead.returncode != 0 or ahead.stdout.strip() not in ("", "0"):
        return False
    # Tracked/staged changes and arbitrary untracked files are relevant retained branch work and
    # still block. Ignore only the known root-level screenshot runner regression: broader treatment
    # of untracked files as scratch could retire a requested new source file before it is pushed.
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if status.returncode != 0:
        return False
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        if not line.startswith("?? ") or not _untracked_respond_scratch(line[3:]):
            return False
    return True


def _settle_respond(record) -> str | None:
    """Release Respond's change claim once the reply is durable, retiring the record with no
    successor and no human handoff (issue #107). Drops the ``building`` claim label so the answered
    PR returns to the normal merge pipeline, proves the label is gone, then returns the PR (or issue)
    URL as the durable proof. Idempotent and crash-safe: removing an already-removed label is a
    no-op, so a repeat re-proves the same release. Returns ``None`` when the issue is unreadable or
    the label is still present, so settlement retries next cycle rather than retiring over a claim it
    never released. Live orchestration, not unit-tested (ADR 0020)."""
    from agentflow.loop import BUILDING
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    github.remove_label(record.repo, number, BUILDING)
    # Labels+url in one snapshot isn't on the typed surface, so this proof read goes through the
    # module's escape hatch; None means unreadable — retry rather than retire over a live claim.
    state = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "labels,url"], parse_json=True)
    if not isinstance(state, dict):
        return None
    if BUILDING in {label.get("name") for label in state.get("labels", [])}:
        return None   # the claim label is still present — retry rather than retire over it
    pr = _park_pr_number(record)
    if pr is not None:
        return f"https://github.com/{record.repo}/pull/{pr}"
    return state.get("url") or f"https://github.com/{record.repo}/issues/{number}"


def _open_pr_for_branch(repo: str, branch: str) -> github.PrRow | None:
    """The one open PR for the owned branch — its number and head SHA — or ``None`` when there is
    none or the read fails. The shared lookup behind every claim-transfer opener; a ``None`` leaves
    the completed record still claimed, so the next reconcile pass retries the transfer rather than
    stranding it."""
    prs = github.list_open_prs(repo, head=branch)
    if not prs:
        return None
    return prs[0]


def _review_context(record) -> tuple[str, str] | None:
    """The issue-anchored acceptance brief and declared UI surfaces for a Review."""
    from agentflow.loop import _surfaces_phrase, ui_surfaces

    parts = _build_source_parts(record)
    if parts is None:
        return None
    workdir, _slug = parts
    acceptance = record.input_ptr if record.stage == "build" and record.input_ptr else None
    if acceptance is None:
        acceptance = github.issue_body(record.repo, record.subject)
        if acceptance is None:   # unreadable stays unknown — the opener refuses rather than guesses
            return None
    return acceptance, _surfaces_phrase(ui_surfaces(workdir))


def _review_assignment_facts(repo: str, pr_number: int, *, conflict_resolution: bool = False,
                             profile: str = "reviewed"):
    """Read the author's depth proposal and current file surface from the PR.

    An unreadable snapshot defaults to Targeted so the opener remains recoverable; the reviewer
    independently fetches the live PR before acting. Sensitive paths and competing conflict choices
    escalate to Full inside the policy module and can never be downgraded by later passes.
    """
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, assign_depth, proposed_depth)

    data = github.api(["pr", "view", str(pr_number), "--repo", repo,
                       "--json", "body,files"], parse_json=True)
    if not isinstance(data, dict):
        if profile == "guarded":
            return ReviewAssignment(
                ReviewDepth.FULL, "guarded profile requires Full review",
                ReviewAxis.PRODUCT), ()
        return ReviewAssignment(
            ReviewDepth.TARGETED, "PR depth proposal was unreadable",
            ReviewAxis.COMBINED), ()
    body = str(data.get("body") or "")
    proposal = proposed_depth(body)
    paths = tuple(
        str(item.get("path")) for item in data.get("files", [])
        if isinstance(item, dict) and item.get("path"))
    assignment = assign_depth(
        proposal.depth.value, proposal.reason, paths, context=body,
        guarded=profile == "guarded")
    axis = ReviewAxis.PRODUCT if assignment.depth is ReviewDepth.FULL else ReviewAxis.COMBINED
    return ReviewAssignment(assignment.depth, assignment.reason, axis), paths


def _open_review_on_completed_build(coord: Coordinator, build_identity: str) -> None:
    """A completed Build opens exactly one waiting Review for the exact PR head SHA and transfers
    the change claim before the Build record retires — no ownership gap (ADR 0028). Submission is
    idempotent on the review identity (repo, subject, review, head SHA), so a repeat or restart
    never opens a second review; a new head SHA is a genuinely new stage. Live — its mapping is
    covered through :func:`review_submission`, and the re-drive after a crash or transient failure
    through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    build = records.get(build_identity)
    if build is None or build.stage != "build":
        return
    facts = _source_facts(build)
    if facts is None:
        return
    _workdir, branch, _wt = facts
    pr = _open_pr_for_branch(build.repo, branch)
    if pr is None:
        return
    context = _review_context(build)
    if context is None:
        return
    acceptance, surfaces = context
    from agentflow.loop import repo_profile
    from agentflow.review_policy import ReviewState
    profile = repo_profile(_workdir)
    assignment, _changed_files = _review_assignment_facts(
        build.repo, pr.number, profile=profile)
    reviewer_tool = (pick_reviewer(build.pool, allow_same_tool=False)
                     if profile == "autonomous" else pick_reviewer(build.pool))
    if reviewer_tool is None:
        return  # ADR 0020: no tool free to review this cycle — post nothing; the completed
                # build keeps its claim and this opener re-drives next cycle.
    submission = review_submission(
        build, pr.head_ref_oid, reviewer_tool, pr.number,
        acceptance=acceptance, surfaces=surfaces,
        review=ReviewState(assignment=assignment, change_author_tool=build.pool))
    if submission is not None:
        coord.submit_stage(submission)


def _review_verdict(review):
    """Re-parse the completed Review's durable verdict for its exact reviewed SHA — read from the
    reviewer's captured final message, never a file in the PR tree (ADR 0018/0028). Live, not
    unit-tested (ADR 0020); the parse itself is covered in the reviewer tests."""
    from agentflow.coordinator.providers import ProviderObserver
    from agentflow.review_policy import ReviewState, merge_findings
    from agentflow.reviewer import Finding, parse_verdict
    obs = ProviderObserver().observe(review)
    verdict = parse_verdict(
        obs.final_message or "", expected_sha=review.target,
        expected_depth=review.review_depth, expected_axis=review.review_axis,
        expected_author=review.change_author_tool)
    prior = ReviewState.from_record(review)
    if prior is None:
        return replace(verdict, clean=False, parsed=False,
                       detail="durable review ledger is unreadable", reviewer_tool=review.pool)
    fixes = tuple(dict.fromkeys(prior.fixes + verdict.fixes))
    follow_ups = tuple(dict.fromkeys(prior.follow_ups + verdict.follow_ups))
    checks = tuple(dict.fromkeys(prior.checks + verdict.checks))
    actions = (verdict.actions if review.review_axis == "fix"
               else merge_findings(prior.findings, verdict.actions))
    compatibility_findings = tuple(
        Finding("blocking" if item.action.value in {"fix_before_completion", "ask_maintainer"}
                else "nit", item.summary, item.file, item.line)
        for item in actions)
    return replace(
        verdict, reviewer_tool=review.pool, fixes=fixes, follow_ups=follow_ups, checks=checks,
        actions=actions, findings=compatibility_findings,
        clean=verdict.clean and not any(
            item.action.value in {"fix_before_completion", "ask_maintainer"} for item in actions),
        follow_up_issues=tuple(item.url for item in follow_ups))


def _open_revise_on_blocking_review(coord: Coordinator, review_identity: str) -> None:
    """A completed Review whose verdict blocks opens exactly one waiting Revise on the builder's
    retained branch/worktree and transfers the change claim before the Review record retires — no
    ownership gap (ADR 0028). A clean verdict is the merge path, not a revise. The auto-revise
    product cap (``MAX_REVISES`` rounds, ADR 0004) is unchanged: once it is spent, a further
    blocking review parks on its own exhaustion rather than looping. Submission is idempotent on
    the revise identity (repo, subject, revise, reviewed SHA, round), so a repeat or restart never
    opens a second revise. Live — its mapping is covered through :func:`revise_submission`, and the
    park and re-drive paths through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    review = records.get(review_identity)
    if review is None or review.stage != "review" or not review.target:
        return
    verdict = _review_verdict(review)
    if verdict.change_author_tool:
        # Structured ADR 0047 reviews stay inside the private review chain. A reviewer push is
        # never self-approved; Full separates product and standards before one fix pass; genuine
        # uncertainty gets one narrow other-tool handoff. Only the final other-tool unchanged pass
        # reaches settlement.
        if verdict.pushed_sha:
            if review.review_passes + 1 >= 3:
                coord.park_completed(review_identity)
                return
            successor = review_successor_submission(review, verdict)
            if successor is not None:
                coord.submit_stage(successor)
            # No eligible reviewer is an availability hold: the completed record keeps the claim
            # and this opener retries without consuming a session or publishing a park.
            return
        if _review_depth_escalated(review, verdict):
            successor = review_axis_successor_submission(
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
            successor = review_axis_successor_submission(
                review, verdict, axis=review.review_axis, uncertainty=True)
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if review.review_axis == "decision":
            successor = conflict_decision_revise_submission(review, verdict)
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if review.review_axis == "product":
            successor = review_axis_successor_submission(review, verdict)
            if successor is None:
                coord.park_completed(review_identity)
            else:
                coord.submit_stage(successor)
            return
        if any(item.action.value == "fix_before_completion" for item in verdict.actions):
            if review.review_axis == "fix":
                coord.park_completed(review_identity)
                return
            successor = review_axis_successor_submission(review, verdict, axis="fix")
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
    facts = _revise_builder_source(review)
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
    submission = revise_submission(
        review, complexity, findings, target_sha=verdict.final_sha or review.target)
    if submission is not None:
        coord.submit_stage(submission)


def _open_review_on_completed_revise(coord: Coordinator, revise_identity: str) -> None:
    """A completed Revise opens exactly one waiting Review bound to the current PR head SHA and
    transfers the change claim before the Revise record retires — no ownership gap (ADR 0028). The
    new review carries the revise round in its identity and starts a fresh review budget, so the
    prior review's record is never reused — even for an evidence-only revision whose head SHA never
    moved. Submission is idempotent on that identity, so a repeat or restart never opens a second
    review. Live — its mapping is covered through :func:`review_submission`, and the evidence-only
    and re-drive paths through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    revise = records.get(revise_identity)
    if revise is None or revise.stage != "revise":
        return
    facts = _source_facts(revise)  # revise reuses the builder worktree, so this parses its branch
    if facts is None:
        return
    _workdir, branch, _wt = facts
    pr = _open_pr_for_branch(revise.repo, branch)
    if pr is None:
        return
    context = _review_context(revise)
    if context is None:
        return
    acceptance, surfaces = context
    if revise.outcome and revise.outcome.startswith(_CONFLICT_UNCERTAINTY_PREFIX):
        submission = conflict_decision_review_submission(
            revise, head_sha=pr.head_ref_oid, pr_number=pr.number,
            acceptance=acceptance, surfaces=surfaces)
        if submission is not None:
            coord.submit_stage(submission)
        return
    conflict_resolution = bool(revise.conflict_round)
    from agentflow.loop import repo_profile
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
        assignment, _changed_files = _review_assignment_facts(
            revise.repo, pr.number, conflict_resolution=conflict_resolution, profile=profile)
        review = ReviewState(assignment=assignment, change_author_tool=revise.pool,
                             reviewed_from_sha=revise.target)
    reviewer_tool = (pick_reviewer(revise.builder_lineage, allow_same_tool=False)
                     if profile == "autonomous" else pick_reviewer(revise.builder_lineage))
    if reviewer_tool is None:
        return  # ADR 0020: no tool free to review this cycle — post nothing; the completed
                # revise keeps its claim and this opener re-drives next cycle.
    submission = review_submission(
        revise, pr.head_ref_oid, reviewer_tool, pr.number,
        acceptance=acceptance, surfaces=surfaces,
        conflict_resolution=conflict_resolution,
        review=review)  # ADR 0038: add discard-check lens
    if submission is not None:
        coord.submit_stage(submission)


def _moved_head_review_submission(record, head_sha: str):
    """One successor Review Submission for a Review whose PR head moved off its immutable target for
    a reason other than an auto-revise — a maintainer-requested rebase, a manual push, a conflict
    fix. It carries the same auto-revise round and builder lineage, points at a fresh detached
    checkout at the live head, and transfers the claim from the stranded record. The identity scheme
    is exactly the one a post-revise review uses (repo, subject, review, new SHA, round), so a repeat
    or restart never double-opens (#208). Returns ``None`` when the source is unreadable or no
    reviewer tool has headroom this cycle — in which case the stranded record keeps its claim and a
    later pass retries."""
    from agentflow.coordinator import Submission
    from agentflow.review_policy import ReviewState
    from agentflow.reviewer import review_worktree
    facts = _review_source_facts(record)
    if facts is None or not head_sha or not record.builder_lineage:
        return None
    workdir, pr = facts
    slug = _review_slug(record)
    from agentflow.loop import repo_profile
    reviewer_tool = pick_reviewer(
        record.builder_lineage, allow_same_tool=repo_profile(workdir) != "autonomous")
    if reviewer_tool is None:
        return None  # ADR 0020: no tool free to review this cycle — leave the record, retry later
    prompt = ((record.input_ptr or "").replace(record.target, head_sha)
              if record.target else (record.input_ptr or ""))
    review = ReviewState.from_record(record)
    if review is None:
        return None
    review = replace(review, reviewed_from_sha=record.target)
    return Submission(
        repo=record.repo, subject=record.subject, stage="review", target=head_sha,
        pool=reviewer_tool, complexity="deep",
        source=str(review_worktree(workdir, reviewer_tool, pr, slug)),
        claim=True, input_ptr=prompt, builder_lineage=record.builder_lineage,
        builder_complexity=record.builder_complexity, round=record.round,
        review=review,
        transfer_from=record.identity, supersede=True)


def _kill_running_family(record) -> None:
    """SIGTERM the provider family of a RUNNING review before retiring or parking it, so the
    orphaned process does not keep burning tokens on a superseded head (#220). Fail-open: a family
    already gone or an os.kill error never blocks the retire or park."""
    import signal
    from agentflow.coordinator.launcher import pid_family_alive
    if not pid_family_alive(record.family):
        return
    try:
        os.kill(int(record.family), signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _review_checkout_owns_head(record, head: str) -> bool:
    """Whether a running review's detached checkout proves it authored the live head move.

    This distinguishes the reviewer's own clean push (let the family finish its structured
    verdict) from a concurrent maintainer push (terminate the stale review and retarget it).
    Unknown or dirty local state fails closed as not-owned.
    """
    from agentflow.loop import _run
    wt = Path(record.source)
    if not wt.exists() or not head:
        return False
    local = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    return (local.returncode == 0 and local.stdout.strip() == head
            and status.returncode == 0 and not status.stdout.strip())


def _resettle_diverged_reviews(coord: Coordinator) -> None:
    """Retire a Review whose PR head has moved off its immutable target before another attempt is
    charged against a head that is no longer live (#208).

    A Review's target SHA is immutable and its verify demands a verdict for exactly that SHA, so a
    head that moves for any reason other than an auto-revise — a maintainer-requested rebase, a
    manual push, a conflict fix — strands the in-flight Review: each attempt re-reviews a head that
    is no longer live, burns one of the three, and the record finally parks "budget exhausted" — even
    on a PR the maintainer has already merged. This runs every reconcile pass, before admission, over
    the durable records:

    - A merged or closed PR retires the Review silently — there is nothing left to review, so no park
      comment and no notification. (A *completed* clean review of a merged PR is left to the normal
      merge path.)
    - An open PR whose head still equals the target is left untouched — the normal review flow.
    - An open PR whose head has diverged retires the stranded record and opens one bounded successor
      Review at the live head; when the auto-revise rounds are spent the PR parks once through the
      existing exhaustion handoff instead.

    A durable blocking verdict is never disturbed — its head move flows through the Revise chain."""
    from agentflow.coordinator.record import COMPLETED, RUNNING
    records = {record.identity: record for record in tracer.load_records()}
    for record in list(records.values()):
        if (record.stage != "review" or record.retired or record.hold_pending
                or not record.claim or not record.target):
            continue
        verdict = _review_verdict(record) if record.state == COMPLETED else None
        if verdict is not None and verdict.blocking:
            continue  # a blocking verdict flows to Revise, which owns the head move
        pr_facts = _review_pr_facts(record)
        if pr_facts is None:
            continue  # GitHub unreadable — fail closed; a later pass retries
        state, head = pr_facts["state"], pr_facts["head"]
        if state == "MERGED" and record.state == COMPLETED:
            continue  # a completed clean review of a merged PR is the normal merge path
        if state in {"MERGED", "CLOSED"}:
            if record.state == RUNNING:
                _kill_running_family(record)
            coord.retire_stale_review(record.identity)
            continue
        expected_head = ((verdict.final_sha or record.target)
                         if verdict is not None else record.target)
        if head == expected_head:
            continue  # the live head still matches the reviewed SHA — nothing to do
        if record.state == RUNNING and _review_checkout_owns_head(record, head):
            # Review is allowed to push bounded fixes. Let the running family finish and bind its
            # verdict to that final head; if it does not, the resulting waiting record is retargeted
            # on the next pass. Never kill a reviewer merely because its own push moved the head.
            continue
        if (record.round >= MAX_REVISES
                or not revise_round_budget_remains(records.values(), record.repo, record.subject)):
            if record.state == RUNNING:
                _kill_running_family(record)
            coord.park_stale_review(record.identity)
            continue
        submission = _moved_head_review_submission(record, head)
        if submission is not None:
            if record.state == RUNNING:
                _kill_running_family(record)
            try:
                coord.submit_stage(submission)
            except StoreUnavailable:
                continue  # the store moved the claim between our snapshot and this submit; retry


def _resume_tainted_reviews(coord: Coordinator) -> None:
    """Reopen only maintainer-forced autonomous taint when independence returns."""
    from agentflow.loop import repo_profile

    records = list(tracer.load_records())
    candidates = [record for record in records
                  if record.stage == "review" and record.retired and record.review_tainted
                  and not record.review_taint_cleared and record.target]
    for record in candidates:
        same_head = [
            other for other in records
            if other.stage == "review" and other.repo == record.repo
            and str(other.subject) == str(record.subject) and other.target == record.target
        ]
        if record is not max(
                same_head, key=lambda item: (item.review_sequence, item.created_at, item.identity)):
            continue
        source_facts = _review_source_facts(record)
        if source_facts is None or repo_profile(source_facts[0]) != "autonomous":
            continue
        pr_facts = _review_pr_facts(record)
        if pr_facts is None or pr_facts["state"] != "OPEN" or pr_facts["head"] != record.target:
            continue
        author = record.change_author_tool or record.builder_lineage
        if not author:
            continue
        reviewer_tool = pick_reviewer(author, allow_same_tool=False)
        if reviewer_tool is None:
            continue  # open PR holds indefinitely without consuming a record or permit
        submission = tainted_review_submission(record, reviewer_tool)
        if submission is not None:
            try:
                coord.submit_stage(submission)
            except StoreUnavailable:
                continue


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
    _resume_tainted_reviews(coord)
    _resettle_diverged_reviews(coord)
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
