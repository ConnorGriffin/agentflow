"""The Revise stage adapter — the third live stage behind the session coordinator (ADR 0030,
issue #105).

Revise has the same three adapter jobs as Build and Review (ADR 0030). It owns a branch and
worktree like Build, but proves a different outcome:

- ``prepare`` reuses the *retained* PR branch and worktree the original Build (and any earlier
  Revise) already owns — never recreating it, so an interrupted revise keeps its local changes
  and a fresh provider session continues them where the last left off. A miss consumes neither a
  permit nor an attempt, and the record simply waits and retries.
- ``observe`` reconstructs the provider observation once the reviser family ends.
- ``verify`` proves the stage's required outcome: the same PR branch now carries the verified
  pushed revision — its head moved past the reviewed SHA the revise was opened against — or the
  required non-code evidence is durably attached (ADR 0028). Provider exit can never stand in for
  it: a reviser that exited badly still completes if the revision is pushed, and a clean exit that
  pushed nothing stays incomplete.

Exhaustion or a permanent condition parks the PR for a human (ADR 0028's exhaustion table), the
Revise-native handoff — without discarding or force-committing the local work.
"""

from __future__ import annotations

from agentflow.coordinator.providers import ProviderObserver


class ReviseStageAdapter:
    """Observes a launched Revise family and verifies its pushed-revision outcome.

    The collaborators are injected so the stage is exercised without a real worktree, GitHub, or
    reviser: ``revision_ready`` answers whether the owned PR branch now carries the verified pushed
    revision (or the required durable non-code evidence) for the record, ``worktree_ready`` proves
    the retained branch/worktree the record already owns is present and checked out, and
    ``observer`` reconstructs the provider observation from the attempt's durable artifacts.
    Production reuses the retained builder worktree and checks the real PR branch head.
    """

    def __init__(self, *, revision_ready, worktree_ready=None, observer=None, handoff=None) -> None:
        self._revision_ready = revision_ready
        self._worktree_ready = worktree_ready or (lambda record: bool(record.source))
        self._observer = observer or ProviderObserver()
        self._handoff = handoff

    def prepare(self, record) -> bool:
        """Reuse the retained PR branch and worktree before admission (ADR 0030). Returns whether
        the owned worktree is present and checked out; a miss consumes no permit and no attempt, and
        the record simply waits and retries — its claim, lineage, branch, worktree, and local
        changes are untouched, so an interrupted revise resumes exactly where it left off."""
        return bool(self._worktree_ready(record))

    def observe(self, record):
        """Reconstruct the provider observation from the attempt's durable session artifacts.
        Extraction only — whether the revision landed is ``verify``'s call, never the provider's."""
        return self._observer.observe(record)

    def verify(self, record, obs) -> bool:
        """The Revise outcome is a verified pushed revision on the same PR branch (its head moved
        past the reviewed SHA), or the required durable non-code evidence (ADR 0028). Independent of
        how the reviser exited: a bad exit with the revision pushed completes; a clean exit that
        pushed nothing does not."""
        return bool(self._revision_ready(record, obs))

    def finalize_hold(self, record) -> str | None:
        """Create the Revise-native human handoff and return its durable proof. Production parks the
        PR and notifies once, leaving the local work intact; tests may omit the collaborator and use
        the coordinator's local proof."""
        if self._handoff is not None:
            return self._handoff(record)
        return f"proof:{record.identity}:pr:parked"
