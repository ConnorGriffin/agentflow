from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from importlib.resources import files
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
import tomllib

import pytest

from agentflow.capability_contracts import ContractRequirement, preflight
from agentflow.cli import main
from agentflow.enroll import doctor, enroll_repository
from agentflow.provider_skills import (
    NATIVE_DISCOVERY_MARKER, NATIVE_DISCOVERY_SKILL,
    materialize_launch_capabilities, provider_skill_status,
    native_discovery_output_is_proof, native_discovery_output_is_unavailable,
    native_discovery_prompt, record_native_discovery_receipt, skill_destination_status)


def _ready_ui_launch_source(tmp_path):
    """Create an enrolled source with a minimal runnable selected Codex UI runtime."""
    checkout = Path(__file__).parents[1]
    source = tmp_path / "source"
    shutil.copytree(
        checkout / ".agents",
        source / ".agents",
        ignore=shutil.ignore_patterns("node_modules"),
    )
    (source / "scripts").mkdir()
    shutil.copy2(checkout / "scripts" / "screenshots.mjs", source / "scripts")
    runtime = source / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    (runtime / "playwright" / "lib").mkdir(parents=True)
    (runtime / "playwright" / "package.json").write_text('{"version":"1.61.1"}\n')
    (runtime / "playwright" / "lib" / "program.js").write_text(
        'module.exports = "1.61.1";\n'
    )
    (runtime / "playwright" / "cli.js").write_text(
        'console.log(require("./lib/program"));\n'
    )
    (runtime / ".bin").mkdir()
    (runtime / ".bin" / "playwright").symlink_to("../playwright/cli.js")
    return source


def _ui_runtime_requirement():
    return (ContractRequirement("playwright", "1.61.1", runtime=True),)


def test_native_discovery_prompts_are_provider_and_mode_specific():
    claude = (
        f"Invoke the project-local skill named {NATIVE_DISCOVERY_SKILL} using only native "
        "skill discovery. Do not use shell commands, search files, read files, or inspect "
        "configuration. If it is unavailable, reply exactly SKILL_UNAVAILABLE."
    )
    assert native_discovery_prompt("claude", "positive") == claude
    assert native_discovery_prompt("claude", "negative") == claude
    assert native_discovery_prompt("codex", "positive") == f"${NATIVE_DISCOVERY_SKILL}"
    negative = native_discovery_prompt("codex", "negative")
    assert f"exact project-local skill named {NATIVE_DISCOVERY_SKILL}" in negative
    assert "$" not in negative
    assert "Do not invoke any skill or use any tool" in negative


def test_claude_discovery_proof_keeps_native_skill_tool_predicate():
    skill_event = f'{{"name":"Skill","input":{{"skill":"{NATIVE_DISCOVERY_SKILL}"}}}}'

    assert native_discovery_output_is_proof(
        "claude", f"{skill_event}\n{NATIVE_DISCOVERY_MARKER}"
    )
    assert not native_discovery_output_is_proof("claude", NATIVE_DISCOVERY_MARKER)


def test_codex_discovery_proof_requires_marker_without_any_command_event():
    assert native_discovery_output_is_proof(
        "codex", f'{{"type":"item.completed"}}\n{NATIVE_DISCOVERY_MARKER}'
    )
    assert not native_discovery_output_is_proof(
        "codex",
        f'{{"type":"item.completed","item":{{"type":"command_execution"}}}}\n'
        f"{NATIVE_DISCOVERY_MARKER}",
    )
    assert not native_discovery_output_is_proof("codex", "SKILL_UNAVAILABLE")


def test_negative_discovery_predicate_rejects_native_or_command_evidence():
    terminal = (
        '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"SKILL_UNAVAILABLE"}}\n{"type":"turn.completed"}'
    )
    assert native_discovery_output_is_unavailable("codex", terminal)
    assert not native_discovery_output_is_unavailable(
        "codex", '{"type":"item.completed","item":{"type":"agent_message",'
        '"text":"SKILL_UNAVAILABLE"}}'
    )
    assert not native_discovery_output_is_unavailable(
        "codex", f"{terminal}\n{NATIVE_DISCOVERY_MARKER}"
    )
    assert not native_discovery_output_is_unavailable(
        "codex", f'{terminal}\n{{"type":"item.started",'
        '"item":{"type":"mcp_tool_call"}}'
    )
    assert not native_discovery_output_is_unavailable(
        "codex", f'{terminal}\n{{"type":"item.completed",'
        '"item":{"type":"unknown_provider_tool"}}'
    )
    assert native_discovery_output_is_unavailable("claude", "SKILL_UNAVAILABLE")


def test_recommended_native_discovery_repair_is_runnable_idempotent_and_releases_hold(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    spec = next(item for item in manifest["capabilities"] if item["id"] == "domain-modeling")
    (repo / ".agents" / "skills").mkdir(parents=True)
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        "agentflow.provider_skills._repository_key", lambda _root: ("repo-key", receipts)
    )
    monkeypatch.setattr(
        "agentflow.provider_skills._provider_fingerprint",
        lambda provider: (f"/providers/{provider}", f"{provider}-sha"),
    )
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/codex")
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status",
        lambda root, provider, _spec:
            __import__("agentflow.provider_skills", fromlist=["native_discovery_status"])
            .native_discovery_status(root, provider),
    )
    runs = []
    monkeypatch.setattr(
        "agentflow.provider_skills._run_native_discovery_probe",
        lambda _root, _provider: runs.append(True) or SimpleNamespace(
            returncode=0, stdout="native output " +
            "AGENTFLOW_582_DISCOVERED_4BAB5FF0_AEE6_4D44_BEA3_1BE5D089256F",
            stderr=""),
    )
    requirement = (ContractRequirement("domain-modeling", spec["version"]),)

    held = preflight(repo, "build", "codex", requirement)
    assert held.state == "missing"
    assert held.repair_command == f"agentflow capability-probe --repo {repo} --provider codex"
    assert main(["capability-probe", "--repo", str(repo), "--provider", "codex"]) == 0
    assert preflight(repo, "build", "codex", requirement).ready
    assert main(["capability-probe", "--repo", str(repo), "--provider", "codex"]) == 0
    assert runs == [True]


def _methodology_enrollment_fixture(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    source = tmp_path / "methodology-source" / "skills"
    names = ("tdd", "codebase-design", "domain-modeling")
    for name in names:
        skill = source / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"# {name}\n\nDeterministic enrollment fixture.\n")

    import agentflow.enroll as enrollment

    manifest = deepcopy(enrollment._manifest())
    fixture_commit = "a" * 40
    manifest["methodology_skills"].update(
        source=str(source.parent), commit=fixture_commit
    )
    for capability in manifest["capabilities"]:
        if capability.get("skill") in names:
            content = (source / capability["skill"] / "SKILL.md").read_bytes()
            capability["files"] = [
                {"path": "SKILL.md", "sha256": hashlib.sha256(content).hexdigest()}
            ]

    config = tmp_path / "config.toml"
    config.write_text(f'[[repositories]]\nrepo = "owner/repo"\nworkdir = "{repo}"\n')
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    monkeypatch.setattr(enrollment, "_manifest", lambda: manifest)
    monkeypatch.setattr(enrollment, "_checkout_problem", lambda _root: None)
    monkeypatch.setattr(
        enrollment.shutil, "which", lambda command: f"/usr/bin/{command}"
    )

    lock = repo / "skills-lock.json"
    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{fixture_commit}\n", stderr="")
        if command[0] == "npx":
            name = command[command.index("--skill") + 1]
            target = repo / ".agents" / "skills" / name
            shutil.copytree(source / name, target)
            document = json.loads(lock.read_text()) if lock.exists() else {
                "version": 1, "skills": {}
            }
            document["skills"][name] = {
                "source": str(tmp_path / "deleted-temp-clone"),
                "sourceType": "local",
                "computedHash": "hash",
                "skillPath": "SKILL.md",
            }
            lock.write_text(json.dumps(document) + "\n")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(enrollment, "_run_command", run)
    return enrollment, repo, source, names, fixture_commit, lock, commands


def test_public_enrollment_installs_methodology_contracts_for_headless_dispatch(
    tmp_path, monkeypatch
):
    enrollment, repo, source, names, fixture_commit, lock, commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )

    report = enroll_repository(str(repo), apply=True)

    methodology = {
        item.id: item for item in report.capabilities if item.id in names
    }
    assert all(item.available for item in methodology.values())
    clone = next(
        command
        for command in commands
        if command[:3] == ["git", "clone", "--no-checkout"]
    )
    assert clone[3] == str(source.parent)
    installs = [command for command in commands if command[0] == "npx"]
    assert [command[command.index("--skill") + 1] for command in installs] == list(names)
    assert all("claude-code" not in command for command in installs)
    for location in (".agents/skills", ".claude/skills"):
        for name in names:
            assert (repo / location / name / "SKILL.md").is_file()
    lock_entries = json.loads(lock.read_text())["skills"]
    assert all(entry["source"] == str(source.parent) for entry in lock_entries.values())
    assert all(entry["sourceType"] == "git" for entry in lock_entries.values())
    assert all(entry["ref"] == fixture_commit for entry in lock_entries.values())
    assert "deleted-temp-clone" not in lock.read_text()

    shutil.rmtree(repo / ".claude" / "skills" / "tdd")
    regressed = doctor(str(repo), stage="build", provider="codex")
    assert regressed.ready is False
    assert any(
        cell.context == "headless" and not cell.ready
        for cell in regressed.stage_matrix
    )


def test_enrollment_materializes_missing_claude_methodology_destinations(
    tmp_path, monkeypatch
):
    enrollment, repo, source, names, _commit, _lock, commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    for name in names:
        shutil.copytree(source / name, repo / ".agents" / "skills" / name)

    report = enrollment.enroll_repository(str(repo), apply=True)

    assert all(
        item.available for item in report.capabilities if item.id in names
    )
    assert not commands
    for name in names:
        destination = repo / ".claude" / "skills" / name
        assert destination.is_dir()
        assert not destination.is_symlink()
        assert (destination / "SKILL.md").read_bytes() == (
            source / name / "SKILL.md"
        ).read_bytes()


@pytest.mark.parametrize("destination_kind", ("drifted", "symlinked"))
def test_enrollment_refuses_occupied_claude_methodology_destinations(
    tmp_path, monkeypatch, destination_kind
):
    enrollment, repo, source, names, _commit, _lock, commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    for name in names:
        shutil.copytree(source / name, repo / ".agents" / "skills" / name)
    destination = repo / ".claude" / "skills" / names[0]
    destination.parent.mkdir(parents=True)
    if destination_kind == "drifted":
        destination.mkdir()
        (destination / "SKILL.md").write_text("operator edit\n")
    else:
        destination.symlink_to(Path("../../.agents/skills") / names[0])

    report = enrollment.enroll_repository(str(repo), apply=True)

    assert report.ready is False
    assert not commands
    assert not (repo / "AGENTS.md").exists()
    assert not (repo / "CLAUDE.md").exists()
    if destination_kind == "drifted":
        assert (destination / "SKILL.md").read_text() == "operator edit\n"
    else:
        assert destination.is_symlink()


def test_enrollment_rolls_back_partial_claude_methodology_materialization(
    tmp_path, monkeypatch
):
    enrollment, repo, source, names, _commit, _lock, _commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    for name in names:
        shutil.copytree(source / name, repo / ".agents" / "skills" / name)
    original_wire = enrollment._wire_claude_skill
    materialized = 0

    def fail_after_second_methodology_skill(root, name):
        nonlocal materialized
        outcome = original_wire(root, name)
        if name in names:
            materialized += 1
            if materialized == 2:
                return "WARN: injected methodology materialization failure"
        return outcome

    monkeypatch.setattr(enrollment, "_wire_claude_skill", fail_after_second_methodology_skill)

    report = enrollment.enroll_repository(str(repo), apply=True)

    assert report.ready is False
    assert materialized == 3
    assert not (repo / ".claude").exists()
    assert not (repo / "AGENTS.md").exists()
    assert not (repo / "CLAUDE.md").exists()


def test_enrollment_warns_and_rolls_back_for_a_non_object_lock(
    tmp_path, monkeypatch
):
    enrollment, repo, _source, _names, _commit, lock, _commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    lock.write_text("[]\n")

    report = enrollment.enroll_repository(str(repo), apply=True)

    assert report.ready is False
    assert lock.read_text() == "[]\n"


def test_enrollment_warns_and_rolls_back_when_lock_write_fails(
    tmp_path, monkeypatch
):
    enrollment, repo, _source, names, _commit, lock, _commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    stale = {
        "version": 1,
        "skills": {
            names[0]: {
                "source": str(tmp_path / "deleted-temp-clone"),
                "sourceType": "local",
                "computedHash": "hash",
                "skillPath": "SKILL.md",
            }
        },
    }
    lock.write_text(json.dumps(stale) + "\n")
    original_write_text = Path.write_text

    def fail_lock_write(path, *args, **kwargs):
        if path == lock:
            raise OSError("read-only lock")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_lock_write)

    report = enrollment.enroll_repository(str(repo), apply=True)

    assert report.ready is False
    assert json.loads(lock.read_text()) == stale


def test_enrollment_normalizes_installer_lock_to_manifest_git_revision(tmp_path):
    import agentflow.enroll as enrollment

    repo = tmp_path / "repo"
    repo.mkdir()
    lock = repo / "skills-lock.json"
    lock.write_text(json.dumps({
        "version": 1,
        "skills": {
            "tdd": {
                "source": str(tmp_path / "deleted-temp-clone"),
                "sourceType": "local",
                "computedHash": "hash",
                "skillPath": "SKILL.md",
            },
            "unrelated": {"source": "keep", "sourceType": "git"},
        },
    }) + "\n")

    before = lock.read_bytes()
    enrollment._normalize_skill_lock(
        repo,
        ["tdd"],
        source="https://github.com/ConnorGriffin/skills",
        commit="a" * 40,
    )
    first = lock.read_bytes()
    enrollment._normalize_skill_lock(
        repo,
        ["tdd"],
        source="https://github.com/ConnorGriffin/skills",
        commit="a" * 40,
    )

    assert first != before
    assert lock.read_bytes() == first
    entry = json.loads(first)["skills"]["tdd"]
    assert entry["source"] == "https://github.com/ConnorGriffin/skills"
    assert entry["sourceType"] == "git"
    assert entry["ref"] == "a" * 40
    assert entry["computedHash"] == "hash"
    assert entry["skillPath"] == "SKILL.md"
    assert "deleted-temp-clone" not in lock.read_text()


def test_repeated_enrollment_migrates_existing_public_and_methodology_locks(
    tmp_path, monkeypatch
):
    import agentflow.enroll as enrollment

    repo = tmp_path / "repo"
    repo.mkdir()
    manifest = deepcopy(enrollment._manifest())
    public_names = tuple(manifest["connor_skills"]["skills"])
    methodology_names = tuple(manifest["methodology_skills"]["skills"])
    all_names = public_names + methodology_names
    for name in all_names:
        content = f"# {name}\n"
        for location in (".agents/skills", ".claude/skills"):
            target = repo / location / name
            target.mkdir(parents=True)
            (target / "SKILL.md").write_text(content)
        capability = next(
            item for item in manifest["capabilities"] if item.get("skill") == name
        )
        capability["files"] = [{
            "path": "SKILL.md",
            "sha256": hashlib.sha256(content.encode()).hexdigest(),
        }]

    public_commit = "b" * 40
    methodology_commit = "c" * 40
    manifest["connor_skills"].update(
        source="https://github.com/ConnorGriffin/skills",
        commit=public_commit,
    )
    manifest["methodology_skills"].update(
        source="https://github.com/ConnorGriffin/skills",
        commit=methodology_commit,
    )
    stale_source = str(tmp_path / "deleted-temp-clone")
    lock = repo / "skills-lock.json"
    lock.write_text(json.dumps({
        "version": 1,
        "skills": {
            name: {
                "source": stale_source,
                "sourceType": "local",
                "computedHash": f"hash-{name}",
                "skillPath": "SKILL.md",
            }
            for name in all_names
        },
    }) + "\n")
    monkeypatch.setattr(enrollment, "_manifest", lambda: manifest)

    assert enrollment._install_connor_skills(repo).startswith("ok:")
    assert enrollment._install_methodology_skills(repo).startswith("ok:")
    migrated = lock.read_bytes()
    assert enrollment._install_connor_skills(repo).startswith("ok:")
    assert enrollment._install_methodology_skills(repo).startswith("ok:")
    assert lock.read_bytes() == migrated

    entries = json.loads(migrated)["skills"]
    for name in public_names:
        assert entries[name]["source"] == manifest["connor_skills"]["source"]
        assert entries[name]["ref"] == public_commit
    for name in methodology_names:
        assert entries[name]["source"] == manifest["methodology_skills"]["source"]
        assert entries[name]["ref"] == methodology_commit
    assert all(entries[name]["sourceType"] == "git" for name in all_names)
    assert stale_source not in lock.read_text()


def test_provider_discovery_requires_provider_specific_native_receipts(tmp_path, monkeypatch):
    content = b"# method\n"
    spec = {
        "skill": "method", "files": [
            {"path": "SKILL.md", "sha256": hashlib.sha256(content).hexdigest()}
        ],
    }
    agent = tmp_path / ".agents" / "skills" / "method"
    agent.mkdir(parents=True)
    (agent / "SKILL.md").write_bytes(content)
    # An ambient/global-looking copy is deliberately irrelevant.
    ambient = tmp_path / "global" / "skills" / "method"
    ambient.mkdir(parents=True)
    (ambient / "SKILL.md").write_bytes(content)

    receipt_dir = tmp_path / "receipts"
    monkeypatch.setattr(
        "agentflow.provider_skills._repository_key",
        lambda _root: ("repo-key", receipt_dir),
    )
    monkeypatch.setattr(
        "agentflow.provider_skills._provider_fingerprint",
        lambda provider: (f"/providers/{provider}", f"{provider}-sha"),
    )

    assert provider_skill_status(tmp_path, "codex", spec)[0] == "missing"
    assert provider_skill_status(tmp_path, "claude", spec)[0] == "missing"
    record_native_discovery_receipt(tmp_path, "codex")
    assert provider_skill_status(tmp_path, "codex", spec)[0] == "ok"

    discovery = tmp_path / ".claude" / "skills" / "method"
    shutil.copytree(agent, discovery)
    assert provider_skill_status(tmp_path, "claude", spec)[0] == "missing"
    record_native_discovery_receipt(tmp_path, "claude")
    assert provider_skill_status(tmp_path, "claude", spec)[0] == "ok"

    monkeypatch.setattr(
        "agentflow.provider_skills._provider_fingerprint",
        lambda provider: (f"/providers/{provider}", f"changed-{provider}-sha"),
    )
    assert provider_skill_status(tmp_path, "codex", spec)[0] == "drifted"
    assert provider_skill_status(tmp_path, "claude", spec)[0] == "drifted"

    shutil.rmtree(discovery)
    discovery.symlink_to(Path("../../.agents/skills/method"))
    assert provider_skill_status(tmp_path, "claude", spec)[0] == "incompatible"


def test_skill_integrity_rejects_symlink_roots_dirs_files_and_manifest_escapes(tmp_path):
    content = b"# method\n"
    digest = hashlib.sha256(content).hexdigest()
    spec = [{"path": "SKILL.md", "sha256": digest}]
    skill = tmp_path / ".agents" / "skills" / "method"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(content)
    assert skill_destination_status(skill, spec) == "ok"

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_bytes(content)
    (skill / "SKILL.md").unlink()
    (skill / "SKILL.md").symlink_to(outside / "SKILL.md")
    assert skill_destination_status(skill, spec) == "incompatible"

    (skill / "SKILL.md").unlink()
    (skill / "SKILL.md").write_bytes(content)
    escaped = [{"path": "../outside/SKILL.md", "sha256": digest}]
    assert skill_destination_status(skill, escaped) == "incompatible"

    shutil.rmtree(skill)
    skill.symlink_to(outside, target_is_directory=True)
    assert skill_destination_status(skill, spec) == "incompatible"

    skill.unlink()
    shutil.rmtree(tmp_path / ".agents" / "skills")
    (tmp_path / ".agents" / "skills").symlink_to(outside, target_is_directory=True)
    escaped_skill = tmp_path / ".agents" / "skills" / "method"
    assert skill_destination_status(escaped_skill, spec) == "incompatible"


def test_launch_materialization_rejects_provider_root_escape_before_writing(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    outside = tmp_path / "outside"
    (source / ".agents" / "skills").mkdir(parents=True)
    destination.mkdir()
    outside.mkdir()
    (destination / ".agents").symlink_to(outside, target_is_directory=True)

    ready, detail = materialize_launch_capabilities(source, destination, "codex")

    assert ready is False and "root" in detail
    assert not (outside / "skills").exists()


def test_launch_materialization_copies_verified_ui_runtime_into_fresh_root(tmp_path, monkeypatch):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "scripts").mkdir()
    shutil.copy2(source / "scripts" / "screenshots.mjs", destination / "scripts")
    monkeypatch.setattr(
        "agentflow.provider_skills.native_discovery_status", lambda *_args: ("ok", "proven")
    )
    executable = shutil.which
    monkeypatch.setattr(
        "agentflow.capability_contracts.shutil.which",
        lambda name: "/bin/codex" if name == "codex" else executable(name),
    )
    requirements = _ui_runtime_requirement()

    assert preflight(source, "build", "codex", requirements).ready
    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    runtime = destination / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    assert ready is True and "materialized" in detail
    assert preflight(destination, "build", "codex", requirements).ready
    assert (runtime / "playwright" / "package.json").read_text() == '{"version":"1.61.1"}\n'
    assert not (runtime / "playwright" / "package.json").samefile(
        source / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
        / "playwright" / "package.json"
    )
    launcher = runtime / ".bin" / "playwright"
    assert launcher.is_symlink()
    assert launcher.readlink() == Path("../playwright/cli.js")
    assert launcher.resolve().is_relative_to(runtime)
    result = subprocess.run(
        ["node", str(launcher)], capture_output=True, check=False, text=True,
    )
    assert result.returncode == 0
    assert result.stdout == "1.61.1\n"
    assert materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )[0] is True


def test_launch_materialization_refuses_occupied_drifted_ui_runtime(tmp_path, monkeypatch):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    shutil.copytree(source / ".agents", destination / ".agents")
    (destination / "scripts").mkdir()
    shutil.copy2(source / "scripts" / "screenshots.mjs", destination / "scripts")
    runtime = destination / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    (runtime / "playwright" / "package.json").write_text('{"version":"0.0.0"}\n')
    monkeypatch.setattr(
        "agentflow.provider_skills.native_discovery_status", lambda *_args: ("ok", "proven")
    )

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False and "runtime destination" in detail
    assert (runtime / "playwright" / "package.json").read_text() == '{"version":"0.0.0"}\n'


def test_launch_materialization_refuses_symlinked_destination_runtime_before_skills(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    destination_runtime = (
        destination / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    )
    destination_runtime.parent.mkdir(parents=True)
    shutil.copytree(
        source / ".agents" / "skills" / "drive-local-webapp",
        destination_runtime.parent,
        ignore=shutil.ignore_patterns("node_modules"), dirs_exist_ok=True,
    )
    external_runtime = tmp_path / "external-runtime"
    shutil.copytree(
        source / ".agents" / "skills" / "drive-local-webapp" / "node_modules",
        external_runtime, symlinks=True,
    )
    destination_runtime.symlink_to(external_runtime, target_is_directory=True)
    (destination / "scripts").mkdir()
    shutil.copy2(source / "scripts" / "screenshots.mjs", destination / "scripts")

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False and "runtime destination" in detail
    assert destination_runtime.is_symlink()
    assert not (destination / ".agents" / "skills" / "tdd").exists()


@pytest.mark.parametrize("launcher_target", ("external", "broken"))
def test_launch_materialization_rejects_unsafe_source_runtime_before_writing(
    tmp_path, launcher_target
):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    outside = tmp_path / "outside.js"
    destination.mkdir()
    (destination / "scripts").mkdir()
    shutil.copy2(source / "scripts" / "screenshots.mjs", destination / "scripts")
    launcher = source / ".agents" / "skills" / "drive-local-webapp" / "node_modules" / ".bin" / "playwright"
    launcher.unlink()
    if launcher_target == "external":
        outside.write_text("outside\n")
        launcher.symlink_to(outside)
    else:
        launcher.symlink_to("../playwright/missing.js")

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False and "source Playwright runtime" in detail
    assert not (destination / ".agents").exists()


def test_launch_materialization_rolls_back_skills_after_copy_failure(tmp_path, monkeypatch):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    original_copytree = shutil.copytree
    copies = 0

    def fail_after_first_skill(source_path, destination_path, *args, **kwargs):
        nonlocal copies
        if Path(source_path).parent.name == "skills":
            copies += 1
            if copies == 2:
                raise OSError("injected skill copy failure")
        return original_copytree(source_path, destination_path, *args, **kwargs)

    monkeypatch.setattr("agentflow.provider_skills.shutil.copytree", fail_after_first_skill)

    ready, detail = materialize_launch_capabilities(source, destination, "codex")

    assert ready is False and "copy failed" in detail
    assert copies == 2
    assert not (destination / ".agents").exists()


def test_launch_materialization_preserves_concurrent_skill_destination(tmp_path, monkeypatch):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    raced = destination / ".agents" / "skills" / "tdd"
    sentinel = raced / "sentinel"
    original_mkdir = Path.mkdir

    def inject_concurrent_destination(path, *args, **kwargs):
        if path == raced:
            original_mkdir(path)
            sentinel.write_text("concurrent owner\n")
            raise FileExistsError("injected concurrent destination")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", inject_concurrent_destination)

    ready, detail = materialize_launch_capabilities(source, destination, "codex")

    assert ready is False and "appeared concurrently" in detail
    assert sentinel.read_text() == "concurrent owner\n"


@pytest.mark.parametrize("cleanup_denied", (False, True))
def test_launch_materialization_reports_failed_mkdir_rollback(
    tmp_path, monkeypatch, cleanup_denied
):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    provider_root = destination / ".agents"
    skills = provider_root / "skills"
    original_mkdir = Path.mkdir
    original_rmdir = Path.rmdir

    def fail_skill_root_creation(path, *args, **kwargs):
        if path == skills:
            raise OSError("injected skill root creation failure")
        return original_mkdir(path, *args, **kwargs)

    def deny_cleanup(path):
        if cleanup_denied and path == provider_root:
            raise OSError("injected cleanup denial")
        return original_rmdir(path)

    monkeypatch.setattr(Path, "mkdir", fail_skill_root_creation)
    monkeypatch.setattr(Path, "rmdir", deny_cleanup)

    ready, detail = materialize_launch_capabilities(source, destination, "codex")

    assert ready is False and "creation failed" in detail
    assert ("rollback failed" in detail) is cleanup_denied
    assert provider_root.exists() is cleanup_denied
