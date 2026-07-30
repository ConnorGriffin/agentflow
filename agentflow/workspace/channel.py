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

import hashlib
import json
import os
import re
import time
from pathlib import Path

from agentflow import live
from agentflow.state import OutsideStateDirectory, state_path

# How fresh the daemon's last cycle must be for a command to be accepted. The daemon stamps its
# status every fast tick (~15s); a spool write is only honored when a daemon is demonstrably
# alive to drain it, so the web layer fails closed instead of letting commands pile up unread.
_LIVENESS_WINDOW_S = int(os.environ.get("AGENTFLOW_WORKSPACE_LIVENESS_S", "90"))
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class InvalidCommandKey(ValueError):
    """A command key cannot safely and unambiguously name one spool entry."""


class CommandChannelUnavailable(RuntimeError):
    """The command spool could not be reached without leaving agentflow's state directory."""


def commands_dir() -> Path:
    return state_path("workspace", "commands")


def _spool_path(key: object) -> Path:
    if not isinstance(key, str) or _KEY.fullmatch(key) is None:
        raise InvalidCommandKey("command key must be a simple identifier")
    name = hashlib.sha256(key.encode()).hexdigest()
    return commands_dir() / f"{name}.json"


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
    tmp: Path | None = None
    try:
        destination = _spool_path(command["key"])
        tmp = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp")
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(command))
        os.replace(tmp, destination)
    except (OSError, OutsideStateDirectory) as exc:
        try:
            if tmp is not None:
                tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise CommandChannelUnavailable("command spool is unavailable") from exc


def pending() -> list[dict]:
    """Every spooled command, oldest first — the daemon drains these each cycle."""
    path = commands_dir()
    if not path.exists():
        return []
    files = []
    for file in path.glob("*.json"):
        try:
            if not file.is_symlink():
                files.append((file.stat().st_mtime, file))
        except OSError:
            continue
    out: list[dict] = []
    for _, file in sorted(files):
        try:
            command = json.loads(file.read_text())
            destination = _spool_path(command.get("key"))
            if file != destination:
                if destination.exists():
                    file.unlink()
                    continue
                else:
                    os.replace(file, destination)
            out.append(command)
        except (InvalidCommandKey, OSError, OutsideStateDirectory, json.JSONDecodeError):
            continue
    return out


def ack(key: str) -> None:
    """Acknowledge a drained command by removing its spool file. Safe to skip on crash — the
    store's idempotency key makes a re-applied command a no-op."""
    try:
        _spool_path(key).unlink(missing_ok=True)
    except (InvalidCommandKey, OSError, OutsideStateDirectory):
        pass
