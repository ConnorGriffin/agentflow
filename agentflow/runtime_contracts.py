"""Static project-local runtime contract inspection."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

from agentflow import provider_skills


def _run_command(command: list[str], *, timeout: int = 30):
    from agentflow.runner import _run

    try:
        return _run(command, timeout=timeout)
    except OSError as exc:
        return subprocess.CompletedProcess(command, returncode=127, stdout="", stderr=str(exc))


def _provider_skill_root(root: Path, provider: str) -> Path | None:
    location = {"codex": ".agents/skills", "claude": ".claude/skills"}.get(provider)
    return root / location if location else None


def _playwright_version(root: Path, provider: str) -> str | None:
    skill_root = _provider_skill_root(root, provider)
    if skill_root is None:
        return None
    package = skill_root / "drive-local-webapp" / "node_modules" / "playwright" / "package.json"
    try:
        installed = json.loads(package.read_text()).get("version")
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return installed if isinstance(installed, str) else None


def playwright_runtime_status(
    root: Path,
    *,
    version: str,
    node_minimum: int,
    manifest: dict,
    provider: str,
) -> tuple[str, str]:
    """Inspect the selected provider's pinned browser runtime contract."""
    specs = {item["id"]: item for item in manifest["capabilities"]}
    skill_root = _provider_skill_root(root, provider)
    if skill_root is None:
        return "incompatible", f"unsupported provider {provider}"
    harness = root / "scripts" / "screenshots.mjs"
    harness_spec = specs["screenshot-harness"]
    if harness.is_symlink():
        return "incompatible", "pinned screenshot harness must not be symlinked"
    if not harness.is_file():
        return "missing", "pinned screenshot harness is missing"
    try:
        harness.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return "incompatible", "pinned screenshot harness escapes the project root"
    import hashlib

    if hashlib.sha256(harness.read_bytes()).hexdigest() != harness_spec["sha256"]:
        return "drifted", "pinned screenshot harness is drifted"
    drive = specs["drive-local-webapp"]
    status = provider_skills.skill_destination_status(
        skill_root / drive["skill"], drive["files"]
    )
    if status == "drifted":
        return "drifted", "pinned drive-local-webapp contract is drifted"
    if status == "absent":
        return "missing", "pinned drive-local-webapp contract is missing"
    if status != "ok":
        return "incompatible", "pinned drive-local-webapp contract is incompatible"
    if shutil.which("node") is None:
        return "missing", "Node runtime is not on PATH"
    node = _run_command(["node", "--version"], timeout=10)
    match = re.match(r"v(\d+)", node.stdout.strip())
    if node.returncode or not match or int(match.group(1)) < node_minimum:
        return "incompatible", f"Node {node_minimum}+ is required"
    installed_version = _playwright_version(root, provider)
    if installed_version is None:
        return "missing", "installed Playwright metadata is missing from a project-local root"
    if installed_version != version:
        return "incompatible", f"installed Playwright metadata does not pin {version}"
    return "ok", f"pinned harness, Node {node_minimum}+, and Playwright {version} metadata are intact"
