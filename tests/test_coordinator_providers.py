"""The two provider adapters extract facts; they never decide policy (ADR 0030).

Claude fixtures preserve every supported structured cause plus unrecognized fields; Codex
fixtures establish a cause only from typed account/rate-limit facts, and an untyped failure
— even a non-zero exit — stays a bounded ``unknown`` interruption because `codex exec`
prose is never a diagnosis.
"""

from __future__ import annotations

import pytest

from agentflow.coordinator.providers import (
    PROVIDER_INPUT_V1, ClaudeProviderAdapter, ProviderCause, classify_claude, classify_codex)


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


def test_claude_terminal_result_subtype_failures_are_process_interruptions():
    assert classify_claude(
        [{"type": "result", "subtype": "error_max_turns"}]).cause is ProviderCause.PROCESS
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
                        lambda self, prompt, model, source, schema=None:
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
