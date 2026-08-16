"""Codex rollout facts belong behind one reader, not in its consumers."""

from __future__ import annotations

import json

from agentflow.codex_transcripts import (
    CodexRateLimitWindow, latest_rate_limits, where_did_session_run, what_did_session_spend)


def _rollout(path):
    path.write_text("\n".join((
        json.dumps({"type": "session_meta", "payload": {"cwd": "/work/agentflow"}}),
        json.dumps({"type": "turn_context", "payload": {"model": "gpt-5.6-terra"}}),
        json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {
                "input_tokens": 1_000, "cached_input_tokens": 100,
                "output_tokens": 200, "reasoning_output_tokens": 50,
                "tool_tokens": 12,
            }},
        }}),
    )) + "\n")


def test_reader_answers_where_a_session_ran_and_what_it_spent(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    _rollout(rollout)

    assert where_did_session_run(rollout) == "/work/agentflow"
    spend = what_did_session_spend(rollout)
    assert spend is not None
    assert spend.model == "gpt-5.6-terra"
    assert spend.input_tokens == 1_000
    assert spend.cached_input_tokens == 100
    assert spend.output_tokens == 200
    assert spend.reasoning_output_tokens == 50
    assert spend.unrecognized == ("tool_tokens",)


def test_reader_returns_the_latest_provider_rate_limits(tmp_path):
    root = tmp_path / "sessions"
    root.mkdir()
    (root / "old.jsonl").write_text(json.dumps({
        "timestamp": "2026-08-16T10:00:00Z", "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {"primary": {"used_percent": 10}},
        },
    }) + "\n")
    (root / "latest.jsonl").write_text(json.dumps({
        "timestamp": "2026-08-16T11:00:00Z", "type": "event_msg", "payload": {
            "type": "token_count", "rate_limits": {
                "primary": {"used_percent": 25, "window_minutes": 300, "resets_at": 300},
                "secondary": {"used_percent": 40, "window_minutes": 10_080, "resets_at": 900},
            },
        },
    }) + "\n")

    limits = latest_rate_limits(root)

    assert limits is not None
    assert limits.observed_at == 1_786_878_000.0
    assert limits.windows == (
        CodexRateLimitWindow(used_percent=25, window_minutes=300, resets_at=300),
        CodexRateLimitWindow(used_percent=40, window_minutes=10_080, resets_at=900),
    )
