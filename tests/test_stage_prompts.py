from agentflow.prompts import requirements_for, stage_prompt_spec
from agentflow.capability_contracts import preflight


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
