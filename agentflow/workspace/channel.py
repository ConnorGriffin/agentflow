"""The local daemon command channel (ADR 0033).

The web layer never applies domain transitions. A workspace command (open an Ask, send a turn)
is transported from FastAPI's POST endpoint to the daemon through this local channel; only the
daemon interprets and applies it. If the daemon is unavailable the command fails *unavailable* —
there is no direct-write fallback (ADR 0033), so a browser can never write workspace state or
launch a session on its own.

The transport is a small append-only spool of command files under ``AGENTFLOW_STATE/workspace``.
The web side atomically writes one file per idempotency key; the daemon drains them each cycle,
applies each through the idempotent workspace command surface, and acknowledges (removes) it. A
crash between apply and acknowledge simply re-applies — the store's idempotency key makes that a
no-op that replays the same outcome.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agentflow import live
from agentflow.workspace.store import workspace_dir

# How fresh the daemon's last cycle must be for a command to be accepted. The daemon stamps its
# status every fast tick (~15s); a spool write is only honored when a daemon is demonstrably
# alive to drain it, so the web layer fails closed instead of letting commands pile up unread.
_LIVENESS_WINDOW_S = int(os.environ.get("AGENTFLOW_WORKSPACE_LIVENESS_S", "90"))


def commands_dir() -> Path:
    return workspace_dir() / "commands"


def daemon_available(*, now: float | None = None, window_s: int = _LIVENESS_WINDOW_S) -> bool:
    """Whether a daemon is alive to drain commands, from its last-cycle stamp. The web layer
    checks this before enqueuing so a command fails *unavailable* when the daemon is down rather
    than being written to a spool nothing will ever read (ADR 0033)."""
    status = live.daemon_status()
    stamped = status.get("last_cycle_at")
    if not stamped:
        return False
    try:
        from datetime import datetime
        last = datetime.fromisoformat(stamped).timestamp()
    except (TypeError, ValueError):
        return False
    return (time.time() if now is None else now) - last <= window_s


def enqueue(command: dict) -> None:
    """Atomically write one command to the spool, keyed by its idempotency key. A repeat of the
    same key overwrites the same file, so a retried POST never enqueues a second command."""
    key = command["key"]
    path = commands_dir()
    path.mkdir(parents=True, exist_ok=True)
    tmp = path / f".{key}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(command))
    os.replace(tmp, path / f"{key}.json")


def pending() -> list[dict]:
    """Every spooled command, oldest first — the daemon drains these each cycle."""
    path = commands_dir()
    if not path.exists():
        return []
    files = sorted((f for f in path.glob("*.json")), key=lambda f: f.stat().st_mtime)
    out: list[dict] = []
    for f in files:
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def ack(key: str) -> None:
    """Acknowledge a drained command by removing its spool file. Safe to skip on crash — the
    store's idempotency key makes a re-applied command a no-op."""
    try:
        (commands_dir() / f"{key}.json").unlink(missing_ok=True)
    except OSError:
        pass
