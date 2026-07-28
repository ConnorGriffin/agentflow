"""The Review stage adapter — the second live stage behind the session coordinator (ADR 0030,
issue #104).

Review has the same three adapter jobs as Build (ADR 0030), but its stage semantics differ:

- ``prepare`` owns a detached, writable checkout of the exact starting PR head SHA. A reviewer may
  ship bounded fixes, so continuations preserve that checkout and its partial work; a miss consumes
  neither a permit nor an attempt, exactly as Build's does.
- ``observe`` reconstructs the provider observation once the reviewer family ends.
- ``verify`` proves the stage's required outcome: a parsed verdict for the *exact* reviewed head
  SHA. Provider success can never stand in for it — a reviewer that exited badly still completes
  the stage if a verdict for the target SHA is durable, and a clean exit whose verdict is missing
  or names a different SHA stays incomplete (ADR 0028 outcome-first).

Exhaustion or a permanent condition parks the PR for a human (ADR 0028's exhaustion table), which
is the Review-native handoff.
"""

from __future__ import annotations

from agentflow.coordinator.providers import ProviderObserver


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


class ReviewStageAdapter:
    """Observes a launched Review family and verifies its verdict outcome.

    The collaborators are injected so the stage is exercised without a real worktree, GitHub, or
    reviewer: ``verdict_ready`` answers whether a parsed verdict for the record's exact target SHA
    is durable, ``worktree_reset`` prepares the writable detached checkout at that SHA and returns
    whether it is ready, and ``observer`` reconstructs the provider observation from the attempt's
    durable artifacts. Production wires the real verdict parse and retained detached checkout.
    """

    def __init__(self, *, verdict_ready, worktree_reset=None, observer=None, handoff=None,
                 settle=None, prepare_settle=None, verdict_error=None) -> None:
        self._verdict_ready = verdict_ready
        self._verdict_error = verdict_error or _contract_error
        self._worktree_reset = worktree_reset or (
            lambda record: bool(record.source and record.target))
        self._observer = observer or ProviderObserver()
        self._handoff = handoff
        self._settle = settle
        self._prepare_settle = prepare_settle

    def prepare(self, record) -> bool:
        """Prepare the writable checkout at the starting head SHA before admission (ADR 0030).
        A continuation retains local review fixes; a fresh record starts from its immutable target.
        A miss consumes no permit and no attempt — the record simply waits and retries."""
        return bool(self._worktree_reset(record))

    def observe(self, record):
        """Reconstruct the provider observation from the attempt's durable session artifacts.
        Extraction only — whether the verdict exists is ``verify``'s call, never the provider's."""
        return self._observer.observe(record)

    def verify(self, record, obs) -> bool:
        """The Review outcome is a parsed verdict for the exact reviewed head SHA (ADR 0028).
        Independent of how the reviewer exited: a bad exit with the verdict present completes; a
        clean exit whose verdict is absent or names another SHA does not."""
        return bool(self._verdict_ready(record, obs))

    def capture(self, record, obs) -> str | None:
        """Persist the exact verdict that completed Review.

        Provider artifacts establish the outcome once. Later settlement and successor handoffs
        consume this durable copy instead of reinterpreting a session that may have disappeared.
        """
        payload = (getattr(obs, "final_message", "") or "").strip()
        return payload if payload and self._verdict_ready(record, obs) else None

    def recover(self, record, obs):
        """Review may own partial fixes in its detached checkout. Preserve and continue them within
        the bounded attempt budget while naming the missing exact-head verdict.

        A review that did reach a verdict and only stated it in a rejected shape has already done
        the work; it earns one repair turn naming the parser's exact error instead of spending the
        continuation budget re-reviewing a head it has already cleared (issue #332)."""
        from agentflow.coordinator.recovery import contract_repair, durable_progress
        error = self._verdict_error(record, obs)
        if error:
            return contract_repair(record, error)
        return durable_progress(record, "a recorded review verdict for the exact reviewed head SHA")

    def finalize_hold(self, record) -> str | None:
        """Create the Review-native human handoff and return its durable proof. Production parks
        the PR and notifies once; tests may omit the collaborator and use the coordinator's local
        proof."""
        if self._handoff is not None:
            return self._handoff(record)
        return f"proof:{record.identity}:pr:parked"

    def finalize_completed(self, record) -> str | None:
        """Project a clean verdict through the repo merge policy; blocking verdicts wait for Revise."""
        if self._settle is not None:
            return self._settle(record)
        return None

    def prepare_completed(self, record) -> bool:
        """Run potentially slow merge-policy observation outside the store transaction."""
        return bool(self._prepare_settle(record)) if self._prepare_settle is not None else True
