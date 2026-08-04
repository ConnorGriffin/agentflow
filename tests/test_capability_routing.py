"""Capability-routed session-led dispatch through agentflow's public seams (#498)."""

import json
import subprocess
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentflow import coordinated_build, coordinated_review, coordinated_revise
from agentflow import runner as runner_mod
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
    # The bans and the named routes are the table's, not prose: an unverified explorer, an
    # inventive prototyper, and an unbenchmarked reviewer are all named as refusals.
    assert "- exploration: bounded Luna → Sonnet → Opus; full-system Sonnet → Opus; never Haiku" \
        in prompt
    assert "- prototyping/UI mockups: Sol → Opus; never Luna" in prompt
    assert "- code review: routine Luna → Sonnet → Opus; load-bearing Opus; never Haiku" in prompt


def test_the_lead_brief_follows_the_shipped_table_rather_than_prose(tmp_path):
    """Editing the table moves what the lead is told — the config is the only source."""
    import agentflow.routing as routing_module
    from agentflow.routing import CapabilityRouting

    source = Path(routing_module.__file__).with_name("model-routing.json")
    data = json.loads(source.read_text())
    data["areas"]["review"]["routes"][1]["ladder"] = ["sonnet"]
    data["areas"]["exploration"]["banned"] = ["haiku", "terra"]
    edited = tmp_path / "routing.json"
    edited.write_text(json.dumps(data))

    brief = CapabilityRouting.from_path(edited).session_lead_instructions("build", "medium")

    assert "load-bearing Sonnet" in brief and "load-bearing Opus" not in brief
    assert "- exploration: bounded Luna → Sonnet → Opus; full-system Sonnet → Opus; " \
        "never Haiku, Terra" in brief


def test_routing_config_is_validated_and_resolves_every_named_model(tmp_path):
    assert routing.provenance.benchmark_date == "2026-08-03"
    assert routing.cli_identifier("claude", "haiku") == "haiku"
    assert routing.cli_identifier("codex", "luna") == "gpt-5.6-luna"
    assert routing.cli_identifier("claude", "fable") == "fable"
    with pytest.raises(RoutingConfigError, match="cannot launch"):
        routing.cli_identifier("claude", "luna")

    # An area nobody benchmarked is refused at load rather than reaching a session lead.
    import agentflow.routing as routing_module
    from agentflow.routing import CapabilityRouting

    source = Path(routing_module.__file__).with_name("model-routing.json")
    data = json.loads(source.read_text())
    data["areas"]["telepathy"] = {"title": "telepathy", "routes": [{"ladder": ["opus"]}],
                                  "banned": []}
    unknown_area = tmp_path / "routing.json"
    unknown_area.write_text(json.dumps(data))
    with pytest.raises(RoutingConfigError, match="routing areas mismatch"):
        CapabilityRouting.from_path(unknown_area)


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
    data["areas"]["implementation"]["routes"][0]["ladder"] = ["missing"]
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


def test_the_lead_brief_tells_the_lead_to_fall_back_to_claude_on_a_codex_provider_failure():
    """Distinct from the verification-failure escalation rule: a `codex exec` worker that fails
    to launch or dies on a provider error must close that ladder's Codex rungs rather than being
    treated as a failed attempt to re-delegate against."""
    brief = routing.session_lead_instructions("build", "medium")

    assert "provider error" in brief
    assert "treat every Codex rung in that" in brief
    assert "unavailable for the rest of this session" in brief
    assert "record the substitution in the final handoff" in brief
    assert "is never a finding to re-delegate" in brief


def test_the_lead_brief_states_codex_is_spent_up_front_when_rate_limited(monkeypatch):
    """When the render-time capacity seam (agentflow.runner.codex_spent_at_render, threaded in by
    the build/revise callers) reports Codex exhausted, the brief says so before the routing table
    so the lead never tries a Codex worker this session."""
    monkeypatch.setenv("AGENTFLOW_CAPACITY_HELPER", "/test/capacity-helper")
    payload = json.dumps({"windows": [
        {"used_percent": 100, "window_minutes": 300, "resets_at": 1234},
    ]})

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)

    brief = routing.session_lead_instructions(
        "build", "medium", codex_spent=runner_mod.codex_spent_at_render())

    assert "Codex is currently unavailable (spent)" in brief
    assert "enter each ladder at its first Claude rung instead" in brief
    assert brief.index("Codex is currently unavailable") < brief.index("Routes (workers enter")


def test_the_lead_brief_names_the_all_codex_ladder_as_a_self_serve_exception_when_spent():
    """plan/spec's ladder (`["terra", "sol"]` in model-routing.json) has no Claude rung, so
    "enter at the first Claude rung" is unsatisfiable there. The brief must call that out by
    name, derived from the loaded ladders rather than hardcoded, and only when Codex is spent."""
    spent = routing.session_lead_instructions("build", "medium", codex_spent=True)
    clear = routing.session_lead_instructions("build", "medium", codex_spent=False)

    assert "plan/spec has no Claude rung to fall back to" in spent
    assert "do that area's work yourself rather than delegating off-table" in spent
    assert "no Claude rung to fall back to" not in clear


def test_the_lead_brief_renders_normally_when_the_capacity_seam_is_unreadable(monkeypatch):
    """Fail-open: a missing or broken capacity helper must never block the ordinary brief."""
    monkeypatch.delenv("AGENTFLOW_CAPACITY_HELPER", raising=False)
    monkeypatch.delenv("AGENTFLOW_TRIAGE_GATE", raising=False)
    monkeypatch.setattr(
        runner_mod.subprocess, "run",
        lambda *args, **kwargs: pytest.fail("no capacity helper should be invoked"))

    brief = routing.session_lead_instructions(
        "build", "medium", codex_spent=runner_mod.codex_spent_at_render())

    assert "Codex is currently unavailable (spent)" not in brief
    assert "Session lead — benchmarked capability routing" in brief


def _stub_codex_spent(monkeypatch):
    """Wire the capacity seam `codex_spent_at_render()` reads so it reports Codex exhausted —
    same fake used by the routing-level tests above, applied here through the daemon-side
    submission builders instead of calling routing directly."""
    monkeypatch.setenv("AGENTFLOW_CAPACITY_HELPER", "/test/capacity-helper")
    payload = json.dumps({"windows": [
        {"used_percent": 100, "window_minutes": 300, "resets_at": 1234},
    ]})
    monkeypatch.setattr(
        runner_mod.subprocess, "run",
        lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 0, payload, ""))


def test_build_submission_carries_the_codex_spent_brief_when_capacity_is_exhausted(
        monkeypatch, tmp_path):
    """Threading proof for coordinated_build.py's `codex_spent=codex_spent_at_render()` call —
    dropping that kwarg back to the routing default leaves this red even though the rest of the
    suite stays green, because build_submission itself never calls the capacity seam directly."""
    _stub_codex_spent(monkeypatch)
    cfg = SimpleNamespace(repo="o/r", workdir=str(tmp_path))

    submission = coordinated_build.build_submission(cfg, _issue())

    assert submission is not None
    assert "Codex is currently unavailable (spent)" in submission.input_ptr


def test_revise_submission_carries_the_codex_spent_brief_when_capacity_is_exhausted(monkeypatch):
    """Threading proof for coordinated_revise.py's `_session_lead_prompt` call — dropping its
    `codex_spent=codex_spent_at_render()` kwarg leaves this red."""
    _stub_codex_spent(monkeypatch)
    review = Record(
        identity="o/r|7|review|sha-1", stage="review", pool="codex", demand=1,
        repo="o/r", subject="7", target="sha-1", builder_lineage="claude",
        builder_complexity="standard", builder_effort="extra",
        source="/home/w/.agentflow/worktrees/codex-review/pr-42-fix-thing",
    )

    submission = coordinated_revise.revise_submission(review, "standard", "- fix it")

    assert submission is not None
    assert "Codex is currently unavailable (spent)" in submission.input_ptr
