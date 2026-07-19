"""The Respond stage adapter — the fifth live stage behind the session coordinator (ADR 0030,
issue #107).

Respond answers one unanswered maintainer comment on an existing agentflow PR. It has the same
three adapter jobs as Build and Revise (ADR 0030) and, like them, owns the PR branch and worktree:

- ``prepare`` reuses the *retained* PR branch and worktree the original change already owns —
  never recreating it, so an interrupted respond keeps any local branch changes and a fresh
  provider session continues them where the last left off. A miss consumes neither a permit nor an
  attempt, and the record simply waits and retries.
- ``observe`` reconstructs the provider observation once the responder family ends.
- ``verify`` proves the stage's required outcome: the marked agentflow reply now exists for the
  maintainer comment this respond answers, and any branch change the responder made is verified
  pushed. Provider exit can never stand in for it — a responder that exited badly still completes
  once the reply is posted and its change is pushed, and a clean exit that posted no reply (or left
  an unpushed local change) stays incomplete (ADR 0028 outcome-first).

Respond is terminal: it has no successor stage, so a completed Respond releases its change claim
and the answered PR returns to the normal merge pipeline (like Intake releasing its triaging claim,
never a park). Exhaustion or a permanent condition instead parks the PR for a human (ADR 0028's
exhaustion table), the Respond-native handoff — without discarding or force-committing the local
work.
"""

from __future__ import annotations

from agentflow.coordinator.providers import ProviderObserver


class RespondStageAdapter:
    """Observes a launched Respond family and verifies its posted-reply outcome.

    The collaborators are injected so the stage is exercised without a real worktree, GitHub, or
    responder: ``reply_ready`` answers whether the marked agentflow reply for the answered
    maintainer comment is durable and any branch change is verified pushed, ``worktree_ready``
    proves the retained branch/worktree the record already owns is present and checked out, and
    ``observer`` reconstructs the provider observation from the attempt's durable artifacts.
    Production reuses the retained PR-branch worktree and reads the real PR comments and branch head.
    """

    def __init__(self, *, reply_ready, worktree_ready=None, observer=None, handoff=None,
                 settle=None) -> None:
        self._reply_ready = reply_ready
        self._worktree_ready = worktree_ready or (lambda record: bool(record.source))
        self._observer = observer or ProviderObserver()
        self._handoff = handoff
        self._settle = settle

    def prepare(self, record) -> bool:
        """Reuse the retained PR branch and worktree before admission (ADR 0030). Returns whether
        the owned worktree is present and checked out; a miss consumes no permit and no attempt, and
        the record simply waits and retries — its claim, lineage, branch, worktree, and local
        changes are untouched, so an interrupted respond resumes exactly where it left off."""
        return bool(self._worktree_ready(record))

    def observe(self, record):
        """Reconstruct the provider observation from the attempt's durable session artifacts.
        Extraction only — whether the reply landed is ``verify``'s call, never the provider's."""
        return self._observer.observe(record)

    def verify(self, record, obs) -> bool:
        """The Respond outcome is the marked agentflow reply to the answered maintainer comment,
        plus any branch change verified pushed (ADR 0028). Independent of how the responder exited:
        a bad exit with the reply posted and its change pushed completes; a clean exit that posted
        no reply, or left an unpushed local change, does not."""
        return bool(self._reply_ready(record, obs))

    def recover(self, record, obs):
        """Respond reuses the PR-branch worktree across a continuation, so a continuation carries
        any partial reply/change forward. Continue within the budget behind a bounded recovery
        envelope pointing the fresh session at that worktree (issue #225)."""
        from agentflow.coordinator.recovery import durable_progress
        return durable_progress(record, "a posted reply to the answered comment (and any pushed change)")

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

    def finalize_hold(self, record) -> str | None:
        """Create the Respond-native human handoff and return its durable proof. Production parks
        the PR and notifies once, leaving the local work intact; tests may omit the collaborator and
        use the coordinator's local proof."""
        if self._handoff is not None:
            return self._handoff(record)
        return f"proof:{record.identity}:pr:parked"
