from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest

from agentflow.capability_contracts import ContractRequirement, preflight
from agentflow.runtime_contracts import playwright_runtime_status


def _runtime(tmp_path, monkeypatch, provider="codex", *, runnable=True):
    harness = tmp_path / "scripts" / "screenshots.mjs"
    harness.parent.mkdir(exist_ok=True)
    harness.write_text("trusted harness\n")
    skill = tmp_path / (".agents" if provider == "codex" else ".claude") / "skills" / "drive-local-webapp"
    skill.mkdir(parents=True)
    skill_file = skill / "SKILL.md"
    skill_file.write_text("trusted skill\n")
    package = skill / "node_modules" / "playwright" / "package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": "1.61.1"}))
    if runnable:
        (package.parent / "lib").mkdir()
        (package.parent / "cli.js").write_text('require("./lib/program")\n')
        (package.parent / "lib" / "program.js").write_text("module.exports = {}\n")
        (package.parent.parent / ".bin").mkdir()
        (package.parent.parent / ".bin" / "playwright").symlink_to("../playwright/cli.js")
    manifest = {"capabilities": [
        {"id": "screenshot-harness", "sha256": hashlib.sha256(harness.read_bytes()).hexdigest()},
        {"id": "drive-local-webapp", "skill": "drive-local-webapp", "files": [
            {"path": "SKILL.md", "sha256": hashlib.sha256(skill_file.read_bytes()).hexdigest()}
        ]},
    ], "playwright": {"version": "1.61.1", "node_minimum": 18}}
    monkeypatch.setattr("agentflow.runtime_contracts.shutil.which", lambda _name: "/bin/node")
    monkeypatch.setattr(
        "agentflow.runtime_contracts._run_command",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0, "stdout": "v22.0.0"})(),
    )
    return harness, manifest


def test_playwright_metadata_without_local_launcher_is_not_ready(tmp_path, monkeypatch):
    _harness, manifest = _runtime(tmp_path, monkeypatch, runnable=False)

    status, _detail = playwright_runtime_status(
        tmp_path, version="1.61.1", node_minimum=18, manifest=manifest, provider="codex",
    )

    assert status == "missing"


@pytest.mark.parametrize("escape", ("runtime", "launcher-directory", "extra-link"))
def test_playwright_runtime_tree_must_be_provider_local(tmp_path, monkeypatch, escape):
    _harness, manifest = _runtime(tmp_path, monkeypatch)
    runtime = tmp_path / ".agents" / "skills" / "drive-local-webapp" / "node_modules"
    if escape == "runtime":
        external = tmp_path / "external-runtime"
        runtime.rename(external)
        runtime.symlink_to(external, target_is_directory=True)
    elif escape == "launcher-directory":
        launcher_directory = runtime / ".bin"
        external = tmp_path / "external-bin"
        launcher_directory.rename(external)
        launcher = external / "playwright"
        launcher.unlink()
        launcher.symlink_to(
            "../.agents/skills/drive-local-webapp/node_modules/playwright/cli.js"
        )
        launcher_directory.symlink_to(external, target_is_directory=True)
    else:
        outside = tmp_path.parent / f"{tmp_path.name}-outside.js"
        outside.write_text("external\n")
        (runtime / "playwright" / "extra.js").symlink_to(outside)

    status, _detail = playwright_runtime_status(
        tmp_path, version="1.61.1", node_minimum=18, manifest=manifest, provider="codex",
    )

    assert status == "incompatible"
    if escape == "extra-link":
        resource = SimpleNamespace()
        resource.joinpath = lambda _name: resource
        resource.read_bytes = lambda: b"runtime fixture"
        monkeypatch.setattr("agentflow.capability_contracts.files", lambda _package: resource)
        monkeypatch.setattr("agentflow.capability_contracts.tomllib.loads", lambda _text: manifest)
        monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/codex")
        admission = preflight(
            tmp_path, "build", "codex",
            (ContractRequirement("playwright", "1.61.1", runtime=True),),
        )
        assert admission.ready is False


@pytest.mark.parametrize("provider,other", (("codex", ".claude"), ("claude", ".agents")))
def test_selected_provider_ui_runtime_does_not_require_the_other_installation(
    tmp_path, monkeypatch, provider, other
):
    _harness, manifest = _runtime(tmp_path, monkeypatch, provider)
    assert not (tmp_path / other).exists()

    status, _detail = playwright_runtime_status(
        tmp_path, version="1.61.1", node_minimum=18, manifest=manifest, provider=provider,
    )

    assert status == "ok"


@pytest.mark.parametrize("target", ("external", "internal"))
def test_screenshot_harness_manifest_file_must_not_be_a_symlink(
    tmp_path, monkeypatch, target
):
    harness, manifest = _runtime(tmp_path, monkeypatch)
    content = harness.read_bytes()
    harness.unlink()
    destination = (
        tmp_path / "outside.mjs"
        if target == "internal"
        else tmp_path.parent / f"{tmp_path.name}-outside.mjs"
    )
    destination.write_bytes(content)
    harness.symlink_to(destination)

    status, detail = playwright_runtime_status(
        tmp_path, version="1.61.1", node_minimum=18, manifest=manifest, provider="codex",
    )

    assert status == "incompatible"
    assert "symlinked" in detail
