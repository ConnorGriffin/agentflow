"""The Research stage adapter — an unattended AFK research session as one coordinated stage (ADR 0037).

The daemon dispatches an unattended session to answer an open, unblocked, unclaimed
``wayfinder:research`` planning ticket. Each dispatch runs as one bounded coordinated stage through
the *existing* session coordinator — no second orchestrator (ADR 0037). Its stable identity is
``(repository, ticket number, research)``, so submission stays idempotent and an interrupted run
continues rather than duplicating.

It has the same adapter jobs as the six pipeline stages (ADR 0030) and — like Converse and Respond
— it is terminal (a resolved ticket has no successor stage):

- ``prepare`` provisions the run's isolated worktree before admission; a miss consumes neither a
  permit nor an attempt, so an interrupted run resumes exactly where it left off.
- ``observe`` reconstructs the provider observation once the session ends.
- ``verify`` proves the run's required outcome: the session recorded its findings in the worktree.
  Provider exit can never stand in for it — a clean exit that recorded nothing is incomplete and
  continues within budget (ADR 0037's outcome-first rule), which is the anti-duplication guarantee.

``finalize_completed`` is the **single place the daemon resolves** the ticket — it posts the findings
comment, closes the ticket, appends one titled line to the parent map's "Decisions so far", and
releases the shared ``wayfinder:resolving`` claim. The dispatched session never writes GitHub, the
map, or coordinator state itself (ADR 0037); only this finalizer resolves. On budget exhaustion
``finalize_hold`` releases the claim alone, leaving the ticket eligible again next cycle.
"""

from __future__ import annotations

from agentflow.coordinator.providers import ProviderObserver


class ResearchStageAdapter:
    """Observes a launched research session and verifies its findings outcome.

    Collaborators are injected so the stage is exercised without a real worktree or provider:
    ``findings_ready`` answers whether the session's durable findings for this ticket exist,
    ``resolve`` posts the findings, closes the ticket, appends the map breadcrumb, and releases the
    shared claim (the single resolution point), ``release`` drops the claim alone on a hold,
    ``worktree_ready`` proves the run's isolated worktree is present, and ``observer`` reconstructs
    the provider observation. Production wires these to the real worktree findings artifact and
    GitHub.
    """

    def __init__(self, *, findings_ready, resolve=None, release=None, worktree_ready=None,
                 observer=None) -> None:
        self._findings_ready = findings_ready
        self._resolve = resolve
        self._release = release
        self._worktree_ready = worktree_ready or (lambda record: bool(record.source))
        self._observer = observer or ProviderObserver()

    def prepare(self, record) -> bool:
        """Provision the run's isolated worktree before admission (ADR 0030). A miss consumes no
        permit and no attempt; the run waits and retries with its claim and local work intact, so an
        interrupted session continues on the same worktree rather than starting a second one."""
        return bool(self._worktree_ready(record))

    def observe(self, record):
        """Reconstruct the provider observation from the attempt's durable session artifacts.
        Extraction only — whether the findings landed is ``verify``'s call, never the provider's."""
        return self._observer.observe(record)

    def verify(self, record, obs) -> bool:
        """The Research outcome is durable findings for this ticket (ADR 0037 outcome-first),
        independent of how the session exited: a bad exit that still recorded findings completes; a
        clean exit that recorded nothing does not, and the run continues within budget."""
        return bool(self._findings_ready(record, obs))

    def recover(self, record, obs):
        """Research accumulates its notes in a retained worktree, so a continuation carries the
        gathered-so-far material forward. Continue within the budget behind a bounded recovery
        envelope pointing the fresh session at that worktree (issue #225)."""
        from agentflow.coordinator.recovery import durable_progress
        return durable_progress(record, "recorded findings for the ticket")

    def finalize_completed(self, record) -> str | None:
        """Resolve the ticket: post the findings comment, close the ticket, append the map
        breadcrumb, and release the shared claim, retiring the record with no successor. This is the
        *only* writer of the outcome — the session never wrote GitHub itself. Withholding the proof
        (``None``) leaves the record completed-and-claimed so resolution retries next cycle rather
        than retiring over a ticket it never durably resolved."""
        if self._resolve is not None:
            return self._resolve(record)
        return f"proof:{record.identity}:resolved"

    def finalize_hold(self, record) -> str | None:
        """Release the shared claim on exhaustion so the ticket is eligible again next cycle,
        returning its durable proof. Idempotent and crash-safe: a repeat re-proves the same
        release."""
        if self._release is not None:
            return self._release(record)
        return f"proof:{record.identity}:claim-released"
