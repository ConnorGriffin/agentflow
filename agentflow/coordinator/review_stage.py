"""The Review stage adapter — the second live stage behind the session coordinator (ADR 0030,
issue #104).

Review has the same three adapter jobs as Build (ADR 0030), but its stage semantics differ:

- ``prepare`` owns a *read-only* checkout of the exact reviewed PR head SHA. Review holds no
  local edits, so preparation may freely discard and recreate that checkout from durable source
  state without losing the record's immutable target; a miss consumes neither a permit nor an
  attempt, exactly as Build's does.
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


class ReviewStageAdapter:
    """Observes a launched Review family and verifies its verdict outcome.

    The collaborators are injected so the stage is exercised without a real worktree, GitHub, or
    reviewer: ``verdict_ready`` answers whether a parsed verdict for the record's exact target SHA
    is durable, ``worktree_reset`` recreates the read-only checkout at that SHA and returns whether
    it is ready, and ``observer`` reconstructs the provider observation from the attempt's durable
    artifacts. Production wires the real verdict parse and a fresh detached checkout.
    """

    def __init__(self, *, verdict_ready, worktree_reset=None, observer=None, handoff=None) -> None:
        self._verdict_ready = verdict_ready
        self._worktree_reset = worktree_reset or (
            lambda record: bool(record.source and record.target))
        self._observer = observer or ProviderObserver()
        self._handoff = handoff

    def prepare(self, record) -> bool:
        """Recreate the read-only checkout at the reviewed head SHA before admission (ADR 0030).
        Review keeps no local changes, so a stale checkout is discarded and rebuilt from durable
        source state; the immutable target SHA is part of the record's identity and is never
        touched. A miss consumes no permit and no attempt — the record simply waits and retries."""
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

    def finalize_hold(self, record) -> str | None:
        """Create the Review-native human handoff and return its durable proof. Production parks
        the PR and notifies once; tests may omit the collaborator and use the coordinator's local
        proof."""
        if self._handoff is not None:
            return self._handoff(record)
        return f"proof:{record.identity}:pr:parked"
