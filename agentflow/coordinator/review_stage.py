"""The Review stage adapter — the second live stage behind the session coordinator (ADR 0030,
issue #104).

Review extends the common adapter skeleton (:class:`~agentflow.coordinator.stage_adapter.
StageAdapter`), and adds the four things that are its own:

- its ``prepare`` needs a detached, writable checkout of the exact starting PR head SHA, so its
  readiness check wants both the source and the target. A reviewer may ship bounded fixes, so
  continuations preserve that checkout and its partial work.
- its required outcome is a parsed verdict for the *exact* reviewed head SHA. Provider success can
  never stand in for it — a reviewer that exited badly still completes the stage if a verdict for
  the target SHA is durable, and a clean exit whose verdict is missing or names a different SHA
  stays incomplete (ADR 0028 outcome-first).
- a review that reached a verdict but stated it in a rejected shape earns a repair turn rather
  than a full re-review.
- a clean verdict is projected through the repo merge policy on completion.

Exhaustion or a permanent condition parks the PR for a human (ADR 0028's exhaustion table), which
is the Review-native handoff.
"""

from __future__ import annotations

from agentflow import github
from agentflow.coordinator.recovery import contract_repair
from agentflow.coordinator.stage_adapter import StageAdapter


def _created_follow_up_issue(events) -> bool:
    """Whether captured session commands used Review's retired tracker-write action."""
    def commands(value):
        if isinstance(value, dict):
            command = value.get("command")
            if isinstance(command, str):
                yield command
            for child in value.values():
                yield from commands(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from commands(child)

    return any(github.command_creates_issue(command) for command in commands(events))


def _contract_error(record, obs) -> str:
    """The parser's own error when the attempt did state a verdict but stated it in a rejected
    shape, or ``""`` when it produced no verdict at all.

    The distinction is what a fresh session would have to do: a missing verdict means the review
    itself is unfinished and must continue; a rejected one means the review is finished and only
    its statement is wrong."""
    import json

    from agentflow.reviewer import parse_verdict
    payload = (getattr(obs, "final_message", "") or "").strip()
    try:
        stated = json.loads(payload)
    except ValueError:
        return ""
    if not isinstance(stated, dict) or "verdict" not in stated:
        return ""
    verdict = parse_verdict(
        payload, expected_sha=record.target, expected_depth=record.review_depth,
        expected_axis=record.review_axis, expected_author=record.change_author_tool)
    return "" if verdict.parsed else (verdict.detail or "")


class ReviewStageAdapter(StageAdapter):
    """Observes a launched Review family and verifies its verdict outcome.

    Beyond the shared collaborators, ``verdict_ready`` answers whether a parsed verdict for the
    record's exact target SHA is durable, ``worktree_reset`` prepares the writable detached
    checkout at that SHA, ``verdict_error`` names the parser's complaint about a rejected verdict
    statement, and ``settle``/``prepare_settle`` project a clean verdict through the merge policy.
    Production wires the real verdict parse and retained detached checkout.
    """

    required_outcome = "a recorded review verdict for the exact reviewed head SHA"

    def __init__(self, *, verdict_ready, worktree_reset=None, observer=None, handoff=None,
                 settle=None, prepare_settle=None, verdict_error=None) -> None:
        super().__init__(
            outcome_ready=verdict_ready, observer=observer, handoff=handoff,
            worktree_ready=worktree_reset or (lambda record: bool(record.source and record.target)))
        self._verdict_error = verdict_error or _contract_error
        self._settle = settle
        self._prepare_settle = prepare_settle

    def capture(self, record, obs) -> str | None:
        """Persist the exact verdict that completed Review.

        Provider artifacts establish the outcome once. Later settlement and successor handoffs
        consume this durable copy instead of reinterpreting a session that may have disappeared.
        """
        payload = (getattr(obs, "final_message", "") or "").strip()
        return payload if payload and self.verify(record, obs) else None

    def verify(self, record, obs):
        """Reject a review that used the retired GitHub follow-up creation action."""
        if _created_follow_up_issue(getattr(obs, "events", ())):
            return False
        return super().verify(record, obs)

    def recover(self, record, obs):
        """Review may own partial fixes in its detached checkout, so the shared continuation
        applies — with one exception. A review that did reach a verdict and only stated it in a
        rejected shape has already done the work; it earns one repair turn naming the parser's
        exact error instead of spending the continuation budget re-reviewing a head it has already
        cleared (issue #332)."""
        error = self._verdict_error(record, obs)
        if error:
            return contract_repair(record, error)
        return super().recover(record, obs)

    def finalize_completed(self, record) -> str | None:
        """Project a clean verdict through the repo merge policy; blocking verdicts wait for Revise."""
        if self._settle is not None:
            return self._settle(record)
        return None

    def prepare_completed(self, record) -> bool:
        """Run potentially slow merge-policy observation outside the store transaction."""
        return bool(self._prepare_settle(record)) if self._prepare_settle is not None else True
