import ast
import json
from pathlib import Path

import pytest

from agentflow.coordinator.providers import (
    PROVIDER_INPUT_V1, provider_command, split_terminal_session_lead_contract)
from agentflow.coordinator.record import Record
from agentflow.coordinator.tracer import ENABLED_STAGES
from agentflow.effective_policy import (
    ApplicabilityFacts, BriefingAuthority, BriefingReceipt, ReadyBriefing, _finish)
from agentflow.prompts import STAGE_PROMPTS, requirements_for, stage_prompt_spec
from agentflow.capability_contracts import ContractRequirement, preflight
from agentflow.routing import routing


ROOT = Path(__file__).parents[1]


def _approved_briefing(stage: str, receipt_id: str = "receipt-1") -> ReadyBriefing:
    revision = "a" * 40
    authority = BriefingAuthority(
        "github", "octo/repo", "pulls/1/files/policy.json", revision, "sha256", "b" * 64,
        "fleet-policy/0-to-1", "approval-1", revision, "b" * 64, "fleet-policy/0-to-1",
        "github-authority", "v1", "verified")
    receipt = BriefingReceipt(receipt_id, "candidate-1", "approval-1", 1, True, authority)
    applicability = ApplicabilityFacts("fleet-policy/0-to-1", stage, revision)
    value = {
        "applicability": applicability.value(), "briefing_digest": "", "briefing_id": "",
        "capabilities": [], "policy_version": 1, "receipts": [receipt.value()],
        "repository": "octo/repo", "schema": "briefing-v1", "stage": stage,
        "status": "ready", "subject_revision": revision,
    }
    digest, identity, _ = _finish(value)
    return ReadyBriefing("octo/repo", stage, revision, digest, identity, 1, (receipt,), (),
                         applicability)


@pytest.mark.parametrize("enveloped", (False, True), ids=("raw", "provider-envelope"))
def test_stale_approved_briefing_is_replaced_in_place(enveloped):
    task_brief = "Keep this durable task byte-for-byte.\n"
    contract = routing.session_lead_instructions("build", "low", parent_provider="claude")
    spec = stage_prompt_spec("build")
    stale = spec.with_briefing(task_brief + contract, _approved_briefing("build", "stale"))
    current = _approved_briefing("build", "current")
    original = {
        "format": PROVIDER_INPUT_V1,
        "prompt": stale,
        "snapshot": {"body": "exact durable bytes", "number": 7},
        "source_ref": "abc123",
    }
    durable = json.dumps(original, sort_keys=True) if enveloped else stale

    updated = spec.with_briefing(durable, current)

    payload = json.loads(updated) if enveloped else {"prompt": updated}
    task, terminal_contract = split_terminal_session_lead_contract(payload["prompt"])
    assert task_brief in task
    assert terminal_contract == contract
    assert payload["prompt"].count("<!-- agentflow-effective-briefing:") == 1
    assert current.briefing_id in payload["prompt"]
    assert _approved_briefing("build", "stale").briefing_id not in payload["prompt"]
    if enveloped:
        assert {key: payload[key] for key in ("format", "snapshot", "source_ref")} == {
            key: original[key] for key in ("format", "snapshot", "source_ref")}


def test_approved_briefing_is_composed_inside_the_provider_input_envelope():
    task_brief = "Implement the durable task exactly.\n"
    contract = routing.session_lead_instructions("build", "low", parent_provider="claude")
    original = {
        "format": PROVIDER_INPUT_V1,
        "prompt": task_brief + contract,
        "snapshot": {"body": "exact durable bytes", "number": 7},
        "source_ref": "abc123",
    }

    composed = stage_prompt_spec("build").with_briefing(
        json.dumps(original, sort_keys=True), _approved_briefing("build"))
    payload = json.loads(composed)
    brief, terminal_contract = split_terminal_session_lead_contract(payload["prompt"])

    assert {key: payload[key] for key in ("format", "snapshot", "source_ref")} == {
        key: original[key] for key in ("format", "snapshot", "source_ref")}
    assert composed == json.dumps(payload, sort_keys=True)
    assert brief.count("<!-- agentflow-effective-briefing:") == 1
    assert terminal_contract == contract


def test_provider_input_envelope_briefing_is_idempotent():
    envelope = json.dumps({
        "format": PROVIDER_INPUT_V1,
        "prompt": "Review the durable change.",
        "snapshot": {"number": 7},
        "source_ref": "abc123",
    }, sort_keys=True)
    spec = stage_prompt_spec("build")
    first = spec.with_briefing(envelope, _approved_briefing("build"))

    assert spec.with_briefing(first, _approved_briefing("build")) == first
    assert json.loads(first)["prompt"].count("<!-- agentflow-effective-briefing:") == 1


def test_multiple_approved_briefing_blocks_are_refused_as_ambiguous():
    spec = stage_prompt_spec("build")
    current = _approved_briefing("build", "current")
    first = spec.with_briefing("Review the durable change.", current)
    second = spec.with_briefing("", _approved_briefing("build", "receipt-2"))

    with pytest.raises(ValueError, match="ambiguous or untrustworthy briefing"):
        spec.with_briefing(first + second, current)


def test_unapproved_or_stage_mismatched_briefing_is_refused():
    spec = stage_prompt_spec("build")

    with pytest.raises(ValueError, match="not an approved advisory authority"):
        spec.with_briefing("Review the durable change.", object())
    with pytest.raises(ValueError, match="stage does not match prompt"):
        spec.with_briefing("Review the durable change.", _approved_briefing("review"))


@pytest.mark.parametrize("stored_tail", [False, True], ids=["new", "durable-tail"])
def test_approved_briefing_keeps_a_session_lead_contract_terminal_at_provider_launch(stored_tail):
    task_brief = "Implement the durable task exactly.\n"
    contract = routing.session_lead_instructions("build", "low", parent_provider="claude")
    prompt = stage_prompt_spec("build").with_briefing(task_brief + contract,
                                                        _approved_briefing("build"))
    brief, refreshed_contract = split_terminal_session_lead_contract(prompt)
    if stored_tail:
        prompt = task_brief + refreshed_contract + brief.removeprefix(task_brief)
    record = Record("session-lead", "build", "claude", 1, model="fable", source="/wt",
                    input_ptr=prompt, session_lead=True, effort="low")

    command = provider_command(record)
    launched_prompt = command[command.index("-p") + 1]

    assert brief.startswith(task_brief)
    assert brief.count("<!-- agentflow-effective-briefing:") == 1
    assert brief.count("receipt-1") == 1
    assert refreshed_contract == contract
    assert launched_prompt.endswith(routing.session_lead_instructions(
        "build", "low", parent_provider="claude"))
    assert launched_prompt.count("<!-- agentflow-effective-briefing:") == 1
    assert task_brief in launched_prompt


def test_slice_bearing_lead_contract_stays_singular_and_survives_launch_refresh():
    task_brief = """Implement the durable task.

## Work order
separability: slice-bearing
### Domain facts
- literal
"""
    contract = routing.session_lead_instructions(
        "build", "low", parent_provider="claude", brief=task_brief)
    record = Record(
        "session-lead", "build", "claude", 1, model="fable", source="/wt",
        input_ptr=task_brief + contract, session_lead=True, effort="low")

    command = provider_command(record)
    launched_prompt = command[command.index("-p") + 1]
    launched_brief, launched_contract = split_terminal_session_lead_contract(launched_prompt)

    assert task_brief in launched_brief
    assert "first in-session worker" in launched_contract
    assert launched_prompt.count("## Session lead — benchmarked capability routing") == 1


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

    review_plain = {item.id for item in requirements_for("review", {"ui": False})}
    review_ui = {item.id for item in requirements_for("review", {"ui": True})}
    assert review_plain == set()
    assert review_ui == {"ui-craft", "drive-local-webapp", "playwright"}


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

    def inspect(
        root, *, version, node_minimum, manifest=None, provider,
        allow_harness_drift=False,
    ):
        calls.append((
            root, version, node_minimum, manifest, provider, allow_harness_drift,
        ))
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
    assert result.ready_fact is None
    assert calls and calls[0][0] == tmp_path
    assert calls[0][4] == "codex"
    assert calls[0][5] is False


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
    assert result.ready_fact is None
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
