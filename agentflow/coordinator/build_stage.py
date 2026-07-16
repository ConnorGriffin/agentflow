"""The Build stage adapter — the first live stage behind the session coordinator (ADR 0030,
issue #103).

A stage adapter has three jobs (ADR 0030): ``prepare`` owns the stage's source and recovery
and proves its claim *before* admission; ``observe`` reconstructs the provider observation once
the family ends; and ``verify`` checks the stage's own required outcome, which provider success
can never stand in for. For Build that outcome is the expected PR on the owned branch — so a
provider that exited badly still completes the stage if the PR exists, and a clean exit with no
PR stays incomplete (ADR 0028 outcome-first).

Because Build owns a branch and worktree, ``prepare`` reuses the retained ones across a
continuation rather than recreating them, which is how a fresh provider session picks up where
an interrupted one left off. A preparation miss returns False, so the coordinator consumes
neither a permit nor an attempt.
"""

from __future__ import annotations

from agentflow.coordinator.providers import ProviderObserver


class BuildStageAdapter:
    """Observes a launched Build family and verifies its PR outcome.

    The three collaborators are injected so the stage is exercised without a real worktree or
    GitHub: ``worktree_ready`` proves the branch/worktree the record already owns is present and
    checked out, ``observer`` reconstructs the provider observation from the attempt's durable
    artifacts, and ``pr_exists`` answers whether the expected PR is open for the owned branch.
    Production defaults reuse the real provider observer and treat a record with no source as
    unprepared.
    """

    def __init__(self, *, pr_exists, worktree_ready=None, observer=None, handoff=None) -> None:
        self._pr_exists = pr_exists
        self._worktree_ready = worktree_ready or (lambda record: bool(record.source))
        self._observer = observer or ProviderObserver()
        self._handoff = handoff

    def prepare(self, record) -> bool:
        """Reuse the retained branch and worktree before admission (ADR 0030). Returns whether
        the owned worktree is present and checked out; a miss consumes no permit and no attempt,
        and the record simply waits and retries — its claim, lineage, branch, and worktree are
        untouched."""
        return bool(self._worktree_ready(record))

    def observe(self, record):
        """Reconstruct the provider observation from the attempt's durable session artifacts.
        Extraction only — whether the stage is done is ``verify``'s call, never the provider's."""
        return self._observer.observe(record)

    def verify(self, record, obs) -> bool:
        """The Build outcome is the expected PR on the owned branch. Independent of how the
        provider exited: a bad exit with the PR present completes; a clean exit without it does
        not (ADR 0028)."""
        return bool(self._pr_exists(record))

    def finalize_hold(self, record) -> str | None:
        """Create the Build-native human handoff and return its durable proof. Production moves
        the issue to ``needs-grilling`` and notifies once; tests may omit the collaborator and
        use the coordinator's local proof for the dormant seam."""
        if self._handoff is not None:
            return self._handoff(record)
        return f"proof:{record.identity}:issue:needs-grilling"
