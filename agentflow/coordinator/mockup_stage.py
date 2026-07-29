"""Mockup's branch-owning coordinated stage adapter (ADR 0030, issue #108)."""

from __future__ import annotations

from dataclasses import replace

from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.stage_adapter import StageAdapter


class MockupStageAdapter(StageAdapter):
    """Preserve one variant-round worktree and verify its committed visual outcome.

    Beyond the shared collaborators, ``outcome_ready`` answers whether committed variants and
    screenshots plus one marked issue comment exist, ``missing_context`` reports the durable
    MISSING-CONTEXT comment that is a human boundary rather than a provider failure, and
    ``settle`` releases the drawing claim at the human-pick boundary.
    """

    required_outcome = "a committed mockup round on the owned branch"

    def __init__(self, *, outcome_ready, worktree_ready=None, missing_context=None,
                 observer=None, handoff=None, settle=None) -> None:
        super().__init__(outcome_ready=outcome_ready, worktree_ready=worktree_ready,
                         observer=observer, handoff=handoff)
        self._missing_context = missing_context or (lambda record: False)
        self._settle = settle

    def observe(self, record):
        """Preserve provider facts; a durable MISSING-CONTEXT comment is a human boundary."""
        observation = super().observe(record)
        if self._missing_context(record):
            return replace(observation, cause=ProviderCause.PERMANENT)
        return observation

    def finalize_completed(self, record) -> str | None:
        """Release the drawing claim at the human-pick boundary and retire idempotently."""
        if self._settle is not None:
            return self._settle(record)
        return f"proof:{record.identity}:drawing-released"
