"""Durable proof that AgentFlow materialized a vendored skill destination."""

from __future__ import annotations

import json
import os
from pathlib import Path


MARKER_SCHEMA = 1
_SKILL = "drive-local-webapp"
_DESTINATION = Path(".agents/skills") / _SKILL
_MARKER = Path(".agentflow/skill-ownership") / f"{_SKILL}.json"


def _marker_path(destination: Path) -> Path | None:
    if len(destination.parents) < 3:
        return None
    root = destination.parents[2]
    if destination != root / _DESTINATION:
        return None
    return root / _MARKER


def mark_skill_owned(destination: str | Path) -> Path:
    """Mark the just-materialized vendored skill without changing its manifest tree."""
    target = Path(destination)
    marker = _marker_path(target)
    state_root = marker.parent.parent if marker is not None else None
    if (
        marker is None
        or state_root is None
        or target.is_symlink()
        or not target.is_dir()
        or state_root.is_symlink()
        or (state_root.exists() and not state_root.is_dir())
        or marker.parent.is_symlink()
    ):
        raise OSError(f"cannot resolve ownership marker for {target}")
    marker.parent.mkdir(parents=True, exist_ok=True)
    if marker.parent.is_symlink():
        raise OSError(f"cannot resolve ownership marker for {target}")
    payload = {"schema": MARKER_SCHEMA, "owner": "agentflow", "skill": _SKILL}
    temporary = marker.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(marker)
    return marker


def skill_ownership(destination: str | Path) -> dict | None:
    """Return a validated ownership marker, or ``None`` for unknown content."""
    marker = _marker_path(Path(destination))
    state_root = marker.parent.parent if marker is not None else None
    if (
        marker is None
        or state_root is None
        or state_root.is_symlink()
        or not state_root.is_dir()
        or marker.parent.is_symlink()
        or marker.is_symlink()
        or not marker.is_file()
    ):
        return None
    try:
        payload = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    expected = {"schema": MARKER_SCHEMA, "owner": "agentflow", "skill": _SKILL}
    return payload if payload == expected else None
