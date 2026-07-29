from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.cli import main


def _git_commit(repo: Path, *args: str) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            *args,
        ],
        check=True,
    )


def _wire_ready_headless_repo(tmp_path, monkeypatch):
    agents = tmp_path / "AGENTS.md"
    agents.write_text("# Project\n\nprofile: reviewed\nui-surfaces: none\n")
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    skill = tmp_path / ".agents" / "skills" / "agentflow" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(
        (Path(__file__).parents[1] / "skills" / "agentflow" / "SKILL.md").read_text()
    )
    claude_skill = tmp_path / ".claude" / "skills" / "agentflow"
    claude_skill.parent.mkdir(parents=True)
    claude_skill.symlink_to("../../.agents/skills/agentflow")
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "owner/project"\nworkdir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))


def test_capability_manifest_pins_the_complete_public_skill_release():
    manifest = tomllib.loads(
        (
            Path(__file__).parents[1] / "agentflow" / "capabilities.toml"
        ).read_text()
    )
    pins = {
        item["skill"]: {file["path"]: file["sha256"] for file in item["files"]}
        for item in manifest["capabilities"]
        if item.get("skill") in {"ui-mockups", "drive-local-webapp"}
    }

    assert manifest["skill_installer"]["version"] == "1.5.9"
    assert manifest["connor_skills"] == {
        "source": "https://github.com/ConnorGriffin/skills",
        "tag": "v0.1.0",
        "commit": "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52",
        "skills": ["ui-mockups", "drive-local-webapp"],
    }
    assert pins["ui-mockups"] == {
        "SKILL.md": "0b42c78273dca661cba5502ecae16d8a8647fb9e6eeb7b539b99818a4be6cb51",
        "agents/openai.yaml": (
            "29e85e216b29bf13e9b75c6bbe92dd757d1eed393a3c0a84e3d4b9005db484cd"
        ),
        "references/variant-agent-prompt.md": (
            "e4b9047aa794b80c1e617cd19fc5cb493d34f3d1efa264eb0cc696d3708510ae"
        ),
    }
    assert pins["drive-local-webapp"] == {
        "SKILL.md": "9cbbc9b845d8d4194beea8da6a9aef0b4e4dd4e21f2aec7b29e7bb263d3b7284",
        "agents/openai.yaml": (
            "fab32a96f50c20e1cc5f915564624f30cefdad5eff81bcd3f39e01b82e5baf06"
        ),
        "package-lock.json": (
            "c3359191305ccfc3901f1b3097cfcd3a4bce96b4d6acd1c3e8d96e624912c9cc"
        ),
        "package.json": (
            "b7c433974c6ac3f176a8337c7c58523ec1165eff1f2fdd9a69220e24cf819269"
        ),
        "scripts/driver.mjs": (
            "492f60a53b7be206174d6f8476e90d3afe29696385ac4ef5669f35c6ad8d9fee"
        ),
        "scripts/self-check.mjs": (
            "faab0352dd85e986ba83a2c30c77879a3490764597a439fa18bd44241602bd96"
        ),
    }


def test_doctor_json_names_missing_required_capabilities(tmp_path, capsys):
    (tmp_path / "frontend").mkdir()

    result = main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert report["schema_version"] == 1
    assert report["ready"] is False
    missing = {
        item["id"]
        for item in report["capabilities"]
        if item["required"] and not item["available"]
    }
    assert {
        "repository-instructions",
        "agentflow-skill",
        "fleet-config",
        "ui-mockups",
        "drive-local-webapp",
        "screenshot-harness",
        "playwright",
    } <= missing
    provider = next(
        item for item in report["capabilities"] if item["id"] == "provider"
    )
    assert provider["required"] is True


def test_doctor_distinguishes_a_drifted_skill_from_a_missing_one(tmp_path, capsys):
    for location in (".agents/skills", ".claude/skills"):
        skill = tmp_path / location / "agentflow" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("changed locally\n")

    main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    states = {item["id"]: item["status"] for item in report["capabilities"]}
    assert states["agentflow-skill"] == "drifted"
    assert states["ui-mockups"] == "missing"


def test_doctor_treats_an_extra_managed_skill_file_as_drift(tmp_path, capsys):
    for location in (".agents/skills", ".claude/skills"):
        directory = tmp_path / location / "agentflow"
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            (Path(__file__).parents[1] / "skills" / "agentflow" / "SKILL.md").read_text()
        )
        (directory / "unexpected.py").write_text("raise SystemExit\n")

    main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    states = {item["id"]: item["status"] for item in report["capabilities"]}
    assert states["agentflow-skill"] == "drifted"


def test_doctor_does_not_execute_a_drifted_screenshot_harness(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    harness = tmp_path / "scripts" / "screenshots.mjs"
    harness.parent.mkdir()
    harness.write_text("throw new Error('untrusted');\n")
    for location in (".agents/skills", ".claude/skills"):
        package = (
            tmp_path
            / location
            / "drive-local-webapp"
            / "node_modules"
            / "playwright"
            / "package.json"
        )
        package.parent.mkdir(parents=True)
        package.write_text('{"version": "1.61.1"}\n')
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )

    def run(command, **kwargs):
        if command == ["node", "--version"]:
            return SimpleNamespace(returncode=0, stdout="v20.0.0\n", stderr="")
        raise AssertionError(f"doctor executed untrusted command: {command}")

    monkeypatch.setattr("agentflow.enroll._run_command", run)

    result = main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    states = {item["id"]: item["status"] for item in report["capabilities"]}
    assert result == 1
    assert states["screenshot-harness"] == "drifted"
    assert states["playwright"] == "missing"


def test_doctor_does_not_execute_runtime_from_a_drifted_drive_skill(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    harness = tmp_path / "scripts" / "screenshots.mjs"
    harness.parent.mkdir()
    harness.write_bytes(
        (Path(__file__).parents[1] / "scripts" / "screenshots.mjs").read_bytes()
    )
    original_status = __import__(
        "agentflow.enroll", fromlist=["_skill_status"]
    )._skill_status
    monkeypatch.setattr(
        "agentflow.enroll._skill_status",
        lambda root, name, files: (
            "drifted" if name == "drive-local-webapp"
            else original_status(root, name, files)
        ),
    )
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    monkeypatch.setattr(
        "agentflow.enroll._run_command",
        lambda command, **kwargs: (_ for _ in ()).throw(
            AssertionError(f"doctor executed drifted runtime: {command}")
        ),
    )

    result = main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    states = {item["id"]: item["status"] for item in report["capabilities"]}
    assert result == 1
    assert states["drive-local-webapp"] == "drifted"
    assert states["playwright"] == "missing"


def test_doctor_rejects_an_incomplete_drive_runtime_manifest(tmp_path, capsys):
    (tmp_path / "frontend").mkdir()
    runtime = tmp_path / ".agents" / "skills" / "drive-local-webapp"
    runtime.mkdir(parents=True)
    (runtime / "package.json").write_text('{"dependencies":{"playwright":"latest"}}\n')
    (runtime / "package-lock.json").write_text("{}\n")
    claude = tmp_path / ".claude" / "skills" / "drive-local-webapp"
    claude.parent.mkdir(parents=True)
    claude.symlink_to("../../.agents/skills/drive-local-webapp")

    main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    states = {item["id"]: item["status"] for item in report["capabilities"]}
    assert states["drive-local-webapp"] == "drifted"


@pytest.mark.parametrize(
    ("installed", "ready"),
    [
        (set(), False),
        ({"claude"}, True),
        ({"codex"}, True),
        ({"claude", "codex"}, True),
    ],
)
def test_doctor_requires_one_provider_and_recommends_the_second(
    tmp_path, monkeypatch, capsys, installed, ready
):
    _wire_ready_headless_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: f"/usr/bin/{command}" if command in installed else None,
    )

    result = main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    capabilities = {item["id"]: item for item in report["capabilities"]}
    assert report["ready"] is ready
    assert result == (0 if ready else 1)
    assert capabilities["provider"]["available"] is bool(installed)
    assert capabilities["claude"]["required"] is False
    assert capabilities["codex"]["required"] is False
    assert capabilities["claude"]["available"] is ("claude" in installed)
    assert capabilities["codex"]["available"] is ("codex" in installed)


@pytest.mark.parametrize(
    "instructions",
    [
        "# Project\n\nprofile: unsupported\nui-surfaces: none\n",
        "# Project\n\nprofile: reviewed\n",
    ],
)
def test_doctor_requires_supported_profile_and_explicit_ui_declaration(
    tmp_path, monkeypatch, capsys, instructions
):
    _wire_ready_headless_repo(tmp_path, monkeypatch)
    (tmp_path / "AGENTS.md").write_text(instructions)
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: "/usr/bin/claude" if command == "claude" else None,
    )

    result = main(["doctor", "--repo", str(tmp_path), "--json"])

    report = json.loads(capsys.readouterr().out)
    instructions_capability = next(
        item
        for item in report["capabilities"]
        if item["id"] == "repository-instructions"
    )
    assert result == 1
    assert instructions_capability["status"] == "drifted"


def test_enroll_dry_run_routes_repairs_through_transactional_apply(
    tmp_path, capsys
):
    (tmp_path / "frontend").mkdir()

    result = main(["enroll", str(tmp_path)])

    output = capsys.readouterr().out
    assert result == 1
    assert list(tmp_path.iterdir()) == [tmp_path / "frontend"]
    assert "dry run" in output
    assert f"agentflow enroll {tmp_path} --apply" in output
    assert "npx " not in output
    assert "npm ci" not in output
    assert "playwright install" not in output


def test_enroll_apply_installs_repo_local_capabilities_idempotently(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "frontend" / "index.html").write_text("<html></html>\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:owner/example.git",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "frontend"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path.parent / "config.toml"))
    monkeypatch.setattr("agentflow.enroll._skills_problem", lambda root, surfaces: None)

    def install_skills(root):
        for agent_root in (".agents/skills", ".claude/skills"):
            for name in ("ui-mockups", "drive-local-webapp"):
                skill = root / agent_root / name / "SKILL.md"
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text(f"---\nname: {name}\n---\n")
        return "DO:   installed the pinned Connor skill pack"

    monkeypatch.setattr("agentflow.enroll._install_connor_skills", install_skills)
    monkeypatch.setattr(
        "agentflow.enroll._install_ui_runtime",
        lambda root: "DO:   installed fake UI runtime",
    )

    first = main(["enroll", str(tmp_path), "--apply"])
    capsys.readouterr()

    assert first == 1  # fake skill bytes intentionally do not match the public manifest
    assert "profile: reviewed" in (tmp_path / "AGENTS.md").read_text()
    assert "ui-surfaces: frontend/" in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / "CLAUDE.md").readlink() == Path("AGENTS.md")
    assert (tmp_path / ".gitignore").read_text() == (
        ".agentflow/\n.agents/skills/**/node_modules/\n"
    )
    codex_skill = tmp_path / ".agents" / "skills" / "agentflow" / "SKILL.md"
    claude_skill = tmp_path / ".claude" / "skills" / "agentflow" / "SKILL.md"
    assert codex_skill.is_file()
    assert claude_skill.resolve() == codex_skill
    assert (tmp_path / "scripts" / "screenshots.mjs").is_file()
    assert "owner/example" in (tmp_path.parent / "config.toml").read_text()

    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "enroll",
        ],
        check=True,
    )
    before = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and ".git" not in path.relative_to(tmp_path).parts
    }
    main(["enroll", str(tmp_path), "--apply"])
    capsys.readouterr()
    after = {
        path.relative_to(tmp_path): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and ".git" not in path.relative_to(tmp_path).parts
    }
    assert after == before


def test_enroll_apply_uses_an_explicit_nonheuristic_ui_declaration(
    tmp_path, monkeypatch, capsys
):
    agents = tmp_path / "AGENTS.md"
    agents.write_text(
        "# Project\n\nprofile: reviewed\nui-surfaces: product/client-shell/\n"
    )
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@github.com:owner/project.git",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "AGENTS.md", "CLAUDE.md"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-qm",
            "instructions",
        ],
        check=True,
    )
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "owner/project"\nworkdir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    monkeypatch.setattr("agentflow.enroll._skills_problem", lambda root, surfaces: None)
    installed = []
    monkeypatch.setattr(
        "agentflow.enroll._install_connor_skills",
        lambda root: installed.append("skills") or "DO:   installed skills",
    )
    monkeypatch.setattr(
        "agentflow.enroll._install_ui_runtime",
        lambda root: installed.append("runtime") or "DO:   installed runtime",
    )

    main(["enroll", str(tmp_path), "--apply"])

    output = capsys.readouterr().out
    assert "ui-surfaces: product/client-shell/" in output
    assert installed == ["skills", "runtime"]
    assert (tmp_path / "scripts" / "screenshots.mjs").is_file()
    assert agents.read_text() == (
        "# Project\n\nprofile: reviewed\nui-surfaces: product/client-shell/\n"
    )


def test_enroll_keeps_an_explicit_headless_declaration_authoritative(
    tmp_path, capsys
):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "AGENTS.md").write_text(
        "# Project\n\nprofile: reviewed\nui-surfaces: none\n"
    )
    (tmp_path / "CLAUDE.md").symlink_to("AGENTS.md")

    result = main(["enroll", str(tmp_path)])

    output = capsys.readouterr().out
    assert result == 1
    assert "ui-surfaces: none" in output
    assert "screenshot harness" not in output


def test_enroll_preserves_conflicting_repository_instructions(
    tmp_path, monkeypatch, capsys
):
    agents = tmp_path / "AGENTS.md"
    claude = tmp_path / "CLAUDE.md"
    agents.write_text("Codex instructions\n")
    claude.write_text("Claude instructions\n")
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path.parent / "config.toml"))
    monkeypatch.setattr("agentflow.enroll._checkout_problem", lambda root: None)

    result = main(["enroll", str(tmp_path), "--apply"])

    assert result == 1
    assert agents.read_text() == "Codex instructions\n"
    assert claude.read_text() == "Claude instructions\n"
    assert "repository left unchanged" in capsys.readouterr().out
    assert not (tmp_path / ".gitignore").exists()
    assert not (tmp_path / ".agents").exists()


def test_enroll_replaces_duplicate_instructions_with_a_recoverable_link(
    tmp_path, monkeypatch, capsys
):
    content = "# Project\n\nprofile: reviewed\nui-surfaces: none\n"
    (tmp_path / "AGENTS.md").write_text(content)
    (tmp_path / "CLAUDE.md").write_text(content)
    monkeypatch.setattr("agentflow.enroll._checkout_problem", lambda root: None)
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "owner/project"\nworkdir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))

    main(["enroll", str(tmp_path), "--apply"])
    capsys.readouterr()

    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "CLAUDE.md").readlink() == Path("AGENTS.md")
    assert (tmp_path / "CLAUDE.md.pre-agentflow").read_text() == content


def test_enroll_promotes_incomplete_duplicate_instructions_without_splitting(
    tmp_path, monkeypatch
):
    content = "# Shared instructions\n"
    (tmp_path / "AGENTS.md").write_text(content)
    (tmp_path / "CLAUDE.md").write_text(content)
    monkeypatch.setattr("agentflow.enroll._checkout_problem", lambda root: None)
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "owner/project"\nworkdir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))

    main(["enroll", str(tmp_path), "--apply"])

    assert (tmp_path / "CLAUDE.md").is_symlink()
    assert (tmp_path / "CLAUDE.md").resolve() == (tmp_path / "AGENTS.md").resolve()
    assert "profile: reviewed" in (tmp_path / "AGENTS.md").read_text()
    assert "ui-surfaces: none" in (tmp_path / "AGENTS.md").read_text()
    assert (tmp_path / "CLAUDE.md.pre-agentflow").read_text() == content


def test_enroll_apply_refuses_a_dirty_checkout(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    untracked = tmp_path / "notes.txt"
    untracked.write_text("keep me\n")

    result = main(["enroll", str(tmp_path), "--apply"])

    assert result == 1
    assert untracked.read_text() == "keep me\n"
    assert not (tmp_path / "AGENTS.md").exists()
    assert "checkout is dirty" in capsys.readouterr().out


def test_relative_config_workdir_is_resolved_from_the_config_file(
    tmp_path, monkeypatch, capsys
):
    config_root = tmp_path / "fleet"
    repo = config_root / "repos" / "project"
    repo.mkdir(parents=True)
    _wire_ready_headless_repo(repo, monkeypatch)
    config = config_root / "config.toml"
    config.write_text(
        '[[repositories]]\nrepo = "owner/project"\nworkdir = "repos/project"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    monkeypatch.setattr("agentflow.enroll._checkout_problem", lambda root: None)

    main(["doctor", "--repo", str(repo), "--json"])
    report = json.loads(capsys.readouterr().out)
    fleet = next(
        item for item in report["capabilities"] if item["id"] == "fleet-config"
    )
    assert fleet["available"] is True

    main(["enroll", str(repo), "--apply"])
    capsys.readouterr()
    assert config.read_text().count("[[repositories]]") == 1


def test_skill_installer_success_for_only_one_agent_path_is_not_ready(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    monkeypatch.setattr("agentflow.enroll._checkout_problem", lambda root: None)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path.parent / "config.toml"))

    def run(command, **kwargs):
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52"
                    "\trefs/tags/v0.1.0\n"
                ),
                stderr="",
            )
        if command[:3] == ["git", "clone", "--no-checkout"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "checkout" in command:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(
                returncode=0,
                stdout="558d1a0cf3cea0e5a97c48eb8d50b2453b433f52\n",
                stderr="",
            )
        if command[0] == "npx":
            assert command[:3] == ["npx", "skills@1.5.9", "add"]
            assert command[3].endswith("/source")
            for name in ("ui-mockups", "drive-local-webapp"):
                skill = tmp_path / ".agents" / "skills" / name / "SKILL.md"
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text("installed in Codex only\n")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr("agentflow.enroll._run_command", run)

    result = main(["enroll", str(tmp_path), "--apply"])

    output = capsys.readouterr().out
    assert result == 1
    assert "both agent paths did not match the manifest" in output
    report = main(["doctor", "--repo", str(tmp_path), "--json"])
    assert report == 1
    states = {
        item["id"]: item["status"]
        for item in json.loads(capsys.readouterr().out)["capabilities"]
    }
    assert states["ui-mockups"] == "missing"
    assert not (tmp_path / ".agents" / "skills" / "ui-mockups").exists()


@pytest.mark.parametrize(
    "stdout",
    [
        "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52\trefs/tags/v0.1.0\n",
        (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v0.1.0\n"
            "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52"
            "\trefs/tags/v0.1.0^{}\n"
        ),
    ],
)
def test_release_verification_accepts_lightweight_and_annotated_tags(
    monkeypatch, stdout
):
    from agentflow.enroll import _manifest, _resolved_skill_release

    commands = []
    monkeypatch.setattr(
        "agentflow.enroll._run_command",
        lambda command, **kwargs: commands.append(command)
        or SimpleNamespace(returncode=0, stdout=stdout, stderr=""),
    )

    resolved, error = _resolved_skill_release(_manifest())

    assert error is None
    assert resolved == "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52"
    assert commands == [
        [
            "git",
            "ls-remote",
            "--tags",
            "https://github.com/ConnorGriffin/skills",
            "refs/tags/v0.1.0",
            "refs/tags/v0.1.0^{}",
        ]
    ]


def test_ui_preflight_verifies_release_even_when_skills_are_already_intact(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "agentflow.enroll._public_skill_destination_states",
        lambda root, manifest: {("path", "skill"): "ok"},
    )
    monkeypatch.setattr(
        "agentflow.enroll._resolved_skill_release",
        lambda manifest: ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", None),
    )

    from agentflow.enroll import _skills_problem

    problem = _skills_problem(tmp_path, ("frontend/",))

    assert "resolved to aaaaaaaaaa" in problem


def test_enroll_rejects_a_moved_skill_tag_before_mutating_the_checkout(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    _git_commit(tmp_path, "--allow-empty", "-qm", "initial")
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    original_run = __import__("agentflow.enroll", fromlist=["_run_command"])._run_command
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return SimpleNamespace(
                returncode=0,
                stdout="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v0.1.0\n",
                stderr="",
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr("agentflow.enroll._run_command", run)

    result = main(["enroll", str(tmp_path), "--apply"])

    assert result == 1
    assert "resolved to aaaaaaaaaa" in capsys.readouterr().out
    assert not config.exists()
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout == ""
    assert not any(command[0] == "npx" for command in commands)


def test_enroll_rolls_back_if_tag_moves_between_preflight_and_install(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    _git_commit(tmp_path, "--allow-empty", "-qm", "initial")
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    original_run = __import__("agentflow.enroll", fromlist=["_run_command"])._run_command
    tag_reads = 0
    commands = []

    def run(command, **kwargs):
        nonlocal tag_reads
        commands.append(command)
        if command[:3] == ["git", "ls-remote", "--tags"]:
            tag_reads += 1
            commit = (
                "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52"
                if tag_reads == 1
                else "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            )
            return SimpleNamespace(
                returncode=0,
                stdout=f"{commit}\trefs/tags/v0.1.0\n",
                stderr="",
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr("agentflow.enroll._run_command", run)

    assert main(["enroll", str(tmp_path), "--apply"]) == 1

    output = capsys.readouterr().out
    assert "resolved to aaaaaaaaaa" in output
    assert "rolled back" in output
    assert tag_reads == 2
    assert not any(command[0] == "npx" for command in commands)
    assert not config.exists()
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout == ""


def test_enroll_rejects_drifted_public_skills_without_running_installer(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    drifted = tmp_path / ".agents" / "skills" / "ui-mockups" / "SKILL.md"
    drifted.parent.mkdir(parents=True)
    drifted.write_text("local edits\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    _git_commit(tmp_path, "-qm", "initial")
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    original_run = __import__("agentflow.enroll", fromlist=["_run_command"])._run_command
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52"
                    "\trefs/tags/v0.1.0\n"
                ),
                stderr="",
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr("agentflow.enroll._run_command", run)

    main(["enroll", str(tmp_path), "--apply"])

    assert "partial or conflicting" in capsys.readouterr().out
    assert drifted.read_text() == "local edits\n"
    assert not config.exists()
    assert not any(command[0] == "npx" for command in commands)
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout == ""


@pytest.mark.parametrize("shape", ["regular-file", "broken-symlink", "empty-directory"])
def test_enroll_treats_existing_non_skill_destinations_as_conflicts(
    tmp_path, monkeypatch, capsys, shape
):
    (tmp_path / "frontend").mkdir()
    destination = tmp_path / ".agents" / "skills" / "ui-mockups"
    destination.parent.mkdir(parents=True)
    if shape == "regular-file":
        destination.write_text("preserve me\n")
    elif shape == "broken-symlink":
        destination.symlink_to("missing-target")
    else:
        destination.mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    _git_commit(tmp_path, "--allow-empty", "-qm", "initial")
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    original_run = __import__("agentflow.enroll", fromlist=["_run_command"])._run_command
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52"
                    "\trefs/tags/v0.1.0\n"
                ),
                stderr="",
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr("agentflow.enroll._run_command", run)

    assert main(["enroll", str(tmp_path), "--apply"]) == 1

    assert "partial or conflicting" in capsys.readouterr().out
    assert not any(command[0] == "npx" for command in commands)
    assert not config.exists()
    if shape == "regular-file":
        assert destination.read_text() == "preserve me\n"
    elif shape == "broken-symlink":
        assert destination.is_symlink()
        assert destination.readlink() == Path("missing-target")
    else:
        assert destination.is_dir()
        assert not any(destination.iterdir())


def test_enroll_promotes_claude_only_instructions_and_is_idempotent(
    tmp_path, monkeypatch, capsys
):
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("# Existing instructions\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "CLAUDE.md"], check=True)
    _git_commit(tmp_path, "-qm", "instructions")
    config = tmp_path.parent / "config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "owner/project"\nworkdir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: "/usr/bin/claude" if command == "claude" else None,
    )

    assert main(["enroll", str(tmp_path), "--apply"]) == 0
    capsys.readouterr()
    assert claude.is_symlink()
    assert claude.resolve() == (tmp_path / "AGENTS.md").resolve()
    assert (tmp_path / "CLAUDE.md.pre-agentflow").read_text() == "# Existing instructions\n"
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    _git_commit(tmp_path, "-qm", "enrolled")
    assert main(["enroll", str(tmp_path), "--apply"]) == 0


@pytest.mark.parametrize(
    "kind",
    ["non-repository", "nested", "status-failure", "no-origin", "non-github-origin"],
)
def test_enroll_strict_git_preflight_leaves_the_target_unchanged(
    tmp_path, monkeypatch, capsys, kind
):
    target = tmp_path
    if kind != "non-repository":
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        if kind != "no-origin":
            origin = (
                "/tmp/owner/project.git"
                if kind == "non-github-origin"
                else "git@github.com:owner/project.git"
            )
            subprocess.run(
                ["git", "-C", str(tmp_path), "remote", "add", "origin",
                 origin],
                check=True,
            )
        _git_commit(tmp_path, "--allow-empty", "-qm", "initial")
    if kind == "nested":
        target = tmp_path / "nested"
        target.mkdir()
    if kind == "status-failure":
        original_run = __import__("agentflow.enroll", fromlist=["_run_command"])._run_command

        def run(command, **kwargs):
            if command[-2:] == ["status", "--porcelain"]:
                return SimpleNamespace(returncode=1, stdout="", stderr="failed")
            return original_run(command, **kwargs)

        monkeypatch.setattr("agentflow.enroll._run_command", run)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path.parent / "config.toml"))

    assert main(["enroll", str(target), "--apply"]) == 1

    assert "repository left unchanged" in capsys.readouterr().out
    assert not (target / "AGENTS.md").exists()


@pytest.mark.parametrize("managed", ["agentflow-skill", "screenshot-harness"])
def test_enroll_rejects_drifted_bundled_files_before_any_write(
    tmp_path, monkeypatch, capsys, managed
):
    if managed == "agentflow-skill":
        target = tmp_path / ".agents" / "skills" / "agentflow" / "SKILL.md"
    else:
        (tmp_path / "frontend").mkdir()
        target = tmp_path / "scripts" / "screenshots.mjs"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("local changes\n")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    _git_commit(tmp_path, "-qm", "initial")
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))

    assert main(["enroll", str(tmp_path), "--apply"]) == 1

    assert f"managed {'AgentFlow skill' if managed == 'agentflow-skill' else 'screenshot harness'}" in capsys.readouterr().out
    assert target.read_text() == "local changes\n"
    assert not config.exists()
    assert subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout == ""


def test_enroll_rejects_invalid_existing_config_before_mutation(
    tmp_path, monkeypatch, capsys
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    _git_commit(tmp_path, "--allow-empty", "-qm", "initial")
    config = tmp_path.parent / "config.toml"
    config.write_text('[[repositories]]\nrepo = "owner/project"\nunknown = true\n')
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))

    main(["enroll", str(tmp_path), "--apply"])

    assert "configuration is not safe" in capsys.readouterr().out
    assert not (tmp_path / "AGENTS.md").exists()
    assert config.read_text() == (
        '[[repositories]]\nrepo = "owner/project"\nunknown = true\n'
    )


def test_enroll_rollback_restores_a_symlinked_config_target(
    tmp_path, monkeypatch
):
    target = tmp_path.parent / f"{tmp_path.name}-config-target.toml"
    original = (
        f'[[repositories]]\nrepo = "owner/project"\nworkdir = "{tmp_path}"\n'
    )
    target.write_text(original)
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    config.symlink_to(target)
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    monkeypatch.setattr("agentflow.enroll._checkout_problem", lambda root: None)

    def fail_after_config_write(root, profile, surfaces):
        with config.open("a") as stream:
            stream.write("\n# partial write\n")
        return ["WARN: simulated external command failure"]

    monkeypatch.setattr("agentflow.enroll._apply_enrollment", fail_after_config_write)

    assert main(["enroll", str(tmp_path), "--apply"]) == 1

    assert config.is_symlink()
    assert config.resolve() == target.resolve()
    assert target.read_text() == original


def test_enroll_rejects_missing_ui_commands_before_mutation(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "frontend").mkdir()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "add", "origin",
         "git@github.com:owner/project.git"],
        check=True,
    )
    _git_commit(tmp_path, "--allow-empty", "-qm", "initial")
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: None if command == "npx" else f"/usr/bin/{command}",
    )

    main(["enroll", str(tmp_path), "--apply"])

    assert "missing required UI command(s): npx" in capsys.readouterr().out
    assert not config.exists()
    assert not (tmp_path / "AGENTS.md").exists()


def test_missing_process_is_reported_instead_of_raising(monkeypatch):
    from agentflow.enroll import _run_command

    monkeypatch.setattr(
        "agentflow.runner._run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    result = _run_command(["npm", "ci"])

    assert result.returncode == 127
    assert result.stderr == "missing"


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "git-clone",
        "git-checkout",
        "git-rev-parse",
        "git-head-mismatch",
        "npx",
        "npm-ci",
        "playwright",
        "skill-self-check",
        "harness-self-check",
    ],
)
def test_enroll_public_ui_command_path_reports_each_stage(
    tmp_path, monkeypatch, capsys, failure
):
    (tmp_path / "frontend").mkdir()
    config = tmp_path.parent / f"{tmp_path.name}-config.toml"
    config.write_text(
        f'[[repositories]]\nrepo = "owner/project"\nworkdir = "{tmp_path}"\n'
    )
    monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))
    monkeypatch.setattr("agentflow.enroll._checkout_problem", lambda root: None)
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which", lambda command: f"/usr/bin/{command}"
    )
    original_destination_status = __import__(
        "agentflow.enroll", fromlist=["_skill_destination_status"]
    )._skill_destination_status
    installed = False
    active_failure = failure
    commands = []
    before = [
        (path.relative_to(tmp_path).as_posix(), "link", str(path.readlink()))
        if path.is_symlink()
        else (
            path.relative_to(tmp_path).as_posix(),
            "dir" if path.is_dir() else "file",
            b"" if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(tmp_path.rglob("*"))
    ]
    config_before = config.read_bytes()

    def destination_status(directory, manifest):
        if directory.name in {"ui-mockups", "drive-local-webapp"}:
            return "ok" if installed else "absent"
        return original_destination_status(directory, manifest)

    def run(command, **kwargs):
        nonlocal active_failure, installed
        commands.append(command)
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v0.1.0\n"
                    "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52"
                    "\trefs/tags/v0.1.0^{}\n"
                ),
                stderr="",
            )
        if command[:3] == ["git", "clone", "--no-checkout"]:
            if active_failure == "git-clone":
                return SimpleNamespace(returncode=1, stdout="", stderr="clone failed")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "checkout" in command:
            if active_failure == "git-checkout":
                return SimpleNamespace(returncode=1, stdout="", stderr="checkout failed")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[-2:] == ["rev-parse", "HEAD"]:
            if active_failure == "git-rev-parse":
                return SimpleNamespace(returncode=1, stdout="", stderr="rev-parse failed")
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
                    if active_failure == "git-head-mismatch"
                    else "558d1a0cf3cea0e5a97c48eb8d50b2453b433f52\n"
                ),
                stderr="",
            )
        if command[0] == "npx":
            if active_failure == "npx":
                return SimpleNamespace(returncode=127, stdout="", stderr="missing npx")
            installed = True
            for name in ("ui-mockups", "drive-local-webapp"):
                codex = tmp_path / ".agents" / "skills" / name
                codex.mkdir(parents=True)
                claude = tmp_path / ".claude" / "skills" / name
                claude.parent.mkdir(parents=True, exist_ok=True)
                claude.symlink_to(Path("../../.agents/skills") / name)
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["npm", "ci"]:
            if active_failure == "npm-ci":
                return SimpleNamespace(returncode=127, stdout="", stderr="missing npm")
            package = (
                Path(kwargs["cwd"])
                / "node_modules"
                / "playwright"
                / "package.json"
            )
            package.parent.mkdir(parents=True)
            package.write_text('{"version": "1.61.1"}\n')
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command == ["node", "--version"]:
            return SimpleNamespace(returncode=0, stdout="v20.0.0\n", stderr="")
        if command[-2:] == ["install", "chromium"] and active_failure == "playwright":
            return SimpleNamespace(returncode=127, stdout="", stderr="missing playwright")
        if command == ["npm", "run", "self-check"] and active_failure == "skill-self-check":
            return SimpleNamespace(returncode=1, stdout="", stderr="skill self-check failed")
        if (
            command[0] == "node"
            and command[-1:] == ["--self-check"]
            and active_failure == "harness-self-check"
        ):
            return SimpleNamespace(returncode=1, stdout="", stderr="harness failed")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        "agentflow.enroll._skill_destination_status", destination_status
    )
    monkeypatch.setattr("agentflow.enroll._run_command", run)

    result = main(["enroll", str(tmp_path), "--apply"])

    output = capsys.readouterr().out
    assert result == (0 if failure is None else 1), output
    source_failures = {
        "git-clone",
        "git-checkout",
        "git-rev-parse",
        "git-head-mismatch",
    }
    if failure in source_failures:
        assert not any(command[0] == "npx" for command in commands)
        assert "skill source" in output
        npx_index = None
    else:
        npx_index = next(i for i, command in enumerate(commands) if command[0] == "npx")
    if npx_index is None:
        pass
    else:
        assert commands[npx_index][3].endswith("/source")
        assert "v0.1.0" not in commands[npx_index][3]
    if failure in source_failures:
        pass
    elif failure == "npx":
        assert "missing npx" in output
    else:
        npm_index = commands.index(["npm", "ci"])
        assert npx_index < npm_index
        if failure == "npm-ci":
            assert "missing npm" in output
        else:
            playwright_index = next(
                i for i, command in enumerate(commands)
                if command[-2:] == ["install", "chromium"]
            )
            assert npm_index < playwright_index
            if failure == "playwright":
                assert "missing playwright" in output
            else:
                skill_check_index = commands.index(["npm", "run", "self-check"])
                assert playwright_index < skill_check_index
                if failure == "skill-self-check":
                    assert "skill self-check failed" in output
                else:
                    harness_index = next(
                        i for i, command in enumerate(commands)
                        if command[0] == "node" and command[-1:] == ["--self-check"]
                    )
                    assert skill_check_index < harness_index
                    if failure == "harness-self-check":
                        assert "screenshot harness self-check failed" in output
    if failure is None:
        return
    after = [
        (path.relative_to(tmp_path).as_posix(), "link", str(path.readlink()))
        if path.is_symlink()
        else (
            path.relative_to(tmp_path).as_posix(),
            "dir" if path.is_dir() else "file",
            b"" if path.is_dir() else path.read_bytes(),
        )
        for path in sorted(tmp_path.rglob("*"))
    ]
    assert after == before
    assert config.read_bytes() == config_before
    active_failure = None
    installed = False
    commands.clear()
    assert main(["enroll", str(tmp_path), "--apply"]) == 0
