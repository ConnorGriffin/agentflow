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


def _playwright_versions(root: Path) -> tuple[str, ...]:
    versions = []
    for location in (".agents/skills", ".claude/skills"):
        package = root / location / "drive-local-webapp" / "node_modules" / "playwright" / "package.json"
        try:
            version = json.loads(package.read_text()).get("version")
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return ()
        if not isinstance(version, str):
            return ()
        versions.append(version)
    return tuple(versions)


def playwright_runtime_status(
    root: Path,
    *,
    version: str,
    node_minimum: int,
    manifest: dict,
) -> tuple[str, str]:
    """Statically inspect the complete pinned browser runtime contract."""
    specs = {item["id"]: item for item in manifest["capabilities"]}
    harness = root / "scripts" / "screenshots.mjs"
    harness_spec = specs["screenshot-harness"]
    if not harness.is_file():
        return "missing", "pinned screenshot harness is missing"
    import hashlib

    if hashlib.sha256(harness.read_bytes()).hexdigest() != harness_spec["sha256"]:
        return "drifted", "pinned screenshot harness is drifted"
    drive = specs["drive-local-webapp"]
    statuses = tuple(
        provider_skills.skill_destination_status(
            root / location / drive["skill"], drive["files"]
        )
        for location in (".agents/skills", ".claude/skills")
    )
    if "drifted" in statuses:
        return "drifted", "pinned drive-local-webapp contract is drifted"
    if "absent" in statuses:
        return "missing", "pinned drive-local-webapp contract is missing"
    if shutil.which("node") is None:
        return "missing", "Node runtime is not on PATH"
    node = _run_command(["node", "--version"], timeout=10)
    match = re.match(r"v(\d+)", node.stdout.strip())
    if node.returncode or not match or int(match.group(1)) < node_minimum:
        return "incompatible", f"Node {node_minimum}+ is required"
    installed_versions = _playwright_versions(root)
    if len(installed_versions) != 2:
        return "missing", "installed Playwright metadata is missing from a project-local root"
    if any(found != version for found in installed_versions):
        return "incompatible", f"installed Playwright metadata does not pin {version}"
    return "ok", f"pinned harness, Node {node_minimum}+, and Playwright {version} metadata are intact"
