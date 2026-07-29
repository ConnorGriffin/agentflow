"""The Converse stage adapter — a Conversation turn as one coordinated logical stage (ADR 0034).

An Ask holds a multi-turn conversation, and each operator message runs as one bounded,
non-interactive coordinated turn through the *existing* session coordinator — no second
orchestrator (ADR 0034). A turn's stable identity is ``(repository, Conversation ID, turn
ordinal)``, so submission stays idempotent and an interrupted turn continues rather than
duplicates.

It extends the common adapter skeleton (:class:`~agentflow.coordinator.stage_adapter.
StageAdapter`) and — like Respond and Research — is terminal: a completed turn has no successor;
the operator's next message submits the next turn. Its required outcome is a durable reply in the
turn's worktree. Provider exit can never stand in for it — a clean exit that recorded *nothing* is
incomplete and continues within budget (ADR 0034's outcome-first rule), which is the
anti-duplication guarantee: a turn is only ever adopted once, so one identity yields one reply.

``finalize_completed`` is the **single place the daemon-side workspace adopts** the accepted turn
— it appends the immutable reply to the Conversation store and releases the turn's claim. The
methodology session itself never writes the workspace, GitHub, coordinator records, or
projections (ADR 0033/0034); only this finalizer adopts. On budget exhaustion the shared
``finalize_hold`` parks the conversation "needs you", the operator's message preserved as the
turn's immutable prompt.
"""

from __future__ import annotations

from agentflow.coordinator.stage_adapter import StageAdapter


class ConverseStageAdapter(StageAdapter):
    """Observes a launched Conversation-turn family and verifies its reply outcome.

    Beyond the shared collaborators, ``reply_ready`` answers whether the session's durable reply
    for this turn exists, ``adopt`` appends that reply into the daemon-owned workspace and returns
    its durable proof (the single adoption point), and ``park`` creates the "needs you" hold
    preserving the operator's message. Production wires these to the real worktree reply artifact
    and the :class:`~agentflow.workspace.store.WorkspaceStore`.
    """

    required_outcome = "an appended reply for this conversation turn"

    def __init__(self, *, reply_ready, adopt=None, park=None, worktree_ready=None,
                 observer=None) -> None:
        super().__init__(outcome_ready=reply_ready, worktree_ready=worktree_ready,
                         observer=observer, handoff=park)
        self._adopt = adopt

    def finalize_completed(self, record) -> str | None:
        """Adopt the accepted turn: append its immutable reply to the daemon-owned workspace and
        release the turn's claim, retiring the record with no successor. This is the *only* writer
        of the reply turn — the session never wrote workspace state itself. Withholding the proof
        (``None``) leaves the record completed-and-claimed so adoption retries next cycle rather
        than retiring over a turn it never durably appended."""
        if self._adopt is not None:
            return self._adopt(record)
        return f"proof:{record.identity}:turn-adopted"
