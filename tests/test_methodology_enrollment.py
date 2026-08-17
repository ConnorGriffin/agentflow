from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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
from agentflow.coordinator.record import Record
from agentflow.enroll import doctor, enroll_repository, repair_capability_refusal
from agentflow.prompts import requirements_for
from agentflow.provider_skills import (
    NATIVE_DISCOVERY_MARKER, NATIVE_DISCOVERY_SKILL,
    materialize_launch_capabilities, provider_skill_status,
    native_discovery_output_is_proof, native_discovery_output_is_unavailable,
    native_discovery_prompt, prove_native_discovery, record_native_discovery_receipt,
    skill_destination_status)


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


_OWNER_REPOSITORY_ORIGIN = "git@github.com:ConnorGriffin/agentflow.git"


def _launch_repository(root: Path, origin: str | None = None) -> None:
    root.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if origin is not None:
        subprocess.run(
            ["git", "-C", str(root), "remote", "add", "origin", origin], check=True
        )


def _track_harness(root: Path) -> None:
    subprocess.run(["git", "-C", str(root), "add", "scripts/screenshots.mjs"], check=True)
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.name=Test", "-c",
            "user.email=test@example.com", "commit", "-qm", "fixture harness",
        ],
        check=True,
    )


def _ui_runtime_requirement():
    return (ContractRequirement("playwright", "1.61.1", runtime=True),)


def _ui_skill_requirements():
    """The real UI requirement chain: ui-craft → drive-local-webapp → pinned runtime."""
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    specs = {item["id"]: item for item in manifest["capabilities"]}
    return (
        ContractRequirement("ui-craft", specs["ui-craft"]["version"]),
        ContractRequirement("drive-local-webapp", specs["drive-local-webapp"]["version"]),
        ContractRequirement("playwright", manifest["playwright"]["version"], runtime=True),
    )


def _stub_discovery_receipts(tmp_path, monkeypatch, *, probe_returncode=0):
    """Point receipts at a private directory and script the provider probe deterministically."""
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        "agentflow.provider_skills._repository_key", lambda _root: ("repo-key", receipts)
    )
    monkeypatch.setattr(
        "agentflow.provider_skills._provider_fingerprint",
        lambda provider: (f"/providers/{provider}", f"{provider}-sha"),
    )
    probes = []

    def probe(_root, provider):
        probes.append(provider)
        skill_event = f'{{"name":"Skill","input":{{"skill":"{NATIVE_DISCOVERY_SKILL}"}}}}\n'
        proof = (skill_event if provider == "claude" else "") + NATIVE_DISCOVERY_MARKER
        return SimpleNamespace(returncode=probe_returncode, stdout=proof, stderr="")

    monkeypatch.setattr("agentflow.provider_skills._run_native_discovery_probe", probe)
    return receipts, probes


def _stub_runnable_node(monkeypatch):
    """Answer the runtime contract's Node checks so a runtime verdict reflects the tree,
    not whether this machine has Node on PATH."""
    executable = shutil.which
    monkeypatch.setattr(
        "agentflow.capability_contracts.shutil.which",
        lambda name: "/bin/node" if name == "node" else executable(name),
    )
    monkeypatch.setattr(
        "agentflow.runtime_contracts._run_command",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="v22.0.0\n"),
    )


def _stale_receipt(root, provider):
    receipt = record_native_discovery_receipt(root, provider)
    payload = json.loads(receipt.read_text())
    payload["manifest_sha256"] = "stale"
    receipt.write_text(json.dumps(payload))
    return receipt


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


@pytest.mark.parametrize(
    "ending",
    ("success", "provider-refusal", "timeout", "interruption", "exception"),
)
def test_provider_discovery_probe_is_disposable_on_every_ending(
    tmp_path, monkeypatch, ending
):
    repo = tmp_path / "repo"
    (repo / ".agents" / "skills").mkdir(parents=True)
    probe_roots = []

    monkeypatch.setattr(
        "agentflow.provider_skills.native_discovery_status",
        lambda *_args: ("missing", "receipt missing"),
    )
    monkeypatch.setattr(
        "agentflow.provider_skills.record_native_discovery_receipt",
        lambda *_args: tmp_path / "receipt.json",
    )

    def run(root, _provider):
        probe_roots.append(root)
        if ending == "success":
            return SimpleNamespace(returncode=0, stdout=NATIVE_DISCOVERY_MARKER, stderr="")
        if ending == "provider-refusal":
            return SimpleNamespace(returncode=1, stdout="SKILL_UNAVAILABLE", stderr="")
        if ending == "timeout":
            raise subprocess.TimeoutExpired(["codex"], 300)
        if ending == "interruption":
            raise KeyboardInterrupt
        raise RuntimeError("provider failed")

    monkeypatch.setattr("agentflow.provider_skills._run_native_discovery_probe", run)

    if ending in {"timeout", "exception"}:
        with pytest.raises((subprocess.TimeoutExpired, RuntimeError)):
            prove_native_discovery(repo, "codex")
    elif ending == "interruption":
        with pytest.raises(KeyboardInterrupt):
            prove_native_discovery(repo, "codex")
    else:
        ready, _detail = prove_native_discovery(repo, "codex")
        assert ready is (ending == "success")

    assert len(probe_roots) == 1
    assert probe_roots[0] != repo
    assert not probe_roots[0].exists()
    for provider_root in (repo / ".agents" / "skills", repo / ".claude" / "skills"):
        assert not (provider_root / NATIVE_DISCOVERY_SKILL).exists()


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
    assert all("check-ignore" in command for command in commands)
    for name in names:
        destination = repo / ".claude" / "skills" / name
        assert destination.is_dir()
        assert not destination.is_symlink()
        assert (destination / "SKILL.md").read_bytes() == (
            source / name / "SKILL.md"
        ).read_bytes()


def test_enrollment_refuses_an_ignored_required_capability_file(
    tmp_path, monkeypatch, capsys
):
    import agentflow.enroll as enrollment

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / ".gitignore").write_text("lib/\n")
    (repo / "AGENTS.md").write_text("# repo\n\nprofile: reviewed\nui-surfaces: frontend/\n")
    (repo / "frontend").mkdir()
    monkeypatch.setattr(enrollment, "_checkout_problem", lambda _root: None)
    monkeypatch.setattr(enrollment, "_config_problem", lambda: None)
    monkeypatch.setattr(enrollment, "_tooling_problem", lambda _surfaces: None)
    monkeypatch.setattr(enrollment, "_managed_files_problem", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(enrollment, "_skills_problem", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(enrollment, "_methodology_problem", lambda _root: None)
    monkeypatch.setattr(
        enrollment, "_apply_enrollment",
        lambda *_args, **_kwargs: pytest.fail("ignored capability file must refuse before apply"),
    )

    report = enrollment.enroll_repository(str(repo), apply=True)

    assert report.ready is False
    output = capsys.readouterr().out
    assert ".agents/skills/ui-craft/scripts/lib/design-parser.mjs" in output
    assert "lib/" in output
    assert not (repo / "CLAUDE.md").exists()


def test_enrollment_with_no_ignored_capability_file_keeps_materializing(
    tmp_path, monkeypatch
):
    enrollment, repo, source, names, _commit, _lock, _commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    original_run = enrollment._run_command

    def run(command, **kwargs):
        if command[:4] == ["git", "-C", str(repo), "check-ignore"]:
            return subprocess.run(command, capture_output=True, text=True)
        return original_run(command, **kwargs)

    monkeypatch.setattr(enrollment, "_run_command", run)

    report = enrollment.enroll_repository(str(repo), apply=True)

    assert report.ready is False  # no local Claude or Codex runner in this focused fixture
    for name in names:
        assert (repo / ".agents" / "skills" / name / "SKILL.md").is_file()
        assert (repo / ".claude" / "skills" / name / "SKILL.md").is_file()


def test_capability_repair_materializes_only_absent_claude_destinations(
    tmp_path, monkeypatch
):
    enrollment, repo, source, names, commit, _lock, commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    for name in names:
        shutil.copytree(source / name, repo / ".agents" / "skills" / name)
    requirements = tuple(ContractRequirement(name, commit) for name in names)

    result = repair_capability_refusal(repo, "claude", requirements)

    assert result is not None and result.repaired
    assert "Claude" in result.detail
    assert not commands
    for name in names:
        destination = repo / ".claude" / "skills" / name
        assert destination.is_dir() and not destination.is_symlink()
        assert (destination / "SKILL.md").read_bytes() == (
            source / name / "SKILL.md"
        ).read_bytes()


def test_capability_repair_installs_an_absent_pinned_runtime(tmp_path, monkeypatch):
    import agentflow.enroll as enrollment

    root = _ready_ui_launch_source(tmp_path)
    # Repair now proves discovery after the install, so the receipt seams are scripted.
    _receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch)
    runtime = root / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    shutil.rmtree(runtime)
    installs = []

    def install(selected_root, *, provider=None):
        installs.append((selected_root, provider))
        (runtime / "playwright").mkdir(parents=True)
        (runtime / "playwright" / "package.json").write_text(
            '{"version":"1.61.1"}\n'
        )
        return "DO:   installed pinned Playwright and Chromium; self-check passed"

    monkeypatch.setattr(enrollment, "_install_ui_runtime", install)

    result = repair_capability_refusal(
        root, "codex", _ui_runtime_requirement()
    )

    assert result is not None and result.repaired
    assert "Playwright" in result.detail
    assert installs == [(root, "codex")]
    assert (runtime / "playwright" / "package.json").is_file()


def test_capability_repair_materializes_claude_ui_skills_before_runtime(
    tmp_path, monkeypatch
):
    import agentflow.enroll as enrollment

    root = _ready_ui_launch_source(tmp_path)
    # Repair now proves discovery after the materialization, so the receipt seams are scripted.
    _receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch)
    requirements = _ui_skill_requirements()
    installs = []

    def install(selected_root, *, provider=None):
        installs.append((selected_root, provider))
        for name in ("ui-craft", "drive-local-webapp"):
            assert (root / ".claude" / "skills" / name).is_dir()
        runtime = root / ".claude" / "skills" / "drive-local-webapp" / "node_modules"
        assert (runtime / "playwright" / "package.json").is_file()
        return "ok:   pinned Playwright, Chromium, and screenshot harness are ready"

    monkeypatch.setattr(enrollment, "_install_ui_runtime", install)

    result = repair_capability_refusal(root, "claude", requirements)

    assert result is not None and result.repaired
    assert installs == [(root, "claude")]
    assert "Claude" in result.detail and "Playwright" in result.detail


def test_failed_runtime_repair_preserves_concurrent_operator_content(
    tmp_path, monkeypatch
):
    import agentflow.enroll as enrollment

    root = _ready_ui_launch_source(tmp_path)
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    specs = {item["id"]: item for item in manifest["capabilities"]}
    requirements = (
        ContractRequirement("ui-craft", specs["ui-craft"]["version"]),
        ContractRequirement("drive-local-webapp", specs["drive-local-webapp"]["version"]),
        ContractRequirement("playwright", manifest["playwright"]["version"], runtime=True),
    )
    operator_note = root / ".agents" / "skills" / "tdd" / "operator-note.txt"

    def fail_install(_selected_root, *, provider=None):
        assert provider == "claude"
        operator_note.write_text("authored while npm was running\n")
        claude_skill = root / ".claude" / "skills" / "ui-craft" / "SKILL.md"
        claude_skill.write_text("operator changed this during repair\n")
        return "WARN: UI runtime setup failed — npm exited 1"

    monkeypatch.setattr(enrollment, "_install_ui_runtime", fail_install)

    result = repair_capability_refusal(root, "claude", requirements)

    changed = root / ".claude" / "skills" / "ui-craft" / "SKILL.md"
    assert result is not None and not result.repaired
    assert operator_note.read_text() == "authored while npm was running\n"
    assert changed.read_text() == "operator changed this during repair\n"


def test_failed_runtime_repair_removes_ownership_markers_with_created_skills(
    tmp_path, monkeypatch
):
    import agentflow.enroll as enrollment

    root = _ready_ui_launch_source(tmp_path)
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    specs = {item["id"]: item for item in manifest["capabilities"]}
    requirements = (
        ContractRequirement("ui-craft", specs["ui-craft"]["version"]),
        ContractRequirement("drive-local-webapp", specs["drive-local-webapp"]["version"]),
        ContractRequirement("playwright", manifest["playwright"]["version"], runtime=True),
    )
    monkeypatch.setattr(
        enrollment, "_install_ui_runtime",
        lambda *_args, **_kwargs: "WARN: UI runtime setup failed",
    )

    result = repair_capability_refusal(root, "claude", requirements)

    assert result is not None and not result.repaired
    assert not (root / ".claude" / "skills" / "ui-craft").exists()
    assert not (root / ".agentflow" / "skill-ownership" / "claude" / "ui-craft.json").exists()


@pytest.mark.parametrize("destination_kind", ("drifted", "symlinked"))
def test_capability_repair_preserves_occupied_claude_destinations(
    tmp_path, monkeypatch, destination_kind
):
    _enrollment, repo, source, names, commit, _lock, commands = (
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
    requirements = tuple(ContractRequirement(name, commit) for name in names)
    probes = []
    monkeypatch.setattr(
        "agentflow.provider_skills.prove_native_discovery",
        lambda *_args: probes.append(True),
    )

    result = repair_capability_refusal(repo, "claude", requirements)

    assert result is None
    assert not probes
    assert not commands
    assert not (repo / ".claude" / "skills" / names[1]).exists()
    if destination_kind == "drifted":
        assert (destination / "SKILL.md").read_text() == "operator edit\n"
    else:
        assert destination.is_symlink()


def test_capability_repair_preserves_an_unknown_harness(tmp_path, monkeypatch):
    root = _ready_ui_launch_source(tmp_path)
    harness = root / "scripts" / "screenshots.mjs"
    harness.write_text("operator-authored harness\n")
    probes = []
    monkeypatch.setattr(
        "agentflow.provider_skills.prove_native_discovery",
        lambda *_args: probes.append(True),
    )

    result = repair_capability_refusal(root, "codex", _ui_runtime_requirement())

    assert result is None
    assert not probes
    assert harness.read_text() == "operator-authored harness\n"


def test_capability_repair_refuses_an_unreadable_native_discovery_receipt(
    tmp_path, monkeypatch
):
    root = _ready_ui_launch_source(tmp_path)
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    spec = next(item for item in manifest["capabilities"] if item["id"] == "domain-modeling")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        "agentflow.provider_skills._repository_key", lambda _root: ("repo-key", receipts)
    )
    monkeypatch.setattr(
        "agentflow.provider_skills._provider_fingerprint",
        lambda provider: (f"/providers/{provider}", f"{provider}-sha"),
    )
    receipt = record_native_discovery_receipt(root, "codex")
    receipt.write_text("operator-authored receipt\n")
    probes = []
    monkeypatch.setattr(
        "agentflow.provider_skills.prove_native_discovery",
        lambda *_args: probes.append(True) or (True, "should not run"),
    )

    result = repair_capability_refusal(
        root, "codex", (ContractRequirement("domain-modeling", spec["version"]),)
    )

    assert result is None
    assert not probes
    assert receipt.read_text() == "operator-authored receipt\n"


def _runtime_intact_claude_root(tmp_path):
    """A Claude UI root whose drive skill and runtime are installed and fully intact."""
    root = _ready_ui_launch_source(tmp_path)
    destination = root / ".claude" / "skills" / "drive-local-webapp"
    shutil.copytree(
        root / ".agents" / "skills" / "drive-local-webapp", destination, symlinks=True
    )
    return root


@pytest.mark.parametrize("receipt_state", ("valid", "stale"))
def test_capability_repair_materializes_a_destination_the_intact_runtime_used_to_swallow(
    tmp_path, monkeypatch, receipt_state
):
    import agentflow.enroll as enrollment

    root = _runtime_intact_claude_root(tmp_path)
    _receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch)
    _stub_runnable_node(monkeypatch)
    if receipt_state == "valid":
        record_native_discovery_receipt(root, "claude")
    else:
        _stale_receipt(root, "claude")
    monkeypatch.setattr(
        enrollment, "_install_ui_runtime",
        lambda *_args, **_kwargs: pytest.fail("an intact runtime must not be reinstalled"),
    )

    result = repair_capability_refusal(root, "claude", _ui_skill_requirements())

    assert result is not None and result.repaired
    assert "Claude" in result.detail and "Playwright" not in result.detail
    assert (root / ".claude" / "skills" / "ui-craft" / "SKILL.md").is_file()
    if receipt_state == "valid":
        assert probes == []
    else:
        assert probes == ["claude"]
        assert "native-discovery receipt" in result.detail
    from agentflow.provider_skills import native_discovery_status

    assert native_discovery_status(root, "claude")[0] == "ok"


@pytest.mark.parametrize("receipt_state", ("stale", "missing"))
def test_capability_repair_reproves_the_receipt_on_a_runtime_intact_root(
    tmp_path, monkeypatch, receipt_state
):
    root = _ready_ui_launch_source(tmp_path)
    _receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch)
    _stub_runnable_node(monkeypatch)
    if receipt_state == "stale":
        _stale_receipt(root, "codex")

    result = repair_capability_refusal(root, "codex", _ui_skill_requirements())

    assert result is not None and result.repaired
    assert "native-discovery receipt" in result.detail
    assert probes == ["codex"]
    from agentflow.provider_skills import native_discovery_status

    assert native_discovery_status(root, "codex")[0] == "ok"


def test_capability_repair_refuses_codex_content_drift_before_installing_runtime(
    tmp_path, monkeypatch
):
    import agentflow.enroll as enrollment

    root = _ready_ui_launch_source(tmp_path)
    receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch)
    _stub_runnable_node(monkeypatch)
    runtime = root / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    shutil.rmtree(runtime)
    receipt = _stale_receipt(root, "codex")
    receipt_before = receipt.read_bytes()
    (root / ".agents" / "skills" / "ui-craft" / "SKILL.md").write_text(
        "operator-authored content drift\n"
    )
    installs = []
    monkeypatch.setattr(
        enrollment, "_install_ui_runtime",
        lambda *_args, **_kwargs: installs.append(True) or "installed runtime",
    )

    result = repair_capability_refusal(root, "codex", _ui_skill_requirements())

    assert result is None
    assert installs == []
    assert probes == []
    assert receipt.read_bytes() == receipt_before
    assert receipts.joinpath("codex.json").read_bytes() == receipt_before


@pytest.mark.parametrize("runtime_fault", ("symlinked", "partial"))
def test_capability_repair_refuses_a_stale_receipt_on_an_occupied_runtime(
    tmp_path, monkeypatch, runtime_fault
):
    root = _ready_ui_launch_source(tmp_path)
    _receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch)
    _stub_runnable_node(monkeypatch)
    runtime = root / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    if runtime_fault == "symlinked":
        operator_runtime = tmp_path / "operator-runtime"
        shutil.move(runtime, operator_runtime)
        runtime.symlink_to(operator_runtime, target_is_directory=True)
    else:
        (runtime / ".bin" / "playwright").unlink()
    package = runtime / "playwright" / "package.json"
    before = package.read_bytes()
    _stale_receipt(root, "codex")

    result = repair_capability_refusal(root, "codex", _ui_skill_requirements())

    assert result is None
    assert probes == []
    assert package.read_bytes() == before
    assert runtime.is_symlink() is (runtime_fault == "symlinked")
    if runtime_fault == "partial":
        assert not (runtime / ".bin" / "playwright").exists()

    # Positive control: under identical stubs an intact sibling root does repair, so the
    # refusal above is attributable to the runtime fault, not to an absent Node.
    sibling = _ready_ui_launch_source(tmp_path / "sibling")
    control = repair_capability_refusal(sibling, "codex", _ui_skill_requirements())
    assert control is not None and control.repaired
    assert probes == ["codex"]


def test_capability_repair_refuses_an_unreadable_receipt_on_a_runtime_intact_root(
    tmp_path, monkeypatch
):
    root = _ready_ui_launch_source(tmp_path)
    _receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch)
    _stub_runnable_node(monkeypatch)
    receipt = record_native_discovery_receipt(root, "codex")
    receipt.write_text("operator-authored receipt\n")

    result = repair_capability_refusal(root, "codex", _ui_skill_requirements())

    assert result is None
    assert probes == []
    assert receipt.read_text() == "operator-authored receipt\n"


def test_failed_probe_after_materialization_reports_failure_without_rollback(
    tmp_path, monkeypatch
):
    import agentflow.enroll as enrollment

    root = _runtime_intact_claude_root(tmp_path)
    _receipts, probes = _stub_discovery_receipts(tmp_path, monkeypatch, probe_returncode=1)
    _stub_runnable_node(monkeypatch)
    receipt = _stale_receipt(root, "claude")
    receipt_before = receipt.read_bytes()
    monkeypatch.setattr(
        enrollment, "_install_ui_runtime",
        lambda *_args, **_kwargs: pytest.fail("an intact runtime must not be reinstalled"),
    )

    result = repair_capability_refusal(root, "claude", _ui_skill_requirements())

    assert result is not None and not result.repaired
    assert "materialized absent pinned capability destinations for Claude" in result.detail
    assert "did not prove native project skill discovery" in result.detail
    assert probes == ["claude"]
    assert (root / ".claude" / "skills" / "ui-craft" / "SKILL.md").is_file()
    assert receipt.read_bytes() == receipt_before
    from agentflow.provider_skills import native_discovery_status

    assert native_discovery_status(root, "claude")[0] == "drifted"


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


def test_capability_repair_is_single_writer_for_a_shared_root(tmp_path, monkeypatch):
    _enrollment, repo, source, names, commit, _lock, _commands = (
        _methodology_enrollment_fixture(tmp_path, monkeypatch)
    )
    for name in names:
        shutil.copytree(source / name, repo / ".agents" / "skills" / name)
    requirements = tuple(ContractRequirement(name, commit) for name in names)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(
            lambda _index: repair_capability_refusal(repo, "claude", requirements),
            range(2),
        ))

    assert sum(result is not None and result.repaired for result in results) == 1
    assert sum(result is None for result in results) == 1
    for name in names:
        assert (repo / ".claude" / "skills" / name / "SKILL.md").is_file()


def test_capability_repair_probes_a_shared_root_once(tmp_path, monkeypatch):
    root = _ready_ui_launch_source(tmp_path)
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    spec = next(item for item in manifest["capabilities"] if item["id"] == "domain-modeling")
    receipts = tmp_path / "receipts"
    monkeypatch.setattr(
        "agentflow.provider_skills._repository_key", lambda _root: ("repo-key", receipts)
    )
    monkeypatch.setattr(
        "agentflow.provider_skills._provider_fingerprint",
        lambda provider: (f"/providers/{provider}", f"{provider}-sha"),
    )
    probes = []
    monkeypatch.setattr(
        "agentflow.provider_skills._run_native_discovery_probe",
        lambda _root, _provider: probes.append(True) or SimpleNamespace(
            returncode=0, stdout=NATIVE_DISCOVERY_MARKER, stderr=""),
    )
    requirements = (ContractRequirement("domain-modeling", spec["version"]),)

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(
            lambda _index: repair_capability_refusal(root, "codex", requirements), range(2),
        ))

    assert sum(result is not None and result.repaired for result in results) == 1
    assert sum(result is None for result in results) == 1
    assert probes == [True]


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


def test_launch_materialization_ignores_missing_unrequired_ui_skill(tmp_path):
    checkout = Path(__file__).parents[1]
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    shutil.copytree(checkout / ".agents", source / ".claude")
    shutil.rmtree(source / ".claude" / "skills" / "ui-craft")
    destination.mkdir()

    requirements = requirements_for("build", {"ui": False})
    ready, detail = materialize_launch_capabilities(
        source, destination, "claude",
        requirement_ids={requirement.id for requirement in requirements},
    )

    assert ready is True and "materialized" in detail
    assert all(
        (destination / ".claude" / "skills" / requirement.id).is_dir()
        for requirement in requirements
    )
    assert not (destination / ".claude" / "skills" / "ui-craft").exists()


def test_launch_materialization_rejects_missing_required_ui_skill(tmp_path):
    checkout = Path(__file__).parents[1]
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    shutil.copytree(checkout / ".agents", source / ".claude")
    shutil.rmtree(source / ".claude" / "skills" / "ui-craft")
    destination.mkdir()

    requirements = requirements_for("review", {"ui": True})
    ready, detail = materialize_launch_capabilities(
        source, destination, "claude",
        requirement_ids={requirement.id for requirement in requirements},
    )

    assert ready is False
    assert detail == "claude source skill ui-craft is not intact"


def test_launch_materialization_refuses_runtime_without_drive_skill_requirement(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True,
        requirement_ids={"playwright"},
    )

    assert ready is False
    assert detail == "codex launch runtime requires drive-local-webapp"


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


def test_launch_materialization_refreshes_a_known_old_pinned_harness(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)
    (destination / "scripts").mkdir()
    checkout = Path(__file__).parents[1]
    old = (checkout / "tests" / "fixtures" / "old-screenshots.mjs").read_bytes()
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    harness = next(
        item for item in manifest["capabilities"] if item["id"] == "screenshot-harness"
    )
    assert hashlib.sha256(old).hexdigest() in harness["known_old_sha256"]
    destination_harness = destination / "scripts" / "screenshots.mjs"
    destination_harness.write_bytes(old)

    lines = []
    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True, _log=lines.append
    )

    assert ready is True and "materialized" in detail
    assert destination_harness.read_bytes() == (
        source / "scripts" / "screenshots.mjs"
    ).read_bytes()
    assert lines == [
        f"capability repair root={destination} requirements=screenshot-harness; "
        "outcome=materialized — copied missing codex capabilities into the launch root"
    ]
    assert all("ready" not in line for line in lines)


@pytest.mark.parametrize("harness_kind", ("unknown", "symlinked"))
def test_launch_materialization_preserves_an_occupied_harness(tmp_path, harness_kind):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)
    (destination / "scripts").mkdir()
    destination_harness = destination / "scripts" / "screenshots.mjs"
    if harness_kind == "unknown":
        destination_harness.write_text("operator-authored harness\n")
    else:
        external = tmp_path / "operator-harness.mjs"
        external.write_text("operator-authored harness\n")
        destination_harness.symlink_to(external)

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False and "harness" in detail
    assert destination_harness.read_text() == "operator-authored harness\n"
    assert destination_harness.is_symlink() is (harness_kind == "symlinked")


def test_launch_materialization_accepts_modified_harness_with_existing_owner_runtime(
    tmp_path,
):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    _launch_repository(source, _OWNER_REPOSITORY_ORIGIN)
    _launch_repository(destination)
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)
    (destination / "scripts").mkdir()
    destination_harness = destination / "scripts" / "screenshots.mjs"
    destination_harness.write_bytes((source / "scripts" / "screenshots.mjs").read_bytes())
    _track_harness(destination)
    destination_harness.write_text("in-branch screenshot harness update\n")
    status = subprocess.run(
        [
            "git", "-C", str(destination), "status", "--porcelain", "--",
            "scripts/screenshots.mjs",
        ],
        check=True, capture_output=True, text=True,
    )
    assert status.stdout == " M scripts/screenshots.mjs\n"

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is True and "materialized" in detail
    assert destination_harness.read_text() == "in-branch screenshot harness update\n"


@pytest.mark.parametrize(
    ("allow_owner_harness_drift", "expected_state"),
    ((True, "ready"), (False, "drifted")),
)
def test_preflight_allows_tracked_edited_harness_only_for_owner(
    tmp_path, monkeypatch, allow_owner_harness_drift, expected_state,
):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    _launch_repository(destination)
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)
    (destination / "scripts").mkdir()
    destination_harness = destination / "scripts" / "screenshots.mjs"
    destination_harness.write_bytes((source / "scripts" / "screenshots.mjs").read_bytes())
    _track_harness(destination)
    destination_harness.write_text("in-branch screenshot harness update\n")
    monkeypatch.setattr(
        "agentflow.capability_contracts.shutil.which", lambda _name: "/bin/codex"
    )

    result = preflight(
        destination, "build", "codex", _ui_runtime_requirement(),
        allow_owner_harness_drift=allow_owner_harness_drift,
    )

    assert result.state == expected_state


def test_non_materializing_owner_source_preflight_rejects_tracked_harness_edit(
    tmp_path, monkeypatch,
):
    from agentflow.pipeline import _capability_preflight

    source = _ready_ui_launch_source(tmp_path)
    _launch_repository(source, _OWNER_REPOSITORY_ORIGIN)
    _track_harness(source)
    (source / "scripts" / "screenshots.mjs").write_text(
        "tracked edit in enrolled source checkout\n"
    )
    (source / "AGENTS.md").write_text("profile: reviewed\nui-surfaces: frontend/\n")
    monkeypatch.setattr(
        "agentflow.capability_contracts.shutil.which", lambda _name: "/bin/codex"
    )
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status",
        lambda *_args: ("ok", "provider skill intact"),
    )
    record = Record(
        identity="ConnorGriffin/agentflow|735|review|-", stage="review", pool="codex",
        demand=1, source=str(source), capability_root=str(source), capability_context="{}",
    )

    result = _capability_preflight(record, False)

    assert result is not None and result.state == "drifted"
    assert any("pinned screenshot harness is drifted" in item for item in result.evidence)


def test_launch_materialization_refuses_non_harness_runtime_failure_for_owner(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    _launch_repository(source, _OWNER_REPOSITORY_ORIGIN)
    _launch_repository(destination)
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)
    (destination / "scripts").mkdir()
    destination_harness = destination / "scripts" / "screenshots.mjs"
    destination_harness.write_text("in-branch screenshot harness update\n")
    _track_harness(destination)
    runtime = destination / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    (runtime / "playwright" / "package.json").write_text('{"version":"0.0.0"}\n')

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False
    assert detail == (
        "codex launch runtime destination is occupied or incompatible: "
        "installed Playwright metadata does not pin 1.61.1"
    )


def test_launch_materialization_refuses_untracked_harness_for_owner(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    _launch_repository(source, _OWNER_REPOSITORY_ORIGIN)
    _launch_repository(destination)
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)
    (destination / "scripts").mkdir()
    destination_harness = destination / "scripts" / "screenshots.mjs"
    destination_harness.write_text("in-branch screenshot harness update\n")

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False
    assert detail == "codex launch screenshot harness is occupied or drifted"
    assert destination_harness.read_text() == "in-branch screenshot harness update\n"


def test_launch_materialization_refuses_spoofed_destination_owner_origin(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    non_owner_origin = "git@github.com:example/not-agentflow.git"
    _launch_repository(source, non_owner_origin)
    _launch_repository(destination, non_owner_origin)
    subprocess.run(
        [
            "git", "-C", str(destination), "remote", "set-url", "origin",
            _OWNER_REPOSITORY_ORIGIN,
        ],
        check=True,
    )
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)
    (destination / "scripts").mkdir()
    destination_harness = destination / "scripts" / "screenshots.mjs"
    destination_harness.write_text("in-branch screenshot harness update\n")
    _track_harness(destination)

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False
    assert detail == "codex launch screenshot harness is occupied or drifted"
    assert destination_harness.read_text() == "in-branch screenshot harness update\n"


def test_launch_materialization_refuses_symlinked_harness_in_packaged_repository(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    _launch_repository(source, _OWNER_REPOSITORY_ORIGIN)
    _launch_repository(destination)
    (destination / "scripts").mkdir()
    external = tmp_path / "operator-harness.mjs"
    external.write_text("in-branch screenshot harness update\n")
    destination_harness = destination / "scripts" / "screenshots.mjs"
    destination_harness.symlink_to(external)

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False
    assert detail == "codex launch screenshot harness is occupied or incompatible"
    assert destination_harness.is_symlink()


def test_launch_materialization_installs_a_missing_pinned_harness(tmp_path):
    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    shutil.copytree(source / ".agents", destination / ".agents", symlinks=True)

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    destination_harness = destination / "scripts" / "screenshots.mjs"
    assert ready is True and "materialized" in detail
    assert destination_harness.read_bytes() == (
        source / "scripts" / "screenshots.mjs"
    ).read_bytes()


def test_launch_rollback_preserves_a_concurrently_edited_created_harness(
    tmp_path, monkeypatch
):
    import agentflow.provider_skills as provider_skills

    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    destination.mkdir()
    harness = destination / "scripts" / "screenshots.mjs"
    lines = []

    def fail_skill_copy(*_args, **_kwargs):
        assert harness.is_file()
        harness.write_text("operator changed this during materialization\n")
        raise shutil.Error("copy failed")

    monkeypatch.setattr(provider_skills.shutil, "copytree", fail_skill_copy)

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True, _log=lines.append
    )

    assert ready is False and "rollback failed" in detail
    assert harness.read_text() == "operator changed this during materialization\n"
    assert len(lines) == 1
    assert f"root={destination}" in lines[0]
    assert "requirements=screenshot-harness" in lines[0]
    assert "outcome=failed" in lines[0]


def test_launch_rollback_never_follows_a_concurrently_substituted_harness_symlink(
    tmp_path, monkeypatch
):
    import agentflow.provider_skills as provider_skills

    source = _ready_ui_launch_source(tmp_path)
    destination = tmp_path / "destination"
    (destination / "scripts").mkdir(parents=True)
    checkout = Path(__file__).parents[1]
    old = (checkout / "tests" / "fixtures" / "old-screenshots.mjs").read_bytes()
    harness = destination / "scripts" / "screenshots.mjs"
    harness.write_bytes(old)
    external = tmp_path / "operator-harness.mjs"
    current = (source / "scripts" / "screenshots.mjs").read_bytes()
    external.write_bytes(current)

    def fail_skill_copy(*_args, **_kwargs):
        harness.unlink()
        harness.symlink_to(external)
        raise shutil.Error("copy failed")

    monkeypatch.setattr(provider_skills.shutil, "copytree", fail_skill_copy)

    ready, detail = materialize_launch_capabilities(
        source, destination, "codex", materialize_runtime=True
    )

    assert ready is False and "rollback failed" in detail
    assert harness.is_symlink()
    assert external.read_bytes() == current


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


def _harness_spec():
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    return next(item for item in manifest["capabilities"] if item["id"] == "screenshot-harness")


def test_converge_refuses_a_locally_modified_screenshot_harness(tmp_path):
    """A drifted harness that is neither the current nor a recorded previous pin is a repo-local
    edit: converge must stop with an actionable message and never touch the bytes (#735)."""
    from agentflow import enroll

    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    harness = root / "scripts" / "screenshots.mjs"
    modified = b"// repo-local capture tweaks\nimport './screenshots.mjs';\n"
    harness.write_bytes(modified)

    problem = enroll._managed_files_problem(root, ("frontend/",), converge=True)

    assert problem is not None
    assert str(harness) in problem
    assert "scripts/screenshots.local.mjs" in problem
    assert harness.read_bytes() == modified  # preflight refused before any write


def test_write_site_never_overwrites_a_locally_modified_harness(tmp_path):
    """Belt and braces: even a caller that skipped the preflight cannot overwrite a modified
    harness, because the write site computes ``overwrite`` from the same digest test (#735)."""
    from agentflow import enroll

    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    harness = root / "scripts" / "screenshots.mjs"
    modified = b"// repo-local capture tweaks\n"
    harness.write_bytes(modified)

    spec = _harness_spec()
    assert enroll._harness_convergeable(harness, spec) is False
    outcome = enroll._install_file(
        harness, enroll._asset_text("scripts/screenshots.mjs"),
        overwrite=enroll._harness_convergeable(harness, spec))

    assert outcome.startswith("WARN:")
    assert harness.read_bytes() == modified


def test_converge_upgrades_a_known_old_pinned_harness(tmp_path):
    """The untouched-upgrade path is preserved: a harness still holding a recorded previous pin is
    convergeable and the write site overwrites it with the current pinned bytes (#735)."""
    from agentflow import enroll

    checkout = Path(__file__).parents[1]
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    harness = root / "scripts" / "screenshots.mjs"
    harness.write_bytes((checkout / "tests" / "fixtures" / "old-screenshots.mjs").read_bytes())

    spec = _harness_spec()
    assert enroll._managed_files_problem(root, ("frontend/",), converge=True) is None
    assert enroll._harness_convergeable(harness, spec) is True
    pinned = enroll._asset_text("scripts/screenshots.mjs")
    outcome = enroll._install_file(
        harness, pinned, overwrite=enroll._harness_convergeable(harness, spec))

    assert outcome.startswith("DO:")
    assert harness.read_text() == pinned
