"""The Respond stage adapter — the fifth live stage behind the session coordinator (ADR 0030,
issue #107).

Respond answers one unanswered maintainer comment on an existing agentflow PR. It extends the
common adapter skeleton (:class:`~agentflow.coordinator.stage_adapter.StageAdapter`) and, like
Build and Revise, owns the retained PR branch and worktree — never recreating it, so an
interrupted respond keeps any local branch changes and a fresh provider session continues them.

Its required outcome is the marked agentflow reply for the maintainer comment this respond
answers, plus any branch change the responder made verified pushed. Provider exit can never stand
in for it — a responder that exited badly still completes once the reply is posted and its change
is pushed, and a clean exit that posted no reply (or left an unpushed local change) stays
incomplete (ADR 0028 outcome-first).

Respond is terminal: it has no successor stage, so a completed Respond releases its change claim
and the answered PR returns to the normal merge pipeline (like Intake releasing its triaging claim,
never a park). Exhaustion or a permanent condition instead parks the PR for a human (ADR 0028's
exhaustion table), the Respond-native handoff — without discarding or force-committing the local
work.
"""

from __future__ import annotations

from agentflow.coordinator.stage_adapter import StageAdapter


class RespondStageAdapter(StageAdapter):
    """Observes a launched Respond family and verifies its posted-reply outcome.

    Beyond the shared collaborators, ``reply_ready`` answers whether the marked agentflow reply for
    the answered maintainer comment is durable and any branch change is verified pushed, and
    ``settle`` releases the change claim on completion. Production reuses the retained PR-branch
    worktree and reads the real PR comments and branch head.
    """

    required_outcome = "a posted reply to the answered comment (and any pushed change)"

    def __init__(self, *, reply_ready, worktree_ready=None, observer=None, handoff=None,
                 settle=None) -> None:
        super().__init__(outcome_ready=reply_ready, worktree_ready=worktree_ready,
                         observer=observer, handoff=handoff)
        self._settle = settle

    def finalize_completed(self, record) -> str | None:
        """Release the change claim once the reply is durable, retiring the record with no
        successor and no human handoff — an answered PR simply returns to the normal merge
        pipeline (issue #107). Production drops the ``building`` claim label and returns the PR
        URL; tests may omit the collaborator and use the coordinator's local proof. Withholding
        the proof (``None``) leaves the record completed-and-claimed so settlement retries next
        cycle rather than retiring over a claim it never released."""
        if self._settle is not None:
            return self._settle(record)
        return f"proof:{record.identity}:building-released"
