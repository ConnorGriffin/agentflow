"""The attacker's read-only stage adapter for durable continuation and routing (ADR 380)."""

from __future__ import annotations

import json

from agentflow.attack import AttackResult, parse_attack
from agentflow.coordinator.recovery import targeted_repair
from agentflow.coordinator.stage_adapter import StageAdapter


def encode_result(result: AttackResult) -> str:
    # `detail` rides along so *why* an unreadable answer spent a round survives the trip — a
    # round whose reason was dropped between capture and settlement leaves nothing anywhere
    # saying whether the attacker found something or never answered at all.
    return json.dumps({"objections": result.objections, "parsed": result.parsed,
                       "detail": result.detail}, sort_keys=True)


def decode_result(payload: str) -> AttackResult:
    data = json.loads(payload)
    return AttackResult(data.get("objections", ""), bool(data.get("parsed", True)),
                        data.get("detail", ""))


class AttackStageAdapter(StageAdapter):
    """Rebuild the attacker's read-only source, capture its objections, and settle the round.

    The captured outcome includes an *unreadable* answer: that is still a durable fact about the
    round — nobody attacked this draft — so it settles the stage and spends the round rather than
    being replayed as a missing outcome. What it must never do is read as the draft surviving,
    which is why :class:`~agentflow.attack.AttackResult` decides that and not this adapter.
    """

    def __init__(self, *, worktree_reset, apply_objections, claim_ready=None,
                 observer=None, handoff=None, worktree_dispose=None) -> None:
        super().__init__(worktree_ready=worktree_reset, observer=observer, handoff=handoff)
        self._apply_objections = apply_objections
        self._claim_ready = claim_ready or (lambda _record: True)
        self._worktree_dispose = worktree_dispose or (lambda _record: True)

    def prepare(self, record) -> bool:
        # Rebuild first, then prove the transferred triaging claim immediately before admission.
        # A removed or unreadable claim fails closed without consuming a permit or attempt.
        return bool(super().prepare(record) and self._claim_ready(record))

    def capture(self, record, obs) -> str | None:
        # A session that produced no final message at all never ran; leave it uncaptured so the
        # attempt budget (not a fabricated verdict) decides what happens next. Anything the
        # session *did* say is parsed, and anything unreadable in it spends the round.
        if not (obs.final_message or "").strip():
            return None
        return encode_result(parse_attack(obs.final_message))

    def verify(self, record, obs) -> bool:
        # The round's outcome is the answer it captured, so the durable record is the whole check.
        return record.outcome is not None

    def recover(self, record, obs):
        """An attack is read-only: it owns no durable partial work, so a clean exit that produced
        no answer would replay identically. Grant one targeted repair naming the missing
        objections, then stop (issue #225)."""
        return targeted_repair(record, "an attack answer (numbered objections, or none)")

    def finalize_completed(self, record) -> str | None:
        if not record.outcome:
            return None
        url = self._apply_objections(record, decode_result(record.outcome))
        if url is None:
            # Either the draft still owes a redraft — the common case, and the redraft opener
            # takes the claim from here — or a projection could not be proved. Both keep the
            # record completed-and-unretired, so neither strands the issue.
            return None
        if not self._worktree_dispose(record):
            return None
        return url
