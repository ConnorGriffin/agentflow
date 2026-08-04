"""The PR park a stopped stage hands a human (ADR 0028/0042, #344).

Review, Revise and Respond all end at the same public boundary when their chain cannot finish
safely: one park comment on the pull request, posted once, proved once, notified once. What
that comment says differs per stage, but the scaffolding does not — the PR number a record's
retained checkout implies, the per-decision proof marker, the two-section decision contract,
and the exact-head Review chain a park asks its decision against are shared. They live here so
no stage module has to reach into another's for them.

Review and Revise share :func:`park_pr` itself, which the daemon injects as both stages'
exhaustion handoff; Respond and the Review settlement build their own comment on top of the
same pieces.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from agentflow import github
from agentflow.coordinator import tracer
from agentflow.coordinator.store import StoreUnavailable
from agentflow.worktree_ref import review_source_facts, source_facts


def park_pr_number(record) -> int | None:
    """The PR number to park for a Review or a Revise record. A Review encodes it directly in its
    detached review worktree path (``.../<tool>-review/pr-<pr>-<slug>``); a Revise instead owns the
    *builder's* branch/worktree (``.../<tool>/issue-<subject>-<slug>``, no PR number), so the open PR
    for that branch is looked up from GitHub. Returns ``None`` when it cannot be resolved, so the
    park handoff stays pending and retries rather than proving a park it never made."""
    facts = review_source_facts(record)
    if facts is not None:
        return facts[1]
    parsed = source_facts(record)
    if parsed is None:
        return None
    _workdir, branch, _wt = parsed
    # A Revise's builder branch PR may already be closed or merged, so this spans all states.
    prs = github.prs_for_branch(record.repo, branch, limit=1)
    if prs is None:
        return None
    return prs[0].number if prs else None


def exact_head_review_chain(records, record) -> list:
    """Every Review record for one PR exact head — the Product/Standards/Fix passes that share a
    ``(repo, subject, target)``. This is the unit the park and the resume both read, so a decision
    recorded by one axis is never lost to a later axis that recorded none (#344)."""
    return [item for item in records
            if item.stage == "review" and item.repo == record.repo
            and str(item.subject) == str(record.subject) and item.target == record.target]


def chain_uncertainty(record):
    """The latest unanswered structured decision anywhere in this Review's exact-head chain.

    A park must print the decision agentflow actually recorded, not only whatever the terminal
    record happened to carry (#344). An unreadable store falls back to this one record rather than
    inventing a generic product choice. ``None`` for a non-Review record or a chain with no
    recorded decision.
    """
    from agentflow.review_policy import unresolved_uncertainty

    if record.stage != "review" or not record.target:
        return None
    try:
        records = tracer.load_records()
    except StoreUnavailable:
        return unresolved_uncertainty([record])
    return unresolved_uncertainty(exact_head_review_chain(records, record))


@dataclass(frozen=True)


class ParkCopy:
    """The maintainer-facing decision wording a park branch owns when no recorded product decision
    supplies it. Every field is optional; an empty one keeps the shared default (#344)."""

    options: tuple[str, ...] = ()
    consequences: str = ""
    recommendation: str = ""
    next_action: str = ""


def park_context(record, verdict, *, reason: str, missing: str, uncertainty=None, wording=None,
                 checks=None):
    """Build the concrete two-section park contract from durable stage state.

    ``uncertainty`` is the exact-head chain's unanswered decision, supplied by the caller so one
    park reads the durable chain once. ``wording`` lets a park with no recorded decision at all
    describe its own execution failure end to end, instead of borrowing options, consequences, and
    a recommendation that all name a decision nobody recorded. ``checks`` lets a stage that
    observed the check facts itself (the head check gate, ADR 417) print them instead of the
    reviewer's own claimed proof.
    """
    from agentflow.gate import ParkContext
    from agentflow.review_policy import ReviewState

    wording = wording or ParkCopy()
    actions = tuple(verdict.actions) if verdict is not None else ()
    uncertainty = (verdict.uncertainty if verdict is not None else None) or uncertainty
    options = (tuple(uncertainty.options) if uncertainty is not None else wording.options or (
        "Clarify the affected behavior and resume this retained stage on the same PR.",
        "Close the PR and leave the currently shipped application behavior unchanged.",
    ))
    locations = tuple(dict.fromkeys(
        f"{item.file}:{item.line}" if item.line else item.file
        for item in actions if item.file))
    if not locations:
        locations = (f"PR #{park_pr_number(record) or '?'} exact head {record.target or 'unknown'}",)
    ledger = ReviewState.from_record(record)
    checks = (tuple(checks) if checks
              else tuple(verdict.checks) if verdict is not None and verdict.checks
              else (ledger.checks if ledger is not None and ledger.checks else (
                  "No completed check proof was recorded before the stage stopped.",)))
    miss = getattr(record, "verify_miss", "")
    if miss:
        # The last attempt's first failed verification conjunct, so the human reading the park
        # sees what actually stopped the machine instead of a generic budget line.
        checks = (f"Last unverified check: {miss}",) + tuple(checks)
    conflicts = (
        f"Missing guidance: {uncertainty.missing_guidance}. "
        f"Agent recommendation: {uncertainty.recommendation}."
        if uncertainty is not None else missing)
    return ParkContext(
        behavior=f"The requested PR behavior {reason}.",
        options=options,
        consequences=(wording.consequences or (
            "Resuming can ship the intended change after the named uncertainty is resolved; "
            "closing preserves the application's current behavior.")),
        recommendation=(uncertainty.recommendation if uncertainty is not None
                        else wording.recommendation
                        or "Resolve the named uncertainty, then resume the retained stage."),
        locations=locations, conflicts=conflicts, checks=checks,
        retained_work=f"`{record.source}` at `{record.target or 'unknown head'}`",
        next_action=(
            wording.next_action
            or "Record the chosen behavior, then resume this exact retained stage on the same PR."),
        decision_needed=(
            uncertainty is not None
            or any(item.action.value == "ask_maintainer" for item in actions)))


def _recorded_review_passes(record) -> int:
    """Count earlier repair-pushing passes and this record's own stored verdict, offline."""
    from agentflow.reviewer import parse_verdict

    own_verdict = False
    if record.outcome:
        verdict = parse_verdict(
            record.outcome, expected_sha=record.target,
            expected_depth=record.review_depth, expected_axis=record.review_axis,
            expected_author=record.change_author_tool,
            owned_heads=((record.review_prior_push,) if record.review_prior_push else ()))
        own_verdict = verdict.parsed
    return record.review_passes + int(own_verdict)


def review_park_missing(record) -> str:
    """What a parked review tells the maintainer from its durable outcome and hold facts.

    The missing-outcome cause keeps its established precedence: a never-started session, then a
    turn-capped session, then the generic failure. Once the ledger proves an earlier pass or this
    record's stored outcome parses to a verdict, unsupported claims that nobody judged the change
    are replaced by the count those durable facts prove. Pure (test surface, #501)."""
    from agentflow.coordinator.coordinator import ended_at_turn_cap, refused_before_start
    hold_reason = getattr(record, "hold_reason", None)
    passes = _recorded_review_passes(record)
    noun = "pass" if passes == 1 else "passes"
    pass_sentence = f"{passes} review {noun} recorded a verdict."
    if record.review_passes:
        earlier = "pass" if record.review_passes == 1 else "passes"
        pass_sentence += (f" The {record.review_passes} earlier {earlier} pushed a repair at "
                          "their own head.")
    if refused_before_start(hold_reason):
        if passes:
            return ("The latest review session did not run at all: the private working copy the "
                    "review needs is pinned open on the machine agentflow runs on, so nothing was "
                    f"checked out. {pass_sentence} Do not treat this as a "
                    "clean review.")
        return ("No review verdict was recorded for this exact head, and no session ran at all: "
                "the private working copy the review needs is pinned open on the machine "
                "agentflow runs on, so nothing was ever checked out. No attempt was used and no "
                "budget was drawn down. Do not treat this as a clean review — nothing has looked "
                "at this change at all.")
    if ended_at_turn_cap(getattr(record, "hold_reason", None)):
        if passes:
            return ("The last review session was cut off at its per-stage turn ceiling. "
                    f"{pass_sentence} Do not treat this as a clean review.")
        return ("No review verdict was recorded for this exact head: the last review session was "
                "cut off at its per-stage turn ceiling before it could reach one — it was stopped "
                "mid-review, not left short of an answer. Do not treat this as a clean review.")
    if passes:
        return f"{pass_sentence} Do not treat this as a clean review."
    return ("No review verdict was recorded for this exact head: the review executions failed "
            "rather than judging the change. Do not treat this as a clean review.")


def park_proof_marker(record, reason: str) -> str:
    """One low-noise current-park proof scoped to this durable stage decision."""
    digest = hashlib.sha256(f"{record.identity}:{reason}".encode()).hexdigest()[:20]
    return f"agentflow-park:{digest}"


def park_pr(record) -> str | None:
    """Park the reviewed PR for a human and tell them (ADR 0028's exhaustion table). Serves both
    the Review-native park and the Revise-native park — Revise owns a builder worktree, so the PR is
    resolved by branch (:func:`park_pr_number`). The crash-safe post-once → prove → notify recipe is
    the shared :class:`DurableHandoff` envelope (ADR 0042): the park comment is the durable proof, so
    a repeat after a daemon crash observes the same comment and never parks twice — but it does ping
    again, because a ping that is only sent when the comment is newly posted is lost outright when a
    crash falls between the two (ADR 0042 Consequences). A Review parks against its whole exact-head chain: any decision that chain recorded and
    no maintainer answered is the decision this park asks about, whichever axis stopped last (#344).
    Live orchestration; exercised with faked GitHub reads in ``tests/test_revise_tracer.py``."""
    from agentflow.coordinator.coordinator import (refused_before_start,
                                                   refused_before_start_detail)
    from agentflow.gate import park
    from agentflow.handoff import DurableHandoff, Notification, Subject
    pr = park_pr_number(record)
    if pr is None:
        return None
    uncertainty = chain_uncertainty(record)
    recorded_passes = _recorded_review_passes(record) if record.stage == "review" else 0
    pass_reason = ""
    pass_wording = None
    if recorded_passes:
        noun = "pass" if recorded_passes == 1 else "passes"
        pass_reason = (f"reached a human hand-off after {recorded_passes} verdict-recording "
                       f"review {noun}")
        pass_wording = ParkCopy(
            options=(f"Resume the review on the live head: `/agentflow review {pr}`.",
                     "Review the retained change by hand and decide this PR yourself."),
            consequences=("Resuming seeks a fresh judgment while preserving the cumulative "
                          "repair-pass ceiling; judging it by hand keeps the recorded verdicts "
                          "as evidence without treating them as a clean final review."),
            recommendation=(f"Resume for a fresh judgment on the live head; {recorded_passes} "
                            f"review {noun} already recorded a verdict."),
            next_action=f"Run `/agentflow review {pr}` to review the live head.")
    wording = None
    checks = None
    if record.stage == "review" and refused_before_start(record.hold_reason):
        # Checked before the decision branch on purpose: a review that never ran recorded no
        # decision and reached no verdict, so every other branch's words — a spent budget, a
        # competing product behavior — would describe work that does not exist (#406).
        blocker = refused_before_start_detail(record.hold_reason)
        reason = pass_reason or "has not been looked at at all — the review could not be started"
        missing = review_park_missing(record)
        checks = (f"No checks ran, and no review session was started. What is in the way: "
                  f"{blocker}",)
        wording = pass_wording or ParkCopy(
            options=(f"Release the pinned working copy on the machine agentflow runs on, then "
                     f"resume: `/agentflow review {pr}`.",
                     "Review this change by hand and decide the PR yourself."),
            consequences=("Releasing it lets the review this change has never had actually run; "
                          "judging it by hand leaves this head with no agentflow review at all."),
            recommendation=("Release the working copy and resume — nothing has judged this "
                            "change yet, and no attempts were spent finding that out."),
            next_action=(f"Clear what is named above on the machine agentflow runs on, then run "
                         f"`/agentflow review {pr}` to review this exact head."))
        notice = "review could not start — needs your action"
    elif record.stage == "review" and (uncertainty is not None
                                       or record.review_axis == "decision"):
        reason = "needs the maintainer to choose between competing product behaviors"
        if recorded_passes:
            reason += f" after {recorded_passes} verdict-recording review passes"
        # A recorded decision prints its own exact wording; this line is what remains when the
        # axis asked for a decision the durable chain no longer holds.
        missing = "Both tools remain unsure and the private decision record is unavailable."
        wording = ParkCopy(next_action=(
            "Reply on this PR with the behavior you want; agentflow resumes the parked "
            "review at this same exact head with your decision."))
        notice = "conflict decision needs your judgment"
    elif record.stage == "review":
        reason = pass_reason or "exhausted its review budget without a durable verdict"
        missing = review_park_missing(record)
        # The options, consequences and recommendation follow the same rule that line does:
        # nothing may name an uncertainty this park deliberately does not have, or an option it
        # does not offer (#344).
        wording = pass_wording or ParkCopy(
            options=(f"Resume the review on this exact head: `/agentflow review {pr}`.",
                     "Review the retained change by hand and decide this PR yourself."),
            consequences=("Resuming runs the review this change never got; judging it by hand "
                          "leaves this head with no agentflow review at all."),
            recommendation="Resume the review — nothing has judged this change yet.",
            next_action=f"Run `/agentflow review {pr}` to resume the review at this exact head.")
        notice = "review parked for your action"
    elif record.conflict_round:
        reason = "could not safely complete and verify the merge-conflict resolution"
        missing = "The conflict resolution did not reach a verified pushed revision."
        notice = "conflict resolution needs your judgment"
    else:
        reason = "could not complete and verify the requested revision"
        missing = "The requested revision did not reach a verified pushed outcome."
        notice = "revision parked for your action"
    marker = park_proof_marker(record, reason)
    return DurableHandoff().hand_off(
        Subject(repo=record.repo, number=pr, kind="pr"),
        identity=record.identity, stage=record.stage,
        marker=marker,
        action=lambda: park(
            record.repo, pr, None, reason=reason, missing_outcome=missing,
            context=park_context(record, None, reason=reason, missing=missing,
                                  uncertainty=uncertainty, wording=wording, checks=checks),
            proof_marker=marker),
        notification=Notification(
            "agentflow needs you", f"{record.repo} PR #{pr}: {notice}"))
