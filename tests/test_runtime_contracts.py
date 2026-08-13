from __future__ import annotations

import hashlib
import json

import pytest

from agentflow.runtime_contracts import playwright_runtime_status


def _runtime(tmp_path, monkeypatch, provider="codex"):
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
    manifest = {"capabilities": [
        {"id": "screenshot-harness", "sha256": hashlib.sha256(harness.read_bytes()).hexdigest()},
        {"id": "drive-local-webapp", "skill": "drive-local-webapp", "files": [
            {"path": "SKILL.md", "sha256": hashlib.sha256(skill_file.read_bytes()).hexdigest()}
        ]},
    ]}
    monkeypatch.setattr("agentflow.runtime_contracts.shutil.which", lambda _name: "/bin/node")
    monkeypatch.setattr(
        "agentflow.runtime_contracts._run_command",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 0, "stdout": "v22.0.0"})(),
    )
    return harness, manifest


@pytest.mark.parametrize("provider,other", (("codex", ".claude"), ("claude", ".agents")))
def test_selected_provider_ui_runtime_does_not_require_the_other_installation(
    tmp_path, monkeypatch, provider, other
):
    _harness, manifest = _runtime(tmp_path, monkeypatch, provider)
    assert not (tmp_path / other).exists()

    status, _detail = playwright_runtime_status(
        tmp_path, version="1.61.1", node_minimum=18, manifest=manifest, provider=provider
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
        tmp_path, version="1.61.1", node_minimum=18, manifest=manifest, provider="codex"
    )

    assert status == "incompatible"
    assert "symlinked" in detail
