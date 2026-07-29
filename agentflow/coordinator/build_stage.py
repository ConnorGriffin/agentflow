"""The Build stage adapter — the first live stage behind the session coordinator (ADR 0030,
issue #103).

Build extends the common adapter skeleton (:class:`~agentflow.coordinator.stage_adapter.
StageAdapter`) and adds what is its own: the expected PR on the owned branch as its required
outcome, and the integration collision a builder may report against ``origin/main``.

Because that outcome — not the provider's exit — decides the stage, a provider that exited badly
still completes if the PR exists, and a clean exit with no PR stays incomplete (ADR 0028
outcome-first). Build owns a branch and worktree, so ``prepare`` reuses the retained ones across a
continuation, which is how a fresh provider session picks up where an interrupted one left off.
"""

from __future__ import annotations

from agentflow.coordinator.stage_adapter import StageAdapter


class BuildStageAdapter(StageAdapter):
    """Observes a launched Build family and verifies its PR outcome.

    Beyond the shared collaborators, ``pr_exists`` answers whether the expected PR is open for the
    owned branch, ``integration_collision`` reads the collision the builder reported this attempt,
    and ``main_head`` reads the ``origin/main`` that collision is judged against.
    """

    required_outcome = "an opened pull request on the owned branch"

    def __init__(self, *, pr_exists, worktree_ready=None, observer=None, handoff=None,
                 integration_collision=None, main_head=None) -> None:
        super().__init__(worktree_ready=worktree_ready, observer=observer, handoff=handoff)
        self._pr_exists = pr_exists
        self._integration_collision = integration_collision
        self._main_head = main_head

    def prepare(self, record) -> bool:
        """Reuse the retained branch and worktree before admission, with one Build-only guard: a
        continuation that reported an integration collision stays unprepared while ``origin/main``
        still equals the head it collided on. That retry is provably identical, so it is deferred —
        never launched — until main moves (issue #209)."""
        if record.collision_main_sha is not None and self._main_head is not None:
            if self._main_head(record) == record.collision_main_sha:
                return False
        return super().prepare(record)

    def integration_collision(self, record) -> str | None:
        """The `origin/main` head this Build reported an integration collision against this attempt,
        or None when it reported none. Read from durable GitHub state, so it is independent of how
        the provider exited (issue #209)."""
        if self._integration_collision is None:
            return None
        return self._integration_collision(record)

    def verify(self, record, obs) -> bool:
        """The Build outcome is the expected PR on the owned branch, read from the record alone —
        the provider's own observation never enters the check (ADR 0028)."""
        return bool(self._pr_exists(record))
