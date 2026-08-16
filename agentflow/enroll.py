"""Inspect and reproducibly enroll a repository into AgentFlow.

The module owns three closely related jobs:

- inspect/install the repository-local capabilities Claude and Codex sessions require;
- sweep any bare pre-enrollment needs-grilling / needs-mockup labels to the agentflow:*
  form (the original job, called by `enroll-standards.sh`);
- declare the repo's user-facing surfaces, so the mechanical UI-evidence gate (ADR 0018)
  is either armed or deliberately headless rather than silently inert.

Usage:
  python -m agentflow.enroll <owner/repo>            # sweep legacy labels
  python -m agentflow.enroll audit                   # fleet declaration and CI-policy census
  python -m agentflow.enroll surfaces <dir> [--apply]  # propose/apply one repo's line

Enrolment seeds `ui-surfaces: none` without looking at the repo, so `surfaces` is also what
corrects that seed on a repo that turned out to have a UI — and `audit` names any repo still
claiming to be headless while its own checkout says otherwise.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import tomllib
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from importlib.resources import files
from pathlib import Path

from agentflow.intake import sweep_legacy_labels
from agentflow.provider_skills import (
    provider_skill_status,
    skill_destination_status as _skill_destination_status,
)
from agentflow.repo_facts import (UI_SURFACES_NONE, SurfaceDeclaration, _UI_SURFACES_RE,
                                  surface_declaration)
from agentflow.runtime_contracts import playwright_runtime_status as _runtime_status
from agentflow.skill_ownership import clear_skill_ownership, mark_skill_owned, skill_ownership

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
_REPAIR_LOCKS: dict[Path, threading.Lock] = {}
_REPAIR_LOCKS_GUARD = threading.Lock()


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
    stage_matrix: tuple["StageCapability", ...] = ()

    def as_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "repository": self.repository,
            "ui": self.ui,
            "ready": self.ready,
            "capabilities": [asdict(item) for item in self.capabilities],
            "stage_matrix": [asdict(item) for item in self.stage_matrix],
        }


@dataclass(frozen=True)
class StageCapability:
    """One dispatchable stage/context/provider capability decision."""
    stage: str
    context: str
    provider: str
    contracts: tuple[str, ...]
    state: str
    evidence: tuple[str, ...]
    repair_command: str
    ready: bool


def _manifest() -> dict:
    return tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())


@contextmanager
def _capability_repair_lock(root: Path):
    """Serialize enrollment-owned repairs for coordinators sharing one repository root.

    Non-reentrant (the per-root ``threading.Lock`` plus the ``flock`` it guards): a nested
    acquisition on the same root within one call chain deadlocks. Callers must not hold this
    lock across a call into `_install_connor_skills`/`_replace_skill_tree`, which acquire it
    themselves.
    """
    lock_dir = root / ".agentflow"
    if lock_dir.is_symlink() or (lock_dir.exists() and not lock_dir.is_dir()):
        raise OSError(".agentflow repair lock directory is incompatible")
    lock_dir.mkdir(exist_ok=True)
    lock_path = lock_dir / "enrollment.lock"
    with _REPAIR_LOCKS_GUARD:
        local_lock = _REPAIR_LOCKS.setdefault(root, threading.Lock())
    with local_lock:
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("enrollment repair lock is not a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)


def repair_capability_refusal(root: str | Path, provider: str, requirements):
    """Repair one deterministic missing capability set without touching occupied content."""
    from agentflow.capability_contracts import CapabilityRepairResult

    requested_root = Path(root).expanduser()
    if requested_root.is_symlink() or not requested_root.is_dir():
        return None
    root = requested_root.resolve()
    if provider not in {"claude", "codex"}:
        return None
    with _capability_repair_lock(root):
        manifest = _manifest()
        specs = {item["id"]: item for item in manifest["capabilities"]}
        skill_specs = [
            specs[item.id] for item in requirements
            if item.id in specs and specs[item.id].get("skill")
        ]
        missing = []
        if provider == "claude":
            for spec in skill_specs:
                name = spec["skill"]
                source = root / ".agents" / "skills" / name
                destination = root / ".claude" / "skills" / name
                if _skill_destination_status(source, spec["files"]) != "ok":
                    return None
                status = _skill_destination_status(destination, spec["files"])
                if status == "absent":
                    missing.append(name)
                elif status != "ok":
                    return None
        runtime_missing = False
        if any(item.runtime for item in requirements):
            harness = specs["screenshot-harness"]
            if _content_status(
                [root / "scripts" / "screenshots.mjs"], harness["sha256"]
            ) != "ok":
                return None
            location = ".agents" if provider == "codex" else ".claude"
            drive = specs["drive-local-webapp"]
            drive_root = root / location / "skills" / drive["skill"]
            drive_status = _skill_destination_status(drive_root, drive["files"])
            materializing_drive = (
                provider == "claude"
                and drive_status == "absent"
                and drive["skill"] in missing
            )
            if drive_status != "ok" and not materializing_drive:
                return None
            runtime = drive_root / "node_modules"
            if runtime.exists() or runtime.is_symlink():
                return None
            runtime_missing = True
        if not missing and not runtime_missing:
            location = ".agents" if provider == "codex" else ".claude"
            if not skill_specs or any(
                _skill_destination_status(
                    root / location / "skills" / spec["skill"], spec["files"]
                ) != "ok"
                for spec in skill_specs
            ):
                return None
            from agentflow.provider_skills import native_discovery_status, prove_native_discovery

            receipt_status, detail = native_discovery_status(root, provider)
            if (receipt_status != "missing"
                    and detail != f"{provider} native-discovery receipt is stale or incompatible"):
                return None
            repaired, probe_detail = prove_native_discovery(root, provider)
            return CapabilityRepairResult(repaired, probe_detail)
        created_skills = [
            (root / ".claude" / "skills" / spec["skill"], spec["files"])
            for spec in skill_specs if spec["skill"] in missing
        ]
        installed_snapshots: dict[Path, tuple | None] = {}

        def tree_snapshot(path: Path) -> tuple | None:
            """Fingerprint every entry without following links; unreadable means preserve."""
            entries = []
            try:
                for current, directories, filenames in os.walk(path, followlinks=False):
                    directories.sort()
                    filenames.sort()
                    base = Path(current)
                    for name in directories + filenames:
                        item = base / name
                        relative = str(item.relative_to(path))
                        if item.is_symlink():
                            entries.append((relative, "link", str(item.readlink())))
                        elif item.is_dir():
                            entries.append((relative, "dir", ""))
                        elif item.is_file():
                            entries.append((
                                relative, "file",
                                hashlib.sha256(item.read_bytes()).hexdigest(),
                            ))
                        else:
                            entries.append((relative, "other", ""))
            except OSError:
                return None
            return tuple(entries)

        def restore_created_skills() -> list[str]:
            """Undo only our still-pinned copies; preserve concurrent changes."""
            errors = []
            for destination, _files_manifest in reversed(created_skills):
                if not destination.exists() and not destination.is_symlink():
                    continue
                installed = installed_snapshots.get(destination)
                if installed is None or tree_snapshot(destination) != installed:
                    errors.append(f"{destination} changed concurrently; preserved")
                    continue
                try:
                    shutil.rmtree(destination)
                    clear_skill_ownership(destination)
                except OSError as exc:
                    errors.append(f"{destination}: {exc}")
            return errors

        try:
            outcomes = []
            for name in missing:
                outcome = _wire_claude_skill(root, name)
                outcomes.append(outcome)
                if not outcome.startswith("WARN:"):
                    destination = root / ".claude" / "skills" / name
                    installed_snapshots[destination] = tree_snapshot(destination)
            if runtime_missing:
                outcomes.append(_install_ui_runtime(root, provider=provider))
            if any(outcome.startswith("WARN:") for outcome in outcomes):
                rollback = restore_created_skills()
                detail = "; ".join(outcomes)
                if rollback:
                    detail += "; rollback: " + "; ".join(rollback)
                return CapabilityRepairResult(False, detail)
        except Exception as exc:
            rollback = restore_created_skills()
            detail = f"{type(exc).__name__}: {exc}"
            if rollback:
                detail += "; rollback: " + "; ".join(rollback)
            return CapabilityRepairResult(False, detail)
        repaired = []
        if missing:
            repaired.append("materialized absent pinned capability destinations for Claude")
        if runtime_missing:
            repaired.append("installed pinned Playwright runtime")
        return CapabilityRepairResult(True, "; ".join(repaired))


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


def _skill_status(root: Path, name: str, files_manifest: list[dict]) -> str:
    statuses = []
    for location in (".agents/skills", ".claude/skills"):
        directory = root / location / name
        statuses.append(_skill_destination_status(directory, files_manifest))
    if "incompatible" in statuses:
        return "incompatible"
    if "drifted" in statuses:
        return "drifted"
    if "absent" in statuses:
        return "missing"
    return "ok"


def _connor_skill_command(
    manifest: dict, source_tree: Path | None = None, *, skill: str | None = None
) -> list[str]:
    installer = manifest["skill_installer"]
    source = manifest["connor_skills"]
    tree = (
        str(source_tree)
        if source_tree is not None
        else f"{source['source']}/tree/{source['tag']}"
    )
    command = ["npx", f"{installer['package']}@{installer['version']}", "add", tree]
    selected = (skill,) if skill else tuple(source["skills"])
    for name in selected:
        command.extend(("--skill", name))
    command.extend(("-a", "codex", "-y"))
    return command


def _resolved_release(source: dict) -> tuple[str | None, str | None]:
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


def _resolved_skill_release(manifest: dict) -> tuple[str | None, str | None]:
    """Resolve the public skill release through the manifest-shaped compatibility seam."""
    return _resolved_release(manifest["connor_skills"])


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


def _methodology_destination_states(root: Path, manifest: dict) -> dict:
    names = manifest["methodology_skills"]["skills"]
    specs = {item.get("skill"): item for item in manifest["capabilities"]}
    return {(location, name): _skill_destination_status(root / location / name, specs[name]["files"])
            for location in (".agents/skills", ".claude/skills") for name in names}


def _methodology_problem(root: Path) -> str | None:
    states = _methodology_destination_states(root, _manifest())
    if all(state == "ok" for state in states.values()) or all(state == "absent" for state in states.values()):
        return None
    manifest = _manifest()
    names = manifest["methodology_skills"]["skills"]
    if all(states[(".agents/skills", name)] == "ok" for name in names) and all(
        states[(".claude/skills", name)] == "absent" for name in names
    ):
        return None
    rendered = ", ".join(f"{location}/{name}={state}" for (location, name), state in states.items())
    return f"existing methodology destinations are partial, conflicting, or drifted ({rendered})"


def _skills_problem(
    root: Path, surfaces: tuple[str, ...], *, converge: bool = False
) -> str | None:
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
    names = manifest["connor_skills"]["skills"]
    if all(destinations[(".agents/skills", name)] == "ok" for name in names) and all(
        destinations[(".claude/skills", name)] == "absent"
        or _legacy_claude_skill_link(root, name)
        for name in names
    ):
        return None
    # Converge may repair drift only when the destination retains a valid ownership marker;
    # unowned or incompatible content remains a blocking precondition. The pinned release is
    # fetched into a temporary installer root, never synthesized from `_asset_text`.
    if converge and all(state in ("ok", "drifted") for state in destinations.values()):
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


def playwright_runtime_status(
    root: Path,
    *,
    version: str,
    node_minimum: int,
    manifest: dict | None = None,
) -> tuple[str, str]:
    resolved_manifest = manifest or _manifest()
    specs = {item["id"]: item for item in resolved_manifest["capabilities"]}
    drive = specs["drive-local-webapp"]
    drive_status = _skill_status(root, drive["skill"], drive["files"])
    if drive_status != "ok":
        return drive_status, f"pinned drive-local-webapp contract is {drive_status}"
    results = tuple(
        _runtime_status(
            root, version=version, node_minimum=node_minimum,
            manifest=resolved_manifest, provider=provider,
        )
        for provider in ("claude", "codex")
    )
    for state in ("missing", "drifted", "incompatible"):
        details = [f"{provider}: {detail}" for provider, (status, detail)
                   in zip(("claude", "codex"), results) if status == state]
        if details:
            return state, "; ".join(details)
    return "ok", "; ".join(
        f"{provider}: {detail}" for provider, (_status, detail)
        in zip(("claude", "codex"), results)
    )


def _playwright_available(
    root: Path, *, version: str, node_minimum: int, verify_runtime: bool = False
) -> bool:
    status, _detail = playwright_runtime_status(
        root, version=version, node_minimum=node_minimum
    )
    if status != "ok":
        return False
    if not verify_runtime:
        return True
    harness = root / "scripts" / "screenshots.mjs"
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


def doctor(workdir: str, *, stage: str | None = None, provider: str | None = None) -> CapabilityReport:
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
        elif name in {"agentflow-skill", "ui-craft", "drive-local-webapp", "tdd", "codebase-design", "domain-modeling"}:
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
            status, runtime_detail = playwright_runtime_status(
                root,
                version=version,
                node_minimum=playwright["node_minimum"],
                manifest=manifest,
            )
            available = status == "ok"
            detail = (
                f"trusted harness and drive skill, Node >= "
                f"{playwright['node_minimum']}, and installed Playwright {version} metadata; "
                f"{runtime_detail}"
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
    from agentflow.capability_contracts import preflight
    from agentflow.prompts import STAGE_PROMPTS, requirements_for

    selected_stages = (stage,) if stage else tuple(STAGE_PROMPTS)
    selected_providers = (provider,) if provider else ("claude", "codex")
    repository_contexts = ((False, "headless"), (True, "ui")) if ui else ((False, "headless"),)
    matrix: list[StageCapability] = []
    for stage_name in selected_stages:
        spec = STAGE_PROMPTS[stage_name]
        selected_contexts = tuple(
            (ui_context, context_name)
            for ui_context, context_name in repository_contexts
            if context_name in spec.contexts
        )
        for ui_context, context_name in selected_contexts:
            for provider_name in selected_providers:
                required_contracts = requirements_for(stage_name, {"ui": ui_context})
                result = preflight(root, stage_name, provider_name, required_contracts)
                matrix.append(StageCapability(
                    stage_name, context_name, provider_name,
                    tuple(f"{item.id}@{item.version}" for item in required_contracts), result.state,
                    result.evidence, result.repair_command, result.ready))
    ready = (
        all(row.available for row in rows if row.required)
        and all(cell.ready for cell in matrix)
    )
    return CapabilityReport(
        schema_version=manifest["schema_version"],
        repository=str(root),
        ui=ui,
        ready=ready,
        capabilities=tuple(rows),
        stage_matrix=tuple(matrix),
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


def _append_once(path: Path, line: str, *, backup: bool = True) -> None:
    existing = path.read_text() if path.exists() else ""
    if line in existing.splitlines():
        return
    if backup:
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
    required = ("node", "npm", "npx") if surfaces else ("npx",)
    missing = [name for name in required if shutil.which(name) is None]
    if missing:
        return f"missing required enrollment command(s): {', '.join(missing)}"
    return None


def _managed_files_problem(
    root: Path, surfaces: tuple[str, ...], *, converge: bool = False
) -> str | None:
    manifest = _manifest()
    agentflow = next(
        item for item in manifest["capabilities"] if item["id"] == "agentflow-skill"
    )
    for location in (".agents/skills", ".claude/skills"):
        directory = root / location / "agentflow"
        status = _skill_destination_status(directory, agentflow["files"])
        if status == "ok" or not (directory.exists() or directory.is_symlink()):
            continue
        if location == ".claude/skills" and _legacy_claude_skill_link(root, "agentflow"):
            continue  # enrollment safely replaces the former exact project-local link
        if (
            converge
            and status == "drifted"
            and directory.is_dir()
            and all(
                (directory / item["path"]).is_file() for item in agentflow["files"]
            )
        ):
            continue  # convergeable: every expected path is a plain file we can overwrite
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
            if converge and status == "drifted" and harness.is_file():
                pass  # convergeable
            else:
                return f"managed screenshot harness is {status} at {harness}"
    return None


def _install_file(path: Path, content: str, *, overwrite: bool = False) -> str:
    if path.is_file():
        if path.read_text() == content:
            return f"ok:   {path} already matches"
        if overwrite:
            path.write_text(content)
            return f"DO:   rewrote {path} to the pinned content"
        return f"WARN: {path} already exists and differs — left unchanged"
    if path.exists() or path.is_symlink():
        return f"WARN: {path} already exists — left unchanged"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return f"DO:   installed {path}"


def _normalize_skill_lock(
    root: Path, names: list[str], *, source: str, commit: str
) -> str | None:
    lock = root / "skills-lock.json"
    try:
        if not lock.exists():
            return None
        if not lock.is_file():
            return f"WARN: {lock} is not a regular skills lock file"
        document = json.loads(lock.read_text())
    except OSError as exc:
        return f"WARN: could not read {lock}: {exc}"
    except json.JSONDecodeError as exc:
        return f"WARN: could not parse {lock}: {exc}"
    if not isinstance(document, dict):
        return f"WARN: {lock} must contain a JSON object"
    skills = document.get("skills")
    if not isinstance(skills, dict):
        return f"WARN: {lock} has no valid skills object"
    if any(not isinstance(entry, dict) for entry in skills.values()):
        return f"WARN: {lock} contains an invalid skill entry"
    changed = False
    for name in names:
        entry = skills.get(name)
        if entry is None:
            continue
        normalized = {**entry, "source": source, "sourceType": "git", "ref": commit}
        if normalized != entry:
            skills[name] = normalized
            changed = True
    if not changed:
        return None
    try:
        lock.write_text(json.dumps(document, indent=2) + "\n")
    except OSError as exc:
        return f"WARN: could not write {lock}: {exc}"
    return None


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


def _replace_skill_tree(destination: Path, source: Path, files_manifest: list[dict]) -> str:
    """Atomically swap one owned skill tree for a complete pinned replacement."""
    with _capability_repair_lock(destination.parents[2]):
        if (
            _skill_destination_status(destination, files_manifest) != "drifted"
            or skill_ownership(destination) is None
        ):
            return f"WARN: {destination} changed before owned-drift repair"
        for leftover in destination.parent.glob(f".{destination.name}-*"):
            if leftover.is_dir() and not leftover.is_symlink():
                shutil.rmtree(leftover)
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent)
        )
        replacement = temporary / "replacement"
        backup = temporary / "previous"
        try:
            shutil.copytree(source, replacement)
            destination.replace(backup)
            try:
                replacement.replace(destination)
            except OSError:
                backup.replace(destination)
                raise
            shutil.rmtree(backup)
            return f"DO:   repaired owned drift at {destination}"
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


def _install_connor_skills(root: Path, *, converge: bool = False) -> str:
    manifest = _manifest()
    names = manifest["connor_skills"]["skills"]
    specs = {item.get("skill"): item for item in manifest["capabilities"]}
    expected = manifest["connor_skills"]["commit"]
    warning = _normalize_skill_lock(
        root,
        names,
        source=manifest["connor_skills"]["source"],
        commit=expected,
    )
    if warning:
        return warning
    destinations = _public_skill_destination_states(root, manifest)
    if all(state == "ok" for state in destinations.values()):
        return "ok:   Connor skill pack already installed"
    if all(destinations[(".agents/skills", name)] == "ok" for name in names) and all(
        destinations[(".claude/skills", name)] == "absent"
        or _legacy_claude_skill_link(root, name)
        for name in names
    ):
        outcomes = [_wire_claude_skill(root, name, expected) for name in names]
        refreshed = _public_skill_destination_states(root, manifest)
        if not any(outcome.startswith("WARN:") for outcome in outcomes) and all(
            state == "ok" for state in refreshed.values()
        ):
            return "DO:   materialized the pinned Connor skill pack for Claude"
    repairs = {
        (location, name)
        for (location, name), state in destinations.items()
        if state == "drifted" and skill_ownership(root / location / name) is not None
    }
    if converge and repairs and all(
        state == "ok" or (location, name) in repairs
        for (location, name), state in destinations.items()
    ):
        repair_names = {name for _location, name in repairs}
    else:
        repair_names = set()
    if any(state != "absent" for state in destinations.values()):
        if not repair_names:
            rendered = ", ".join(
                f"{location}/{name}={state}"
                for (location, name), state in destinations.items()
            )
            return (
                "WARN: existing public skill destinations are partial or conflicting; "
                f"installer was not run ({rendered})"
            )
    resolved, error = _resolved_skill_release(manifest)
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
        for name in names:
            if repair_names and name not in repair_names:
                continue
            command = _connor_skill_command(manifest, source_tree, skill=name)
            install_root = Path(temporary) if name in repair_names else root
            result = _run_command(command, cwd=install_root, timeout=120)
            if result.returncode:
                reason = (result.stderr or result.stdout).strip().splitlines()
                tail = reason[-1] if reason else f"exit {result.returncode}"
                return f"WARN: Connor skill install failed — {tail}"
            warning = _normalize_skill_lock(
                root,
                [name],
                source=manifest["connor_skills"]["source"],
                commit=expected,
            )
            if warning:
                return warning
            if name in repair_names:
                source = install_root / ".agents" / "skills" / name
                for location, repaired_name in repairs:
                    if repaired_name != name:
                        continue
                    destination = root / location / name
                    outcome = _replace_skill_tree(destination, source, specs[name]["files"])
                    if outcome.startswith("WARN:"):
                        return outcome
                    try:
                        mark_skill_owned(destination, expected)
                    except OSError as exc:
                        return f"WARN: could not record AgentFlow ownership for {destination}: {exc}"
            else:
                destination = root / ".agents" / "skills" / name
                try:
                    mark_skill_owned(destination, expected)
                except OSError as exc:
                    return f"WARN: could not record AgentFlow ownership for {destination}: {exc}"
            wiring = _wire_claude_skill(root, name, expected)
            if wiring.startswith("WARN:"):
                return wiring
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


def _install_methodology_skills(root: Path) -> str:
    """Install all method contracts atomically enough to reject any occupied destination.

    The installer is allowed only when every declared provider-local destination is absent.  It
    never reads global skill roots, so an ambient copy cannot satisfy nor be overwritten by this
    recovery path.
    """
    manifest = _manifest()
    source = manifest["methodology_skills"]
    warning = _normalize_skill_lock(
        root,
        source["skills"],
        source=source["source"],
        commit=source["commit"],
    )
    if warning:
        return warning
    states = _methodology_destination_states(root, manifest)
    if all(state == "ok" for state in states.values()):
        return "ok:   methodology contracts already installed"
    if all(states[(".agents/skills", name)] == "ok" for name in source["skills"]) and all(
        states[(".claude/skills", name)] == "absent"
        for name in source["skills"]
    ):
        outcomes = [_wire_claude_skill(root, name) for name in source["skills"]]
        refreshed = _methodology_destination_states(root, manifest)
        if not any(outcome.startswith("WARN:") for outcome in outcomes) and all(
            state == "ok" for state in refreshed.values()
        ):
            return "DO:   materialized pinned methodology contracts for Claude"
    if any(state != "absent" for state in states.values()):
        return "WARN: methodology destinations are partial, conflicting, or drifted; installer was not run"
    with tempfile.TemporaryDirectory(prefix="agentflow-methodology-") as temporary:
        tree = Path(temporary) / "source"
        for command in (["git", "clone", "--no-checkout", source["source"], str(tree)],
                        ["git", "-C", str(tree), "checkout", "--detach", source["commit"]],
                        ["git", "-C", str(tree), "rev-parse", "HEAD"]):
            result = _run_command(command, timeout=120)
            if result.returncode:
                return "WARN: exact methodology source fetch failed"
        if result.stdout.strip() != source["commit"]:
            return "WARN: exact methodology source checkout did not match manifest"
        for name in source["skills"]:
            command = [
                "npx",
                f"{manifest['skill_installer']['package']}@{manifest['skill_installer']['version']}",
                "add",
                str(tree),
                "--skill",
                name,
                "-a",
                "codex",
                "-y",
            ]
            result = _run_command(command, cwd=root, timeout=120)
            if result.returncode:
                return "WARN: methodology contract install failed"
            warning = _normalize_skill_lock(
                root,
                [name],
                source=source["source"],
                commit=source["commit"],
            )
            if warning:
                return warning
            wiring = _wire_claude_skill(root, name)
            if wiring.startswith("WARN:"):
                return wiring
    if not all(state == "ok" for state in _methodology_destination_states(root, manifest).values()):
        return "WARN: methodology installer completed but project-local contracts do not match manifest"
    return "DO:   installed pinned methodology contracts"


def _install_ui_runtime(root: Path, *, provider: str | None = None) -> str:
    manifest = _manifest()
    specs = {item.get("skill"): item for item in manifest["capabilities"]}
    locations = (
        ({"codex": ".agents/skills", "claude": ".claude/skills"}[provider],)
        if provider is not None else (".agents/skills", ".claude/skills")
    )
    for name in manifest["connor_skills"]["skills"]:
        if any(
            _skill_destination_status(root / location / name, specs[name]["files"]) != "ok"
            for location in locations
        ):
            return "WARN: UI runtime skipped because the pinned skill pack is not intact"
    playwright = manifest["playwright"]

    def available() -> bool:
        if provider is None:
            return _playwright_available(
                root, version=playwright["version"],
                node_minimum=playwright["node_minimum"], verify_runtime=True,
            )
        status, _detail = _runtime_status(
            root, version=playwright["version"], node_minimum=playwright["node_minimum"],
            manifest=manifest, provider=provider,
        )
        if status != "ok":
            return False
        result = _run_command(
            ["node", str(root / "scripts" / "screenshots.mjs"), "--self-check"],
            cwd=root, timeout=30,
        )
        return result.returncode == 0

    if available():
        return "ok:   pinned Playwright, Chromium, and screenshot harness are ready"
    skill_dirs = {
        (root / location / "drive-local-webapp").resolve() for location in locations
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
    if not available():
        return "WARN: UI runtime setup completed but the screenshot harness self-check failed"
    return "DO:   installed pinned Playwright and Chromium; self-check passed"


def _legacy_claude_skill_link(root: Path, name: str) -> bool:
    target = root / ".claude" / "skills" / name
    desired = Path("../../.agents/skills") / name
    return target.is_symlink() and target.readlink() == desired


def _wire_claude_skill(root: Path, name: str, pin: str | None = None) -> str:
    """Materialize a repo skill's Claude-local copy and mark its provenance.

    ``pin`` should be the same resolved pin the caller already recorded for this skill's
    other destination, so one enrollment run records one pin per skill rather than two —
    pass it explicitly whenever the caller has already resolved one. Callers that have not
    (e.g. wiring a pre-existing ``.agents`` destination whose own pin is unknown here) fall
    back to that destination's own marker, then to the manifest's pinned ``version``.
    """
    target = root / ".claude" / "skills" / name
    source = root / ".agents" / "skills" / name
    if target.is_dir() and not target.is_symlink():
        return f"ok:   {target} already materializes the shared repo skill"
    if _legacy_claude_skill_link(root, name):
        target.unlink()
    if target.exists() or target.is_symlink():
        return f"WARN: {target} already exists — left unchanged"
    if not source.is_dir() or source.is_symlink():
        return f"WARN: {source} is not a safe materialization source"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    try:
        if pin is None:
            source_ownership = skill_ownership(source)
            if source_ownership is not None:
                pin = source_ownership["pin"]
            else:
                specs = {item.get("skill"): item for item in _manifest()["capabilities"]}
                pin = specs[name].get("version") or specs[name].get("source") or "unpinned"
        mark_skill_owned(target, pin)
    except (KeyError, OSError) as exc:
        shutil.rmtree(target)
        return f"WARN: could not record AgentFlow ownership for {target}: {exc}"
    return f"DO:   materialized {target} for Claude Code"


class _EnrollmentJournal:
    def __init__(self, paths: list[Path]):
        self._temporary = tempfile.TemporaryDirectory(prefix="agentflow-enroll-")
        self._root = Path(self._temporary.name)
        self._entries: list[tuple[Path, Path | None]] = []
        # Only a parent this enrollment run watched come into existence is ours to remove on
        # rollback — a parent that already existed may be a concurrent repair's ``.agentflow``,
        # and blindly rmdir-ing it races that repair's own mkdir+open of its lock file.
        self._absent_parents_at_start = {
            path.parent for path in dict.fromkeys(paths) if not path.parent.exists()
        }
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
        for path, _backup in self._entries:
            if path.name == "skill-ownership" and path.parent in self._absent_parents_at_start:
                try:
                    path.parent.rmdir()
                except OSError:
                    pass

    def discard_created_backups(self) -> None:
        """Remove rollback copies that a successful enrollment no longer needs."""
        for path, backup in self._entries:
            if backup is None and path.name.endswith(".pre-agentflow") and path.is_file():
                path.unlink()

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
        root / ".agentflow" / "skill-ownership",
        config,
    ]
    if config.is_symlink():
        paths.append(config.resolve())
    return _EnrollmentJournal(
        paths
    )


def _apply_enrollment(
    root: Path, profile: str, surfaces: tuple[str, ...], *, converge: bool = False
) -> list[str]:
    outcomes: list[str] = []
    _append_once(root / ".gitignore", ".agentflow/", backup=False)
    if surfaces:
        _append_once(root / ".gitignore", ".agents/skills/**/node_modules/", backup=False)
        _append_once(root / ".gitignore", ".claude/skills/**/node_modules/", backup=False)

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
    outcomes.append(
        _install_file(
            skill, _asset_text("skills/agentflow/SKILL.md"), overwrite=converge
        )
    )
    outcomes.append(_wire_claude_skill(root, "agentflow"))
    outcomes.append(_ensure_fleet_config(root))
    outcomes.append(_install_methodology_skills(root))

    if surfaces:
        harness = root / "scripts" / "screenshots.mjs"
        outcomes.append(
            _install_file(
                harness, _asset_text("scripts/screenshots.mjs"), overwrite=converge
            )
        )
        outcomes.append(
            _install_connor_skills(root, converge=True)
            if converge else _install_connor_skills(root)
        )
        outcomes.append(_install_ui_runtime(root))
    return outcomes


def _enrollment_write_targets(root: Path, surfaces: tuple[str, ...]) -> list[tuple[Path, str]]:
    """The repository-local paths enrollment must be able to stage after a clean apply."""
    targets = [
        (root / ".gitignore", "enrollment file"),
        (root / "AGENTS.md", "enrollment file"),
        (root / "CLAUDE.md", "enrollment file"),
        (root / "skills-lock.json", "enrollment file"),
    ]
    manifest = _manifest()
    skills = set(manifest["methodology_skills"]["skills"])
    for spec in manifest["capabilities"]:
        name = spec.get("skill")
        if not name or "files" not in spec:
            continue
        required = (
            spec["id"] == "agentflow-skill"
            or name in skills
            or (surfaces and spec["requirement"] == "ui")
        )
        if not required:
            continue
        for location in (".agents/skills", ".claude/skills"):
            targets.extend(
                (root / location / name / item["path"], "required capability file")
                for item in spec["files"]
            )
    if surfaces:
        targets.append((root / "scripts" / "screenshots.mjs", "required capability file"))
    return targets


def _ignored_enrollment_path(root: Path, surfaces: tuple[str, ...]) -> str | None:
    """Name the first enrollment write Git would exclude, using Git as the rule authority."""
    for path, kind in _enrollment_write_targets(root, surfaces):
        if any(parent.is_symlink() for parent in path.parents if parent != root):
            # A legacy Claude skill link is replaced by a materialized directory during
            # apply (#708). Git cannot check a would-be child below that link.
            continue
        relative = path.relative_to(root)
        result = _run_command(
            ["git", "-C", str(root), "check-ignore", "-v", "--", str(relative)], timeout=30
        )
        if result.returncode == 1:
            continue
        if result.returncode:
            detail = (result.stderr or result.stdout).strip() or "git check-ignore failed"
            return f"could not verify whether {relative} is ignored — {detail}"
        rule = (result.stdout or "").strip().split("\t", 1)[0]
        if rule:
            return f"{kind} {relative} is ignored by {rule}"
    return None


def enroll_repository(
    workdir: str,
    *,
    apply: bool = False,
    profile: str = "reviewed",
    converge: bool = False,
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
            or _managed_files_problem(root, surfaces, converge=converge)
            or _skills_problem(root, surfaces, converge=converge)
            or _methodology_problem(root)
            or _ignored_enrollment_path(root, surfaces)
        )
        if problem:
            print(f"  WARN: {problem} — repository left unchanged")
            return replace(doctor(str(root)), ready=False)
        journal = _enrollment_journal(root)
        try:
            outcomes = _apply_enrollment(root, profile, surfaces, converge=converge)
            if any(outcome.startswith("WARN:") for outcome in outcomes):
                journal.restore()
                outcomes.append("WARN: enrollment failed and all managed changes were rolled back")
            else:
                journal.discard_created_backups()
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
        print("  PLAN: install pinned methodology contracts for Claude Code and Codex")
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


_SYNC_BRANCH = "agentflow/enroll-sync"


def _repo_drift(root: Path) -> tuple[list[str], list[str]]:
    """The `drifted`/`missing` capability rows, split by `item.required`: `drift` is what
    a converge would fix (or what is still wrong after it ran), `notes` is informational
    only — never required here, so it never plans or blocks convergence."""
    report = doctor(str(root))
    drift = []
    notes = []
    for item in report.capabilities:
        if item.status not in ("drifted", "missing", "incompatible"):
            continue
        line = f"{item.id}: {item.status}"
        if item.required:
            drift.append(line)
        else:
            notes.append(line)
    return drift, notes


def _uncommitted_paths(status: str) -> list[str]:
    """The non-index side of porcelain output after converge stages the clean checkout."""
    paths = []
    for line in status.splitlines():
        if len(line) < 4 or (line[:2] != "??" and line[1] == " "):
            continue
        paths.append(line[3:])
    return paths


def _current_ref(root: Path) -> str | None:
    """The branch name to restore to afterwards, or the commit sha if detached. None if
    even that can't be determined."""
    branch = _run_command(
        ["git", "-C", str(root), "symbolic-ref", "--quiet", "--short", "HEAD"], timeout=30
    )
    if not branch.returncode:
        return (branch.stdout or "").strip() or None
    sha = _run_command(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=30)
    if not sha.returncode:
        return (sha.stdout or "").strip() or None
    return None


def _converge_and_ship(root: Path, repo: str) -> tuple[bool, str]:
    """Converge one repo and open its PR. Any step failing — the converge apply itself,
    the commit, the push, or `gh pr create` — is a convergence failure. Whatever branch
    (or detached commit) the checkout was on before is restored afterwards, on every exit
    path including exceptions from `enroll_repository`."""
    original = _current_ref(root)

    def _do_converge() -> tuple[bool, str]:
        branch = _run_command(
            ["git", "-C", str(root), "checkout", "-B", _SYNC_BRANCH], timeout=30
        )
        if branch.returncode:
            return False, f"could not create branch {_SYNC_BRANCH}"
        report = enroll_repository(str(root), apply=True, converge=True)
        if not report.ready:
            return False, "enrollment did not converge cleanly"
        add = _run_command(
            ["git", "-C", str(root), "add", "-A"], timeout=30
        )
        status = _run_command(["git", "-C", str(root), "status", "--porcelain"], timeout=30)
        uncommitted = _uncommitted_paths(status.stdout or "")
        if uncommitted:
            return False, "enrollment did not converge — uncommitted paths: " + ", ".join(uncommitted)
        if add.returncode:
            reason = (add.stderr or add.stdout or "").strip()
            return False, f"git add failed — {reason}"
        if not (status.stdout or "").strip():
            return True, "already current after converge"
        commit = _run_command(
            [
                "git", "-C", str(root), "commit", "-s", "-m",
                "agentflow: converge repository enrollment artifacts",
            ],
            timeout=30,
        )
        if commit.returncode:
            reason = (commit.stderr or commit.stdout or "").strip()
            return False, f"git commit failed — {reason}"
        push = _run_command(
            ["git", "-C", str(root), "push", "-u", "origin", _SYNC_BRANCH], timeout=60
        )
        if push.returncode:
            reason = (push.stderr or push.stdout or "").strip()
            return False, f"git push failed — {reason}"
        from agentflow import github

        pr = github.create_pr(
            repo,
            head=_SYNC_BRANCH,
            title="agentflow: converge repository enrollment artifacts",
            body=(
                "Automated by `agentflow enroll --sync --apply`: commits every repository-local "
                "enrollment artifact it materialized or converged. A drifted vendored skill pack "
                "(ui-craft, drive-local-webapp) is repaired here only when AgentFlow's ownership "
                "record proves it materialized the destination; unowned drift stays reported by "
                "`agentflow doctor`."
            ),
        )
        if pr.url is None:
            return False, f"gh pr create failed — {pr.error}"
        return True, pr.url

    restore_failure: str | None = None
    try:
        ok, detail = _do_converge()
    finally:
        # Non-destructive: a plain checkout back to wherever we started, never forced.
        if original is None:
            restore_failure = "could not determine original ref"
        else:
            restore = _run_command(["git", "-C", str(root), "checkout", original], timeout=30)
            if restore.returncode:
                restore_failure = (restore.stderr or restore.stdout or "").strip()

    if restore_failure is not None:
        detail = f"{detail}; checkout left on {_SYNC_BRANCH} — restore failed: {restore_failure}"
        ok = False
    return ok, detail


def sync_fleet(repos, *, apply: bool) -> int:
    """`agentflow enroll --sync [--apply]` — the fleet-wide converge sweep.

    Dry run reports the plan and always exits 0 (a drifted fleet is what a dry run is
    for). With `--apply`, a dirty checkout is skipped — never a failure — and any other
    convergence failure drives exit 1.
    """
    converged = current = failed = skipped = 0
    for cfg in repos:
        root = Path(cfg.workdir).expanduser().resolve()
        print(f"{cfg.repo}")
        if _checkout_problem(root) == "checkout is dirty":
            print(f"  SKIP: {cfg.repo} — checkout is dirty")
            skipped += 1
            continue
        drift, notes = _repo_drift(root)
        if not drift:
            print(f"  ok:   {cfg.repo} is already current")
            for line in notes:
                print(f"  note: {line} (not required here)")
            current += 1
            continue
        for line in notes:
            print(f"  note: {line} (not required here)")
        for line in drift:
            print(f"  drifted: {line}")
        if not apply:
            print(f"  PLAN: converge {cfg.repo}")
            converged += 1
            continue
        ok, detail = _converge_and_ship(root, cfg.repo)
        if ok:
            print(f"  DO:   converged {cfg.repo} — {detail}")
            converged += 1
        else:
            print(f"  WARN: {cfg.repo} failed to converge — {detail}")
            failed += 1
    print(
        f"{converged} converged / {current} already current / "
        f"{failed} failed / {skipped} skipped (dirty)"
    )
    if not apply:
        return 0
    return 1 if failed else 0


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
    from agentflow.ci_policy import audit_workflows

    repos = list(repos)
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
        lines.extend(f"  {cfg.repo}: {finding}" for finding in audit_workflows(cfg.workdir))
        if not declaration.declared:
            undeclared.append(cfg.repo)
    declared = len(repos) - len(undeclared)
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


def configured_repositories():
    """The fleet enumerator — the same source `daemon.run()` reads.

    A configured `workdir` that is not a directory is a config error (`load_config`
    raises `ConfigurationError`), not a per-repo report state, so there is nothing here
    to filter: every repository this returns is a real checkout on disk.
    """
    from agentflow.config import default_config_path, load_config

    return load_config(default_config_path()).repositories


def _audit_command() -> None:
    print("Fleet enrollment and CI-policy audit")
    for line in audit_lines(configured_repositories()):
        print(line)


def main(argv: list[str] | None = None) -> None:
    from agentflow.config import ConfigurationError

    args = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in args
    positional = [a for a in args if a != "--apply"]
    if positional[:1] == ["audit"] and len(positional) == 1:
        try:
            _audit_command()
        except ConfigurationError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(2)
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
