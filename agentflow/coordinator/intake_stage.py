"""Intake's read-only stage adapter for durable continuation and routing."""

from __future__ import annotations

import json

from agentflow.coordinator.recovery import targeted_repair
from agentflow.coordinator.stage_adapter import StageAdapter
from agentflow.intake import IntakeResult, IntakeRoute, parse_intake
from agentflow.runner import Complexity, Effort


def encode_result(result: IntakeResult) -> str:
    return json.dumps({
        "route": result.route.value, "body": result.body, "title": result.title,
        "complexity": result.complexity.value if result.complexity else None,
        "effort": result.effort.value if result.effort else None,
        "parsed": result.parsed,
    }, sort_keys=True)


def decode_result(payload: str) -> IntakeResult:
    data = json.loads(payload)
    return IntakeResult(
        IntakeRoute(data["route"]), data.get("body", ""), data.get("title", ""),
        Complexity(data["complexity"]) if data.get("complexity") else None,
        Effort(data["effort"]) if data.get("effort") else None,
        bool(data.get("parsed", True)),
    )


class IntakeStageAdapter(StageAdapter):
    """Rebuild Intake's read-only source, capture its parsed route, and apply it once durable."""

    def __init__(self, *, worktree_reset, apply_route, claim_ready=None,
                 observer=None, handoff=None, worktree_dispose=None) -> None:
        super().__init__(worktree_ready=worktree_reset, observer=observer, handoff=handoff)
        self._apply_route = apply_route
        self._claim_ready = claim_ready or (lambda _record: True)
        # A completed Intake disposes its read-only checkout before retiring so it cannot linger
        # as ambiguous legacy activation evidence (issue #106). The default no-op keeps a bare
        # adapter's read-only stage side-effect-free; production wires the real disposer.
        self._worktree_dispose = worktree_dispose or (lambda _record: True)

    def prepare(self, record) -> bool:
        # Rebuild first, then prove the GitHub claim immediately before admission. A removed or
        # unreadable claim fails closed without consuming a permit or attempt.
        return bool(super().prepare(record) and self._claim_ready(record))

    def capture(self, record, obs) -> str | None:
        result = parse_intake(obs.final_message or "")
        return encode_result(result) if result.parsed else None

    def verify(self, record, obs) -> bool:
        # Intake's outcome is the route it captured, so the durable record is the whole check.
        return record.outcome is not None

    def recover(self, record, obs):
        """Intake is read-only: it owns no durable partial work, so a clean exit that parsed no
        route would replay identically. Grant one targeted repair naming the missing route, then
        stop (issue #225)."""
        return targeted_repair(record, "a parsed intake route (e.g. ready / grill / close)")

    def finalize_completed(self, record) -> str | None:
        if not record.outcome:
            return None
        url = self._apply_route(record, decode_result(record.outcome))
        if url is None:
            return None
        # Dispose the read-only worktree before returning the proof that lets the coordinator
        # retire this record. If it cannot be disposed, withhold the proof so settlement retries
        # next cycle rather than retiring over a checkout that would read as legacy evidence.
        if not self._worktree_dispose(record):
            return None
        return url
