"""Static project-local runtime contract inspection."""

from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess

from agentflow.filesystem_contracts import runtime_tree_status, skill_destination_status


def _run_command(command: list[str], *, timeout: int = 30):
    from agentflow.runner import _run

    try:
        return _run(command, timeout=timeout)
    except OSError as exc:
        return subprocess.CompletedProcess(command, returncode=127, stdout="", stderr=str(exc))


def _provider_skill_root(root: Path, provider: str) -> Path | None:
    location = {"codex": ".agents/skills", "claude": ".claude/skills"}.get(provider)
    return root / location if location else None


def playwright_runtime_status(
    root: Path,
    *,
    version: str,
    node_minimum: int,
    manifest: dict,
    provider: str,
    allow_harness_drift: bool = False,
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

    if (
        hashlib.sha256(harness.read_bytes()).hexdigest() != harness_spec["sha256"]
        and not allow_harness_drift
    ):
        return "drifted", "pinned screenshot harness is drifted"
    drive = specs["drive-local-webapp"]
    status = skill_destination_status(skill_root / drive["skill"], drive["files"])
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
    drive_root = skill_root / drive["skill"]
    runtime = drive_root / "node_modules"
    tree_status, tree_detail = runtime_tree_status(runtime)
    if tree_status != "ok":
        return tree_status, tree_detail
    try:
        runtime_root = runtime.resolve(strict=True)
        runtime_root.relative_to(drive_root.resolve(strict=True))
    except (FileNotFoundError, OSError, ValueError):
        return "incompatible", "installed Playwright runtime escapes its provider-local skill"
    package_root = runtime / "playwright"
    package = package_root / "package.json"
    cli = package_root / "cli.js"
    program = package_root / "lib" / "program.js"
    required_files = (package, cli, program)
    if any(not path.exists() and not path.is_symlink() for path in required_files):
        return "missing", "installed Playwright package files are missing from a project-local root"
    try:
        for path in required_files:
            if path.is_symlink() or not path.is_file():
                return "incompatible", "installed Playwright package files are incompatible"
            path.resolve(strict=True).relative_to(runtime_root)
    except (FileNotFoundError, OSError, ValueError):
        return "incompatible", "installed Playwright package files escape the project-local runtime"
    launcher_directory = runtime / ".bin"
    if not launcher_directory.exists() and not launcher_directory.is_symlink():
        return "missing", "installed Playwright launcher directory is missing"
    if launcher_directory.is_symlink() or not launcher_directory.is_dir():
        return "incompatible", "installed Playwright launcher directory is incompatible"
    launcher = launcher_directory / "playwright"
    if not launcher.exists() and not launcher.is_symlink():
        return "missing", "installed Playwright launcher is missing from a project-local root"
    try:
        if not launcher.is_symlink():
            return "incompatible", "installed Playwright launcher is not a safe relative link"
        resolved_launcher = launcher.resolve(strict=True)
        if resolved_launcher != cli.resolve(strict=True):
            return "incompatible", "installed Playwright launcher does not target its local CLI"
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return "incompatible", "installed Playwright launcher is broken or escapes its runtime"
    try:
        installed_version = json.loads(package.read_text()).get("version")
    except (OSError, json.JSONDecodeError):
        return "incompatible", "installed Playwright metadata is incompatible"
    if installed_version != version:
        return "incompatible", f"installed Playwright metadata does not pin {version}"
    return "ok", f"pinned harness, Node {node_minimum}+, and Playwright {version} runtime are intact"
