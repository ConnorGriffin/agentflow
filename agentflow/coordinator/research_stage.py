"""The Research stage adapter — an unattended AFK research session as one coordinated stage (ADR 0037).

The daemon dispatches an unattended session to answer an open, unblocked, unclaimed
``wayfinder:research`` planning ticket. Each dispatch runs as one bounded coordinated stage through
the *existing* session coordinator — no second orchestrator (ADR 0037). Its stable identity is
``(repository, ticket number, research)``, so submission stays idempotent and an interrupted run
continues rather than duplicating.

It extends the common adapter skeleton (:class:`~agentflow.coordinator.stage_adapter.
StageAdapter`) and — like Converse and Respond — is terminal (a resolved ticket has no successor
stage). Its required outcome is the findings the session recorded in its worktree. Provider exit
can never stand in for it — a clean exit that recorded nothing is incomplete and continues within
budget (ADR 0037's outcome-first rule), which is the anti-duplication guarantee.

``finalize_completed`` is the **single place the daemon resolves** the ticket — it posts the findings
comment, closes the ticket, appends one titled line to the parent map's "Decisions so far", and
releases the shared ``wayfinder:resolving`` claim. The dispatched session never writes GitHub, the
map, or coordinator state itself (ADR 0037); only this finalizer resolves. On budget exhaustion the
shared ``finalize_hold`` releases the claim alone, leaving the ticket eligible again next cycle.
"""

from __future__ import annotations

from agentflow.coordinator.stage_adapter import StageAdapter


class ResearchStageAdapter(StageAdapter):
    """Observes a launched research session and verifies its findings outcome.

    Beyond the shared collaborators, ``findings_ready`` answers whether the session's durable
    findings for this ticket exist, ``resolve`` posts the findings, closes the ticket, appends the
    map breadcrumb, and releases the shared claim (the single resolution point), and ``release``
    drops the claim alone on a hold. Production wires these to the real worktree findings artifact
    and GitHub.
    """

    required_outcome = "recorded findings for the ticket"

    def __init__(self, *, findings_ready, resolve=None, release=None, worktree_ready=None,
                 observer=None) -> None:
        super().__init__(outcome_ready=findings_ready, worktree_ready=worktree_ready,
                         observer=observer, handoff=release)
        self._resolve = resolve

    def finalize_completed(self, record) -> str | None:
        """Resolve the ticket: post the findings comment, close the ticket, append the map
        breadcrumb, and release the shared claim, retiring the record with no successor. This is the
        *only* writer of the outcome — the session never wrote GitHub itself. Withholding the proof
        (``None``) leaves the record completed-and-claimed so resolution retries next cycle rather
        than retiring over a ticket it never durably resolved."""
        if self._resolve is not None:
            return self._resolve(record)
        return f"proof:{record.identity}:resolved"
