"""The two provider adapters extract facts; they never decide policy (ADR 0030).

Claude fixtures preserve every supported structured cause plus unrecognized fields; Codex
fixtures establish a cause only from typed account/rate-limit facts, and an untyped failure
— even a non-zero exit — stays a bounded ``unknown`` interruption because `codex exec`
prose is never a diagnosis.
"""

from __future__ import annotations

import pytest

from agentflow.coordinator.providers import (
    ProviderCause, classify_claude, classify_codex)


@pytest.mark.parametrize(
    ("subtype", "cause"),
    [
        ("capacity", ProviderCause.CAPACITY),
        ("rate_limit", ProviderCause.CAPACITY),
        ("authentication", ProviderCause.PERMANENT),
        ("billing", ProviderCause.PERMANENT),
        ("permission", ProviderCause.PERMANENT),
        ("configuration", ProviderCause.PERMANENT),
        ("server", ProviderCause.SERVER),
        ("transport", ProviderCause.SERVER),
    ],
)
def test_claude_typed_error_subtypes_map_to_their_cause(subtype, cause):
    obs = classify_claude([{"type": "error", "subtype": subtype}])
    assert obs.cause is cause


def test_claude_capacity_carries_the_reset_time():
    obs = classify_claude([{"type": "error", "subtype": "capacity", "reset_at": 900}])
    assert obs.cause is ProviderCause.CAPACITY and obs.reset_at == 900


def test_claude_timeout_and_process_come_from_supervisor_and_exit():
    assert classify_claude([], timed_out=True).cause is ProviderCause.TIMEOUT
    assert classify_claude([], exit_status=1).cause is ProviderCause.PROCESS
    assert classify_claude([], signal=9).cause is ProviderCause.PROCESS


def test_claude_clean_run_leaves_cause_to_the_stage_outcome():
    obs = classify_claude([{"type": "assistant", "text": "done"},
                           {"type": "result", "final_message": "ok"}], exit_status=0)
    assert obs.cause is ProviderCause.NONE
    assert obs.final_message == "ok"


def test_claude_preserves_unrecognized_events_and_reports_unknown():
    obs = classify_claude([
        {"type": "assistant", "text": "hi"},
        {"type": "telemetry", "weird": {"nested": 1}},
        {"type": "error", "subtype": "brand_new_thing"},
    ])
    assert obs.cause is ProviderCause.UNKNOWN
    kinds = {e.get("type") for e in obs.unrecognized}
    assert "telemetry" in kinds and "error" in kinds
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
    assert classify_claude([{"type": "error", "subtype": "capacity"}]).classification() == "recoverable"
    assert classify_claude([{"type": "error", "subtype": "billing"}]).classification() == "permanent"
    assert classify_codex(exit_status=1).classification() == "unknown"
