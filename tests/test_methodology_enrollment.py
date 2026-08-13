from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import shutil
from types import SimpleNamespace

from agentflow.enroll import doctor, enroll_repository


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
        enrollment, "_resolved_skill_release", lambda _manifest: (fixture_commit, None)
    )
    monkeypatch.setattr(
        enrollment.shutil, "which", lambda command: f"/usr/bin/{command}"
    )

    commands = []

    def run(command, **_kwargs):
        commands.append(command)
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=f"{fixture_commit}\n", stderr="")
        if command[0] == "npx":
            for name in names:
                target = repo / ".agents" / "skills" / name
                shutil.copytree(source / name, target)
                link = repo / ".claude" / "skills" / name
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(Path("../../.agents/skills") / name)
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
    for location in (".agents/skills", ".claude/skills"):
        for name in names:
            assert (repo / location / name / "SKILL.md").is_file()

    (repo / ".claude" / "skills" / "tdd").unlink()
    regressed = doctor(str(repo), stage="build", provider="codex")
    assert regressed.ready is False
    assert any(
        cell.context == "headless" and not cell.ready
        for cell in regressed.stage_matrix
    )
