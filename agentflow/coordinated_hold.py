"""Retire durable human holds whose GitHub subjects have definitively resolved."""

from __future__ import annotations

from agentflow import github
from agentflow.coordinator import tracer
from agentflow.coordinator.record import HELD
from agentflow.labels import BUILDING, DRAWING, TRIAGING, release


_ISSUE_STAGES = frozenset({"intake", "attack", "build", "mockup"})
_PR_STAGES = frozenset({"review", "respond", "revise"})
_ISSUE_CLAIMS = {
    "intake": TRIAGING,
    "attack": TRIAGING,
    "build": BUILDING,
    "mockup": DRAWING,
}
_HELD_LABEL = {
    "intake": "agentflow:needs-grilling",
    "attack": "agentflow:needs-grilling",
    "build": "agentflow:needs-grilling",
    "mockup": "agentflow:needs-mockup",
}


def _retire_closed_holds(coord) -> None:
    """Retire a parked stage only after GitHub definitively says its subject is closed.

    A held record is terminal for dispatch but still appears in the operator's durable board.
    Issue-bound holds resolve against their issue; Review, Respond, and Revise resolve against
    their pull request, since GitHub's shared number space makes an issue lookup on a PR unsafe.
    ``None`` from either read is unknown and never retires a record. Issue-backed claim and held
    labels must be provably absent before the coordinator forgets the record; a PR-bound record
    that retained a claim has no safely derivable issue label, so it remains for a later pass.
    """
    states: dict[tuple[str, str, int], str | None] = {}
    for record in tracer.load_records():
        if (not coord._manages_repository(record.repo) or record.retired
                or record.state != HELD):
            continue
        try:
            subject = int(record.subject)
        except (TypeError, ValueError):
            continue
        if record.stage in _ISSUE_STAGES:
            kind = "issue"
            closed = {"CLOSED"}
        elif record.stage in _PR_STAGES:
            kind = "pr"
            closed = {"CLOSED", "MERGED"}
        else:
            continue
        key = (kind, record.repo, subject)
        if key not in states:
            states[key] = (github.issue_state(record.repo, subject)
                           if kind == "issue" else github.pr_state(record.repo, subject))
        if states[key] not in closed:
            continue
        if kind == "pr":
            if record.claim:
                continue  # no safe issue number from a PR-bound legacy hold: fail closed
        else:
            labels = (_ISSUE_CLAIMS[record.stage], _HELD_LABEL[record.stage])
            if any(not release(record.repo, subject, label) for label in labels):
                continue
        coord.retire_stale_hold(record.identity)
