"""The Converse stage adapter — a Conversation turn as one coordinated logical stage (ADR 0034).

An Ask holds a multi-turn conversation, and each operator message runs as one bounded,
non-interactive coordinated turn through the *existing* session coordinator — no second
orchestrator (ADR 0034). A turn's stable identity is ``(repository, Conversation ID, turn
ordinal)``, so submission stays idempotent and an interrupted turn continues rather than
duplicates.

It has the same three adapter jobs as the six pipeline stages (ADR 0030), and — like Respond —
it is terminal (a completed turn has no successor; the operator's next message submits the next
turn):

- ``prepare`` reuses the turn's isolated worktree before admission; a miss consumes neither a
  permit nor an attempt, so an interrupted turn resumes exactly where it left off.
- ``observe`` reconstructs the provider observation once the turn's session ends.
- ``verify`` proves the turn's required outcome: the session produced a durable reply in its
  worktree. Provider exit can never stand in for it — a clean exit that recorded *nothing* is
  incomplete and continues within budget (ADR 0034's outcome-first rule), which is the
  anti-duplication guarantee: a turn is only ever adopted once, so one identity yields one reply.

``finalize_completed`` is the **single place the daemon-side workspace adopts** the accepted turn
— it appends the immutable reply to the Conversation store and releases the turn's claim. The
methodology session itself never writes the workspace, GitHub, coordinator records, or
projections (ADR 0033/0034); only this finalizer adopts. On budget exhaustion ``finalize_hold``
parks the conversation "needs you", the operator's message preserved as the turn's immutable
prompt.
"""

from __future__ import annotations

from agentflow.coordinator.providers import ProviderObserver


class ConverseStageAdapter:
    """Observes a launched Conversation-turn family and verifies its reply outcome.

    Collaborators are injected so the stage is exercised without a real worktree or provider:
    ``reply_ready`` answers whether the session's durable reply for this turn exists, ``adopt``
    appends that reply into the daemon-owned workspace and returns its durable proof (the single
    adoption point), ``park`` creates the "needs you" hold preserving the operator's message,
    ``worktree_ready`` proves the turn's isolated worktree is present, and ``observer``
    reconstructs the provider observation. Production wires these to the real worktree reply
    artifact and the :class:`~agentflow.workspace.store.WorkspaceStore`.
    """

    def __init__(self, *, reply_ready, adopt=None, park=None, worktree_ready=None,
                 observer=None) -> None:
        self._reply_ready = reply_ready
        self._adopt = adopt
        self._park = park
        self._worktree_ready = worktree_ready or (lambda record: bool(record.source))
        self._observer = observer or ProviderObserver()

    def prepare(self, record) -> bool:
        """Reuse the turn's isolated worktree before admission (ADR 0030). A miss consumes no
        permit and no attempt; the turn waits and retries with its claim and local work intact, so
        an interrupted turn continues on the same worktree rather than starting a second session."""
        return bool(self._worktree_ready(record))

    def observe(self, record):
        """Reconstruct the provider observation from the attempt's durable session artifacts.
        Extraction only — whether the reply landed is ``verify``'s call, never the provider's."""
        return self._observer.observe(record)

    def verify(self, record, obs) -> bool:
        """The Converse outcome is a durable reply for this turn (ADR 0034 outcome-first),
        independent of how the session exited: a bad exit with a reply recorded completes; a clean
        exit that recorded nothing does not, and the turn continues within budget."""
        return bool(self._reply_ready(record, obs))

    def recover(self, record, obs):
        """A Converse turn works in a retained Ask worktree, so a continuation carries its
        partial reply forward. Continue within the budget behind a bounded recovery envelope
        pointing the fresh session at that worktree (issue #225)."""
        from agentflow.coordinator.recovery import durable_progress
        return durable_progress(record, "an appended reply for this conversation turn")

    def finalize_completed(self, record) -> str | None:
        """Adopt the accepted turn: append its immutable reply to the daemon-owned workspace and
        release the turn's claim, retiring the record with no successor. This is the *only* writer
        of the reply turn — the session never wrote workspace state itself. Withholding the proof
        (``None``) leaves the record completed-and-claimed so adoption retries next cycle rather
        than retiring over a turn it never durably appended."""
        if self._adopt is not None:
            return self._adopt(record)
        return f"proof:{record.identity}:turn-adopted"

    def finalize_hold(self, record) -> str | None:
        """Park the conversation "needs you" on exhaustion, preserving the operator's message (it
        is already the turn's immutable prompt), and return its durable proof. Idempotent and
        crash-safe: a repeat re-observes the durable park without re-notifying."""
        if self._park is not None:
            return self._park(record)
        return f"proof:{record.identity}:ask:parked"
