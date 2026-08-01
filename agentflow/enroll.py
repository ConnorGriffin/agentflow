"""Inspect and reproducibly enroll a repository into AgentFlow.

The module owns three closely related jobs:

- inspect/install the repository-local capabilities Claude and Codex sessions require;
- sweep any bare pre-enrollment needs-grilling / needs-mockup labels to the agentflow:*
  form (the original job, called by `enroll-standards.sh`);
- declare the repo's user-facing surfaces, so the mechanical UI-evidence gate (ADR 0018)
  is either armed or deliberately headless rather than silently inert.

Usage:
  python -m agentflow.enroll <owner/repo>            # sweep legacy labels
  python -m agentflow.enroll audit                   # fleet declaration census
  python -m agentflow.enroll surfaces <dir> [--apply]  # propose/apply one repo's line

Enrolment seeds `ui-surfaces: none` without looking at the repo, so `surfaces` is also what
corrects that seed on a repo that turned out to have a UI — and `audit` names any repo still
claiming to be headless while its own checkout says otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import asdict, dataclass, replace
from importlib.resources import files
from pathlib import Path

from agentflow.intake import sweep_legacy_labels
from agentflow.repo_facts import (UI_SURFACES_NONE, SurfaceDeclaration, _UI_SURFACES_RE,
                                  surface_declaration)

# Directory names that hold a user-facing surface when a repo has one. Deliberately narrow:
# a wrong guess here writes a declaration that either misses real UI or gates a backend path.
# A bare `src` is not on the list — in a Node service it is the server, not the page.
_UI_DIR_NAMES = ("frontend", "webui", "public", "www", "ui", "client", "static")
# Never look inside these: build output and vendored code aren't authored surfaces, and
# `docs/` must stay outside every declaration or committed screenshots would trip the gate.
_SKIP_DIRS = {".git", ".agentflow", "node_modules", "dist", "build", ".venv", "venv",
              "__pycache__", "docs", "mockups", "tests", "test", "archive", "coverage"}
_MAX_DEPTH = 3

_DECLARATION_KEY = "ui-surfaces:"


@dataclass(frozen=True)
class Capability:
    id: str
    description: str
    required: bool
    available: bool
    status: str
    detail: str
    install: str | None = None


@dataclass(frozen=True)
class CapabilityReport:
    schema_version: int
    repository: str
    ui: bool
    ready: bool
    capabilities: tuple[Capability, ...]

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "ui": self.ui,
            "ready": self.ready,
            "capabilities": [asdict(item) for item in self.capabilities],
        }


def _manifest() -> dict:
    return tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())


def _run_command(command: list[str], *, cwd: Path | None = None, timeout: int = 30):
    from agentflow.runner import _run

    try:
        return _run(command, cwd=str(cwd) if cwd else None, timeout=timeout)
    except OSError as exc:
        return subprocess.CompletedProcess(
            command, returncode=127, stdout="", stderr=str(exc)
        )


def _content_status(paths: list[Path], expected_sha256: str) -> str:
    if not all(path.is_file() for path in paths):
        return "missing"
    if any(
        hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256
        for path in paths
    ):
        return "drifted"
    return "ok"


def _file_status(path: Path, expected_sha256: str) -> str:
    if not path.is_file():
        return "missing"
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        return "drifted"
    return "ok"


def _skill_destination_status(directory: Path, files_manifest: list[dict]) -> str:
    expected = {item["path"]: item["sha256"] for item in files_manifest}
    if not directory.is_dir():
        return "absent" if not directory.exists() and not directory.is_symlink() else "drifted"
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
        and "node_modules" not in path.relative_to(directory).parts
    }
    if not set(expected).issubset(actual):
        return "drifted"
    if actual != set(expected):
        return "drifted"
    if any(
        _file_status(directory / relative, sha256) != "ok"
        for relative, sha256 in expected.items()
    ):
        return "drifted"
    return "ok"


def _skill_status(root: Path, name: str, files_manifest: list[dict]) -> str:
    statuses = []
    for location in (".agents/skills", ".claude/skills"):
        directory = root / location / name
        statuses.append(_skill_destination_status(directory, files_manifest))
    if "drifted" in statuses:
        return "drifted"
    if "absent" in statuses:
        return "missing"
    return "ok"


def _connor_skill_command(manifest: dict, source_tree: Path | None = None) -> list[str]:
    installer = manifest["skill_installer"]
    source = manifest["connor_skills"]
    tree = (
        str(source_tree)
        if source_tree is not None
        else f"{source['source']}/tree/{source['tag']}"
    )
    command = ["npx", f"{installer['package']}@{installer['version']}", "add", tree]
    for name in source["skills"]:
        command.extend(("--skill", name))
    command.extend(("-a", "claude-code", "-a", "codex", "-y"))
    return command


def _resolved_skill_release(manifest: dict) -> tuple[str | None, str | None]:
    source = manifest["connor_skills"]
    tag_ref = f"refs/tags/{source['tag']}"
    peeled_ref = f"{tag_ref}^{{}}"
    result = _run_command(
        ["git", "ls-remote", "--tags", source["source"], tag_ref, peeled_ref],
        timeout=30,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip() or "git ls-remote failed"
        return None, detail
    refs = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2:
            refs[fields[1]] = fields[0]
    resolved = refs.get(peeled_ref) or refs.get(tag_ref)
    if not resolved:
        return None, f"release tag {source['tag']} was not found"
    return resolved, None


def _public_skill_destination_states(root: Path, manifest: dict) -> dict:
    names = manifest["connor_skills"]["skills"]
    specs = {item.get("skill"): item for item in manifest["capabilities"]}
    return {
        (location, name): _skill_destination_status(
            root / location / name, specs[name]["files"]
        )
        for location in (".agents/skills", ".claude/skills")
        for name in names
    }


def _skills_problem(root: Path, surfaces: tuple[str, ...]) -> str | None:
    if not surfaces:
        return None
    manifest = _manifest()
    resolved, error = _resolved_skill_release(manifest)
    if error:
        return f"public skill release could not be verified: {error}"
    expected = manifest["connor_skills"]["commit"]
    if resolved != expected:
        return f"public skill release tag resolved to {resolved}, expected {expected}"
    destinations = _public_skill_destination_states(root, manifest)
    if all(state == "ok" for state in destinations.values()):
        return None
    if any(state != "absent" for state in destinations.values()):
        rendered = ", ".join(
            f"{location}/{name}={state}"
            for (location, name), state in destinations.items()
        )
        return f"existing public skill destinations are partial or conflicting ({rendered})"
    return None


def _instructions_status(root: Path) -> str:
    agents = root / "AGENTS.md"
    claude = root / "CLAUDE.md"
    if not agents.is_file() or not claude.is_file():
        return "missing"
    if claude.resolve() != agents.resolve():
        return "drifted"
    profiles = re.findall(
        r"(?m)^profile:\s*(autonomous|reviewed|guarded)\s*$",
        agents.read_text(),
    )
    if len(profiles) != 1 or not surface_declaration(str(root)).declared:
        return "drifted"
    return "ok"


def _playwright_versions(root: Path) -> tuple[str, ...]:
    versions = []
    for location in (".agents/skills", ".claude/skills"):
        package = (
            root
            / location
            / "drive-local-webapp"
            / "node_modules"
            / "playwright"
            / "package.json"
        )
        try:
            version = json.loads(package.read_text()).get("version")
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return ()
        if not isinstance(version, str):
            return ()
        versions.append(version)
    return tuple(versions)


def _playwright_available(
    root: Path, *, version: str, node_minimum: int, verify_runtime: bool = False
) -> bool:
    harness = root / "scripts" / "screenshots.mjs"
    manifest = _manifest()
    specs = {item["id"]: item for item in manifest["capabilities"]}
    drive = specs["drive-local-webapp"]
    if (
        _file_status(harness, specs["screenshot-harness"]["sha256"]) != "ok"
        or _skill_status(root, drive["skill"], drive["files"]) != "ok"
        or shutil.which("node") is None
    ):
        return False
    node = _run_command(["node", "--version"], timeout=10)
    match = re.match(r"v(\d+)", node.stdout.strip())
    installed_versions = _playwright_versions(root)
    if (
        node.returncode
        or not match
        or int(match.group(1)) < node_minimum
        or any(found != version for found in installed_versions)
        or len(installed_versions) != 2
    ):
        return False
    if not verify_runtime:
        return True
    result = _run_command(
        ["node", str(harness), "--self-check"], cwd=root, timeout=30
    )
    return result.returncode == 0


def _fleet_config_available(root: Path) -> bool:
    from agentflow.config import ConfigurationError, default_config_path, load_config

    config_path = default_config_path()
    try:
        runtime = load_config(config_path)
    except ConfigurationError:
        return False
    return any(
        Path(entry.workdir).resolve() == root for entry in runtime.repositories
    )


def doctor(workdir: str) -> CapabilityReport:
    """Inspect one repository against the checked-in capability manifest."""
    root = Path(workdir).expanduser().resolve()
    declaration = surface_declaration(str(root))
    surfaces = (
        declaration.surfaces
        if declaration.declared
        else propose_surfaces(str(root))
    )
    ui = bool(surfaces)
    manifest = _manifest()
    providers = {name: shutil.which(name) is not None for name in ("claude", "codex")}
    rows: list[Capability] = []
    for spec in manifest["capabilities"]:
        requirement = spec["requirement"]
        required = requirement == "always" or (requirement == "ui" and ui)
        name = spec["id"]
        install = None
        status = "missing"
        if name == "repository-instructions":
            status = _instructions_status(root)
            available = status == "ok"
            detail = (
                "AGENTS.md and CLAUDE.md share one file with one supported "
                "profile and an explicit ui-surfaces declaration"
            )
            install = f"agentflow enroll {root} --apply"
        elif name == "fleet-config":
            available = _fleet_config_available(root)
            detail = "config.toml contains this checkout"
            install = f"agentflow enroll {root} --apply"
        elif name == "provider":
            available = any(providers.values())
            installed = [provider for provider, present in providers.items() if present]
            detail = (
                "installed runner: " + ", ".join(installed)
                if installed
                else "install Claude Code or Codex"
            )
            install = "Install Claude Code or Codex and ensure its command is on PATH"
        elif name in {"agentflow-skill", "ui-craft", "drive-local-webapp"}:
            skill_name = spec["skill"]
            status = _skill_status(root, skill_name, spec["files"])
            available = status == "ok"
            detail = "discoverable in .agents/skills and .claude/skills"
            if name == "agentflow-skill":
                install = f"agentflow enroll {root} --apply"
            else:
                install = f"agentflow enroll {root} --apply"
        elif name == "screenshot-harness":
            status = _content_status(
                [root / "scripts" / "screenshots.mjs"], spec["sha256"]
            )
            available = status == "ok"
            detail = "scripts/screenshots.mjs exists"
            install = f"agentflow enroll {root} --apply"
        elif name == "playwright":
            playwright = manifest["playwright"]
            version = playwright["version"]
            available = _playwright_available(
                root, version=version, node_minimum=playwright["node_minimum"]
            )
            detail = (
                f"trusted harness and drive skill, Node >= "
                f"{playwright['node_minimum']}, and installed Playwright {version} metadata"
            )
            install = f"agentflow enroll {root} --apply"
        elif name in {"claude", "codex"}:
            available = providers[name]
            detail = (
                f"{name} is on PATH"
                if available
                else f"{name} is not installed (optional when the other runner is present)"
            )
        else:
            available = bool(
                shutil.which("codebase-memory-mcp")
                or (root / ".codebase-memory").exists()
                or (root / ".cbmignore").is_file()
            )
            detail = "codebase-memory-mcp is configured or the repository is onboarded"
            install = (
                "Install codebase-memory-mcp, then index this repository; "
                "AgentFlow continues without it."
            )
        if available:
            status = "ok"
        rows.append(
            Capability(
                id=name,
                description=spec["description"],
                required=required,
                available=available,
                status=status,
                detail=detail,
                install=None if available else install,
            )
        )
    ready = all(row.available for row in rows if row.required)
    return CapabilityReport(
        schema_version=manifest["schema_version"],
        repository=str(root),
        ui=ui,
        ready=ready,
        capabilities=tuple(rows),
    )


def print_doctor(report: CapabilityReport, *, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(report.as_dict(), indent=2))
        return
    print(f"AgentFlow doctor — {report.repository}")
    for item in report.capabilities:
        scope = "required" if item.required else "optional"
        state = item.status
        print(f"  {state:7} {item.id} ({scope}) — {item.detail}")
        if item.install:
            print(f"          install: {item.install}")
    print("ready" if report.ready else "not ready")


def _asset_text(relative: str) -> str:
    checkout = Path(__file__).parents[1] / relative
    if checkout.is_file():
        return checkout.read_text()
    return files("agentflow").joinpath("_bundled", relative).read_text()


def _backup_once(path: Path) -> None:
    backup = path.with_name(f"{path.name}.pre-agentflow")
    if path.is_file() and path.stat().st_size and not backup.exists():
        shutil.copy2(path, backup)


def _append_once(path: Path, line: str) -> None:
    existing = path.read_text() if path.exists() else ""
    if line in existing.splitlines():
        return
    _backup_once(path)
    separator = "" if not existing or existing.endswith("\n") else "\n"
    path.write_text(f"{existing}{separator}{line}\n")


def _ensure_profile(path: Path, profile: str) -> str:
    existing = path.read_text()
    match = re.search(
        r"(?m)^profile:\s*(autonomous|reviewed|guarded)\s*$", existing
    )
    if match:
        return f"ok:   preserving existing profile: {match.group(1)}"
    _append_once(path, f"profile: {profile}")
    return f"DO:   wrote profile: {profile} to {path}"


def _instructions_problem(root: Path) -> str | None:
    agents = root / "AGENTS.md"
    claude = root / "CLAUDE.md"
    if agents.exists() and (not agents.is_file() or agents.is_symlink()):
        return "AGENTS.md must be a regular file"
    if claude.is_symlink():
        if not agents.is_file() or claude.resolve() != agents.resolve():
            return "CLAUDE.md symlink must resolve to AGENTS.md"
    elif claude.exists() and not claude.is_file():
        return "CLAUDE.md must be a regular file or an AGENTS.md symlink"
    if (
        agents.is_file()
        and claude.is_file()
        and not claude.is_symlink()
        and agents.read_bytes() != claude.read_bytes()
    ):
        return "AGENTS.md and CLAUDE.md differ"
    return None


def _checkout_problem(root: Path) -> str | None:
    top = _run_command(["git", "-C", str(root), "rev-parse", "--show-toplevel"])
    if top.returncode:
        return "target is not a Git checkout"
    try:
        toplevel = Path(top.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        return "Git did not return a valid checkout root"
    if toplevel != root:
        return f"target is nested inside Git checkout {toplevel}"
    status = _run_command(["git", "-C", str(root), "status", "--porcelain"])
    if status.returncode:
        return "Git status failed"
    if status.stdout.strip():
        return "checkout is dirty"
    if not checkout_repo(str(root)):
        return "GitHub origin could not be resolved to owner/name"
    return None


def _config_problem() -> str | None:
    from agentflow.config import ConfigurationError, default_config_path, load_config

    target = default_config_path()
    if not target.exists():
        return None
    try:
        if not target.read_text().strip():
            return None
        load_config(target)
    except (ConfigurationError, OSError) as exc:
        return f"configuration is not safe to update: {exc}"
    return None


def _tooling_problem(surfaces: tuple[str, ...]) -> str | None:
    if not surfaces:
        return None
    missing = [name for name in ("node", "npm", "npx") if shutil.which(name) is None]
    if missing:
        return f"missing required UI command(s): {', '.join(missing)}"
    return None


def _managed_files_problem(root: Path, surfaces: tuple[str, ...]) -> str | None:
    manifest = _manifest()
    agentflow = next(
        item for item in manifest["capabilities"] if item["id"] == "agentflow-skill"
    )
    for location in (".agents/skills", ".claude/skills"):
        directory = root / location / "agentflow"
        status = _skill_destination_status(directory, agentflow["files"])
        if status != "ok" and (directory.exists() or directory.is_symlink()):
            return f"managed AgentFlow skill is {status} at {directory}"
    if surfaces:
        harness = root / "scripts" / "screenshots.mjs"
        spec = next(
            item
            for item in manifest["capabilities"]
            if item["id"] == "screenshot-harness"
        )
        status = _file_status(harness, spec["sha256"])
        if status != "ok" and (harness.exists() or harness.is_symlink()):
            return f"managed screenshot harness is {status} at {harness}"
    return None


def _install_file(path: Path, content: str) -> str:
    if path.is_file():
        if path.read_text() == content:
            return f"ok:   {path} already matches"
        return f"WARN: {path} already exists and differs — left unchanged"
    if path.exists() or path.is_symlink():
        return f"WARN: {path} already exists — left unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"DO:   installed {path}"


def _ensure_fleet_config(root: Path) -> str:
    from agentflow.config import ConfigurationError, default_config_path, load_config

    target = default_config_path()
    entries = []
    if target.exists() and target.read_text().strip():
        try:
            runtime = load_config(target)
            document = tomllib.loads(target.read_text())
        except (ConfigurationError, OSError, tomllib.TOMLDecodeError) as exc:
            return f"WARN: {target} is not safe to update: {exc}"
        entries = document.get("repositories", [])
        for entry in runtime.repositories:
            if Path(entry.workdir).resolve() == root:
                return f"ok:   {root} is already in {target}"
    repo = checkout_repo(str(root))
    if not repo:
        return (
            "WARN: GitHub repository could not be resolved; "
            "config.toml was not changed"
        )
    for entry in entries:
        if isinstance(entry, dict):
            if entry.get("repo") == repo:
                return (
                    f"WARN: {target} already configures {repo} at "
                    f"{entry.get('workdir')} — left unchanged"
                )
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text() if target.exists() else ""
    separator = "\n" if existing and not existing.endswith("\n\n") else ""
    with target.open("a") as stream:
        stream.write(
            f'{separator}[[repositories]]\nrepo = {json.dumps(repo)}\n'
            f"workdir = {json.dumps(str(root))}\n"
        )
    return f"DO:   added {repo} to {target}"


def _install_connor_skills(root: Path) -> str:
    manifest = _manifest()
    names = manifest["connor_skills"]["skills"]
    specs = {item.get("skill"): item for item in manifest["capabilities"]}
    destinations = _public_skill_destination_states(root, manifest)
    if all(state == "ok" for state in destinations.values()):
        return "ok:   Connor skill pack already installed"
    if any(state != "absent" for state in destinations.values()):
        rendered = ", ".join(
            f"{location}/{name}={state}"
            for (location, name), state in destinations.items()
        )
        return (
            "WARN: existing public skill destinations are partial or conflicting; "
            f"installer was not run ({rendered})"
        )
    resolved, error = _resolved_skill_release(manifest)
    expected = manifest["connor_skills"]["commit"]
    if error:
        return f"WARN: public skill release could not be verified — {error}"
    if resolved != expected:
        return (
            f"WARN: public skill release tag resolved to {resolved}, "
            f"expected {expected}; installer was not run"
        )
    with tempfile.TemporaryDirectory(prefix="agentflow-skills-") as temporary:
        source_tree = Path(temporary) / "source"
        commands = (
            ["git", "clone", "--no-checkout", manifest["connor_skills"]["source"], str(source_tree)],
            ["git", "-C", str(source_tree), "checkout", "--detach", expected],
            ["git", "-C", str(source_tree), "rev-parse", "HEAD"],
        )
        for command in commands:
            result = _run_command(command, timeout=120)
            if result.returncode:
                reason = (result.stderr or result.stdout).strip().splitlines()
                tail = reason[-1] if reason else f"exit {result.returncode}"
                return f"WARN: exact skill source fetch failed — {tail}"
        if result.stdout.strip() != expected:
            return (
                "WARN: exact skill source checkout resolved to "
                f"{result.stdout.strip()}, expected {expected}"
            )
        command = _connor_skill_command(manifest, source_tree)
        result = _run_command(command, cwd=root, timeout=120)
        if result.returncode:
            reason = (result.stderr or result.stdout).strip().splitlines()
            tail = reason[-1] if reason else f"exit {result.returncode}"
            return f"WARN: Connor skill install failed — {tail}"
    states = {
        name: _skill_status(root, name, specs[name]["files"]) for name in names
    }
    if any(state != "ok" for state in states.values()):
        rendered = ", ".join(f"{name}={state}" for name, state in states.items())
        return (
            "WARN: skill installer completed but both agent paths did not match "
            f"the manifest ({rendered})"
        )
    return "DO:   installed the pinned Connor skill pack"


def _install_ui_runtime(root: Path) -> str:
    manifest = _manifest()
    specs = {item.get("skill"): item for item in manifest["capabilities"]}
    for name in manifest["connor_skills"]["skills"]:
        if _skill_status(root, name, specs[name]["files"]) != "ok":
            return "WARN: UI runtime skipped because the pinned skill pack is not intact"
    playwright = manifest["playwright"]
    if _playwright_available(
        root,
        version=playwright["version"],
        node_minimum=playwright["node_minimum"],
        verify_runtime=True,
    ):
        return "ok:   pinned Playwright, Chromium, and screenshot harness are ready"
    skill_dirs = {
        (root / location / "drive-local-webapp").resolve()
        for location in (".agents/skills", ".claude/skills")
    }
    for skill in skill_dirs:
        commands = (
            ["npm", "ci"],
            [
                str(skill / "node_modules" / ".bin" / "playwright"),
                "install",
                "chromium",
            ],
            ["npm", "run", "self-check"],
        )
        for command in commands:
            result = _run_command(command, cwd=skill, timeout=180)
            if result.returncode:
                reason = (result.stderr or result.stdout).strip().splitlines()
                tail = reason[-1] if reason else f"exit {result.returncode}"
                return f"WARN: UI runtime setup failed — {tail}"
    if not _playwright_available(
        root,
        version=playwright["version"],
        node_minimum=playwright["node_minimum"],
        verify_runtime=True,
    ):
        return "WARN: UI runtime setup completed but the screenshot harness self-check failed"
    return "DO:   installed pinned Playwright and Chromium; self-check passed"


def _wire_claude_skill(root: Path, name: str) -> str:
    target = root / ".claude" / "skills" / name
    desired = Path("../../.agents/skills") / name
    if target.is_symlink() and target.readlink() == desired:
        return f"ok:   {target} already links to the shared repo skill"
    if target.exists() or target.is_symlink():
        return f"WARN: {target} already exists — left unchanged"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(desired)
    return f"DO:   linked {target} for Claude Code"


class _EnrollmentJournal:
    def __init__(self, paths: list[Path]):
        self._temporary = tempfile.TemporaryDirectory(prefix="agentflow-enroll-")
        self._root = Path(self._temporary.name)
        self._entries: list[tuple[Path, Path | None]] = []
        for index, path in enumerate(dict.fromkeys(paths)):
            backup = self._root / str(index)
            if path.is_symlink():
                backup.symlink_to(path.readlink())
            elif path.is_dir():
                shutil.copytree(path, backup, symlinks=True)
            elif path.exists():
                shutil.copy2(path, backup)
            else:
                backup = None
            self._entries.append((path, backup))

    def restore(self) -> None:
        for path, backup in reversed(self._entries):
            if path.is_symlink() or path.is_file():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
            if backup is None:
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            if backup.is_symlink():
                path.symlink_to(backup.readlink())
            elif backup.is_dir():
                shutil.copytree(backup, path, symlinks=True)
            else:
                shutil.copy2(backup, path)

    def close(self) -> None:
        self._temporary.cleanup()


def _enrollment_journal(root: Path) -> _EnrollmentJournal:
    from agentflow.config import default_config_path

    config = default_config_path()
    paths = [
        root / ".gitignore",
        root / ".gitignore.pre-agentflow",
        root / "AGENTS.md",
        root / "AGENTS.md.pre-agentflow",
        root / "CLAUDE.md",
        root / "CLAUDE.md.pre-agentflow",
        root / ".agents",
        root / ".claude",
        root / "scripts",
        root / "skills-lock.json",
        config,
    ]
    if config.is_symlink():
        paths.append(config.resolve())
    return _EnrollmentJournal(
        paths
    )


def _apply_enrollment(root: Path, profile: str, surfaces: tuple[str, ...]) -> list[str]:
    outcomes: list[str] = []
    _append_once(root / ".gitignore", ".agentflow/")
    if surfaces:
        _append_once(root / ".gitignore", ".agents/skills/**/node_modules/")

    agents = root / "AGENTS.md"
    claude = root / "CLAUDE.md"
    promoted_claude_only = False
    duplicate_instructions = (
        agents.is_file()
        and claude.is_file()
        and not claude.is_symlink()
        and agents.read_bytes() == claude.read_bytes()
    )
    if not agents.exists() and claude.is_file() and not claude.is_symlink():
        shutil.copy2(claude, agents)
        promoted_claude_only = True
        outcomes.append(f"DO:   promoted {claude} to shared repository instructions")
    if not agents.exists():
        agents.write_text(
            f"# {root.name}\n\nprofile: {profile}\n{declaration_line(surfaces)}\n"
        )
        outcomes.append(f"DO:   created {agents}")
    else:
        outcomes.append(_ensure_profile(agents, profile))
        outcomes.append(write_declaration(str(root), surfaces))

    if promoted_claude_only or duplicate_instructions:
        _backup_once(claude)
        claude.unlink()
        claude.symlink_to("AGENTS.md")
        outcomes.append(f"DO:   replaced {claude} with an AGENTS.md link")
    elif claude.is_symlink() and claude.resolve() == agents.resolve():
        outcomes.append(f"ok:   {claude} already links to AGENTS.md")
    elif claude.is_file() and claude.read_bytes() == agents.read_bytes():
        _backup_once(claude)
        claude.unlink()
        claude.symlink_to("AGENTS.md")
        outcomes.append(f"DO:   replaced duplicate {claude} with an AGENTS.md link")
    elif claude.exists() or claude.is_symlink():
        outcomes.append(f"WARN: {claude} already exists — left unchanged")
    else:
        claude.symlink_to("AGENTS.md")
        outcomes.append(f"DO:   linked {claude} to AGENTS.md")

    skill = root / ".agents" / "skills" / "agentflow" / "SKILL.md"
    outcomes.append(_install_file(skill, _asset_text("skills/agentflow/SKILL.md")))
    outcomes.append(_wire_claude_skill(root, "agentflow"))
    outcomes.append(_ensure_fleet_config(root))

    if surfaces:
        harness = root / "scripts" / "screenshots.mjs"
        outcomes.append(
            _install_file(harness, _asset_text("scripts/screenshots.mjs"))
        )
        outcomes.append(_install_connor_skills(root))
        outcomes.append(_install_ui_runtime(root))
    return outcomes


def enroll_repository(
    workdir: str, *, apply: bool = False, profile: str = "reviewed"
) -> CapabilityReport:
    """Plan or apply the reproducible, repo-local AgentFlow enrollment."""
    root = Path(workdir).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    declaration = surface_declaration(str(root))
    surfaces = (
        declaration.surfaces
        if declaration.declared
        else propose_surfaces(str(root))
    )
    print(f"AgentFlow enrollment — {root}")
    print(f"  profile: {profile}")
    print(f"  {declaration_line(surfaces)}")
    if apply:
        problem = (
            _checkout_problem(root)
            or _instructions_problem(root)
            or _config_problem()
            or _tooling_problem(surfaces)
            or _managed_files_problem(root, surfaces)
            or _skills_problem(root, surfaces)
        )
        if problem:
            print(f"  WARN: {problem} — repository left unchanged")
            return replace(doctor(str(root)), ready=False)
        journal = _enrollment_journal(root)
        try:
            outcomes = _apply_enrollment(root, profile, surfaces)
            if any(outcome.startswith("WARN:") for outcome in outcomes):
                journal.restore()
                outcomes.append("WARN: enrollment failed and all managed changes were rolled back")
        except Exception:
            journal.restore()
            raise
        finally:
            journal.close()
        for outcome in outcomes:
            print(f"  {outcome}")
    else:
        print("  PLAN: write shared repository instructions")
        print("  PLAN: install the bundled agentflow skill for Claude Code and Codex")
        if surfaces:
            print("  PLAN: install the canonical screenshot harness")

    report = doctor(str(root))
    if apply and any(outcome.startswith("WARN:") for outcome in outcomes):
        report = replace(report, ready=False)
    for item in report.capabilities:
        if item.install and item.required:
            print(f"  NEXT: {item.install}")
    optional = next(item for item in report.capabilities if item.id == "codebase-memory")
    if not optional.available and optional.install:
        print(f"  OPTIONAL: {optional.install}")
    print(
        "  WARN: GitHub queue labels and pull-request CI are not changed; "
        "verify them before starting the daemon"
    )
    if not apply:
        print("  (dry run — nothing changed; pass --apply to write repository files)")
    return report


def propose_surfaces(workdir: str) -> tuple[str, ...]:
    """The surfaces this checkout looks like it has — empty means headless.

    Detection lives here, in enrollment, and nowhere near the merge path: the gate reads a
    written declaration and never guesses (ADR 0018). Prefixes end in `/` so a declared
    `frontend/` can't also claim `frontend-notes.md`; a repo whose whole UI is one root
    file (a Google Apps Script sidebar, say) declares that file literally.
    """
    root = Path(workdir)
    found: list[Path] = []
    for dirpath, dirnames, _files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP_DIRS and not d.startswith("."))
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
            continue
        for name in list(dirnames):
            if name in _UI_DIR_NAMES:
                found.append(Path(dirpath) / name)
                dirnames.remove(name)   # a surface's own subdirs are part of that surface
    if found:
        return tuple(_prefix_for(root, path) for path in found)
    return tuple(sorted(p.name for p in root.glob("*.html")))


def _prefix_for(root: Path, path: Path) -> str:
    """A surface directory's declared prefix — its `src/` when it has one (a bundled app
    keeps config, lockfiles and build output beside the authored source)."""
    rel = path.relative_to(root).as_posix()
    return f"{rel}/src/" if (path / "src").is_dir() else f"{rel}/"


def declaration_line(surfaces: tuple[str, ...]) -> str:
    """The AGENTS.md line for a proposal — `none` when the repo is headless."""
    return f"{_DECLARATION_KEY} {', '.join(surfaces) if surfaces else UI_SURFACES_NONE}"


def contradicts_checkout(declaration: SurfaceDeclaration, workdir: str) -> bool:
    """A repo claiming to be headless when the checkout plainly has a user-facing surface.

    Enrolment seeds `none` without looking at the repo, so this is how a repo that turned out
    to have a UI stays visible: left alone it would count as answered, keeping the UI-evidence
    gate switched off there for good — the exact hole the declaration exists to close.
    """
    return declaration.headless and bool(propose_surfaces(workdir))


def write_declaration(workdir: str, surfaces: tuple[str, ...]) -> str:
    """Add the declaration to the repo's AGENTS.md, keeping everything already in it.

    Idempotent: a hand-written surface list is never touched, and neither is a `none` the
    checkout agrees with, so re-running the backfill is a no-op. The one line it rewrites is
    a `none` the checkout contradicts — the enrolment seed on a repo that has a UI after all.
    Returns a human-readable outcome.
    """
    target = Path(workdir) / "AGENTS.md"
    if not target.exists():
        return f"SKIP: no AGENTS.md in {workdir} — run enroll-standards.sh --apply first"
    current = surface_declaration(workdir)
    if current.surfaces or (current.headless and not surfaces):
        return "ok:   already declared — leaving it alone"
    backup = target.with_name("AGENTS.md.pre-agentflow")
    if not backup.exists():
        shutil.copy2(target, backup)
    existing = target.read_text()
    line = declaration_line(surfaces)
    corrected, replaced = _UI_SURFACES_RE.subn(lambda _m: line, existing, count=1)
    if replaced:
        target.write_text(corrected)
        return f"DO:   corrected '{line}' in {target}"
    separator = "" if existing.endswith("\n") or not existing else "\n"
    target.write_text(f"{existing}{separator}\n{line}\n")
    return f"DO:   wrote '{line}' to {target}"


def newly_gated_prs(repo: str, surfaces: tuple[str, ...]) -> list[int] | None:
    """The open PRs that this declaration would newly park — measured, not guessed.

    Returns ``None`` when GitHub can't be read, so an operator never reads an unreachable
    API as "nothing would be affected".
    """
    from agentflow import github
    from agentflow.gate import ui_evidence_gap
    if not surfaces:
        return []
    rows = github.list_open_prs(repo)
    if rows is None:
        return None
    return [row.number for row in rows
            if ui_evidence_gap(repo, row.number, list(surfaces))]


def audit_lines(repos) -> list[str]:
    """One line per enrolled repo plus a census tail, naming every repo the gate cannot fire
    in: the ones that never answered, and the ones whose headless answer their own checkout
    contradicts."""
    lines = []
    undeclared = []
    contradicted = []
    for cfg in repos:
        declaration = surface_declaration(cfg.workdir)
        state = _audit_state(declaration)
        if contradicts_checkout(declaration, cfg.workdir):
            state = f"{UI_SURFACES_NONE} — but this checkout has a user-facing surface"
            contradicted.append(cfg.repo)
        lines.append(f"  {cfg.repo}: {state}")
        if not declaration.declared:
            undeclared.append(cfg.repo)
    declared = len(lines) - len(undeclared)
    lines.append(f"{declared} declared / {len(undeclared)} undeclared")
    if undeclared:
        lines.append("undeclared (the UI-evidence gate cannot fire there): "
                     + ", ".join(undeclared))
    if contradicted:
        lines.append("declared headless but the checkout says otherwise (re-run "
                     "`python -m agentflow.enroll surfaces <dir>`): " + ", ".join(contradicted))
    return lines


def _audit_state(declaration: SurfaceDeclaration) -> str:
    if declaration.surfaces:
        return ", ".join(declaration.surfaces)
    return UI_SURFACES_NONE if declaration.headless else "UNDECLARED"


def checkout_repo(workdir: str) -> str:
    """The `owner/name` this checkout pushes to, or empty when it can't be resolved.

    Only ever the repo rooted at this directory: git answers from the nearest enclosing
    checkout, so a directory that is not itself one would otherwise borrow its parent's repo
    and the impact preview would name a different repo's open PRs.
    """
    from agentflow.runner import _run
    # `--show-prefix` is empty only at a checkout's root; ask git rather than comparing paths,
    # which a case-insensitive filesystem gets wrong (a `SampleApp/` checkout rooted at `sampleapp/`).
    prefix = _run(["git", "-C", workdir, "rev-parse", "--show-prefix"])
    if prefix.returncode != 0 or (prefix.stdout or "").strip():
        return ""
    r = _run(["git", "-C", workdir, "remote", "get-url", "origin"])
    if r.returncode != 0:
        return ""
    url = (r.stdout or "").strip().removesuffix("/").removesuffix(".git")
    match = re.match(
        r"^(?:git@github\.com:|https?://github\.com/|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+)$",
        url,
    )
    if not match:
        return ""
    return f"{match.group(1)}/{match.group(2)}"


def _surfaces_command(workdir: str, apply: bool) -> None:
    print(f"UI surfaces — {workdir}")
    current = surface_declaration(workdir)
    if current.declared and not contradicts_checkout(current, workdir):
        print(f"  ok:   already declares {_audit_state(current)}")
        return
    if current.headless:
        print("  WARN: declares none, but this checkout has a user-facing surface — the "
              "UI-evidence gate is switched off here")
    proposal = propose_surfaces(workdir)
    print(f"  proposal: {declaration_line(proposal)}")
    repo = checkout_repo(workdir)
    if not repo:
        print("  WARN: could not resolve the GitHub repo — impact on open PRs is unknown")
    else:
        affected = newly_gated_prs(repo, proposal)
        if affected is None:
            print("  WARN: could not read open PRs — impact is unknown; re-run when GitHub is reachable")
        elif affected:
            print("  impact: these open PRs would newly need screenshots: "
                  + ", ".join(f"#{n}" for n in affected))
        else:
            print("  impact: no open PR would newly need screenshots")
    if apply:
        print(f"  {write_declaration(workdir, proposal)}")
    else:
        print("  (dry run — nothing changed; pass --apply to write it)")


def _audit_command() -> None:
    from agentflow.daemon import REPOS
    print("UI-surface declarations across the enrolled fleet")
    for line in audit_lines(REPOS):
        print(line)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in args
    positional = [a for a in args if a != "--apply"]
    if positional[:1] == ["audit"] and len(positional) == 1:
        _audit_command()
        return
    if positional[:1] == ["surfaces"] and len(positional) == 2:
        _surfaces_command(positional[1], apply)
        return
    if len(positional) != 1 or "/" not in positional[0]:
        print("usage: python -m agentflow.enroll <owner/repo>\n"
              "       python -m agentflow.enroll audit\n"
              "       python -m agentflow.enroll surfaces <dir> [--apply]", file=sys.stderr)
        sys.exit(2)
    repo = positional[0]
    print(f"Sweeping legacy labels in {repo}...")
    changed = sweep_legacy_labels(repo)
    if not changed:
        print("  nothing to change — all issues already use agentflow:* vocabulary")
    else:
        for line in changed:
            print(f"  {line}")
        print(f"  {len(changed)} issue(s) updated")


if __name__ == "__main__":
    main()
