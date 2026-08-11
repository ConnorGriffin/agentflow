"""The two provider adapters extract facts; they never decide policy (ADR 0030).

Claude fixtures preserve every supported structured cause plus unrecognized fields; Codex
fixtures establish a cause only from typed account/rate-limit facts, and an untyped failure
— even a non-zero exit — stays a bounded ``unknown`` interruption because `codex exec`
prose is never a diagnosis.
"""

from __future__ import annotations

import json
import os

import pytest

from agentflow.coordinator.providers import (
    PROVIDER_INPUT_V1, ClaudeProviderAdapter, EndingReason, ProviderCause, classify_claude,
    classify_codex)
from agentflow.coordinator.record import Record
from agentflow.coordinator.telemetry import AttemptTelemetry, ModelCost, record_attempt, telemetry_dir
from agentflow.intake import parse_intake
from agentflow.reviewer import parse_verdict


@pytest.mark.parametrize(
    ("error_type", "cause"),
    [
        ("rate_limit_error", ProviderCause.CAPACITY),
        ("overloaded_error", ProviderCause.SERVER),
        ("api_error", ProviderCause.SERVER),
        ("authentication_error", ProviderCause.PERMANENT),
        ("billing_error", ProviderCause.PERMANENT),
        ("permission_error", ProviderCause.PERMANENT),
        ("not_found_error", ProviderCause.PERMANENT),
        ("invalid_request_error", ProviderCause.PERMANENT),
    ],
)
def test_claude_assistant_error_values_map_to_their_cause(error_type, cause):
    # The real Agent SDK surfaces provider errors as an `error` value on the assistant turn,
    # not an invented `type:error` stream event.
    obs = classify_claude([{"type": "assistant", "error": {"type": error_type}}])
    assert obs.cause is cause
    nested = classify_claude([{"type": "assistant", "message": {"error": {"type": error_type}}}])
    assert nested.cause is cause


@pytest.mark.parametrize(
    ("error_value", "cause"),
    [
        ("rate_limit", ProviderCause.CAPACITY),
        ("authentication_failed", ProviderCause.PERMANENT),
        ("billing_error", ProviderCause.PERMANENT),
        ("invalid_request", ProviderCause.PERMANENT),
        ("server_error", ProviderCause.SERVER),
        ("max_output_tokens", ProviderCause.PROCESS),
    ],
)
def test_claude_sdk_assistant_error_strings_map_to_their_cause(error_value, cause):
    assert classify_claude([{"type": "assistant", "error": error_value}]).cause is cause


@pytest.mark.parametrize(
    ("error_type", "reason"),
    [
        ("authentication_failed", EndingReason.ACCESS),
        ("authentication_error", EndingReason.ACCESS),
        ("permission_error", EndingReason.ACCESS),
        ("billing_error", EndingReason.ACCESS),
        ("invalid_request", EndingReason.REJECTED_REQUEST),
        ("invalid_request_error", EndingReason.REJECTED_REQUEST),
        ("request_too_large", EndingReason.REJECTED_REQUEST),
        ("not_found_error", EndingReason.REJECTED_REQUEST),
    ],
)
def test_claude_permanent_errors_preserve_which_condition_fired(error_type, reason):
    """A refused sign-in and a rejected request are both permanent, but they need different
    remediations, so the adapter preserves which one it was (issue #342). The category itself
    is untouched — no trigger changes class."""
    obs = classify_claude([{"type": "assistant", "error": {"type": error_type}}])
    assert obs.classification() == "permanent" and obs.ending_reason is reason


def test_claude_spend_ceiling_is_permanent_for_its_own_reason():
    obs = classify_claude([{"type": "result", "subtype": "error_max_budget_usd"}])
    assert obs.classification() == "permanent"
    assert obs.ending_reason is EndingReason.SPEND


@pytest.mark.parametrize("status", [401, 402, 403])
def test_claude_permanent_result_statuses_are_access_refusals(status):
    obs = classify_claude([{"type": "result", "is_error": True, "api_error_status": status}])
    assert obs.classification() == "permanent"
    assert obs.ending_reason is EndingReason.ACCESS


@pytest.mark.parametrize("error_type", ["rate_limit_error", "server_error", "max_output_tokens"])
def test_a_non_permanent_ending_names_no_ending_reason(error_type):
    """The reason is a sibling of the cause, never a second classifier: a recoverable or
    incomplete ending leaves it unspecified so nothing downstream can read a remedy into it."""
    obs = classify_claude([{"type": "assistant", "error": {"type": error_type}}])
    assert obs.classification() != "permanent"
    assert obs.ending_reason is EndingReason.UNSPECIFIED


def test_an_unmodeled_permanent_ending_leaves_the_reason_unspecified():
    """Nothing typed named the condition, so the observation says exactly that rather than
    letting a stage handoff prescribe a remedy it can't justify."""
    from agentflow.coordinator.providers import ProviderObservation

    obs = ProviderObservation(cause=ProviderCause.PERMANENT)
    assert obs.classification() == "permanent"
    assert obs.ending_reason is EndingReason.UNSPECIFIED


def test_claude_rejected_rate_limit_event_is_capacity_with_reset():
    obs = classify_claude([
        {"type": "rate_limit_event",
         "rate_limit_info": {"status": "rejected", "resetsAt": 900}},
    ])
    assert obs.cause is ProviderCause.CAPACITY and obs.reset_at == 900


def test_claude_allowed_rate_limit_event_establishes_no_cause():
    obs = classify_claude([
        {"type": "rate_limit_event",
         "rate_limit_info": {"status": "allowed", "resetsAt": 900}},
        {"type": "result", "subtype": "success", "result": "done"},
    ])
    assert obs.cause is ProviderCause.NONE and obs.final_message == "done"


def test_claude_allowed_rate_limit_event_still_yields_the_five_hour_quota_fact():
    """The five-hour utilization/reset fact is extracted on every status — an ``allowed`` reading
    is the fresh headroom the balancer needs, not only a rejection (#305). Utilization arrives as a
    0..1 fraction and is normalized to a percentage; the observation time is stamped from the clock."""
    obs = classify_claude(
        [{"type": "rate_limit_event",
          "rate_limit_info": {"status": "allowed", "resetsAt": 5_000, "utilization": 0.42}}],
        observed_at=1_000)
    assert obs.cause is ProviderCause.NONE          # an allowed reading is not a capacity cause
    assert obs.quota is not None
    assert obs.quota.used_percent == 42.0 and obs.quota.resets_at == 5_000
    assert obs.quota.observed_at == 1_000 and obs.quota.provenance == "claude:rate_limit_event"


def test_claude_rejected_event_carries_both_the_capacity_cause_and_the_quota_fact():
    obs = classify_claude(
        [{"type": "rate_limit_event",
          "rate_limit_info": {"status": "rejected", "resetsAt": 5_000, "utilization": 98.0}}],
        observed_at=1_000)
    assert obs.cause is ProviderCause.CAPACITY and obs.reset_at == 5_000
    assert obs.quota is not None and obs.quota.used_percent == 98.0


def test_claude_seven_day_event_does_not_seed_the_five_hour_fact():
    """The headless stream emits one window at a time — often ``seven_day`` (#309). Only a genuine
    five-hour event may seed the five-hour dispatch authority, so a seven_day reading yields no
    quota fact rather than being mis-persisted as five-hour headroom."""
    obs = classify_claude(
        [{"type": "rate_limit_event",
          "rate_limit_info": {"status": "allowed_warning", "resetsAt": 5_000,
                              "rateLimitType": "seven_day", "utilization": 0.59}}],
        observed_at=1_000)
    assert obs.cause is ProviderCause.NONE
    assert obs.quota is None


def test_claude_explicit_five_hour_event_still_seeds_the_fact():
    """A genuine five-hour event is the one the extractor keeps — the corroborating source for the
    independent poll (#309)."""
    obs = classify_claude(
        [{"type": "rate_limit_event",
          "rate_limit_info": {"status": "allowed", "resetsAt": 5_000,
                              "rateLimitType": "five_hour", "utilization": 0.42}}],
        observed_at=1_000)
    assert obs.quota is not None and obs.quota.used_percent == 42.0


def test_claude_rate_limit_event_without_a_utilization_yields_no_quota_fact():
    """A malformed or absent utilization never becomes a fabricated reading — the fact stays
    ``None`` and the balancer fails closed on it rather than admitting at a bogus percentage."""
    no_util = classify_claude(
        [{"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "resetsAt": 5_000}}])
    bad_util = classify_claude(
        [{"type": "rate_limit_event",
          "rate_limit_info": {"status": "allowed", "resetsAt": 5_000, "utilization": "lots"}}])
    assert no_util.quota is None and bad_util.quota is None


def test_claude_terminal_result_subtype_failures_are_process_interruptions():
    # A max-turns end is now a real per-stage ceiling hit — recoverable TIMEOUT, not incomplete
    # PROCESS (ADR 0044): the same class as the wall-clock deadline.
    assert classify_claude(
        [{"type": "result", "subtype": "error_max_turns"}]).cause is ProviderCause.TIMEOUT
    assert classify_claude(
        [{"type": "result", "subtype": "error_during_execution"}]).cause is ProviderCause.PROCESS
    assert classify_claude(
        [{"type": "result", "subtype": "error_max_budget_usd"}]
    ).cause is ProviderCause.PERMANENT
    assert classify_claude(
        [{"type": "result", "subtype": "error_max_structured_output_retries"}]
    ).cause is ProviderCause.PROCESS


@pytest.mark.parametrize(
    ("status", "cause"),
    [
        (429, ProviderCause.CAPACITY),
        ("429", ProviderCause.CAPACITY),
        (500, ProviderCause.SERVER),
        (529, ProviderCause.SERVER),
        (401, ProviderCause.PERMANENT),
        (402, ProviderCause.PERMANENT),
        (403, ProviderCause.PERMANENT),
    ],
)
def test_claude_result_api_errors_override_a_success_subtype(status, cause):
    obs = classify_claude([{
        "type": "result", "subtype": "success", "is_error": True,
        "api_error_status": status, "errors": ["request failed"],
    }])
    assert obs.cause is cause


def test_claude_untyped_result_errors_are_unknown_and_preserved():
    event = {"type": "result", "subtype": "success", "is_error": True,
             "errors": ["new SDK failure shape"]}
    obs = classify_claude([event])
    assert obs.cause is ProviderCause.UNKNOWN
    assert obs.unrecognized == (event,)


def test_claude_timeout_and_process_come_from_supervisor_and_exit():
    assert classify_claude([], timed_out=True).cause is ProviderCause.TIMEOUT
    assert classify_claude([], exit_status=1).cause is ProviderCause.PROCESS
    assert classify_claude([], signal=9).cause is ProviderCause.PROCESS


def test_claude_clean_run_leaves_cause_to_the_stage_outcome():
    obs = classify_claude([
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "hidden"},
            {"type": "text", "text": "done"},
        ]}},
        {"type": "result", "result": "ok"},
    ], exit_status=0)
    assert obs.cause is ProviderCause.NONE
    assert obs.final_message == "ok"


def test_claude_native_structured_output_reaches_stage_parsers_instead_of_result_prose():
    intake = {
        "route": "ready", "title": "Scoped", "body": "brief",
        "complexity": "deep", "effort": "medium",
    }
    intake_obs = classify_claude([{
        "type": "result", "subtype": "success", "result": "Routed ready.",
        "structured_output": intake,
    }], exit_status=0)
    assert json.loads(intake_obs.final_message) == intake
    assert parse_intake(intake_obs.final_message).parsed

    review = {"verdict": "PASS", "reviewed_sha": "sha-a", "findings": []}
    review_obs = classify_claude([{
        "type": "result", "subtype": "success", "result": "PASS.",
        "structured_output": review,
    }], exit_status=0)
    assert json.loads(review_obs.final_message) == review
    assert parse_verdict(review_obs.final_message, expected_sha="sha-a").parsed


def test_claude_preserves_unrecognized_events_and_reports_unknown():
    obs = classify_claude([
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hi"},
        ]}},
        {"type": "telemetry", "weird": {"nested": 1}},
        {"type": "assistant", "error": {"type": "brand_new_error"}},
    ])
    assert obs.cause is ProviderCause.UNKNOWN
    kinds = {e.get("type") for e in obs.unrecognized}
    assert "telemetry" in kinds and "assistant" in kinds
    assert len(obs.events) == 3  # nothing dropped


@pytest.mark.parametrize(
    ("kind", "cause"),
    [
        ("rate_limited", ProviderCause.CAPACITY),
        ("capacity", ProviderCause.CAPACITY),
        ("unauthenticated", ProviderCause.PERMANENT),
        ("billing", ProviderCause.PERMANENT),
        ("plan_required", ProviderCause.PERMANENT),
    ],
)
def test_codex_uses_only_typed_account_facts(kind, cause):
    obs = classify_codex(account_fact={"kind": kind, "reset_at": 1234})
    assert obs.cause is cause
    if cause is ProviderCause.CAPACITY:
        assert obs.reset_at == 1234
    else:
        # Every permanent Codex account kind is an access refusal — the one condition a
        # re-authenticate actually fixes (issue #342).
        assert obs.ending_reason is EndingReason.ACCESS


def test_codex_prose_never_establishes_a_cause():
    prose = "I hit my rate limit and cannot continue, sorry!"
    obs = classify_codex(exit_status=1, final_message=prose)
    assert obs.cause is ProviderCause.UNKNOWN     # the prose is captured but never diagnoses
    assert obs.final_message == prose


def test_codex_untyped_failure_stays_bounded_unknown_but_timeout_is_typed():
    assert classify_codex(exit_status=1).cause is ProviderCause.UNKNOWN
    assert classify_codex(signal=9).cause is ProviderCause.UNKNOWN
    assert classify_codex(timed_out=True).cause is ProviderCause.TIMEOUT


def test_classification_labels_bridge_to_coordinator_vocabulary():
    assert classify_claude(
        [{"type": "assistant", "error": {"type": "rate_limit_error"}}]
    ).classification() == "recoverable"
    assert classify_claude(
        [{"type": "assistant", "error": {"type": "billing_error"}}]
    ).classification() == "permanent"
    assert classify_codex(exit_status=1).classification() == "unknown"


def test_provider_command_unwraps_the_versioned_durable_prompt(monkeypatch):
    import json
    from agentflow.coordinator.record import Record
    from agentflow.runner import ClaudeRunner

    prompts = []
    monkeypatch.setattr(ClaudeRunner, "structured_argv",
                        lambda self, prompt, model, source, schema=None, profile=None:
                        prompts.append(prompt) or ["claude"])
    record = Record("i", "intake", "claude", 1, model="opus", source="/wt",
                    input_ptr=json.dumps({"format": PROVIDER_INPUT_V1,
                                          "prompt": "ground the issue",
                                          "snapshot": {"number": 7}}))

    assert ClaudeProviderAdapter().command(record) == ["claude"]
    assert prompts == ["ground the issue"]


def test_provider_adapters_observe_from_durable_session_artifacts(tmp_path, monkeypatch):
    """Each production adapter reconstructs the full observation from a launched attempt's
    durable events + exit artifacts. Claude reads its structured stream; Codex reads only its
    typed account fact and exit — its `--json` prose is preserved but never diagnoses."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    from agentflow.coordinator.providers import ClaudeProviderAdapter, CodexProviderAdapter
    from agentflow.coordinator.record import Record
    from agentflow.coordinator.session import events_path, exit_path
    from agentflow.coordinator.store import default_store_path

    def artifacts(token, events_text, exit_text):
        ev = events_path(default_store_path(), token)
        ev.parent.mkdir(parents=True, exist_ok=True)
        ev.write_text(events_text)
        exit_path(default_store_path(), token).write_text(exit_text)

    artifacts("tok-1", '{"type":"assistant","message":{"content":'
                       '[{"type":"text","text":"hi"}]}}\n'
                       '{"type": "rate_limit_event", "rate_limit_info":'
                       ' {"status": "rejected", "resetsAt": 42}}\n', "0\n")
    claude_rec = Record("i", "review", "claude", 1, launch_token="tok-1", family="123")
    obs = ClaudeProviderAdapter().observe(claude_rec)
    assert obs.cause is ProviderCause.CAPACITY and obs.reset_at == 42 and obs.exit_status == 0
    assert obs.final_message == "hi"
    assert any(e.get("type") == "assistant" for e in obs.events)  # events preserved

    artifacts("tok-2", "I am rate limited, sorry\n", "1\n")  # prose, not structured
    codex_rec = Record("j", "review", "codex", 2, launch_token="tok-2")
    assert CodexProviderAdapter(account_of=lambda record: None).observe(codex_rec).cause is ProviderCause.UNKNOWN
    typed = lambda record: {"kind": "rate_limited", "reset_at": 99}
    typed_obs = CodexProviderAdapter(account_of=typed).observe(codex_rec)
    assert typed_obs.cause is ProviderCause.CAPACITY and typed_obs.reset_at == 99


def test_default_codex_adapter_queries_the_typed_limit_companion(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    from agentflow.coordinator.providers import CodexProviderAdapter
    from agentflow.coordinator.record import Record
    from agentflow.coordinator.session import write_result
    from agentflow.coordinator.store import default_store_path
    from agentflow.runner import CodexRunner

    monkeypatch.setattr(
        CodexRunner, "account_fact",
        lambda self: {"kind": "rate_limited", "reset_at": 77})
    write_result(default_store_path(), "tok", exit_status=1,
                 signal=None, timed_out=False)

    observation = CodexProviderAdapter().observe(
        Record("codex", "review", "codex", 2, launch_token="tok"))
    assert observation.cause is ProviderCause.CAPACITY
    assert observation.reset_at == 77


# --- lead-run Codex worker capture (issue #516 slice 2) -------------------------------------

def _write_rollout(path, cwd, model="gpt-5.6-terra", input_tokens=1000, cached=100,
                   output_tokens=200, reasoning=50, rollout_id=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"type": "session_meta", "payload": {
            "cwd": cwd, **({"id": rollout_id} if rollout_id else {})}}),
        json.dumps({"type": "turn_context", "payload": {"model": model}}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {
                "input_tokens": input_tokens, "cached_input_tokens": cached,
                "output_tokens": output_tokens, "reasoning_output_tokens": reasoning}}}}),
    ]
    path.write_text("\n".join(lines) + "\n")


def _lead_session_artifacts(tmp_path, token):
    from agentflow.coordinator.session import events_path, exit_path
    from agentflow.coordinator.store import default_store_path

    ev = events_path(default_store_path(), token)
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text('{"type":"result","subtype":"success","result":"done",'
                 '"usage":{"input_tokens":10,"output_tokens":5},'
                 '"modelUsage":{"fable":{"costUSD":0.01}}}\n')
    exit_path(default_store_path(), token).write_text("0\n")


def test_lead_run_claude_observation_merges_codex_worker_usage_from_matching_rollouts(
        tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    sessions_root = tmp_path / ".codex" / "sessions"

    _write_rollout(sessions_root / "matching.jsonl", str(workspace))
    _write_rollout(sessions_root / "other.jsonl", str(tmp_path / "elsewhere"))
    _lead_session_artifacts(tmp_path, "lead-tok")

    record = Record("i", "build", "claude", 1, launch_token="lead-tok",
                    model="fable", source=str(workspace), started_at=0)

    observation = ClaudeProviderAdapter().observe(record)

    costs = {cost.model: cost for cost in observation.usage.model_costs}
    assert "fable" in costs                       # the lead's own Claude usage is kept
    assert "gpt-5.6-terra" in costs                # the matched worker's usage is merged in
    worker = costs["gpt-5.6-terra"]
    assert worker.input_tokens == 900              # 1000 gross - 100 cached, netted like codex_usage
    assert worker.cached_input_tokens == 100
    assert worker.output_tokens == 200
    assert worker.reasoning_output_tokens == 50
    assert worker.cost_usd is None                 # priced at report time, not here

    # No second telemetry record is produced for the worker — it rides the one attempt.
    entry = AttemptTelemetry(
        token="lead-tok", identity="o/r|5|build|", repo="o/r", subject="5", stage="build",
        pool="claude", model="fable", complexity="deep", effort="high", reasoning_effort=None,
        attempt=1, continuation=False, restart_resumes=0, round=0, conflict_round=0,
        verified=True, outcome="pr opened", cause="none", classification="incomplete",
        started_at=0, finalized_at=1, usage=observation.usage)
    store = tmp_path / "records.db"
    record_attempt(store, entry)
    record_attempt(store, entry)   # a restart re-observing the same ended family
    assert len(list(telemetry_dir(store).iterdir())) == 1


def test_lead_run_codex_observation_merges_codex_worker_usage_from_matching_rollouts(
        tmp_path, monkeypatch):
    from agentflow.coordinator.providers import CodexProviderAdapter

    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _write_rollout(tmp_path / ".codex" / "sessions" / "parent.jsonl", str(workspace),
                   model="gpt-5.6-sol", rollout_id="parent-thread")
    _write_rollout(tmp_path / ".codex" / "sessions" / "worker.jsonl", str(workspace),
                   model="gpt-5.6-sol", rollout_id="worker-thread")
    _lead_session_artifacts(tmp_path, "sol-lead-tok")
    from agentflow.coordinator.session import events_path
    parent_events = events_path(tmp_path / "coordinator" / "records.db", "sol-lead-tok")
    parent_events.parent.mkdir(parents=True, exist_ok=True)
    parent_events.write_text(
        '{"type":"thread.started","thread_id":"parent-thread"}\n')
    record = Record("i", "build", "codex", 1, launch_token="sol-lead-tok",
                    model="sol", source=str(workspace), started_at=0)

    observation = CodexProviderAdapter(account_of=lambda _: None).observe(record)

    sol = [cost for cost in observation.usage.model_costs if cost.model == "gpt-5.6-sol"]
    assert len(sol) == 1 and sol[0].input_tokens == 900


def test_a_rollout_with_a_different_cwd_is_not_merged(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    sessions_root = tmp_path / ".codex" / "sessions"
    _write_rollout(sessions_root / "other.jsonl", str(tmp_path / "elsewhere"))
    _lead_session_artifacts(tmp_path, "lead-tok-2")

    record = Record("i", "build", "claude", 1, launch_token="lead-tok-2",
                    model="fable", source=str(workspace), started_at=0)
    observation = ClaudeProviderAdapter().observe(record)

    assert observation.usage.model_costs == (ModelCost("fable", 0.01),)


def test_worker_capture_is_skipped_for_non_lead_run_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    sessions_root = tmp_path / ".codex" / "sessions"
    _write_rollout(sessions_root / "matching.jsonl", str(workspace))
    _lead_session_artifacts(tmp_path, "lead-tok-3")

    # Same workspace, but not a lead (fable) build/revise attempt — no merge should happen.
    record = Record("i", "review", "claude", 1, launch_token="lead-tok-3",
                    model="sonnet", source=str(workspace), started_at=0)
    observation = ClaudeProviderAdapter().observe(record)

    assert observation.usage.model_costs == (ModelCost("fable", 0.01),)


def test_a_rollout_from_an_earlier_attempt_is_not_absorbed_by_a_later_attempt_reusing_the_workspace(
        tmp_path, monkeypatch):
    """A worktree is reused across build -> revise, and each admission resets `started_at`. A
    worker rollout the build attempt spawned must stay the build's, never re-absorbed by a
    later revise attempt that reuses the same workspace, even though the rollout's mtime falls
    inside the revise's old backward-looking slack window (issue #516 slice 2 regression)."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    sessions_root = tmp_path / ".codex" / "sessions"

    build_started = 1_000_000
    revise_started = build_started + 120       # revise admitted after the build's worker ran
    rollout = sessions_root / "worker.jsonl"
    _write_rollout(rollout, str(workspace))
    os.utime(rollout, (build_started + 60, build_started + 60))   # written during the build

    _lead_session_artifacts(tmp_path, "build-tok")
    _lead_session_artifacts(tmp_path, "revise-tok")

    build_record = Record("i", "build", "claude", 1, launch_token="build-tok",
                          model="fable", source=str(workspace), started_at=build_started)
    revise_record = Record("i", "revise", "claude", 1, launch_token="revise-tok",
                           model="fable", source=str(workspace), started_at=revise_started)

    build_costs = {c.model: c for c in
                   ClaudeProviderAdapter().observe(build_record).usage.model_costs}
    revise_costs = {c.model: c for c in
                    ClaudeProviderAdapter().observe(revise_record).usage.model_costs}

    assert "gpt-5.6-terra" in build_costs        # the build attempt still absorbs its own worker
    assert "gpt-5.6-terra" not in revise_costs   # the revise attempt does not re-absorb it
