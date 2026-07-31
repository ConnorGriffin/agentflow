"""The plan audit's read-only stage adapter for durable continuation and routing (ADR 380)."""

from __future__ import annotations

import json

from agentflow.coordinator.recovery import targeted_repair
from agentflow.coordinator.stage_adapter import StageAdapter
from agentflow.plan_audit import PlanAuditResult, PlanAuditVerdict, parse_plan_audit


def encode_result(result: PlanAuditResult) -> str:
    # `detail` rides along so *why* a fail-safe bounced survives the round trip — a bounce whose
    # reason was dropped between capture and projection leaves nothing anywhere saying whether the
    # auditor rejected the plan or never returned a readable verdict at all.
    return json.dumps({"verdict": result.verdict.value, "objections": result.objections,
                       "parsed": result.parsed, "detail": result.detail}, sort_keys=True)


def decode_result(payload: str) -> PlanAuditResult:
    data = json.loads(payload)
    return PlanAuditResult(PlanAuditVerdict(data["verdict"]), data.get("objections", ""),
                           bool(data.get("parsed", True)), data.get("detail", ""))


class PlanAuditStageAdapter(StageAdapter):
    """Rebuild the audit's read-only source, capture its verdict, and apply it once durable.

    The captured outcome is the verdict *including a fail-safe bounce*: an unreadable decision is
    still a durable answer — "this brief was not audited, hold it" — so it settles the stage
    rather than being replayed as a missing outcome. That is what keeps an unparseable verdict
    from ever leaving the issue silently ready and dispatchable.
    """

    def __init__(self, *, worktree_reset, apply_verdict, claim_ready=None,
                 observer=None, handoff=None, worktree_dispose=None) -> None:
        super().__init__(worktree_ready=worktree_reset, observer=observer, handoff=handoff)
        self._apply_verdict = apply_verdict
        self._claim_ready = claim_ready or (lambda _record: True)
        self._worktree_dispose = worktree_dispose or (lambda _record: True)

    def prepare(self, record) -> bool:
        # Rebuild first, then prove the GitHub claim immediately before admission. A removed or
        # unreadable claim fails closed without consuming a permit or attempt.
        return bool(super().prepare(record) and self._claim_ready(record))

    def capture(self, record, obs) -> str | None:
        # A session that produced no final message at all never ran; leave it uncaptured so the
        # attempt budget (not a fabricated verdict) decides what happens next. Anything the
        # session *did* say is parsed, and anything unreadable in it bounces.
        if not (obs.final_message or "").strip():
            return None
        return encode_result(parse_plan_audit(obs.final_message))

    def verify(self, record, obs) -> bool:
        # The audit's outcome is the verdict it captured, so the durable record is the whole check.
        return record.outcome is not None

    def recover(self, record, obs):
        """The audit is read-only: it owns no durable partial work, so a clean exit that produced
        no verdict would replay identically. Grant one targeted repair naming the missing verdict,
        then stop (issue #225)."""
        return targeted_repair(record, "a plan audit verdict (countersign or bounce)")

    def finalize_completed(self, record) -> str | None:
        if not record.outcome:
            return None
        url = self._apply_verdict(record, decode_result(record.outcome))
        if url is None:
            return None
        # Dispose the read-only worktree before returning the proof that lets the coordinator
        # retire this record, so no checkout survives to read as legacy activation evidence.
        if not self._worktree_dispose(record):
            return None
        return url
