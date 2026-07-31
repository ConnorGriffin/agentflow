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

``finalize_completed`` is the **single place the daemon records** the result. It closes explicit
no-build and concrete deferred outcomes; a handoff-required result instead leaves the ticket open,
indexes it under "Awaiting disposition", marks the pending state, releases the active claim, and
returns durable proof so the completed run can retire. The dispatched session never writes GitHub,
the map, or coordinator state itself (ADR 0037). On a hold — budget exhaustion, or a permanent
provider condition that stopped the session outright — ``finalize_hold`` parks the ticket in the
open: one comment naming what stopped the run, one durable label that takes it out of unattended
selection, and it is never dispatched again (ADR 362).
"""

from __future__ import annotations

from agentflow.coordinator.stage_adapter import StageAdapter


class ResearchStageAdapter(StageAdapter):
    """Observes a launched research session and verifies its findings outcome.

    Collaborators are injected so the stage is exercised without a real worktree or provider:
    ``findings_ready`` answers whether the session's durable findings for this ticket exist,
    ``resolve`` posts the findings and durably routes their disposition (the single result-writing
    point), ``park`` hands the ticket back to the operator on a hold, naming what stopped the run,
    ``worktree_ready`` proves the run's isolated worktree is present, and ``observer`` reconstructs
    the provider observation. Production wires these to the real worktree findings artifact and
    GitHub.
    """

    required_outcome = "recorded findings for the ticket"

    def __init__(self, *, findings_ready, resolve=None, park=None, worktree_ready=None,
                 observer=None) -> None:
        super().__init__(outcome_ready=findings_ready, worktree_ready=worktree_ready,
                         observer=observer, handoff=park)
        self._resolve = resolve

    def finalize_completed(self, record) -> str | None:
        """Durably route the findings, then retire the run with no successor.

        The proof may represent a closed no-build/deferred ticket or an intentionally open
        handoff-required ticket whose pending map entry, state label, and released claim all agree.
        Withholding it leaves the record completed-and-claimed so replay finishes the durable route.
        """
        if self._resolve is not None:
            return self._resolve(record)
        return f"proof:{record.identity}:resolved"
