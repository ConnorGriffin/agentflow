"""The Review stage behind the durable session coordinator (issues #103–#108, ADR 0047).

A completed Build or Revise hands its change claim to one durable ``review`` submission bound to
the *exact* PR head SHA it judges — an immutable target, so a new head is always a new review.
This module is the daemon-side glue, mirroring its stage siblings:

- **submission mapping** — every Review a chain can open: the first review of a head, the
  serialized same-head axis / fix / uncertainty passes, the successor after a reviewer pushed,
  the taint-clearing independent pass, the maintainer-answered decision resume, the private
  conflict-decision pass, the survivor review of an already-open PR, and the retarget after a
  head moved.
- **stage collaborators** — the exact-head verdict outcome, the detached bounded-fix checkout
  preparation, and the two-phase settlement that consumes a verdict through the repository's
  merge policy. These are the production wiring :mod:`agentflow.pipeline` injects into
  :class:`ReviewStageAdapter`; the PR park Review shares with Revise is
  :mod:`agentflow.pr_park`.
- **reconciliation** — retargeting or parking a Review whose PR head moved off its immutable
  target (#208), and reopening a forced same-tool result when independence returns.

The mappings are pure and exercised directly; the live GitHub/git reads are exercised through the
adapter seam (ADR 0020).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from agentflow import github
from agentflow.balancer import BUILD_POOLS, pick_reviewer
from agentflow.coordinator import Coordinator, tracer
from agentflow.coordinator.store import StoreUnavailable
from agentflow.gate import MAX_REVISES, revise_round_budget_remains
from agentflow.labels import BUILDING, claim
from agentflow.pr_park import (chain_uncertainty, exact_head_review_chain, park_context,
                               park_proof_marker)
from agentflow.prompts import UI_GAP_REASON
from agentflow.repo_facts import repo_profile, surface_declaration, surfaces_phrase, ui_surfaces
from agentflow.reviewer import review_worktree
from agentflow.runner import _run, remove_worktree_if_safe
from agentflow.stage_worktree import worktree_owns_head
from agentflow.worktree_ref import WorktreeRef, review_source_facts


# The one extra lens a re-review gains when a conflict Revise produced the head under review (ADR
# 0038): the reviewer must confirm it kept every compatible behavior and did not silently choose a
# winner for genuinely competing product intent.
CONFLICT_REVIEW_LENS = (
    "\n\nThis head was produced by resolving a merge conflict against `main`. One extra lens: "
    "verify the resolution preserves both sides wherever their behavior is compatible. If the "
    "sides encode genuinely competing product intent, verify that the private second-opinion path "
    "resolved it; never accept a silent choice based on which side is newer.")


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
    from agentflow.reviewer import REVIEW_PROMPT, with_review_assignment
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
        CONFLICT_UNCERTAINTY_PREFIX, ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState,
        Uncertainty, other_tool)

    if (not revise_record.conflict_round or not revise_record.outcome
            or not revise_record.outcome.startswith(CONFLICT_UNCERTAINTY_PREFIX)):
        return None
    try:
        uncertainty = json.loads(
            revise_record.outcome[len(CONFLICT_UNCERTAINTY_PREFIX):])
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


def review_successor_submission(review_record, verdict):
    """Map a reviewer-authored pushed head to the other tool's exact-head review.

    This is ADR 0047's independence boundary: the mutating reviewer becomes the current change
    author, the opposite tool receives a private bounded handoff, and the stale builder checkout is
    never reused. Three consecutive mutating passes have no successor; the caller parks once.
    """
    from agentflow.coordinator import Submission
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState)
    from agentflow.reviewer import with_review_assignment

    facts = review_source_facts(review_record)
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
    from agentflow.reviewer import with_review_assignment

    facts = review_source_facts(review_record)
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
    from agentflow.reviewer import with_review_assignment

    facts = review_source_facts(review_record)
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


def decision_resume_review_submission(review_record, reviewer_tool: str, *, target: str,
                                      answer: str, sequence: int):
    """Reopen one parked exact-head Review with the maintainer's own answer to its decision (#344).

    The resumed pass keeps everything the parked chain established — the immutable reviewed head,
    builder lineage, the current change author, the retained review checkout naming, and the
    accumulated fix/check/follow-up ledger — advances the same-head sequence, and records the answer
    as its private next-agent context so the chain reads that decision as settled and never asks it
    again. Independence, taint, and human-merge rules are the caller's existing ones. Pure: the
    mapping is the test surface (ADR 0020). ``None`` when the retained review source, the answered
    comment, or the durable ledger is unreadable.
    """
    from agentflow.coordinator import Submission
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState, decision_answer_handoff)
    from agentflow.reviewer import with_review_assignment

    facts = review_source_facts(review_record)
    author = review_record.change_author_tool or review_record.builder_lineage
    if facts is None or not review_record.target or not target or not answer.strip():
        return None
    prior = ReviewState.from_record(review_record)
    if prior is None:
        return None
    workdir, pr = facts
    depth = ReviewDepth(review_record.review_depth)
    axis = ReviewAxis.PRODUCT if depth is ReviewDepth.FULL else ReviewAxis.COMBINED
    reason = review_record.depth_reason or "maintainer answered the recorded product decision"
    handoff = decision_answer_handoff(target, answer)
    prompt = with_review_assignment(
        review_record.input_ptr or "",
        depth=depth, reason=reason, axis=axis, change_author_tool=author or "", handoff=handoff)
    state = replace(
        prior, assignment=ReviewAssignment(depth, reason, axis),
        change_author_tool=author, sequence=sequence,
        cross_tool_covered=reviewer_tool != author, handoff=handoff, uncertainty=None)
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
    from agentflow.review_policy import ReviewState
    from agentflow.reviewer import REVIEW_PROMPT, with_review_assignment

    if not head_sha or builder_tool not in BUILD_POOLS or reviewer_tool not in BUILD_POOLS:
        return None
    prompt = REVIEW_PROMPT.format(
        pr=pr_number, starting_sha=head_sha, acceptance=acceptance or "(none provided)",
        surfaces=surfaces_phrase(surface_declaration(cfg.workdir)))
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

    return validate_follow_ups(
        record.repo, verdict.follow_ups,
        issue_url=lambda number: github.issue_url(record.repo, number),
        issue_search=lambda query: github.find_issues(record.repo, query, limit=100))

# Consecutive review-prepare failures per source path, so a genuinely stuck
# review (one that never checks out) is surfaced periodically instead of silently
# no-op'ing admission every cycle. Process-local — a daemon restart re-arms it.
_REVIEW_PREPARE_FAILURES: dict[str, int] = {}


def _commit_is_gone(workdir: str, sha: str) -> bool:
    """Whether ``sha`` is absent from the repository — the branch was rebased or amended past it
    and it survives on no ref. Absence is only claimed on a definite answer, so an unreadable
    repository reads as present and keeps the more cautious message."""
    if not sha:
        return False
    return _run(["git", "-C", workdir, "cat-file", "-e", f"{sha}^{{commit}}"]).returncode != 0


def _review_worktree_reset(record, _log=None) -> bool:
    """Prepare Review's detached, writable exact-head checkout (ADR 0030, amended).

    The first attempt starts at the immutable target. A continuation reuses the registered
    checkout exactly as it is so an interrupted review keeps its fixes; it is never reset or
    cleaned. A fresh logical review may reuse a clean prior checkout and reset it to its own
    target. Any git failure skips admission without consuming a permit or attempt.
    """
    from agentflow.runner import ClaudeRunner, CodexRunner
    facts = review_source_facts(record)
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
            if _commit_is_gone(workdir, record.target):
                # The reviewed head was rebased or amended away. The record is not stuck on its
                # checkout and no human can clear it: the diverged-review reconciler supersedes it
                # with one at the live head as soon as a reviewer pool has headroom. Say that,
                # rather than sending someone after a checkout that is fine.
                _log(f"{record.repo}: review target {record.target[:12]} no longer exists — "
                     "awaiting retarget to the live PR head (needs reviewer headroom)")
            else:
                _log(f"{record.repo}: review checkout keeps failing at {record.source} — "
                     "admission is stuck; the PR will not be reviewed until it is cleared")
        return False
    _REVIEW_PREPARE_FAILURES.pop(record.source, None)
    return True


def _review_slug(record) -> str:
    """The slug in a Review record's detached checkout path (``.../pr-<pr>-<slug>``), reused to
    name the finished worktree so a review reads as the same issue's pair on disk. ``""`` when the
    source is not a well-formed review path."""
    ref = WorktreeRef.parse(record.source)
    return ref.slug if ref is not None else ""


def _finish_review(cfg, reviewer_tool: str, pr: int, sl: str, merged: bool = False) -> None:
    """Dispose the reviewer's checkout once the review's outcome is durable on GitHub — merged,
    or carrying our park handoff. Anything less leaves the worktree in place so the next pass can
    finish from it rather than starting cold."""
    comments = github.pr_comment_rows(cfg.repo, pr)
    durable = merged or (comments is not None and any(
        "agentflow: parked for human review" in c.get("body", "") for c in comments))
    if durable:
        remove_worktree_if_safe(
            cfg.workdir, review_worktree(cfg.workdir, reviewer_tool, pr, sl))


def resume_answered_review(cfg, coordinator, pr: int, *, comment: str, target: str,
                           baseline: str) -> str | None:
    """Resume the parked exact-head Review the maintainer's PR reply answers (#344).

    Returns a status when this reply is that answer — so no generic Respond ever claims the issue
    for it — and ``None`` when it is ordinary PR discussion, which the Respond path then owns
    unchanged. The answer counts only when a decision recorded against the PR's *current* head is
    still unanswered and the reply follows the park handoff that asked for it. What settles a
    decision is the maintainer's own answer on the durable chain, so a second decision round after
    a resume stays just as answerable as the first.

    Ordering is the crash contract: the resumed Review record is created before the public
    answered-marker comment, so a crash in between converges — the next pass finds the review
    already bound to this exact comment and only completes the marker, opening no second lifecycle,
    claim, or notification. Live orchestration; its mapping is
    :func:`decision_resume_review_submission`.
    """
    from agentflow.coordinator.record import HELD
    from agentflow.gate import decision_resume_disclaimer, park_awaiting_decision
    from agentflow.review_policy import decision_answer_target, unresolved_uncertainty

    if not target or not baseline:
        return None
    try:
        records = tracer.load_records()
    except StoreUnavailable:
        return f"PR #{pr}: coordinator state unreadable — deferring the parked-review answer"

    def parked_on_this_pr(record) -> bool:
        # The review's own retained checkout names the PR, so the head SHA is never the only
        # binding: this park belongs to *this* PR at *this* exact head.
        facts = review_source_facts(record)
        return (record.stage == "review" and record.repo == cfg.repo
                and record.target == baseline and record.state == HELD and not record.retired
                and facts is not None and facts[1] == pr)

    parked = [record for record in records if parked_on_this_pr(record)]
    if not parked:
        return None
    record = max(parked, key=lambda item: (item.created_at, item.review_sequence, item.identity))
    chain = exact_head_review_chain(records, record)
    bound = any(not item.retired
                and decision_answer_target(item.review_handoff) == str(target)
                for item in chain)
    if not bound and unresolved_uncertainty(chain) is None:
        return None                # nothing was ever asked here — this is ordinary PR discussion
    comments = github.pr_comment_rows(cfg.repo, pr)
    if comments is None:
        return f"PR #{pr}: PR thread unreadable — deferring the parked-review answer"
    if not bound and not park_awaiting_decision(comments, target):
        return None                # the reply does not follow the decision handoff
    issue = int(record.subject)
    if not bound:
        if any(not item.retired and item.claim for item in chain):
            # Another exact-head review already owns this issue — a maintainer recovery that landed
            # first. Report and wait: it consumes the decision, and meanwhile no Respond claims the
            # answer either.
            return (f"#{issue}: an exact-head review already owns this issue — deferring the "
                    f"answered decision on PR #{pr}")
        author = record.change_author_tool or record.builder_lineage
        source_facts = review_source_facts(record)
        if not author or source_facts is None:
            return None            # no lineage to resume — the park stays the human's move
        reviewer_tool = pick_reviewer(
            author, allow_same_tool=repo_profile(source_facts[0]) != "autonomous")
        if reviewer_tool is None:
            return (f"#{issue}: no eligible reviewer for the answered decision on PR #{pr} — "
                    "deferring")
        submission = decision_resume_review_submission(
            record, reviewer_tool, target=str(target), answer=comment,
            sequence=max(item.review_sequence for item in chain) + 1)
        if submission is None:
            return None
        if not claim(cfg.repo, issue, BUILDING):
            return f"#{issue}: could not claim the resumed review on PR #{pr}"
        try:
            coordinator.submit_stage(submission)
        except StoreUnavailable:
            return f"#{issue}: coordinator refused the resumed review on PR #{pr} — retrying"
    body = (f"{decision_resume_disclaimer(target)}\n\n"
            "Your decision is recorded against this PR's exact reviewed head and the parked review "
            "has been resumed with it. No separate reply stage will answer this comment.")
    if not github.pr_comment(cfg.repo, pr, body):
        return f"#{issue}: resumed review recorded; answer marker still pending on PR #{pr}"
    return f"#{issue}: maintainer decision resumed the parked review on PR #{pr}"


def _review_pr_facts(record) -> dict | None:
    """The PR's current head and state, or ``None`` when GitHub is unreadable."""
    facts = review_source_facts(record)
    if facts is None:
        return None
    _workdir, pr = facts
    live = github.pr_facts(record.repo, pr)
    if live is None:
        return None
    if not live.head_ref_oid or live.state not in {"OPEN", "CLOSED", "MERGED"}:
        return None
    return {"head": live.head_ref_oid, "state": live.state}


def _review_pr_head(record) -> str | None:
    facts = _review_pr_facts(record)
    return facts["head"] if facts is not None else None


def _review_depth_escalated(record, verdict) -> bool:
    from agentflow.review_policy import ReviewDepth

    order = {ReviewDepth.FOCUSED: 0, ReviewDepth.TARGETED: 1, ReviewDepth.FULL: 2}
    return order[verdict.depth] > order[ReviewDepth(record.review_depth)]


_REVIEW_CI_OBSERVED: dict[str, bool] = {}


def _prepare_review_settlement(record) -> bool:
    """Perform slow CI observation outside the coordinator store transaction.

    Only an exact-head, independent, clean autonomous review can merge and therefore needs the
    bounded CI wait. Every park/revise path is immediately ready for its short transactional
    finalization. Settlement rechecks both head and CI immediately before the merge.
    """
    from agentflow.gate import ci_is_green

    facts = review_source_facts(record)
    if facts is None:
        return False
    workdir, _pr = facts
    verdict = _review_verdict(record)
    if (verdict.change_author_tool
            and (_review_depth_escalated(record, verdict)
                 or verdict.pushed_sha or verdict.uncertainty is not None
                 or record.review_axis in {"product", "decision"}
                 or any(item.action.value == "fix_before_completion"
                        for item in verdict.actions))):
        return False  # the private successor opener owns this non-terminal pass
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
    from agentflow import ratchet

    marker = park_proof_marker(record, reason)
    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=pr, kind="pr"),
        identity=record.identity, stage="review",
        marker=marker,
        action=lambda: park(
            record.repo, pr, verdict, reason=reason,
            context=park_context(
                record, verdict, reason=reason,
                missing=verdict.detail or "Grounded review actions remain unresolved.",
                uncertainty=chain_uncertainty(record)),
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

    facts = review_source_facts(record)
    if facts is None:
        return None
    workdir, pr = facts
    verdict = _review_verdict(record)
    if (verdict.change_author_tool
            and (_review_depth_escalated(record, verdict)
                 or verdict.pushed_sha or verdict.uncertainty is not None
                 or record.review_axis in {"product", "decision"}
                 or any(item.action.value == "fix_before_completion"
                        for item in verdict.actions))):
        return None  # reviewer-authored/axis/decision work must transfer privately first
    if verdict.blocking:
        return None  # durable opener transfers this claim to Revise
    comments = github.pr_comment_rows(record.repo, pr)
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
        reason = (UI_GAP_REASON if ui_gap
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
        reason = UI_GAP_REASON if ui_gap else "forced same-tool review remains unresolved"
        return _park_review_settlement(
            record, verdict, workdir, pr, reason=reason, autonomous=True)
    if not verdict.clean:
        return _park_review_settlement(
            record, verdict, workdir, pr,
            reason="review did not produce an actionable clean verdict", autonomous=True)
    if not record.auto_merge_allowed:
        reason = UI_GAP_REASON if ui_gap else "could not be auto-merged after review"
        return _park_review_settlement(
            record, verdict, workdir, pr, reason=reason, autonomous=True)

    # CI already completed in prepare_completed, outside SQLite's write transaction. Recheck it
    # once without polling, together with the exact head, immediately before merge.
    ci_green = _REVIEW_CI_OBSERVED.pop(record.identity, None)
    if ci_green is None:
        return None
    # The one merge decision: the UI-evidence gap and an unanswered maintainer question are the
    # gate's own blockers, decided there rather than a second time here. A clean verdict can only
    # come back non-MERGE as a blocker or on red CI, and settlement parks either — it never churns
    # a revise round over a red build.
    pending_reply = reply_pending(comments)
    decision = decide_merge(
        verdict=verdict, ci_green=ci_green, reviewer_tool=record.pool,
        builder_tool=record.change_author_tool or record.builder_lineage or "",
        revises_used=record.round,
        ui_evidence_missing=ui_gap, reply_pending=pending_reply)
    if decision is not MergeDecision.MERGE:
        # A maintainer's own unanswered question outranks red CI in the park notice: telling them
        # the build failed when the pipeline is really waiting on their answer sends them to the
        # wrong place. Same precedence the pre-gate settlement used.
        reason = (UI_GAP_REASON if ui_gap
                  else "could not be auto-merged after review" if pending_reply
                  else "CI did not complete successfully within the review settlement window"
                  if not ci_green else "could not be auto-merged after review")
        return _park_review_settlement(
            record, verdict, workdir, pr, reason=reason, autonomous=True)
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


def _review_context(record) -> tuple[str, str] | None:
    """The issue-anchored acceptance brief and declared UI surfaces for a Review."""

    parts = _build_source_parts(record)
    if parts is None:
        return None
    workdir, _slug = parts
    acceptance = record.input_ptr if record.stage == "build" and record.input_ptr else None
    if acceptance is None:
        acceptance = github.issue_body(record.repo, record.subject)
        if acceptance is None:   # unreadable stays unknown — the opener refuses rather than guesses
            return None
    return acceptance, surfaces_phrase(surface_declaration(workdir))


def _review_assignment_facts(repo: str, pr_number: int, *, conflict_resolution: bool = False,
                             profile: str = "reviewed"):
    """Read the author's depth proposal and current file surface from the PR.

    An unreadable snapshot defaults to Targeted so the opener remains recoverable; the reviewer
    independently fetches the live PR before acting. Sensitive paths and competing conflict choices
    escalate to Full inside the policy module and can never be downgraded by later passes.
    """
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, assign_depth, proposed_depth)

    content = github.pr_content(repo, pr_number)
    if content is None:
        if profile == "guarded":
            return ReviewAssignment(
                ReviewDepth.FULL, "guarded profile requires Full review",
                ReviewAxis.PRODUCT), ()
        return ReviewAssignment(
            ReviewDepth.TARGETED, "PR depth proposal was unreadable",
            ReviewAxis.COMBINED), ()
    body = content.body
    proposal = proposed_depth(body)
    paths = content.paths
    assignment = assign_depth(
        proposal.depth.value, proposal.reason, paths, context=body,
        guarded=profile == "guarded")
    axis = ReviewAxis.PRODUCT if assignment.depth is ReviewDepth.FULL else ReviewAxis.COMBINED
    return ReviewAssignment(assignment.depth, assignment.reason, axis), paths


def _review_verdict(review):
    """Re-parse the completed Review's durable verdict for its exact reviewed SHA.

    New records capture that terminal message at completion. The provider artifact fallback drains
    older records without making a mutable session the source of truth for newly completed work.
    """
    from agentflow.coordinator.providers import ProviderObserver
    from agentflow.review_policy import ReviewState, merge_findings
    from agentflow.reviewer import Finding, parse_verdict
    payload = review.outcome
    if not payload:
        payload = ProviderObserver().observe(review).final_message or ""
    verdict = parse_verdict(
        payload, expected_sha=review.target,
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
    facts = review_source_facts(record)
    if facts is None or not head_sha or not record.builder_lineage:
        return None
    workdir, pr = facts
    slug = _review_slug(record)
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
    wt = Path(record.source)
    if not wt.exists() or not head:
        return False
    return worktree_owns_head(wt, head)


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
        source_facts = review_source_facts(record)
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
