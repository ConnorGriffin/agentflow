from __future__ import annotations

from copy import deepcopy
import hashlib
from importlib.resources import files
from pathlib import Path
import shutil
from types import SimpleNamespace
import tomllib

from agentflow.capability_contracts import ContractRequirement, preflight
from agentflow.cli import main
from agentflow.enroll import doctor, enroll_repository
from agentflow.provider_skills import (
    NATIVE_DISCOVERY_MARKER, NATIVE_DISCOVERY_SKILL,
    materialize_launch_capabilities, provider_skill_status,
    native_discovery_output_is_proof, native_discovery_output_is_unavailable,
    native_discovery_prompt, record_native_discovery_receipt, skill_destination_status)


def test_native_discovery_prompts_are_provider_specific():
    assert native_discovery_prompt("claude") == (
        f"Invoke the project-local skill named {NATIVE_DISCOVERY_SKILL} using only native "
        "skill discovery. Do not use shell commands, search files, read files, or inspect "
        "configuration. If it is unavailable, reply exactly SKILL_UNAVAILABLE."
    )
    assert native_discovery_prompt("codex") == f"${NATIVE_DISCOVERY_SKILL}"


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
    assert native_discovery_output_is_unavailable("codex", "SKILL_UNAVAILABLE")
    assert not native_discovery_output_is_unavailable(
        "codex", f"SKILL_UNAVAILABLE\n{NATIVE_DISCOVERY_MARKER}"
    )
    assert not native_discovery_output_is_unavailable(
        "codex", 'SKILL_UNAVAILABLE\n{"type":"command_execution"}'
    )


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


def test_public_enrollment_installs_methodology_contracts_for_headless_dispatch(
    tmp_path, monkeypatch
):
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

    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{fixture_commit}\n", stderr="")
        if command[0] == "npx":
            name = command[command.index("--skill") + 1]
            target = repo / ".agents" / "skills" / name
            shutil.copytree(source / name, target)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(enrollment, "_run_command", run)

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

    shutil.rmtree(repo / ".claude" / "skills" / "tdd")
    regressed = doctor(str(repo), stage="build", provider="codex")
    assert regressed.ready is False
    assert any(
        cell.context == "headless" and not cell.ready
        for cell in regressed.stage_matrix
    )


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
