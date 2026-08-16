"""Durable proof that AgentFlow materialized a vendored skill destination.

Provenance means "AgentFlow materialized this path from pin X" — it is recorded once, at
materialization time, and does not change when the tree later drifts. A marker's validity is
about identity, not content: a well-formed payload naming this exact destination, backed by a
destination that is still a regular directory rather than a symlink. The recorded pin is
provenance data for humans and for callers that want to know what release a destination was
last (re)built from; it is never compared against the current manifest to decide validity, so a
pin bump does not retroactively strip ownership from destinations materialized under an older
pin, and drift does not either — drift is exactly what convergence repair exists to fix.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


MARKER_SCHEMA = 1
_DESTINATION_ROOTS = (Path(".agents/skills"), Path(".claude/skills"))
_MARKER_ROOT = Path(".agentflow/skill-ownership")
_MARKER_KEYS = {"schema", "owner", "destination", "pin"}


def _marker_path(destination: Path) -> tuple[Path, str] | None:
    if len(destination.parents) < 3:
        return None
    root = destination.parents[2]
    for location in _DESTINATION_ROOTS:
        if destination.parent == root / location and destination.name:
            identity = (location / destination.name).as_posix()
            return root / _MARKER_ROOT / location.parts[0][1:] / f"{destination.name}.json", identity
    return None


def mark_skill_owned(destination: str | Path, pin: str) -> Path:
    """Record that AgentFlow just materialized ``destination`` from release ``pin``.

    ``pin`` is provenance data only (the connor_skills/methodology_skills commit, or a
    capability's pinned ``version``) — it is written for audit purposes and is never read back
    as a validity precondition.
    """
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
    payload = {
        "schema": MARKER_SCHEMA,
        "owner": "agentflow",
        "destination": identity,
        "pin": pin,
    }
    temporary = marker.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(marker)
    return marker


def skill_ownership(destination: str | Path) -> dict | None:
    """Return a validated ownership marker, or ``None`` for unknown/invalid content.

    Validity is destination identity plus a well-formed payload, checked against the
    destination's current shape (a regular directory, not a symlink) — not against its
    content. A marker survives its destination drifting; that is what makes repair possible.
    An operator who overwrites a marker-owned directory in place is indistinguishable from
    drift and will read as owned — the residual risk is bounded by marker cleanup on rollback
    and by the refusal to touch symlinked or incompatible destinations.
    """
    target = Path(destination)
    resolved = _marker_path(target)
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
        or target.is_symlink()
        or not target.is_dir()
    ):
        return None
    try:
        payload = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    if (
        not isinstance(payload, dict)
        or set(payload) != _MARKER_KEYS
        or payload.get("schema") != MARKER_SCHEMA
        or payload.get("owner") != "agentflow"
        or payload.get("destination") != identity
        or not isinstance(payload.get("pin"), str)
        or not payload["pin"]
    ):
        return None
    return payload


def clear_skill_ownership(destination: str | Path) -> None:
    """Remove a marker when rollback removes the materialized destination."""
    resolved = _marker_path(Path(destination))
    if resolved is None:
        return
    marker, _identity = resolved
    if marker.is_file() and not marker.is_symlink():
        marker.unlink()
