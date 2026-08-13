import pytest

from agentflow.prompts import requirements_for, stage_prompt_spec
from agentflow.capability_contracts import ContractRequirement, preflight


def test_build_prompt_and_requirements_share_one_skill_authority():
    spec = stage_prompt_spec("build")

    assert "/tdd" in spec.render(repo="o/r", n=1, title="x", body="", effort="low", surfaces="none")
    assert {requirement.id for requirement in requirements_for("build", {"ui": False})} >= {
        "tdd", "codebase-design", "domain-modeling"
    }


def test_ui_requirement_is_conditional_and_transitive():
    plain = {requirement.id for requirement in requirements_for("build", {"ui": False})}
    ui = {requirement.id for requirement in requirements_for("build", {"ui": True})}

    assert "ui-craft" not in plain
    assert {"ui-craft", "drive-local-webapp", "playwright"} <= ui


def test_preflight_names_missing_project_local_contract_and_repair(tmp_path):
    result = preflight(tmp_path, "build", "codex", requirements_for("build", {"ui": False}))

    assert result.state == "missing"
    assert not result.ready
    assert "agentflow enroll" in result.repair_command


@pytest.mark.parametrize("runtime_state", ("missing", "drifted"))
def test_runtime_preflight_reuses_trusted_static_inspection_and_fails_closed(
    tmp_path, monkeypatch, runtime_state
):
    calls = []

    def inspect(root, *, version, node_minimum, manifest=None):
        calls.append((root, version, node_minimum, manifest))
        return runtime_state, f"pinned browser runtime is {runtime_state}"

    monkeypatch.setattr("agentflow.enroll.playwright_runtime_status", inspect)
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/provider")

    result = preflight(
        tmp_path,
        "build",
        "codex",
        (ContractRequirement("playwright", "1.61.1", runtime=True),),
    )

    assert result.state == runtime_state
    assert result.ready is False
    assert calls and calls[0][0] == tmp_path


def test_runtime_preflight_rejects_a_requirement_outside_the_manifest_pin(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/provider")

    result = preflight(
        tmp_path,
        "build",
        "codex",
        (ContractRequirement("playwright", "9.9.9", runtime=True),),
    )

    assert result.state == "incompatible"
    assert "manifest pins" in result.evidence[0]
