"""Provider-local skill integrity and native-discovery receipts.

Static files prove what a skill contains, never that a provider will discover it natively.  A
successful real-provider probe records a repository-scoped receipt bound to the provider binary
and capability manifest.  Every launch-root preflight rechecks both halves and fails closed.
"""

from __future__ import annotations

import hashlib
import json
import os
from importlib.resources import files
from pathlib import Path, PurePosixPath
import shutil
import tomllib


RECEIPT_SCHEMA = 1
NATIVE_DISCOVERY_MARKER = "AGENTFLOW_582_DISCOVERED_4BAB5FF0_AEE6_4D44_BEA3_1BE5D089256F"


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


def _manifest_fingerprint() -> str:
    content = files("agentflow").joinpath("capabilities.toml").read_bytes()
    return hashlib.sha256(content).hexdigest()


def _provider_fingerprint(provider: str) -> tuple[str, str] | None:
    executable = shutil.which(provider)
    if not executable:
        return None
    path = Path(executable)
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            return None
        return str(resolved), hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        return None


def _repository_key(root: Path) -> tuple[str, Path]:
    """Return the shared git identity and private receipt directory for this checkout."""
    from agentflow.runner import _run

    result = _run([
        "git", "-C", str(root), "rev-parse", "--path-format=absolute", "--git-common-dir",
    ])
    if result.returncode == 0 and result.stdout.strip():
        common = Path(result.stdout.strip()).resolve()
        return str(common), common / "agentflow-capability-receipts"
    resolved = root.resolve(strict=True)
    return str(resolved), resolved / ".agentflow" / "capability-receipts"


def _receipt_path(root: Path, provider: str) -> tuple[str, Path]:
    repository, directory = _repository_key(root)
    return repository, directory / f"{provider}.json"


def clear_native_discovery_receipt(root: str | Path, provider: str) -> None:
    """Invalidate an earlier proof before a fresh positive probe runs."""
    _repository, path = _receipt_path(Path(root), provider)
    if path.is_file() and not path.is_symlink():
        path.unlink()


def record_native_discovery_receipt(root: str | Path, provider: str) -> Path:
    """Record a receipt only after the caller has validated the native positive probe output."""
    checkout = Path(root)
    if checkout.is_symlink() or not checkout.is_dir():
        raise ValueError("probe root must be a real project-local directory")
    fingerprint = _provider_fingerprint(provider)
    if fingerprint is None:
        raise ValueError(f"{provider} executable is unavailable")
    repository, path = _receipt_path(checkout, provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("receipt directory must not be symlinked")
    payload = {
        "schema_version": RECEIPT_SCHEMA,
        "provider": provider,
        "repository": repository,
        "provider_path": fingerprint[0],
        "provider_sha256": fingerprint[1],
        "manifest_sha256": _manifest_fingerprint(),
        "proof": NATIVE_DISCOVERY_MARKER,
    }
    temporary = path.with_suffix(f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(payload, sort_keys=True) + "\n")
    temporary.replace(path)
    return path


def native_discovery_status(root: Path, provider: str) -> tuple[str, str]:
    """Validate the durable provider-native discovery proof for this repository checkout."""
    if root.is_symlink() or not root.is_dir():
        return "incompatible", "provider launch root is missing or symlinked"
    fingerprint = _provider_fingerprint(provider)
    if fingerprint is None:
        return "incompatible", f"{provider} executable is unavailable"
    repository, path = _receipt_path(root, provider)
    if path.is_symlink() or not path.is_file():
        return "missing", f"{provider} native-discovery receipt is missing"
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return "drifted", f"{provider} native-discovery receipt is unreadable"
    expected = {
        "schema_version": RECEIPT_SCHEMA,
        "provider": provider,
        "repository": repository,
        "provider_path": fingerprint[0],
        "provider_sha256": fingerprint[1],
        "manifest_sha256": _manifest_fingerprint(),
        "proof": NATIVE_DISCOVERY_MARKER,
    }
    if payload != expected:
        return "drifted", f"{provider} native-discovery receipt is stale or incompatible"
    return "ok", f"{provider} native discovery was proven for this repository and binary"


def provider_skill_status(root: Path, provider: str, spec: dict) -> tuple[str, str]:
    """Inspect one provider's actual launch-root skill and native discovery contract."""
    if root.is_symlink() or not root.is_dir():
        return "incompatible", "provider launch root is missing or symlinked"
    name = spec["skill"]
    location = ".agents" if provider == "codex" else ".claude"
    destination = root / location / "skills" / name
    status = skill_destination_status(destination, spec["files"])
    if status == "absent":
        return "missing", f"{provider} project-local skill destination is missing"
    if status != "ok":
        return status, f"{provider} pinned project-local skill is {status}"
    receipt, detail = native_discovery_status(root, provider)
    return receipt, detail


def materialize_launch_capabilities(
    source: Path, destination: Path, provider: str
) -> tuple[bool, str]:
    """Copy missing pinned provider skills into a prepared launch root without overwriting.

    The source checkout is only an installation source; readiness is always checked against the
    destination afterward.  Existing destinations, symlinks, and malformed roots are untouched.
    """
    if (
        source.is_symlink()
        or destination.is_symlink()
        or not source.is_dir()
        or not destination.is_dir()
    ):
        return False, "capability source or launch root is missing or symlinked"
    if source.resolve() == destination.resolve():
        return True, "launch root is the enrolled source checkout"
    location = ".agents" if provider == "codex" else ".claude"
    source_provider_root = source / location
    source_skills = source_provider_root / "skills"
    destination_provider_root = destination / location
    destination_skills = destination_provider_root / "skills"
    if (
        not _regular_directory(source_provider_root)
        or not _contained(source_provider_root, source)
        or not _regular_directory(source_skills)
        or not _contained(source_skills, source_provider_root)
    ):
        return False, f"{provider} capability source root is missing or incompatible"
    if destination_provider_root.is_symlink() or (
        destination_provider_root.exists() and not destination_provider_root.is_dir()
    ):
        return False, f"{provider} launch provider root is incompatible"
    if destination_provider_root.exists() and not _contained(destination_provider_root, destination):
        return False, f"{provider} launch provider root escapes the launch root"
    if destination_skills.is_symlink() or (
        destination_skills.exists() and not destination_skills.is_dir()
    ):
        return False, f"{provider} launch skill root is incompatible"
    destination_skills.mkdir(parents=True, exist_ok=True)
    if (
        destination_provider_root.is_symlink()
        or destination_skills.is_symlink()
        or not _contained(destination_provider_root, destination)
        or not _contained(destination_skills, destination_provider_root)
    ):
        return False, f"{provider} launch skill root is symlinked"
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    for spec in manifest["capabilities"]:
        name = spec.get("skill")
        if not name or "version" not in spec:
            continue
        source_skill = source_skills / name
        target_skill = destination_skills / name
        if target_skill.exists() or target_skill.is_symlink():
            continue
        if skill_destination_status(source_skill, spec["files"]) != "ok":
            return False, f"{provider} source skill {name} is not intact"
        shutil.copytree(source_skill, target_skill)
    return True, f"materialized missing {provider} capabilities into the launch root"
