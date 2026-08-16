"""Retire durable human holds whose GitHub subjects have definitively resolved."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

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
_PULL_REQUEST_PATH = re.compile(r"^/([^/]+)/([^/]+)/pull/([0-9]+)$")


def _pull_request_from_handoff_proof(record) -> int | None:
    """The same-repository pull request named by a durable handoff proof, if exact.

    Handoff proof is durable cross-process data, so every shape mismatch is unknown rather than a
    reason to reinterpret the record's issue subject as a pull-request number.
    """
    if not isinstance(record.handoff_proof, str):
        return None
    try:
        proof = urlsplit(record.handoff_proof)
    except ValueError:
        return None
    if (proof.scheme != "https" or proof.netloc != "github.com" or proof.query
            or proof.fragment):
        return None
    match = _PULL_REQUEST_PATH.fullmatch(proof.path)
    if match is None or f"{match[1]}/{match[2]}" != record.repo:
        return None
    return int(match[3])


def _retire_closed_holds(coord) -> None:
    """Retire a parked stage only after GitHub definitively says its subject is closed.

    A held record is terminal for dispatch but still appears in the operator's durable board.
    Issue-bound holds resolve against their issue; Review, Respond, and Revise resolve against
    the exact same-repository pull request named in their durable handoff proof. ``None`` from
    either read, or an unparseable proof, is unknown and never retires a record. Issue-backed
    claim and held labels must be provably absent before the coordinator forgets the record; a
    PR-bound record that retained a claim has no safely derivable issue label, so it remains for
    a later pass.
    """
    states: dict[tuple[str, str, int], str | None] = {}
    for record in tracer.load_records():
        if (not coord._manages_repository(record.repo) or record.retired
                or record.state != HELD):
            continue
        if record.stage in _ISSUE_STAGES:
            kind = "issue"
            closed = {"CLOSED"}
            try:
                number = int(record.subject)
            except (TypeError, ValueError):
                continue
        elif record.stage in _PR_STAGES:
            kind = "pr"
            closed = {"CLOSED", "MERGED"}
            number = _pull_request_from_handoff_proof(record)
            if number is None:
                continue
        else:
            continue
        key = (kind, record.repo, number)
        if key not in states:
            states[key] = (github.issue_state(record.repo, number)
                           if kind == "issue" else github.pr_state(record.repo, number))
        if states[key] not in closed:
            continue
        if kind == "pr":
            if record.claim:
                continue  # no safe issue number from a PR-bound legacy hold: fail closed
        else:
            labels = (_ISSUE_CLAIMS[record.stage], _HELD_LABEL[record.stage])
            if any(not release(record.repo, number, label) for label in labels):
                continue
        coord.retire_stale_hold(record.identity)
