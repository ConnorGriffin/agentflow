import ast
from pathlib import Path

import pytest

from agentflow.coordinator.tracer import ENABLED_STAGES
from agentflow.prompts import STAGE_PROMPTS, requirements_for, stage_prompt_spec
from agentflow.capability_contracts import ContractRequirement, preflight


ROOT = Path(__file__).parents[1]


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


def test_direct_invocations_and_transitive_dependencies_remain_distinct_and_ordered():
    direct = stage_prompt_spec("build").invocations

    assert [item.requirement.id for item in direct] == [
        "tdd", "codebase-design", "ui-craft"
    ]
    assert [item.requirement.id for item in direct if item.condition == "ui"] == ["ui-craft"]
    assert [item.id for item in requirements_for("build", {"ui": True})] == [
        "tdd", "codebase-design", "domain-modeling",
        "ui-craft", "drive-local-webapp", "playwright",
    ]


def test_every_dispatchable_stage_invokes_a_structured_prompt_spec():
    """Inventory production prompt invocations, not a hand-maintained doctor-only list."""
    invoked = set()
    for path in sorted((ROOT / "agentflow").glob("coordinated_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            owner = node.func.value
            if node.func.attr != "render" or not isinstance(owner, ast.Call):
                continue
            if not isinstance(owner.func, ast.Name) or owner.func.id != "stage_prompt_spec":
                continue
            if owner.args and isinstance(owner.args[0], ast.Constant):
                invoked.add(owner.args[0].value)

    assert set(STAGE_PROMPTS) == set(ENABLED_STAGES)
    assert invoked == set(ENABLED_STAGES)


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

    monkeypatch.setattr("agentflow.capability_contracts.playwright_runtime_status", inspect)
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


@pytest.mark.parametrize(
    "requirement",
    (
        ContractRequirement("tdd", "v9.9.9"),
        ContractRequirement("codebase-design", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"),
    ),
)
def test_methodology_preflight_fails_closed_for_release_or_dependency_incompatibility(
    tmp_path, monkeypatch, requirement
):
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/provider")
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status",
        lambda *_args: ("ok", "provider discovery contract intact"),
    )

    result = preflight(tmp_path, "build", "codex", (requirement,))

    assert result.state == "incompatible"
