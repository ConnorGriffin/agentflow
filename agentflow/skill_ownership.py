"""Durable proof that AgentFlow materialized a vendored skill destination."""

from __future__ import annotations

import json
import os
from pathlib import Path


MARKER_SCHEMA = 1
_DESTINATION_ROOTS = (Path(".agents/skills"), Path(".claude/skills"))
_MARKER_ROOT = Path(".agentflow/skill-ownership")


def _marker_path(destination: Path) -> tuple[Path, str] | None:
    if len(destination.parents) < 3:
        return None
    root = destination.parents[2]
    for location in _DESTINATION_ROOTS:
        if destination.parent == root / location and destination.name:
            identity = (location / destination.name).as_posix()
            return root / _MARKER_ROOT / location.parts[0][1:] / f"{destination.name}.json", identity
    return None


def mark_skill_owned(destination: str | Path) -> Path:
    """Mark the just-materialized vendored skill without changing its manifest tree."""
    target = Path(destination)
    resolved = _marker_path(target)
    marker, identity = resolved if resolved is not None else (None, None)
    state_root = marker.parents[2] if marker is not None else None
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
    payload = {"schema": MARKER_SCHEMA, "owner": "agentflow", "destination": identity}
    temporary = marker.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(marker)
    return marker


def skill_ownership(destination: str | Path) -> dict | None:
    """Return a validated ownership marker, or ``None`` for unknown content."""
    resolved = _marker_path(Path(destination))
    marker, identity = resolved if resolved is not None else (None, None)
    state_root = marker.parents[2] if marker is not None else None
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
    expected = {"schema": MARKER_SCHEMA, "owner": "agentflow", "destination": identity}
    return payload if payload == expected else None
