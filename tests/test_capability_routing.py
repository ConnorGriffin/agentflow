"""Capability-routed session-led dispatch through agentflow's public seams (#498)."""

import json
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentflow import coordinated_build, coordinated_review, coordinated_revise
from agentflow.balancer import PoolStatus, choose_session_lead
from agentflow.coordinator.providers import provider_command
from agentflow.coordinator.admission import ADMISSION_MATRIX, admission_demand
from agentflow.coordinator.record import Record
from agentflow.routing import RoutingConfigError, routing


def _issue(complexity="standard", effort="high"):
    return {
        "number": 498,
        "title": "Route fleet work",
        "body": "Use the benchmarked capability table.",
        "labels": [
            {"name": "ready-for-agent"},
            {"name": f"agentflow:complexity:{complexity}"},
            {"name": f"agentflow:effort:{effort}"},
        ],
    }


def test_build_submission_launches_a_low_effort_fable_session_lead(make_coord, tmp_path):
    cfg = SimpleNamespace(repo="o/r", workdir=str(tmp_path))
    submission = coordinated_build.build_submission(cfg, _issue())
    coord = make_coord()

    identity = coord.submit_stage(submission)
    record = coord.stage_record(identity)
    command = provider_command(record)
    prompt = command[command.index("-p") + 1]

    assert record.pool == "claude" and record.model == "fable"
    assert command[command.index("--model") + 1] == "fable"
    assert command[command.index("--effort") + 1] == "low"
    assert "Session lead" in prompt
    assert "Do not write the implementation directly" in prompt
    assert "Terra → Sonnet → Opus" in prompt
    assert "Luna (codex): gpt-5.6-luna" in prompt
    assert "Haiku (claude): haiku" in prompt
    assert "worker reasoning rung: high" in prompt
    assert "run the repository test gate" in prompt
    assert "second failure" in prompt and "ladder top" in prompt


def test_routing_config_is_validated_and_resolves_every_named_model():
    assert routing.provenance.benchmark_date == "2026-08-03"
    assert routing.route("implementation").ladder == ("terra", "sonnet", "opus")
    assert routing.route("exploration", variant="full-system").model == "sonnet"
    assert routing.route("review", variant="load-bearing").ladder == ("opus",)
    assert routing.cli_identifier("claude", "haiku") == "haiku"
    assert routing.cli_identifier("codex", "luna") == "gpt-5.6-luna"
    assert routing.cli_identifier("claude", "fable") == "fable"
    with pytest.raises(RoutingConfigError, match="unknown routing area"):
        routing.route("not-an-area")
    with pytest.raises(RoutingConfigError, match="cannot launch"):
        routing.cli_identifier("claude", "luna")


def test_review_tier_uses_builder_complexity_and_pool_specific_models():
    build = Record(
        identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
        repo="o/r", subject="7", complexity="standard",
        source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing",
    )
    standard = coordinated_review.review_submission(build, "sha-1", "codex", 42)
    assert standard is not None and standard.complexity == "standard"

    # The coordinator resolves the cheap reviewer on the selected independent side.
    assert routing.model_for_stage("review", "codex", standard.complexity,
                                   standard.builder_complexity) == "luna"
    assert routing.model_for_stage("review", "claude", standard.complexity,
                                   standard.builder_complexity) == "sonnet"
    assert routing.model_for_stage("review", "codex", "deep", "deep") == "sol"
    assert routing.model_for_stage("review", "claude", "deep", "deep") == "opus"
    assert routing.model_for_stage("review", "codex", "standard", "standard") != "haiku"


def test_revise_and_re_review_keep_the_parent_and_original_tier(make_coord):
    review = Record(
        identity="o/r|7|review|sha-1", stage="review", pool="codex", demand=1,
        repo="o/r", subject="7", target="sha-1", builder_lineage="claude",
        builder_complexity="standard", builder_effort="extra",
        source="/home/w/.agentflow/worktrees/codex-review/pr-42-fix-thing",
    )
    submission = coordinated_revise.revise_submission(review, "standard", "- fix it")
    assert submission is not None
    coord = make_coord()
    identity = coord.submit_stage(replace(submission, transfer_from=None))
    record = coord.stage_record(identity)
    command = provider_command(record)

    assert record.pool == "claude" and record.model == "fable"
    assert command[command.index("--effort") + 1] == "low"
    assert "worker reasoning rung: xhigh" in command[command.index("-p") + 1]

    re_review = coordinated_review.review_submission(record, "sha-2", "codex", 42)
    assert re_review is not None and re_review.complexity == "standard"
    re_review_record = coord.submit_stage(replace(re_review, transfer_from=None))
    assert coord.stage_record(re_review_record).model == "luna"


def test_loader_rejects_unknown_models(tmp_path):
    import agentflow.routing as routing_module

    bad = tmp_path / "routing.json"
    source = Path(routing_module.__file__).with_name("model-routing.json")
    data = json.loads(source.read_text())
    data["areas"]["implementation"]["routes"]["default"] = ["missing"]
    bad.write_text(json.dumps(data))
    from agentflow.routing import CapabilityRouting

    with pytest.raises(RoutingConfigError, match="unknown model"):
        CapabilityRouting.from_path(bad)


def test_session_lead_launch_is_claude_gated_even_when_codex_is_clear():
    runners = {"claude": "CLAUDE", "codex": "CODEX"}
    claude_blocked = PoolStatus("claude", False, 100.0)
    codex_clear = PoolStatus("codex", True, 5.0)
    assert choose_session_lead(claude_blocked, codex_clear, runners) == (None, None)

    claude_clear = PoolStatus("claude", True, 90.0)
    codex_blocked = PoolStatus("codex", False, 100.0)
    assert choose_session_lead(claude_clear, codex_blocked, runners) == ("CLAUDE", None)


def test_session_parent_and_cheap_review_admission_are_explicit():
    build_demands = {
        "standard": {"low": 3, "medium": 4, "high": 5, "extra": 5},
        "deep": {"low": 4, "medium": 4, "high": 5, "extra": 5},
    }
    for complexity, efforts in build_demands.items():
        # Revise is effort-blind (ADR 0029): one asserted row per complexity answers every dial.
        assert ("revise", "claude", "fable", complexity, None) in ADMISSION_MATRIX
        for effort, expected in efforts.items():
            key = ("build", "claude", "fable", complexity, effort)
            assert key in ADMISSION_MATRIX
            assert admission_demand(*key) == expected
            assert admission_demand("revise", "claude", "fable", complexity, effort) == 3

    assert admission_demand("review", "codex", "luna", "standard") <= \
        admission_demand("review", "codex", "sol", "deep")
    assert admission_demand("review", "claude", "sonnet", "standard") <= \
        admission_demand("review", "claude", "opus", "deep")
