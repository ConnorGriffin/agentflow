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


def park_context(record, verdict, *, reason: str, missing: str, uncertainty=None, wording=None):
    """Build the concrete two-section park contract from durable stage state.

    ``uncertainty`` is the exact-head chain's unanswered decision, supplied by the caller so one
    park reads the durable chain once. ``wording`` lets a park with no recorded decision at all
    describe its own execution failure end to end, instead of borrowing options, consequences, and
    a recommendation that all name a decision nobody recorded.
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
    checks = (tuple(verdict.checks) if verdict is not None and verdict.checks
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


def review_park_missing(record) -> str:
    """What a parked review tells the maintainer it is missing, chosen from the persisted hold
    reason so a crash-resumed handoff composes the same words.

    No decision was ever recorded for this head, so the honest fact is an execution failure —
    inventing a product choice here is what made a parked review unanswerable (#344). *Which*
    execution failure matters too: a review whose session was cut off at the per-stage turn
    ceiling never got to finish thinking, and wants a different answer from one that thought and
    ran out of room (#411). The park's own headline stays fixed either way — its post-once proof
    keys on that. Pure (test surface)."""
    from agentflow.coordinator.coordinator import ended_at_turn_cap
    if ended_at_turn_cap(getattr(record, "hold_reason", None)):
        return ("No review verdict was recorded for this exact head: the last review session was "
                "cut off at its per-stage turn ceiling before it could reach one — it was stopped "
                "mid-review, not left short of an answer. Do not treat this as a clean review.")
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
    from agentflow.gate import park
    from agentflow.handoff import DurableHandoff, Notification, Subject
    pr = park_pr_number(record)
    if pr is None:
        return None
    uncertainty = chain_uncertainty(record)
    wording = None
    if record.stage == "review" and (uncertainty is not None
                                     or record.review_axis == "decision"):
        reason = "needs the maintainer to choose between competing product behaviors"
        # A recorded decision prints its own exact wording; this line is what remains when the
        # axis asked for a decision the durable chain no longer holds.
        missing = "Both tools remain unsure and the private decision record is unavailable."
        wording = ParkCopy(next_action=(
            "Reply on this PR with the behavior you want; agentflow resumes the parked "
            "review at this same exact head with your decision."))
        notice = "conflict decision needs your judgment"
    elif record.stage == "review":
        reason = "exhausted its review budget without a durable verdict"
        missing = review_park_missing(record)
        # The options, consequences and recommendation follow the same rule that line does:
        # nothing may name an uncertainty this park deliberately does not have, or an option it
        # does not offer (#344).
        wording = ParkCopy(
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
                                  uncertainty=uncertainty, wording=wording),
            proof_marker=marker),
        notification=Notification(
            "agentflow needs you", f"{record.repo} PR #{pr}: {notice}"))
