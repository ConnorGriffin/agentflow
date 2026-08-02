"""Generated live-session projections for the operator console.

The daemon atomically replaces this file from durable coordinator ``running`` records. No
provider, runner, or recovery path mutates individual entries, and no production decision reads
it. The console may read it as derived state; corrupt or missing projections render as idle.

Reads fail soft: a missing, partial, or corrupt file reads as "fleet idle" (no running
sessions), never an error — the console must render an empty board, not a 500. Writes are
atomic (temp + rename) so a concurrent reader never sees a half-written file.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from agentflow.state import state_dir

STATE_DIR = state_dir()
LIVE_FILE = STATE_DIR / "live-sessions.json"
DAEMON_FILE = STATE_DIR / "daemon-status.json"
SNAPSHOT_FILE = STATE_DIR / "snapshot.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return default


def _entries() -> list[dict]:
    data = _read(LIVE_FILE, [])
    return data if isinstance(data, list) else []


def _write_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data))
    os.replace(tmp, path)


def running() -> list[dict]:
    """Every recorded live session. `[]` on a missing / partial / corrupt file (fleet idle)."""
    return _entries()


def replace_projection(entries: list[dict]) -> None:
    """Publish the coordinator's running rows as the entire live-board projection.

    The board is write-only derived state for the console. Production ownership, recovery,
    attempts, claims, and permits never read it (issue #109).
    """
    _write_atomic(LIVE_FILE, list(entries))


def mark_cycle(poll_seconds: int) -> None:
    """Stamp the daemon's status after a cycle — when it last ran and how often it polls,
    the runtime facts the snapshot's `daemon` block needs but the snapshot builder can't see."""
    _write_atomic(DAEMON_FILE, {"last_cycle_at": _now(), "poll_seconds": poll_seconds})


def daemon_status() -> dict:
    """The daemon's last-cycle / poll status, or `{}` when it hasn't run a cycle yet."""
    data = _read(DAEMON_FILE, {})
    return data if isinstance(data, dict) else {}


def write_snapshot(snap: dict) -> None:
    """Publish the fleet snapshot the console serves. The daemon is the only writer,
    once per tick — the whole reason the web server never queries GitHub (ADR 0026)."""
    _write_atomic(SNAPSHOT_FILE, snap)


def read_snapshot() -> dict | None:
    """The last daemon-published snapshot, or None when no daemon has ever published
    one (missing / partial / corrupt file — the console renders an empty fleet)."""
    data = _read(SNAPSHOT_FILE, None)
    return data if isinstance(data, dict) else None
