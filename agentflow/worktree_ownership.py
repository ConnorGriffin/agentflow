"""Durable proof that AgentFlow created a Git worktree."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess


MARKER_SCHEMA = 1
MARKER_NAME = "agentflow-owned.json"


def _marker_path(worktree: Path) -> Path | None:
    result = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--path-format=absolute", "--git-path",
         MARKER_NAME],
        text=True,
        capture_output=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip())


def mark_worktree_owned(worktree: str | Path, *, disposable: bool) -> Path:
    """Mark a just-created worktree as AgentFlow-owned without dirtying its checkout."""
    root = Path(worktree)
    marker = _marker_path(root)
    if marker is None or marker.is_symlink() or not marker.parent.is_dir():
        raise OSError(f"cannot resolve ownership marker for {root}")
    payload = {
        "schema": MARKER_SCHEMA,
        "owner": "agentflow",
        "worktree": os.path.realpath(root),
        "disposable": disposable,
    }
    temporary = marker.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(marker)
    return marker


def worktree_ownership(worktree: str | Path) -> dict | None:
    """Return a validated ownership marker, or ``None`` for unknown content."""
    root = Path(worktree)
    marker = _marker_path(root)
    if marker is None or marker.is_symlink() or not marker.is_file():
        return None
    try:
        payload = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    if (
        type(payload) is not dict
        or payload.get("schema") != MARKER_SCHEMA
        or payload.get("owner") != "agentflow"
        or payload.get("worktree") != os.path.realpath(root)
        or type(payload.get("disposable")) is not bool
        or set(payload) != {"schema", "owner", "worktree", "disposable"}
    ):
        return None
    return payload
