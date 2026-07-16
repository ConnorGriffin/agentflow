"""Mockup's branch-owning coordinated stage adapter (ADR 0030, issue #108)."""

from __future__ import annotations

from dataclasses import replace

from agentflow.coordinator.providers import ProviderCause, ProviderObserver


class MockupStageAdapter:
    """Preserve one variant-round worktree and verify its committed visual outcome.

    The collaborators keep GitHub, git, and provider details behind this stage-local interface;
    callers only submit the durable stage facts and cycle the coordinator.
    """

    def __init__(self, *, outcome_ready, worktree_ready=None, missing_context=None,
                 observer=None, handoff=None, settle=None) -> None:
        self._outcome_ready = outcome_ready
        self._worktree_ready = worktree_ready or (lambda record: bool(record.source))
        self._missing_context = missing_context or (lambda record: False)
        self._observer = observer or ProviderObserver()
        self._handoff = handoff
        self._settle = settle

    def prepare(self, record) -> bool:
        """Reuse or create the one owned branch/worktree before consuming permits."""
        return bool(self._worktree_ready(record))

    def observe(self, record):
        """Preserve provider facts; a durable MISSING-CONTEXT comment is a human boundary."""
        observation = self._observer.observe(record)
        if self._missing_context(record):
            return replace(observation, cause=ProviderCause.PERMANENT)
        return observation

    def verify(self, record, obs) -> bool:
        """Only committed variants/screenshots plus one marked issue comment complete Mockup."""
        return bool(self._outcome_ready(record, obs))

    def finalize_completed(self, record) -> str | None:
        """Release the drawing claim at the human-pick boundary and retire idempotently."""
        if self._settle is not None:
            return self._settle(record)
        return f"proof:{record.identity}:drawing-released"

    def finalize_hold(self, record) -> str | None:
        """Create Mockup's issue-native hold without discarding its local work."""
        if self._handoff is not None:
            return self._handoff(record)
        return f"proof:{record.identity}:issue:needs-mockup"
