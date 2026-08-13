from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.capability_contracts import ContractRequirement
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
    monkeypatch.setattr("agentflow.enroll._tooling_problem", lambda _surfaces: None)
    monkeypatch.setattr(
        "agentflow.enroll._install_methodology_skills",
        lambda _root: "ok:   methodology contracts supplied by focused fixture",
    )


def test_capability_manifest_pins_the_complete_public_skill_release():
    manifest = tomllib.loads(
        (
            Path(__file__).parents[1] / "agentflow" / "capabilities.toml"
        ).read_text()
    )
    pins = {
        item["skill"]: {file["path"]: file["sha256"] for file in item["files"]}
        for item in manifest["capabilities"]
        if item.get("skill") in {"ui-craft", "drive-local-webapp"}
    }

    assert manifest["skill_installer"]["version"] == "1.5.9"
    assert manifest["connor_skills"] == {
        "source": "https://github.com/ConnorGriffin/skills",
        "tag": "v0.3.0",
        "commit": "230e71a55ab07f0cd9beaa61649b583cb9d1bde1",
        "skills": ["ui-craft", "drive-local-webapp"],
    }
    assert pins["ui-craft"] == {
        "SKILL.md": "a33d188dd0cd9b648795e959338f98fd6f9f135fd0c6cef4ddd48d0011a3f7c7",
        "agents/openai.yaml": "87e22f100ffc1d87b342ada266ccddbcc271b20709cfe5c3e7b5179c635f9b56",
        "reference/audit.md": "ec0f1c0a472493e048d6ed7de07d40e6b39398c183c6c27c060045a55886a579",
        "reference/brand.md": "6523c1d15a9127a3e7fe46c2509a8eab4bc586c3eceb523b9b091af580b84d3a",
        "reference/build.md": "bc750e2e19b5e2220e5586e3f4d8e87aca29d4da782db06b23a7c2b8964de6af",
        "reference/codex.md": "6301dd5df63d2f06829a486e8b18f6f1f2cd549c45f347137fbebc60578bb2ac",
        "reference/critique.md": "b41b26ecf8b9a8a1960bfa1346dcca9c4980bfc439768c131e743e070b287ed7",
        "reference/design-rules.md": "a9bb21e324e30ca641dd0503365660cb8b3d63e345070a471d6dd07f3b830469",
        "reference/document.md": "e0a95b583c00c10c34342df8e862bbc5e80ae70b2e2d45ac0562f18626c63b58",
        "reference/init.md": "36138702e864a139113e6b1f2b8ee9bd3c3d38233880e8c3021d3390d4ac8d62",
        "reference/lock.md": "cd3d645487998242a7fb974468af86f10047ee2a7f2e5ce0623eac9f6ea3c781",
        "reference/polish.md": "abee7a59b47e26fdb3852d58fa21c19101aafc428587b0043e1ca7517ef5cbab",
        "reference/product.md": "9ea8cc99ec208f4c1addc66369980ec6ee2a0ae7732aa8f14672aeed3418b6aa",
        "reference/resettle.md": "ee42459887603619f8d17e906903275d03f4fe3b35fc6ec923618deccd8d85e8",
        "reference/variant-agent-prompt.md": "e4b9047aa794b80c1e617cd19fc5cb493d34f3d1efa264eb0cc696d3708510ae",
        "scripts/command-metadata.json": "759cc4028134797401d8050aa97c111bfc5ece9db1caba92df24e01ff7ba743d",
        "scripts/context-signals.mjs": "7846ab9d3f71171b041bd9090b34136686589d53ff87bdc91313f3cee1702ff9",
        "scripts/context.mjs": "d3a9254e5375e09b2ee12724dd9d5738d9b311b638ad2bce01563737d7eadcba",
        "scripts/critique-storage.mjs": "0ae2c767ee8e5d7820c3a2a5bc96aeb3721d25fafda54b72e419af9e1775444a",
        "scripts/detect-csp.mjs": "2d80520bef13cb93107699714bc0d2be5a2787a9aa079ecec91b94508a8125a4",
        "scripts/detect.mjs": "f5dfd05ca1e314acd8ed79c6301a20a1b844f2eb6691bd63e04116a8e7efb187",
        "scripts/detector/browser/injected/index.mjs": "4c0fb5155e1eeef365e0c526b9c80ab250392f9fb68156b3acaaba5b1e974a19",
        "scripts/detector/cli/main.mjs": "1ead1f652d202417351d259161a26a7a36a963b627c3caa5a6d1851ec3eaa677",
        "scripts/detector/design-system.mjs": "6339cb3e13c7d203badc051af5e4deb820f8aaabde82636fc0140521e5534193",
        "scripts/detector/detect-antipatterns-browser.js": "0c069d9cd423819a8671397ea313c20b092571fc6f309de0d4fb82e6757b0ece",
        "scripts/detector/detect-antipatterns.mjs": "f5240e4162b270efafd099b81d315309a6f25a83a887aedf02bf9bcf3ed5c724",
        "scripts/detector/engines/browser/detect-url.mjs": "7ef2274debad8467e9dec97261b1ad2581f221b308c1b4b3fcc501a551080088",
        "scripts/detector/engines/regex/detect-text.mjs": "cb9365dce43719483be6831243493356fc6e71d7b960b070d8712aec2b5a8dea",
        "scripts/detector/engines/static-html/css-cascade.mjs": "919a55f7f73771e8911ae842f729d700b82c3bd8c991f45900bab5a245b0525f",
        "scripts/detector/engines/static-html/detect-html.mjs": "f8729c12ef4b40af1146b516ac02fbdcc61be43052163914e67309f5dad92ee0",
        "scripts/detector/engines/visual/screenshot-contrast.mjs": "b4520b3f001079bd175bf20d46c5197b1b8ec5fb98fdf72c50a24c0b6598ee99",
        "scripts/detector/findings.mjs": "555330225cda4da2221e000c2d06abcbbd03c2c0246a5b066b5c81f8057ccce6",
        "scripts/detector/node/file-system.mjs": "4004ac034bd6c09419558608b1185271e4ceb741e8edb3b06082af122ac14697",
        "scripts/detector/profile/profiler.mjs": "5e201f3557360bd368a3977fecf925f376fd95f8a4dd6c4cc63fbd62848cda43",
        "scripts/detector/registry/antipatterns.mjs": "22adf773f16d1738edc7b7735fde267561ccc5d7b1c8ef8c52d6709b70808ba3",
        "scripts/detector/rules/checks.mjs": "2e51f831a9b85d648a3aede84ffbe3eb7ecfaa65a5df42ec047da147f75b3273",
        "scripts/detector/shared/color.mjs": "01a68231413473f2c1183e12663ccf8aca26a87d067de75fbf3c21e73b751a83",
        "scripts/detector/shared/constants.mjs": "2e61e0815e7216fe74ce89eccfff2761bb72268964d804ee89987119130e4edd",
        "scripts/detector/shared/inline-ignores.mjs": "74c80303e25f017b4671ae299b9fca424a5f7714203d19207a8d8896dfd2eecc",
        "scripts/detector/shared/page.mjs": "b4cef5548d84fa90d15e6b42b20be9ef74ed05e4e6e46076ec5acc61b343315c",
        "scripts/lib/design-parser.mjs": "096747b1225f0b78f6fe155477324c592f4fc38021b7007177e70b2dc55df588",
        "scripts/lib/impeccable-config.mjs": "b8da16081a158d2aa6e86ff4fa74028977ee73e6559eda13e6a068b60123c7cb",
        "scripts/lib/impeccable-paths.mjs": "e8a8fb45408e92e08bdc6cbc5e94c53b42166f7dd468ffeab98b1808dfa09362",
        "scripts/lib/is-generated.mjs": "ed5b0b00e99ed385db541c7a068cb85f72456a4b6aa7a5460037d24b58bd90cd",
        "scripts/lib/target-args.mjs": "8f8b902d9ec7dbad0431226bb8aea935cd67d68878d19d95a605be0585056f31",
        "scripts/palette.mjs": "6e43ef19ede979019ebf86175fed0d6876473ddc1cf6c40c8b310e7ac4c5f45a",
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
        "ui-craft",
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
    assert states["ui-craft"] == "missing"


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
    assert states["playwright"] == "drifted"


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
    assert states["playwright"] == "drifted"


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
        ({"claude"}, False),
        ({"codex"}, False),
        ({"claude", "codex"}, True),
    ],
)
def test_doctor_full_matrix_requires_every_selected_provider(
    tmp_path, monkeypatch, capsys, installed, ready
):
    _wire_ready_headless_repo(tmp_path, monkeypatch)
    monkeypatch.setattr("agentflow.prompts.requirements_for", lambda *_args: ())
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


def test_doctor_provider_filter_narrows_readiness_to_the_selected_matrix(
    tmp_path, monkeypatch, capsys
):
    _wire_ready_headless_repo(tmp_path, monkeypatch)
    monkeypatch.setattr("agentflow.prompts.requirements_for", lambda *_args: ())
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: "/usr/bin/claude" if command == "claude" else None,
    )

    result = main(
        ["doctor", "--repo", str(tmp_path), "--provider", "claude", "--json"]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["ready"] is True
    assert {cell["provider"] for cell in report["stage_matrix"]} == {"claude"}


def test_doctor_headless_repository_excludes_ui_contexts_from_dispatchable_matrix(
    tmp_path, monkeypatch, capsys
):
    _wire_ready_headless_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "agentflow.prompts.requirements_for",
        lambda _stage, context: (
            (ContractRequirement("ui-craft", "v0.3.0"),)
            if context["ui"]
            else ()
        ),
    )
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: "/usr/bin/claude" if command == "claude" else None,
    )

    result = main([
        "doctor", "--repo", str(tmp_path), "--stage", "build",
        "--provider", "claude", "--json",
    ])

    report = json.loads(capsys.readouterr().out)
    assert result == 0
    assert report["ready"] is True
    assert {cell["context"] for cell in report["stage_matrix"]} == {"headless"}


def test_doctor_ui_repository_includes_ui_contexts_in_dispatchable_matrix(
    tmp_path, monkeypatch, capsys
):
    _wire_ready_headless_repo(tmp_path, monkeypatch)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "AGENTS.md").write_text(
        "# Project\n\nprofile: reviewed\nui-surfaces: frontend/\n"
    )
    monkeypatch.setattr(
        "agentflow.prompts.requirements_for",
        lambda _stage, context: (
            (ContractRequirement("ui-craft", "v0.3.0"),)
            if context["ui"]
            else ()
        ),
    )
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: "/usr/bin/claude" if command == "claude" else None,
    )

    result = main([
        "doctor", "--repo", str(tmp_path), "--stage", "build",
        "--provider", "claude", "--json",
    ])

    report = json.loads(capsys.readouterr().out)
    cells = {cell["context"]: cell for cell in report["stage_matrix"]}
    assert result == 1
    assert report["ready"] is False
    assert cells["headless"]["ready"] is True
    assert cells["ui"]["ready"] is False
    assert cells["ui"]["contracts"] == ["ui-craft@v0.3.0"]


def test_doctor_stage_filter_fails_when_any_selected_cell_is_missing(
    tmp_path, monkeypatch, capsys
):
    _wire_ready_headless_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )

    result = main(
        [
            "doctor",
            "--repo",
            str(tmp_path),
            "--stage",
            "build",
            "--provider",
            "codex",
            "--json",
        ]
    )

    report = json.loads(capsys.readouterr().out)
    assert result == 1
    assert report["ready"] is False
    assert any(not cell["ready"] for cell in report["stage_matrix"])


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
    monkeypatch.setattr("agentflow.enroll._skills_problem", lambda root, surfaces, **_: None)

    def install_skills(root):
        for agent_root in (".agents/skills", ".claude/skills"):
            for name in ("ui-craft", "drive-local-webapp"):
                skill = root / agent_root / name / "SKILL.md"
                skill.parent.mkdir(parents=True, exist_ok=True)
                skill.write_text(f"---\nname: {name}\n---\n")
        return "DO:   installed the pinned Connor skill pack"

    monkeypatch.setattr("agentflow.enroll._install_connor_skills", install_skills)
    monkeypatch.setattr("agentflow.enroll._install_methodology_skills",
                        lambda root: "DO:   installed methodology contracts")
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
    monkeypatch.setattr("agentflow.enroll._skills_problem", lambda root, surfaces, **_: None)
    installed = []
    monkeypatch.setattr(
        "agentflow.enroll._install_connor_skills",
        lambda root: installed.append("skills") or "DO:   installed skills",
    )
    monkeypatch.setattr("agentflow.enroll._install_methodology_skills",
                        lambda root: "DO:   installed methodology contracts")
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
    monkeypatch.setattr("agentflow.enroll._tooling_problem", lambda _surfaces: None)
    monkeypatch.setattr(
        "agentflow.enroll._install_methodology_skills",
        lambda _root: "ok:   methodology contracts supplied by focused fixture",
    )

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
    monkeypatch.setattr("agentflow.enroll._tooling_problem", lambda _surfaces: None)
    monkeypatch.setattr(
        "agentflow.enroll._install_methodology_skills",
        lambda _root: "ok:   methodology contracts supplied by focused fixture",
    )

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
    monkeypatch.setattr(
        "agentflow.enroll._install_methodology_skills",
        lambda _root: "ok:   methodology contracts supplied by focused fixture",
    )

    def run(command, **kwargs):
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "230e71a55ab07f0cd9beaa61649b583cb9d1bde1"
                    "\trefs/tags/v0.3.0\n"
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
                stdout="230e71a55ab07f0cd9beaa61649b583cb9d1bde1\n",
                stderr="",
            )
        if command[0] == "npx":
            assert command[:3] == ["npx", "skills@1.5.9", "add"]
            assert command[3].endswith("/source")
            for name in ("ui-craft", "drive-local-webapp"):
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
    assert states["ui-craft"] == "missing"
    assert not (tmp_path / ".agents" / "skills" / "ui-craft").exists()


@pytest.mark.parametrize(
    "stdout",
    [
        "230e71a55ab07f0cd9beaa61649b583cb9d1bde1\trefs/tags/v0.3.0\n",
        (
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v0.3.0\n"
            "230e71a55ab07f0cd9beaa61649b583cb9d1bde1"
            "\trefs/tags/v0.3.0^{}\n"
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

    resolved, error = _resolved_skill_release(_manifest()["connor_skills"])

    assert error is None
    assert resolved == "230e71a55ab07f0cd9beaa61649b583cb9d1bde1"
    assert commands == [
        [
            "git",
            "ls-remote",
            "--tags",
            "https://github.com/ConnorGriffin/skills",
            "refs/tags/v0.3.0",
            "refs/tags/v0.3.0^{}",
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
                stdout="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v0.3.0\n",
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
                "230e71a55ab07f0cd9beaa61649b583cb9d1bde1"
                if tag_reads == 1
                else "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            )
            return SimpleNamespace(
                returncode=0,
                stdout=f"{commit}\trefs/tags/v0.3.0\n",
                stderr="",
            )
        return original_run(command, **kwargs)

    monkeypatch.setattr("agentflow.enroll._run_command", run)

    assert main(["enroll", str(tmp_path), "--apply"]) == 1

    output = capsys.readouterr().out
    assert "resolved to aaaaaaaaaa" in output
    assert "rolled back" in output
    assert tag_reads >= 2
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
    drifted = tmp_path / ".agents" / "skills" / "ui-craft" / "SKILL.md"
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
                    "230e71a55ab07f0cd9beaa61649b583cb9d1bde1"
                    "\trefs/tags/v0.3.0\n"
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
    destination = tmp_path / ".agents" / "skills" / "ui-craft"
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
                    "230e71a55ab07f0cd9beaa61649b583cb9d1bde1"
                    "\trefs/tags/v0.3.0\n"
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
    monkeypatch.setattr("agentflow.enroll._tooling_problem", lambda _surfaces: None)
    monkeypatch.setattr(
        "agentflow.enroll._install_methodology_skills",
        lambda _root: "ok:   methodology contracts supplied by focused fixture",
    )
    monkeypatch.setattr("agentflow.prompts.requirements_for", lambda *_args: ())
    monkeypatch.setattr(
        "agentflow.enroll.shutil.which",
        lambda command: f"/usr/bin/{command}",
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

    def fail_after_config_write(root, profile, surfaces, **_):
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

    assert "missing required enrollment command(s): npx" in capsys.readouterr().out
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
        if directory.name in {"ui-craft", "drive-local-webapp", "tdd", "codebase-design", "domain-modeling"}:
            return "ok" if installed else "absent"
        return original_destination_status(directory, manifest)

    def run(command, **kwargs):
        nonlocal active_failure, installed
        commands.append(command)
        if command[:3] == ["git", "ls-remote", "--tags"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\trefs/tags/v0.3.0\n"
                    "230e71a55ab07f0cd9beaa61649b583cb9d1bde1"
                    "\trefs/tags/v0.3.0^{}\n"
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
                    else "230e71a55ab07f0cd9beaa61649b583cb9d1bde1\n"
                ),
                stderr="",
            )
        if command[0] == "npx":
            if active_failure == "npx":
                return SimpleNamespace(returncode=127, stdout="", stderr="missing npx")
            installed = True
            names = (("tdd", "codebase-design", "domain-modeling")
                     if "tdd" in command else ("ui-craft", "drive-local-webapp"))
            for name in names:
                codex = tmp_path / ".agents" / "skills" / name
                codex.mkdir(parents=True, exist_ok=True)
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
        assert "v0.3.0" not in commands[npx_index][3]
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
