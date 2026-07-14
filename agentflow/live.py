"""Live sessions (ADR 0023, issue #70) — the one file that says which agents run now.

The daemon is the only process that knows which sessions are executing this second; this
module is where it writes that down so the console can read it. One entry per running
session, keyed by its worktree, recorded as the session starts and removed as it finishes
(both from the single `worktree_session` write seam in `runner.py`). A crash leaves entries
behind; `reap` drops any whose owning worktree is no longer alive — reusing the very
liveness signal the worktree-recovery pass already trusts, never a second notion of "alive".

Reads fail soft: a missing, partial, or corrupt file reads as "fleet idle" (no running
sessions), never an error — the console must render an empty board, not a 500. Writes are
atomic (temp + rename) so a concurrent reader never sees a half-written file.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path(os.environ.get("AGENTFLOW_STATE", os.path.expanduser("~/.agentflow")))
LIVE_FILE = STATE_DIR / "live-sessions.json"
DAEMON_FILE = STATE_DIR / "daemon-status.json"


@dataclass(frozen=True, slots=True)
class Session:
    """One running agent session — the semantic half of the `running[]` contract the locked
    console binds. The write seam stamps the runtime half (`worktree`, `pid`, `started_at`)."""

    repo: str
    number: int
    title: str
    stage: str          # triaging | building | reviewing
    tool: str           # claude | codex
    model: str          # the runner's resolved model; stored shortened (opus/sonnet/sol/terra)
    branch: str | None  # None before the session has a branch (triage / mockup)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_model(model: str) -> str:
    """The console shows the short model name — `opus`, `sonnet`, `sol`, `terra` — not the
    runner's full id (`gpt-5.6-sol`). The last `-`-separated segment is that short form."""
    return model.rsplit("-", 1)[-1]


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


def record(session: Session, worktree: str) -> None:
    """Add this session's entry as it starts (replacing any stale entry for the same
    worktree). `pid` + `started_at` are stamped here — the runtime facts the seam holds."""
    entries = [e for e in _entries() if e.get("worktree") != worktree]
    entries.append({**asdict(session), "worktree": worktree,
                    "model": _short_model(session.model),
                    "pid": os.getpid(), "started_at": _now()})
    _write_atomic(LIVE_FILE, entries)


def remove(worktree: str) -> None:
    """Drop this session's entry as it finishes."""
    _write_atomic(LIVE_FILE, [e for e in _entries() if e.get("worktree") != worktree])


def running() -> list[dict]:
    """Every recorded live session. `[]` on a missing / partial / corrupt file (fleet idle)."""
    return _entries()


def reap(is_alive: Callable[[str], bool]) -> list[dict]:
    """Drop entries whose owning worktree is no longer alive — the dead-session sweep the
    daemon runs at startup, so a crashed run never leaves a phantom session on the board.
    `is_alive` is the recovery pass's own liveness check. Returns the dropped entries."""
    entries = _entries()
    survivors = [e for e in entries if is_alive(e.get("worktree", ""))]
    if len(survivors) != len(entries):
        _write_atomic(LIVE_FILE, survivors)
    return [e for e in entries if e not in survivors]


def mark_cycle(poll_seconds: int) -> None:
    """Stamp the daemon's status after a cycle — when it last ran and how often it polls,
    the runtime facts the snapshot's `daemon` block needs but the snapshot builder can't see."""
    _write_atomic(DAEMON_FILE, {"last_cycle_at": _now(), "poll_seconds": poll_seconds})


def daemon_status() -> dict:
    """The daemon's last-cycle / poll status, or `{}` when it hasn't run a cycle yet."""
    data = _read(DAEMON_FILE, {})
    return data if isinstance(data, dict) else {}
