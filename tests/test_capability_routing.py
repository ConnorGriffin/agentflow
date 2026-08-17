"""Capability-routed session-led dispatch through agentflow's public seams (#498)."""

import json
import subprocess
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agentflow import coordinated_build, coordinated_review, coordinated_revise
from agentflow import runner as runner_mod
from agentflow.balancer import LeadAvailability, PoolStatus, choose_session_lead, pick_session_lead
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


def test_build_submission_launches_a_low_effort_fable_session_lead(
        make_coord, tmp_path, monkeypatch):
    revision = "1" * 40
    monkeypatch.setattr(coordinated_build, "capture_subject_revision", lambda _root: revision)
    cfg = SimpleNamespace(repo="o/r", workdir=str(tmp_path))
    submission = coordinated_build.build_submission(cfg, _issue())
    coord = make_coord()

    identity = coord.submit_stage(submission)
    record = coord.stage_record(identity)
    command = provider_command(record)
    prompt = command[command.index("-p") + 1]

    assert record.pool == "claude" and record.model == "fable"
    assert record.subject_revision == revision
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


def test_build_submission_activates_slicing_from_its_durable_issue_brief(tmp_path, monkeypatch):
    monkeypatch.setattr(
        coordinated_build, "capture_subject_revision", lambda _root: "1" * 40)
    issue = _issue(complexity="deep")
    issue["body"] = """## Work order
separability: slice-bearing
### Domain facts
- literal
"""
    cfg = SimpleNamespace(repo="o/r", workdir=str(tmp_path))

    submission = coordinated_build.build_submission(cfg, issue)

    assert submission is not None
    assert "first in-session worker" in submission.input_ptr


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


def test_sol_reasoning_floor_names_the_spellings_the_routing_table_resolves():
    """ADR 752 floors the Sol session lead at ``medium``, keyed on Sol by name inside
    ``agentflow/coordinator/profiles.py``. That module cannot read the pair off the routing table —
    ``routing`` already imports it, and the import-cycle gate in ``test_dispatch.py`` fails on the
    ring a deferred import would close — so the internal name and the provider CLI id are restated
    there as a constant. This test is what keeps the copy honest, and it lives here because this is
    where importing ``routing`` is legitimate: it fails the moment the CLI id changes in the table
    without a matching edit in ``profiles.py``.
    """
    from agentflow.coordinator.profiles import _SOL_IDENTITIES

    assert _SOL_IDENTITIES == {"sol", routing.cli_identifier("codex", "sol")}


def test_review_tier_uses_builder_complexity_and_pool_specific_models():
    build = Record(
        identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
        repo="o/r", subject="7", complexity="standard",
        builder_lineage="claude",
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


def test_revise_submission_activates_slicing_when_its_brief_carries_the_work_order():
    review = Record(
        identity="o/r|7|review|sha-1", stage="review", pool="codex", demand=1,
        repo="o/r", subject="7", target="sha-1", builder_lineage="claude",
        builder_complexity="deep", builder_effort="high",
        source="/home/w/.agentflow/worktrees/codex-review/pr-42-fix-thing",
    )
    findings = """## Work order
separability: slice-bearing
### Domain facts
- literal
"""

    submission = coordinated_revise.revise_submission(review, "deep", findings)

    assert submission is not None
    assert "first in-session worker" in submission.input_ptr


def test_rate_card_estimates_from_the_price_snapshot_and_resolves_both_name_forms():
    # Terra: $2.50/$15 per million input/output (provenance.price_snapshot).
    estimate = routing.estimate_cost_usd("terra", input_tokens=300, output_tokens=60)
    assert estimate == pytest.approx(300 * 2.5 / 1_000_000 + 60 * 15 / 1_000_000)
    # The provider/CLI id resolves to the same card entry as the internal name.
    assert routing.estimate_cost_usd("gpt-5.6-terra", input_tokens=300, output_tokens=60) \
        == estimate
    # Codex reports reasoning tokens as a subset of the blended output total, not additive —
    # with output_tokens present, adding reasoning tokens must not change the estimate.
    with_reasoning = routing.estimate_cost_usd(
        "terra", input_tokens=300, output_tokens=60, reasoning_output_tokens=40)
    assert with_reasoning == pytest.approx(estimate)
    # With no output_tokens fact at all, reasoning_output_tokens is the fallback output figure.
    reasoning_only = routing.estimate_cost_usd(
        "terra", input_tokens=300, reasoning_output_tokens=40)
    assert reasoning_only == pytest.approx(300 * 2.5 / 1_000_000 + 40 * 15 / 1_000_000)


def test_rate_card_never_guesses_an_unknown_model_or_a_fully_absent_fact():
    assert routing.estimate_cost_usd("nonexistent-model", input_tokens=100) is None
    assert routing.provider_for("nonexistent-model") is None
    # fable (session-lead) has no rate-card entry — no price snapshot names one.
    assert routing.estimate_cost_usd("fable", input_tokens=100) is None
    # No token fact at all is not the same as zero tokens: it must not be guessed either.
    assert routing.estimate_cost_usd("terra") is None


def test_provider_for_resolves_internal_and_cli_names():
    assert routing.provider_for("terra") == "codex"
    assert routing.provider_for("gpt-5.6-terra") == "codex"
    assert routing.provider_for("opus") == "claude"


def test_rate_card_rejects_a_price_for_an_unknown_model(tmp_path):
    import agentflow.routing as routing_module
    from agentflow.routing import CapabilityRouting, RoutingConfigError

    source = Path(routing_module.__file__).with_name("model-routing.json")
    data = json.loads(source.read_text())
    data["rate_card"]["ghost"] = {"input": 1, "output": 2}
    bad = tmp_path / "routing.json"
    bad.write_text(json.dumps(data))
    with pytest.raises(RoutingConfigError, match="unknown model"):
        CapabilityRouting.from_path(bad)


def test_rate_card_rejects_a_malformed_rate(tmp_path):
    import agentflow.routing as routing_module
    from agentflow.routing import CapabilityRouting, RoutingConfigError

    source = Path(routing_module.__file__).with_name("model-routing.json")
    data = json.loads(source.read_text())
    data["rate_card"]["terra"] = {"input": "cheap", "output": 15}
    bad = tmp_path / "routing.json"
    bad.write_text(json.dumps(data))
    with pytest.raises(RoutingConfigError, match="malformed rate"):
        CapabilityRouting.from_path(bad)


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


def test_session_lead_prefers_claude_and_falls_back_to_codex_when_needed():
    runners = {"claude": "CLAUDE", "codex": "CODEX"}
    claude_blocked = PoolStatus("claude", False, 100.0)
    codex_clear = PoolStatus("codex", True, 5.0)
    assert choose_session_lead(claude_blocked, codex_clear, runners) == ("CODEX", None)

    claude_clear = PoolStatus("claude", True, 90.0)
    codex_blocked = PoolStatus("codex", False, 100.0)
    assert choose_session_lead(claude_clear, codex_blocked, runners) == ("CLAUDE", None)

    assert choose_session_lead(claude_blocked, codex_blocked, runners) == (None, None)


def test_session_lead_uses_the_selected_parent_demand_against_live_permits(monkeypatch):
    clear = PoolStatus("claude", True, 10.0)
    codex = PoolStatus("codex", True, 10.0)
    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, *_args, **_kwargs: clear if tool == "claude" else codex)

    lead, _reviewer, _reason = pick_session_lead(
        operator=True, stage="build", complexity="standard", effort="low",
        availability=LeadAvailability({"claude": 3, "codex": 0},
                                      {"claude": False, "codex": False}))

    assert lead is not None and lead.tool == "codex"  # Fable needs 3; Sol reserves all five


def test_issue_build_yields_a_pool_with_pr_bound_work_but_revise_does_not(monkeypatch):
    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, *_args, **_kwargs: PoolStatus(tool, True, 10.0))
    monkeypatch.setattr("agentflow.balancer._live_lead_availability", lambda: LeadAvailability(
        {"claude": 0, "codex": 0}, {"claude": True, "codex": False}))

    build, _reviewer, reason = pick_session_lead(
        operator=True, floodgates=True, stage="build", complexity="standard", effort="low")
    revise, _reviewer, _reason = pick_session_lead(
        operator=True, stage="revise", complexity="deep", effort=None)

    assert build is not None and build.tool == "codex"
    assert "PR-bound work waiting" not in reason
    assert revise is not None and revise.tool == "claude"


def test_issue_build_fails_closed_when_its_shared_availability_read_fails(monkeypatch):
    """An unreadable PR-bound barrier is never an all-clear cold Build selection."""
    from agentflow.coordinator.store import StoreUnavailable

    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, *_args, **_kwargs: PoolStatus(tool, True, 10.0))
    monkeypatch.setattr("agentflow.balancer.Store",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(StoreUnavailable("read")))

    lead, _reviewer, reason = pick_session_lead(
        operator=True, stage="build", complexity="standard", effort="low")

    assert lead is None and "permit budget" in reason


@pytest.mark.parametrize(("stage", "complexity", "effort"), [
    ("build", "standard", "low"),
    ("revise", "deep", None),
])
def test_session_lead_keeps_claude_when_the_actual_parent_cell_fits(
        monkeypatch, stage, complexity, effort):
    """Build and Revise pass their real dials, not the old exclusive-five defaults."""
    clear = PoolStatus("claude", True, 10.0)
    monkeypatch.setattr("agentflow.balancer._query_pool", lambda *_args, **_kwargs: clear)

    lead, _reviewer, _reason = pick_session_lead(
        operator=True, stage=stage, complexity=complexity, effort=effort,
        availability=LeadAvailability({"claude": 2, "codex": 0},
                                      {"claude": False, "codex": False}))

    assert lead is not None and lead.tool == "claude"  # Fable's actual demand is three.


@pytest.mark.parametrize(("operator", "floodgates", "expected_reservation", "expected_lead"), [
    (False, False, 15.0, "codex"),
    (True, False, 0.0, "claude"),
    (False, True, 0.0, "claude"),
])
def test_session_lead_matches_admission_claude_inflight_reservation(
        monkeypatch, operator, floodgates, expected_reservation, expected_lead):
    """A stale Claude quota reading cannot stamp a record that admission will defer."""
    calls = []

    def query(tool, _operator=False, **kwargs):
        reservation = kwargs.get("reserved_pct", 0.0)
        calls.append((tool, reservation))
        return PoolStatus(tool, tool != "claude" or 71.0 + reservation < 85.0,
                          71.0, ceiling=85.0)

    monkeypatch.setattr("agentflow.balancer._query_pool", query)
    monkeypatch.setattr("agentflow.balancer._live_lead_availability", lambda: LeadAvailability(
        {"claude": 1, "codex": 0}, {"claude": False, "codex": False}))
    monkeypatch.setattr("agentflow.balancer._claude_dispatch_status",
                        lambda status, *_args, **_kwargs: status)
    monkeypatch.setattr("agentflow.balancer._codex_dispatch_status",
                        lambda status, *_args, **_kwargs: status)

    lead, _reviewer, _reason = pick_session_lead(
        operator=operator, floodgates=floodgates, stage="build", complexity="standard",
        effort="low")

    assert lead is not None and lead.tool == expected_lead
    assert calls[0] == ("claude", expected_reservation)


def test_synthetic_historical_replay_keeps_the_new_sol_parent_exclusive(make_coord):
    submission = coordinated_build.build_submission(
        SimpleNamespace(repo="o/r", workdir="/work"), _issue("deep", "low"), parent_pool="codex")
    coord = make_coord()
    record = coord.stage_record(coord.submit_stage(submission))

    assert record.model == "sol" and record.demand == 5


def test_session_parent_and_cheap_review_admission_are_explicit():
    build_demands = {
        "standard": {"low": 3, "medium": 4, "high": 5, "extra": 5},
        "deep": {"low": 4, "medium": 4, "high": 5, "extra": 5},
    }
    for complexity, efforts in build_demands.items():
        # Revise is effort-blind (ADR 0029): one asserted row per complexity answers every dial.
        assert ("revise", "claude", "fable", complexity, None) in ADMISSION_MATRIX
        assert ("revise", "codex", "sol", complexity, None) in ADMISSION_MATRIX
        assert admission_demand("revise", "codex", "sol", complexity) == 5
        for effort, expected in efforts.items():
            key = ("build", "claude", "fable", complexity, effort)
            assert key in ADMISSION_MATRIX
            assert admission_demand(*key) == expected
            assert admission_demand("revise", "claude", "fable", complexity, effort) == 3
            assert admission_demand("build", "codex", "sol", complexity, effort) == 5

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
    assert "treat every rung from that provider" in brief
    assert "unavailable for the rest of this session" in brief
    assert "record the substitution in the final handoff" in brief
    assert "is never a finding to re-delegate" in brief


def test_a_resolvable_single_default_continues_privately_with_its_grounding():
    brief = routing.session_lead_instructions("review", None)

    assert "exactly one materially compatible outcome remains" in brief
    assert "re-read that exact place at decision time" in brief
    assert "confirm the resolved text supports the outcome" in brief
    assert "continue the current stage" in brief
    assert "no public park comment or maintainer notification" in brief
    assert "Outcome:" in brief
    assert "Citation:" in brief
    assert "Resolved text:" in brief


def test_an_unresolvable_default_citation_parks():
    brief = routing.session_lead_instructions("review", None)

    assert "If the citation cannot be resolved or read, park" in brief
    assert "do not infer or reconstruct its contents" in brief


def test_a_citation_whose_text_does_not_support_the_outcome_parks():
    brief = routing.session_lead_instructions("review", None)

    assert "If the resolved text does not support the claimed outcome, park" in brief
    assert "self-asserted grounding is not evidence" in brief


def test_two_materially_compatible_grounded_outcomes_still_park():
    brief = routing.session_lead_instructions("review", None)

    assert "more than one materially compatible outcome remains" in brief
    assert "park even when every outcome has valid grounding" in brief


def test_a_load_bearing_policy_choice_parks_even_with_a_perfect_citation():
    brief = routing.session_lead_instructions("review", None)

    assert "product intent, safety, security, permissions, or another load-bearing policy" in brief
    assert "park even with a perfect citation" in brief


def test_genuinely_unresolved_maintainer_intent_keeps_the_existing_park():
    brief = routing.session_lead_instructions("review", None)

    assert "genuinely unresolved maintainer intent" in brief
    assert "existing two-option public decision handoff" in brief


@pytest.mark.parametrize("task_brief", [
    "Implement the scoped issue through the existing interface.",
    "## Work order\nseparability: declined\n### Why indivisible\n- one atomic invariant",
], ids=["no-work-order", "declined-work-order"])
def test_a_brief_without_a_slice_bearing_work_order_keeps_the_existing_lead_contract_byte_identical(
        task_brief):
    ordinary = routing.session_lead_instructions("build", "medium")

    rendered = routing.session_lead_instructions(
        "build", "medium", brief=task_brief)

    assert rendered == ordinary
    assert "Slicer" not in rendered
    assert ("Fable is lead-only and is never a delegate target.\n\n"
            "Before parking for a decision") in rendered
    assert ("Resolved text: <the text re-read from that place>\n\n"
            "worker reasoning rung:") in rendered


def test_a_slice_bearing_work_order_makes_the_lead_slice_first_and_commit_each_slice():
    work_order = """## Work order
separability: slice-bearing
### Domain facts
- the durable fact is literal
### Fixtures
- fixture_one
### Named invariant tests
- test_invariant
"""

    brief = routing.session_lead_instructions("build", "medium", brief=work_order)

    assert "first in-session worker" in brief
    assert "Slicer" in brief
    assert "file-level slice list" in brief
    assert "repository as it stands at pickup" in brief
    assert "ordinary benchmarked capability ladder" in brief
    assert "commit once per finished slice" in brief
    assert "verify each slice" in brief


def test_slice_workers_are_sealed_for_deciding_and_open_for_reading():
    work_order = "## Work order\nseparability: slice-bearing\n"

    brief = routing.session_lead_instructions("build", "medium", brief=work_order)

    assert "no unnamed domain fact or scope choice" in brief
    assert "read the repository freely" in brief
    assert "match house style" in brief
    assert "allow-list is a grounding floor, not a reading ceiling" in brief


def test_the_lead_brief_stops_and_surfaces_a_provider_failure_with_no_opposite_rung():
    """#509 Blocker B: a single-provider ladder (plan/spec is Codex-only; code review's
    load-bearing route is Claude-only in model-routing.json) has no remaining-provider rung to
    re-enter at, so the brief must tell the lead to stop and hand back the failure by name
    rather than inventing a substitute model or silently doing the work itself."""
    assert routing._areas["plan"].routes[0].ladder == ("terra", "sol")
    assert all(routing._models[m]["provider"] == "codex"
               for m in routing._areas["plan"].routes[0].ladder)
    load_bearing = next(r for r in routing._areas["review"].routes if r.when == "load-bearing")
    assert all(routing._models[m]["provider"] == "claude" for m in load_bearing.ladder)

    brief = routing.session_lead_instructions("build", "medium")

    assert "has no rung from any other provider" in brief
    assert "stop delegating in that area and hand back the" in brief
    assert "provider failure by name in the final handoff" in brief
    assert "do not invent a substitute" in brief
    assert "do not silently do the work yourself" in brief


def test_codex_parent_uses_the_bounded_worker_command_and_installed_claude_cli():
    brief = routing.session_lead_instructions("build", "medium", parent_provider="codex")

    assert "agentflow-codex-worker" in brief
    assert "--effort medium" in brief
    assert "--timeout 900" in brief
    assert ("mktemp" in brief and "chmod 600" in brief
            and "< \"<absolute-private-prompt-file>\"" in brief)
    assert "submit exactly that bounded command with `sandbox_permissions=require_escalated`" in brief
    assert ("/bin/zsh -lc 'agentflow-codex-worker --worker <routed-allowlisted-name> "
            "--effort medium --timeout 900 < \"<absolute-private-prompt-file>\"'" in brief)
    assert "installed `claude` CLI" in brief
    assert "Provider launch identifiers: Fable (claude): fable" in brief
    assert "Sol (codex): gpt-5.6-sol" in brief
    assert "Never use `spawn_agent`" in brief
    assert "agent_type" in brief
    assert "provider error" in brief and "first remaining-provider\nrung" in brief


def test_codex_parent_polls_a_yielded_worker_command_without_relaunching_it():
    brief = routing.session_lead_instructions("build", "medium", parent_provider="codex")

    assert "yielded agentflow-codex-worker command" in brief
    assert "poll that exact handle until terminal" in brief
    assert "never relaunch it while active" in brief


def test_extra_effort_reaches_the_worker_as_extra_then_maps_to_xhigh(tmp_path):
    from agentflow import codex_worker

    brief = routing.session_lead_instructions("build", "extra", parent_provider="codex")
    assert "--effort extra" in brief
    argv = codex_worker.worker_argv("terra", "extra")
    assert "model_reasoning_effort=xhigh" in argv


def test_codex_parent_never_depends_on_native_helper_capability():
    """#509 Blocker A: an installed Codex build outside the 0.144.0 compatibility allowlist must
    never be told to call `spawn_agent` with the hidden role/model fields — the brief tells the
    lead to treat every Codex rung as a provider failure from the start instead."""
    brief = routing.session_lead_instructions("build", "medium", parent_provider="codex")

    assert "agentflow-codex-worker" in brief
    assert "native sub-agents" not in brief


def test_claude_parent_keeps_codex_on_the_opposite_provider_cli_boundary():
    brief = routing.session_lead_instructions("build", "medium", parent_provider="claude")

    assert "`codex exec`" in brief
    assert "Never use `spawn_agent`" in brief


def test_codex_parent_submission_keeps_its_parent_and_branch_lineage(make_coord, tmp_path):
    cfg = SimpleNamespace(repo="o/r", workdir=str(tmp_path))
    submission = coordinated_build.build_submission(cfg, _issue(), parent_pool="codex")
    coord = make_coord()
    record = coord.stage_record(coord.submit_stage(submission))

    assert record.pool == record.builder_lineage == "codex"
    assert record.model == "sol"
    assert submission.pool == submission.builder_lineage == submission.branch_lineage == "codex"
    assert "/codex/issue-498-" in submission.source
    assert "agentflow-codex-worker" in submission.input_ptr


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


def test_the_lead_brief_names_the_all_codex_ladder_as_a_provider_failure_handback_when_spent():
    """plan/spec's ladder (`["terra", "sol"]` in model-routing.json) has no Claude rung, so
    "enter at the first Claude rung" is unsatisfiable there. The brief must call that out by
    name, derived from the loaded ladders rather than hardcoded, and only when Codex is spent —
    and it must route to the same named provider-failure handback as every other exhausted
    ladder, never tell the lead to do the work itself (that contradicted the handback rule
    stated later in the same brief)."""
    spent = routing.session_lead_instructions("build", "medium", codex_spent=True)
    clear = routing.session_lead_instructions("build", "medium", codex_spent=False)

    assert "plan/spec has no Claude rung to fall back to" in spent
    assert "no remaining provider from the very start of this session" in spent
    assert "hand back the provider failure by name in the final handoff" in spent
    assert "do that area's work yourself" not in spent
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


@pytest.mark.parametrize(("parent_provider", "unavailable", "provider"), [
    ("codex", frozenset({"claude"}), "Claude"),
    ("claude", frozenset({"codex"}), "Codex"),
])
def test_the_lead_brief_excludes_a_durably_paused_provider_for_the_entire_session(
        parent_provider, unavailable, provider):
    """A pause snapshot constrains workers even when its provider owns the session lead."""
    brief = routing.session_lead_instructions(
        "build", "medium", parent_provider=parent_provider,
        unavailable_providers=unavailable)

    assert f"{provider} is currently unavailable (pool paused)" in brief
    assert f"skip every {provider} rung in every ladder" in brief
    assert "hand back the provider failure by name in the final handoff" in brief


def test_the_lead_brief_is_unchanged_for_an_empty_unavailable_provider_snapshot():
    ordinary = routing.session_lead_instructions("build", "medium")
    snapshotted = routing.session_lead_instructions(
        "build", "medium", unavailable_providers=frozenset())

    assert snapshotted == ordinary


def test_build_submission_carries_the_durable_paused_provider_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(coordinated_build, "pool_paused", lambda pool: pool == "codex",
                        raising=False)
    cfg = SimpleNamespace(repo="o/r", workdir=str(tmp_path))

    submission = coordinated_build.build_submission(cfg, _issue())

    assert submission is not None
    assert "Codex is currently unavailable (pool paused)" in submission.input_ptr


def test_shared_revise_prompt_carries_the_durable_paused_provider_snapshot(monkeypatch):
    """Every revise variant reaches the shared prompt helper before it becomes a submission."""
    monkeypatch.setattr(coordinated_revise, "pool_paused", lambda pool: pool == "claude",
                        raising=False)

    brief = coordinated_revise._session_lead_prompt("prompt", "medium", "codex")

    assert "Claude is currently unavailable (pool paused)" in brief
