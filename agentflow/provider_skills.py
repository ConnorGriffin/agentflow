"""Provider-local skill integrity and native-discovery receipts.

Static files prove what a skill contains, never that a provider will discover it natively.  A
successful real-provider probe records a repository-scoped receipt bound to the provider binary
and capability manifest.  Every launch-root preflight rechecks both halves and fails closed.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
from importlib.resources import files
from pathlib import Path
import re
import shutil
import stat
import tempfile
import tomllib

from agentflow.filesystem_contracts import (
    _contained,
    _regular_directory,
    _safe_manifest_path,
    runtime_tree_status,
    skill_destination_status,
)
from agentflow.runtime_contracts import playwright_runtime_status


RECEIPT_SCHEMA = 1
NATIVE_DISCOVERY_MARKER = "AGENTFLOW_582_DISCOVERED_4BAB5FF0_AEE6_4D44_BEA3_1BE5D089256F"
NATIVE_DISCOVERY_SKILL = "agentflow-582-probe-4bab5ff0"
_NATIVE_DISCOVERY_FIXTURE = f"""---
name: {NATIVE_DISCOVERY_SKILL}
description: Harmless project-local capability discovery probe for AgentFlow issue 582.
---

# AgentFlow 582 discovery probe

When invoked, reply with this exact line and nothing else:

{NATIVE_DISCOVERY_MARKER}
"""


def _git_inspection_environment() -> dict[str, str]:
    """A minimal environment that cannot redirect repository or config discovery."""
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


def _github_repository(root: Path) -> str:
    """Return a checkout-root GitHub repository identity, or nothing when it is unavailable."""
    from agentflow.runner import _run

    environment = _git_inspection_environment()
    prefix = _run(
        ["git", "-C", str(root), "rev-parse", "--show-prefix"], env=environment
    )
    if prefix.returncode != 0 or (prefix.stdout or "").strip():
        return ""
    remote = _run(
        ["git", "-C", str(root), "remote", "get-url", "origin"], env=environment
    )
    if remote.returncode != 0:
        return ""
    url = (remote.stdout or "").strip().removesuffix("/").removesuffix(".git")
    match = re.fullmatch(
        r"(?:git@github\.com:|https?://github\.com/|ssh://git@github\.com/)"
        r"([^/]+)/([^/]+)",
        url,
    )
    return f"{match.group(1)}/{match.group(2)}".casefold() if match else ""


def _is_packaged_project_source(root: Path) -> bool:
    """Whether the enrolled source belongs to the repository supplying this package."""
    package_repository = _github_repository(Path(str(files("agentflow"))).parent)
    return bool(package_repository) and _github_repository(root) == package_repository


def _tracked_destination_harness(root: Path) -> bool:
    """Whether the destination harness is known to its launch checkout's index."""
    from agentflow.runner import _run

    environment = _git_inspection_environment()
    path = "scripts/screenshots.mjs"
    result = _run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", path],
        env=environment,
    )
    return result.returncode == 0


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


def native_discovery_prompt(provider: str, mode: str = "positive") -> str:
    """Return the provider-native invocation form proven by the release contract."""
    if mode not in {"positive", "negative"}:
        raise ValueError(f"unsupported probe mode {mode}")
    if provider == "codex" and mode == "positive":
        return f"${NATIVE_DISCOVERY_SKILL}"
    if provider == "codex":
        return (
            "Do not invoke any skill or use any tool. Report whether the exact project-local "
            f"skill named {NATIVE_DISCOVERY_SKILL} is available in this session. If it is "
            "unavailable, reply exactly SKILL_UNAVAILABLE."
        )
    if provider == "claude":
        return (
            f"Invoke the project-local skill named {NATIVE_DISCOVERY_SKILL} using only native "
            "skill discovery. Do not use shell commands, search files, read files, or inspect "
            "configuration. If it is unavailable, reply exactly SKILL_UNAVAILABLE."
        )
    raise ValueError(f"unsupported provider {provider}")


def native_discovery_output_has_tool_event(output: str) -> bool:
    """Recognize a provider event that proves the availability probe invoked a tool."""
    def contains_tool(value: object) -> bool:
        if isinstance(value, dict):
            event_type = value.get("type")
            if isinstance(event_type, str) and (
                event_type in {
                    "command_execution", "function_call", "mcp_tool_call",
                    "tool_call", "web_search", "file_change",
                }
                or event_type.endswith("_tool_call")
            ):
                return True
            return any(contains_tool(item) for item in value.values())
        if isinstance(value, list):
            return any(contains_tool(item) for item in value)
        return False

    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(event, dict) and event.get("type") in {"item.started", "item.completed"}:
            item = event.get("item")
            if (isinstance(item, dict) and isinstance(item.get("type"), str)
                    and item["type"] not in {"agent_message", "reasoning"}):
                return True
        if contains_tool(event):
            return True
    return False


def _codex_unavailable_is_terminal(output: str) -> bool:
    unavailable = False
    terminal = False
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "turn.completed":
            terminal = True
        item = event.get("item")
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "agent_message"
                and item.get("text") == "SKILL_UNAVAILABLE"):
            unavailable = True
    return unavailable and terminal


def native_discovery_output_is_proof(provider: str, output: str) -> bool:
    """Validate the provider-specific positive native-discovery evidence."""
    if NATIVE_DISCOVERY_MARKER not in output:
        return False
    if provider == "claude":
        return ('"name":"Skill"' in output
                and f'"skill":"{NATIVE_DISCOVERY_SKILL}"' in output)
    if provider == "codex":
        return not native_discovery_output_has_tool_event(output)
    return False


def native_discovery_output_is_unavailable(provider: str, output: str) -> bool:
    """Validate a negative probe without accepting leaked invocation evidence."""
    return (
        provider in {"claude", "codex"}
        and (
            output.strip() == "SKILL_UNAVAILABLE"
            if provider == "claude"
            else _codex_unavailable_is_terminal(output)
        )
        and NATIVE_DISCOVERY_MARKER not in output
        and '"name":"Skill"' not in output
        and not native_discovery_output_has_tool_event(output)
    )


def _run_native_discovery_probe(root: Path, provider: str):
    """Run the provider proof; kept as the deterministic CI seam."""
    from agentflow.runner import ClaudeRunner, CodexRunner, run_provider_discovery_probe

    prompt = native_discovery_prompt(provider, "positive")
    argv = (
        ClaudeRunner().structured_argv(prompt, "sonnet", str(root))
        if provider == "claude"
        else CodexRunner().structured_argv(prompt, "terra", str(root))
    )
    return run_provider_discovery_probe(argv, str(root))


def prove_native_discovery(root: str | Path, provider: str) -> tuple[bool, str]:
    """Idempotently prove provider-native project skill discovery and issue its receipt."""
    checkout = Path(root)
    if provider not in {"claude", "codex"}:
        return False, f"unsupported provider {provider}"
    current, detail = native_discovery_status(checkout, provider)
    if current == "ok":
        return True, detail
    if checkout.is_symlink() or not checkout.is_dir():
        return False, "probe root must be a real project-local directory"
    location = ".agents" if provider == "codex" else ".claude"
    skill_root = checkout / location / "skills"
    if (not _regular_directory(skill_root) or not _contained(skill_root, checkout)):
        return False, f"{provider} project-local skill root is missing or incompatible"
    try:
        with tempfile.TemporaryDirectory(prefix="agentflow-provider-probe-") as temporary:
            probe_root = Path(temporary)
            fixture = probe_root / location / "skills" / NATIVE_DISCOVERY_SKILL
            fixture.mkdir(parents=True)
            (fixture / "SKILL.md").write_text(_NATIVE_DISCOVERY_FIXTURE)
            result = _run_native_discovery_probe(probe_root, provider)
            output = (result.stdout or "") + (result.stderr or "")
            proven = result.returncode == 0 and native_discovery_output_is_proof(provider, output)
            if not proven:
                return False, f"{provider} did not prove native project skill discovery"
            receipt = record_native_discovery_receipt(checkout, provider)
            return True, f"recorded {provider} native-discovery receipt at {receipt}"
    except OSError as exc:
        return False, f"native-discovery probe failed: {exc}"


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
    source: Path, destination: Path, provider: str, materialize_runtime: bool = False,
    *, requirement_ids: set[str] | None = None, _log=None,
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
    provider_root_existed = destination_provider_root.exists()
    if destination_provider_root.is_symlink() or (
        provider_root_existed and not destination_provider_root.is_dir()
    ):
        return False, f"{provider} launch provider root is incompatible"
    if provider_root_existed and not _contained(destination_provider_root, destination):
        return False, f"{provider} launch provider root escapes the launch root"
    skills_existed = destination_skills.exists()
    if destination_skills.is_symlink() or (
        skills_existed and not destination_skills.is_dir()
    ):
        return False, f"{provider} launch skill root is incompatible"
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    specs = [
        spec for spec in manifest["capabilities"]
        if spec.get("skill") and "version" in spec
    ]
    if requirement_ids is not None:
        specs = [
            spec for spec in specs
            if spec["id"] in requirement_ids or spec["skill"] in requirement_ids
        ]
    source_runtime = source_skills / "drive-local-webapp" / "node_modules"
    destination_drive = destination_skills / "drive-local-webapp"
    destination_runtime = destination_drive / "node_modules"
    missing_skills = [
        (spec, source_skills / spec["skill"], destination_skills / spec["skill"])
        for spec in specs
        if not (destination_skills / spec["skill"]).exists()
        and not (destination_skills / spec["skill"]).is_symlink()
    ]
    for spec, source_skill, _target_skill in missing_skills:
        if skill_destination_status(source_skill, spec["files"]) != "ok":
            return False, f"{provider} source skill {spec['skill']} is not intact"
    _log = _log or (lambda _line: None)
    created: list[tuple[Path, bool]] = []
    created_snapshots: dict[Path, tuple | None] = {}
    replaced_files: list[tuple[Path, bytes, tuple[int, int, int, str]]] = []
    attempted_requirements: list[str] = []
    audit_emitted = False

    def attempted(requirement: str) -> None:
        if requirement not in attempted_requirements:
            attempted_requirements.append(requirement)

    def audit(outcome: str, detail: str) -> None:
        nonlocal audit_emitted
        if audit_emitted or not attempted_requirements:
            return
        audit_emitted = True
        _log(
            f"capability repair root={destination} "
            f"requirements={','.join(attempted_requirements)}; "
            f"outcome={outcome} — {detail}"
        )

    def path_snapshot(path: Path) -> tuple | None:
        """Fingerprint identity and bytes without following links; unreadable means preserve."""
        try:
            root_stat = path.lstat()
            if path.is_symlink():
                return ((".", "link", root_stat.st_dev, root_stat.st_ino, root_stat.st_mode,
                         str(path.readlink())),)
            if path.is_file():
                return ((".", "file", root_stat.st_dev, root_stat.st_ino, root_stat.st_mode,
                         hashlib.sha256(path.read_bytes()).hexdigest()),)
            if not path.is_dir():
                return ((".", "other", root_stat.st_dev, root_stat.st_ino,
                         root_stat.st_mode, ""),)
            entries = [(
                ".", "dir", root_stat.st_dev, root_stat.st_ino, root_stat.st_mode, "",
            )]
            for current, directories, filenames in os.walk(path, followlinks=False):
                directories.sort()
                filenames.sort()
                base = Path(current)
                for name in directories + filenames:
                    item = base / name
                    relative = str(item.relative_to(path))
                    item_stat = item.lstat()
                    if item.is_symlink():
                        kind, content = "link", str(item.readlink())
                    elif item.is_dir():
                        kind, content = "dir", ""
                    elif item.is_file():
                        kind = "file"
                        content = hashlib.sha256(item.read_bytes()).hexdigest()
                    else:
                        kind, content = "other", ""
                    entries.append((
                        relative, kind, item_stat.st_dev, item_stat.st_ino,
                        item_stat.st_mode, content,
                    ))
            return tuple(entries)
        except OSError:
            return None

    def rollback() -> list[str]:
        errors = []
        for path, content, installed in reversed(replaced_files):
            descriptor = None
            try:
                flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
                descriptor = os.open(path, flags)
                with os.fdopen(descriptor, "r+b") as stream:
                    descriptor = None
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                    observed = os.fstat(stream.fileno())
                    identity = (
                        observed.st_dev, observed.st_ino, observed.st_mode,
                        hashlib.sha256(stream.read()).hexdigest(),
                    )
                    if not stat.S_ISREG(observed.st_mode) or identity != installed:
                        errors.append(f"{path} changed concurrently; replacement preserved")
                        continue
                    stream.seek(0)
                    stream.truncate()
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError as exc:
                errors.append(f"{path} changed concurrently; replacement preserved: {exc}")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        for path, recursive in reversed(created):
            try:
                if not path.exists() and not path.is_symlink():
                    continue
                installed = created_snapshots.get(path)
                if installed is None or path_snapshot(path) != installed:
                    errors.append(f"{path} changed concurrently; created content preserved")
                    continue
                if not recursive:
                    path.rmdir()
                elif path.is_symlink():
                    path.unlink()
                elif path.is_file():
                    path.unlink()
                elif path.exists():
                    if not _contained(path, destination):
                        errors.append(f"{path} escapes the launch root")
                        continue
                    shutil.rmtree(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        return errors

    def failed(message: str) -> tuple[bool, str]:
        cleanup_errors = rollback()
        if cleanup_errors:
            message = f"{provider} rollback failed after {message}: {'; '.join(cleanup_errors)}"
        audit("failed", message)
        return False, message

    def claim_directory(path: Path, *, recursive: bool = True) -> tuple[bool, str] | None:
        try:
            path.mkdir()
        except FileExistsError:
            return failed(f"{provider} launch destination appeared concurrently: {path}")
        except OSError as exc:
            return failed(f"{provider} launch destination creation failed: {path}: {exc}")
        created.append((path, recursive))
        created_snapshots[path] = path_snapshot(path)
        return None

    def materialize_harness() -> tuple[bool, str] | None:
        harness = next(
            item for item in manifest["capabilities"] if item["id"] == "screenshot-harness"
        )
        source_harness = source / "scripts" / "screenshots.mjs"
        destination_scripts = destination / "scripts"
        destination_harness = destination_scripts / "screenshots.mjs"
        source_bytes = source_harness.read_bytes() if source_harness.is_file() else b""
        pinned_digest = harness["sha256"]
        if (
            source_harness.is_symlink()
            or hashlib.sha256(source_bytes).hexdigest() != pinned_digest
        ):
            return failed(f"{provider} source screenshot harness is not intact")
        scripts_existed = destination_scripts.exists()
        if destination_scripts.is_symlink() or (
            scripts_existed and not destination_scripts.is_dir()
        ):
            return failed(f"{provider} launch screenshot harness directory is incompatible")
        if not destination_harness.exists() and not destination_harness.is_symlink():
            attempted("screenshot-harness")
        if not scripts_existed:
            if error := claim_directory(destination_scripts, recursive=False):
                return error
        if destination_harness.is_symlink() or (
            destination_harness.exists() and not destination_harness.is_file()
        ):
            return failed(f"{provider} launch screenshot harness is occupied or incompatible")
        if not destination_harness.exists():
            try:
                with destination_harness.open("xb") as stream:
                    stream.write(source_bytes)
            except (FileExistsError, OSError) as exc:
                return failed(
                    f"{provider} launch screenshot harness creation failed: {exc}"
                )
            created.append((destination_harness, True))
            created_snapshots[destination_harness] = path_snapshot(destination_harness)
            return None
        try:
            with destination_harness.open("r+b") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                prior = stream.read()
                prior_digest = hashlib.sha256(prior).hexdigest()
                if prior_digest == pinned_digest:
                    return None
                if prior_digest not in frozenset(harness.get("known_old_sha256", ())):
                    if (
                        _is_packaged_project_source(source)
                        and _tracked_destination_harness(destination)
                    ):
                        return None
                    return failed(
                        f"{provider} launch screenshot harness is occupied or drifted"
                    )
                attempted("screenshot-harness")
                stream.seek(0)
                stream.truncate()
                stream.write(source_bytes)
                stream.flush()
                os.fsync(stream.fileno())
                installed_stat = os.fstat(stream.fileno())
                replaced_files.append((
                    destination_harness, prior,
                    (
                        installed_stat.st_dev, installed_stat.st_ino,
                        installed_stat.st_mode, pinned_digest,
                    ),
                ))
        except OSError as exc:
            return failed(f"{provider} launch screenshot harness refresh failed: {exc}")
        return None

    runtime_existed = False
    if materialize_runtime:
        runtime = manifest["playwright"]
        status, detail = playwright_runtime_status(
            source, version=runtime["version"], node_minimum=runtime["node_minimum"],
            manifest=manifest, provider=provider,
        )
        if status != "ok":
            return False, f"{provider} source Playwright runtime is not intact: {detail}"
        if error := materialize_harness():
            return error
        drive = next(
            (spec for spec in specs if spec["skill"] == "drive-local-webapp"), None
        )
        if drive is None:
            return failed(f"{provider} launch runtime requires drive-local-webapp")
        if destination_drive.is_symlink():
            return failed(f"{provider} launch runtime destination is symlinked")
        if destination_drive.exists() and skill_destination_status(
            destination_drive, drive["files"]
        ) != "ok":
            return failed(
                f"{provider} launch runtime destination skill is occupied or incompatible"
            )
        runtime_existed = destination_runtime.exists() or destination_runtime.is_symlink()
        if runtime_existed:
            status, detail = playwright_runtime_status(
                destination, version=runtime["version"], node_minimum=runtime["node_minimum"],
                manifest=manifest, provider=provider,
                allow_harness_drift=(
                    _is_packaged_project_source(source)
                    and _tracked_destination_harness(destination)
                ),
            )
            if status != "ok":
                return failed(
                    f"{provider} launch runtime destination is occupied or {status}: {detail}"
                )

    for spec, _source_skill, _target_skill in missing_skills:
        attempted(spec["id"])
    if materialize_runtime and not runtime_existed:
        attempted("playwright")
    if not provider_root_existed:
        if error := claim_directory(destination_provider_root, recursive=False):
            return error
    if not skills_existed:
        if error := claim_directory(destination_skills, recursive=False):
            return error
    if (
        destination_provider_root.is_symlink()
        or destination_skills.is_symlink()
        or not _contained(destination_provider_root, destination)
        or not _contained(destination_skills, destination_provider_root)
    ):
        return failed(f"{provider} launch skill root is symlinked")

    for spec, source_skill, target_skill in missing_skills:
        if error := claim_directory(target_skill):
            return error
        try:
            shutil.copytree(
                source_skill, target_skill,
                dirs_exist_ok=True,
                **({"ignore": shutil.ignore_patterns("node_modules")}
                   if spec["skill"] == "drive-local-webapp" else {}),
            )
        except (OSError, shutil.Error) as exc:
            return failed(f"{provider} skill copy failed: {exc}")
        created_snapshots[target_skill] = path_snapshot(target_skill)
    if materialize_runtime and not runtime_existed:
        if error := claim_directory(destination_runtime):
            return error
        try:
            shutil.copytree(
                source_runtime, destination_runtime, symlinks=True, dirs_exist_ok=True
            )
        except (OSError, shutil.Error) as exc:
            return failed(f"{provider} Playwright runtime copy failed: {exc}")
        created_snapshots[destination_runtime] = path_snapshot(destination_runtime)
        tree_status, detail = runtime_tree_status(destination_runtime)
        if tree_status != "ok":
            return failed(f"{provider} copied Playwright runtime is incompatible: {detail}")
    # The audit line reports only what was copied: readiness belongs to the preflight that
    # runs against the launch root afterward, which this function never observes.
    detail = f"materialized missing {provider} capabilities into the launch root"
    audit("materialized", f"copied missing {provider} capabilities into the launch root")
    return True, detail
