"""Pure filesystem contracts shared by capability and runtime inspection."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath


def _regular_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return False
    return True


def _safe_manifest_path(value: object) -> PurePosixPath | None:
    if not isinstance(value, str):
        return None
    relative = PurePosixPath(value)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        return None
    return relative


def skill_destination_status(directory: Path, files_manifest: list[dict]) -> str:
    """Compare one non-symlinked project-local skill directory with its pinned manifest."""
    root = directory.parent.parent
    skill_root = directory.parent
    if not root.exists() and not root.is_symlink():
        return "absent"
    if not _regular_directory(root):
        return "incompatible"
    if not skill_root.exists() and not skill_root.is_symlink():
        return "absent"
    if not _regular_directory(skill_root):
        return "incompatible"
    if not directory.exists() and not directory.is_symlink():
        return "absent"
    if (
        not _contained(skill_root, root)
        or not _regular_directory(directory)
        or not _contained(directory, root)
    ):
        return "incompatible"
    expected: dict[str, str] = {}
    for item in files_manifest:
        relative = _safe_manifest_path(item.get("path")) if isinstance(item, dict) else None
        digest = item.get("sha256") if isinstance(item, dict) else None
        if relative is None or not isinstance(digest, str):
            return "incompatible"
        target = directory.joinpath(*relative.parts)
        if target.is_symlink() or (target.exists() and not _contained(target, directory)):
            return "incompatible"
        if not target.is_file():
            # Once the skill directory exists, a missing tracked file is drift, not
            # absence.  In particular, an occupied empty directory must never look like
            # a safe destination for enrollment.
            return "drifted"
        expected[relative.as_posix()] = digest
    actual: set[str] = set()
    for path in directory.rglob("*"):
        relative = path.relative_to(directory)
        if "node_modules" in relative.parts:
            continue
        if path.is_symlink():
            return "incompatible"
        if path.is_file():
            if not _contained(path, directory):
                return "incompatible"
            actual.add(relative.as_posix())
    if actual != set(expected):
        return "drifted"
    if any(
        hashlib.sha256((directory / relative).read_bytes()).hexdigest() != digest
        for relative, digest in expected.items()
    ):
        return "drifted"
    return "ok"


def runtime_tree_status(runtime: Path) -> tuple[str, str]:
    """Verify that every runtime symlink is relative, intact, and internally resolved."""
    if not runtime.exists() and not runtime.is_symlink():
        return "missing", "installed Playwright runtime is missing"
    if runtime.is_symlink() or not runtime.is_dir():
        return "incompatible", "installed Playwright runtime root is incompatible"
    try:
        runtime_root = runtime.resolve(strict=True)
        for path in runtime.rglob("*"):
            if not path.is_symlink():
                continue
            if path.readlink().is_absolute():
                return "incompatible", "installed Playwright runtime contains an external link"
            path.resolve(strict=True).relative_to(runtime_root)
    except (OSError, RuntimeError, ValueError):
        return "incompatible", "installed Playwright runtime contains a broken or external link"
    return "ok", "installed Playwright runtime links are internally resolved"
