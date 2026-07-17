"""The bounded workspace JSON read model (ADR 0033).

The daemon is the only writer of read models. It turns the durable workspace facts into a
bounded JSON projection the web layer serves; the projection is disposable and never a recovery
source or policy input. A generation carries its own read-model time and workspace revision so
the console can age it honestly, exactly like the fleet snapshot (ADR 0026/0033).

This projection surfaces the "in a conversation" background weight and the turn transcript, and —
for the build-issue tracer — the staged Build-Issue Proposals: each carries its immutable versions
and their content hashes, the exact hash awaiting decision, and, once published, its verified issue
receipt so the console can render the copper "awaiting your decision" weight and the non-copper
"published ✓" weight (ADR 0033/0034). A discarded Proposal is dropped from the read model. Turn
states are surfaced honestly (working / replied / paused), never faked as live chat (ADR 0034).
"""

from __future__ import annotations

from datetime import datetime, timezone

from agentflow.workspace.store import DISCARDED, PAUSED, WORKING


def _iso(epoch: int) -> str | None:
    if not epoch:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _turn_json(turn) -> dict:
    return {
        "ordinal": turn.ordinal,
        "prompt": turn.prompt,
        "reply": turn.reply,
        "state": turn.state,
        "skill": turn.skill,
        "priority": turn.priority,
        "started_at": _iso(turn.started_at),
        "replied_at": _iso(turn.replied_at),
        "park_reason": turn.park_reason,
    }


def _current_turn(convo) -> dict | None:
    """The last turn's honest state summary for the shelf / dialogue state block."""
    if not convo.turns:
        return None
    turn = convo.turns[-1]
    note = None
    if turn.state == WORKING:
        note = "bounded background session in flight"
    elif turn.state == PAUSED:
        note = turn.park_reason or "parked — needs you"
    return {
        "state": turn.state,
        "ordinal": turn.ordinal,
        "skill": turn.skill,
        "priority": turn.priority,
        "started_at": _iso(turn.started_at),
        "note": note,
    }


def _conversation_json(convo) -> dict:
    last = convo.turns[-1] if convo.turns else None
    return {
        "id": convo.id,
        "title": convo.title,
        "verb": convo.verb,
        "scope": convo.scope,
        "state": convo.state,
        "revision": convo.revision,
        "opened_at": _iso(convo.created_at),
        "last_turn_at": _iso(last.replied_at or last.started_at) if last else None,
        "turns_count": len(convo.turns),
        "turn": _current_turn(convo),
        "turns": [_turn_json(t) for t in convo.turns],
    }


def _version_json(v, *, current: bool) -> dict:
    return {
        "version": v.version,
        "content_hash": v.content_hash,
        "staged_at": _iso(v.staged_at),
        "current": current,
    }


def _proposal_json(prop) -> dict:
    """One Build-Issue Proposal's read model: the latest version drives the copper card / approval
    view; every version is listed so the operator can approve the exact hash they saw."""
    latest = prop.latest
    return {
        "id": prop.conversation_id,
        "conversation_id": prop.conversation_id,
        "state": prop.state,
        "title": prop.title,
        "summary": latest.summary if latest else "",
        "acceptance": latest.acceptance if latest else [],
        "version": latest.version if latest else 0,
        "content_hash": latest.content_hash if latest else None,
        "staged_at": _iso(latest.staged_at) if latest else None,
        "approved_hash": prop.approved_hash,
        "versions": [_version_json(v, current=(latest is not None and v.version == latest.version))
                     for v in prop.versions],
        "publication": ({
            "issue_number": prop.published_issue,
            "issue_url": prop.published_url,
            "published_at": _iso(prop.published_at),
        } if prop.state == "published" else None),
    }


def workspace_projection(projects: list[dict], *, read_model_at: int, revision: int,
                         daemon_available: bool = True) -> dict:
    """Assemble the bounded workspace projection.

    ``projects`` is one entry per enrolled Project: ``{"id", "repo", "profile", "conversations"}``
    where ``conversations`` is an iterable of :class:`~agentflow.workspace.store.Conversation`.
    A generation is only current when the whole projection is durable, so the daemon builds this
    from a settled read of the store and publishes it atomically.
    """
    return {
        "workspace": {
            "read_model_at": _iso(read_model_at),
            "revision": revision,
            "available": daemon_available,
        },
        "projects": [
            {
                "id": p["id"],
                "repo": p["repo"],
                "profile": p.get("profile", ""),
                "conversations": [_conversation_json(c) for c in p["conversations"]],
                "proposals": [_proposal_json(pr) for pr in p.get("proposals", [])
                              if pr.state != DISCARDED],
            }
            for p in projects
        ],
    }
