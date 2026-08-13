"""Provider-local skill discovery inspection shared by enrollment and dispatch."""

from __future__ import annotations

import hashlib
from pathlib import Path


def _file_status(path: Path, expected_sha256: str) -> str:
    if not path.is_file():
        return "missing"
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        return "drifted"
    return "ok"


def skill_destination_status(directory: Path, files_manifest: list[dict]) -> str:
    """Compare one project-local skill directory with its pinned file manifest."""
    expected = {item["path"]: item["sha256"] for item in files_manifest}
    if not directory.is_dir():
        return "absent" if not directory.exists() and not directory.is_symlink() else "drifted"
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
        and "node_modules" not in path.relative_to(directory).parts
    }
    if actual != set(expected):
        return "drifted"
    if any(
        _file_status(directory / relative, sha256) != "ok"
        for relative, sha256 in expected.items()
    ):
        return "drifted"
    return "ok"


def provider_skill_status(root: Path, provider: str, spec: dict) -> tuple[str, str]:
    """Inspect one selected provider's pinned project-local discovery contract."""
    name = spec["skill"]
    agent_destination = root / ".agents" / "skills" / name
    status = skill_destination_status(agent_destination, spec["files"])
    if status == "absent":
        return "missing", "project-local skill destination is missing"
    if status != "ok":
        return "drifted", "pinned project-local skill files are drifted"
    if provider == "codex":
        return "ok", "Codex project discovery contract is intact"
    discovery = root / ".claude" / "skills" / name
    if not discovery.exists() and not discovery.is_symlink():
        return "missing", "Claude project discovery reference is missing"
    if not discovery.is_symlink() or discovery.resolve() != agent_destination.resolve():
        return "incompatible", "Claude project discovery reference is incompatible"
    return "ok", "Claude project discovery contract is intact"
