"""The Revise stage adapter — the third live stage behind the session coordinator (ADR 0030,
issue #105).

Revise extends the common adapter skeleton (:class:`~agentflow.coordinator.stage_adapter.
StageAdapter`). Like Build it owns the retained PR branch and worktree — never recreating it, so
an interrupted revise keeps its local changes and a fresh provider session continues them — but it
proves a different outcome: the same PR branch now carries the verified pushed revision, its head
moved past the reviewed SHA the revise was opened against, or the required non-code evidence is
durably attached (ADR 0028). Provider exit can never stand in for it: a reviser that exited badly
still completes if the revision is pushed, and a clean exit that pushed nothing stays incomplete.

Genuine conflict ambiguity is Revise's own capture. Exhaustion or a permanent condition parks the
PR for a human (ADR 0028's exhaustion table), the Revise-native handoff — without discarding or
force-committing the local work.
"""

from __future__ import annotations

from agentflow.coordinator.stage_adapter import StageAdapter


class ReviseStageAdapter(StageAdapter):
    """Observes a launched Revise family and verifies its pushed-revision outcome.

    Beyond the shared collaborators, ``revision_ready`` answers whether the owned PR branch now
    carries the verified pushed revision (or the required durable non-code evidence) for the
    record, and ``uncertainty`` records genuine conflict ambiguity. Production reuses the retained
    builder worktree and checks the real PR branch head.
    """

    required_outcome = "a pushed revision on the PR branch"

    def __init__(self, *, revision_ready, worktree_ready=None, observer=None, handoff=None,
                 uncertainty=None) -> None:
        super().__init__(outcome_ready=revision_ready, worktree_ready=worktree_ready,
                         observer=observer, handoff=handoff)
        self._uncertainty = uncertainty

    def capture(self, record, obs) -> str | None:
        """Capture genuine conflict ambiguity as private durable state, never a PR comment."""
        if self._uncertainty is None or not record.conflict_round:
            return None
        return self._uncertainty(record, obs)
