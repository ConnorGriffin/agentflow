"""Durable proof that AgentFlow materialized a vendored skill destination."""

from __future__ import annotations

import json
import os
import hashlib
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


def _manifest_digest(files_manifest: list[dict]) -> str:
    return hashlib.sha256(
        json.dumps(files_manifest, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _tree_digest(destination: Path) -> str | None:
    if destination.is_symlink() or not destination.is_dir():
        return None
    entries = []
    try:
        for path in sorted(destination.rglob("*")):
            relative = path.relative_to(destination)
            if "node_modules" in relative.parts:
                continue
            if path.is_symlink() or not path.is_file():
                if path.is_symlink():
                    return None
                continue
            entries.append((relative.as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    except OSError:
        return None
    return hashlib.sha256(json.dumps(entries, separators=(",", ":")).encode()).hexdigest()


def mark_skill_owned(destination: str | Path, files_manifest: list[dict]) -> Path:
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
    tree_digest = _tree_digest(target)
    if tree_digest is None:
        raise OSError(f"cannot fingerprint ownership marker for {target}")
    payload = {
        "schema": MARKER_SCHEMA,
        "owner": "agentflow",
        "destination": identity,
        "manifest_sha256": _manifest_digest(files_manifest),
        "tree_sha256": tree_digest,
    }
    temporary = marker.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    temporary.replace(marker)
    return marker


def skill_ownership(destination: str | Path, files_manifest: list[dict]) -> dict | None:
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
    tree_digest = _tree_digest(Path(destination))
    if tree_digest is None:
        return None
    expected = {
        "schema": MARKER_SCHEMA,
        "owner": "agentflow",
        "destination": identity,
        "manifest_sha256": _manifest_digest(files_manifest),
        "tree_sha256": tree_digest,
    }
    return payload if payload == expected else None


def clear_skill_ownership(destination: str | Path) -> None:
    """Remove a marker when rollback removes the materialized destination."""
    resolved = _marker_path(Path(destination))
    if resolved is None:
        return
    marker, _identity = resolved
    if marker.is_file() and not marker.is_symlink():
        marker.unlink()
