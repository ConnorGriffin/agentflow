"""Per-attempt spend telemetry (ADR 0040 / issue #223).

Two surfaces are exercised: the pure normalization at the provider seam (representative
Claude and Codex usage events become one normalized :class:`AttemptUsage`), and the durable
per-attempt persistence the coordinator stamps through its public ``submit_stage`` / ``cycle``
seam. The end-to-end tests assert that every ended attempt is recorded exactly once — even
across a daemon restart that re-observes the same family — that retries stay separately
attributable, that the bounded projection reports totals and missing-data counts, and that no
prompt or provider message text is ever persisted.
"""

from __future__ import annotations

import json

import pytest

from conftest import FakeSession

from agentflow.coordinator import Submission
from agentflow.coordinator.providers import ProviderCause, classify_claude, classify_codex
from agentflow.coordinator.telemetry import (
    AttemptTelemetry, AttemptUsage, claude_usage, codex_usage, project,
    read_attempts, record_attempt, telemetry_dir)

# --- representative provider usage streams -------------------------------------------------

CLAUDE_RESULT = {
    "type": "result", "subtype": "success", "result": "done",
    "duration_ms": 91234, "num_turns": 7, "total_cost_usd": 0.42,
    "usage": {"input_tokens": 120, "cache_creation_input_tokens": 2000,
              "cache_read_input_tokens": 51000, "output_tokens": 1500,
              "service_tier": "standard"},          # an unmodeled usage field
    "modelUsage": {"claude-opus-4": {"costUSD": 0.42}},
}

CODEX_STREAM = [
    {"type": "thread.started", "thread_id": "th-1"},
    {"type": "turn.completed", "usage": {"input_tokens": 100000, "cached_input_tokens": 40000,
                                         "output_tokens": 8000, "reasoning_output_tokens": 3000}},
    {"type": "turn.completed", "usage": {"input_tokens": 64000, "cached_input_tokens": 20000,
                                         "output_tokens": 5000, "reasoning_output_tokens": 2000,
                                         "tool_tokens": 12}},   # an unmodeled usage field
    {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
]


def test_claude_usage_normalizes_the_terminal_result():
    usage = claude_usage([{"type": "assistant"}, CLAUDE_RESULT])
    assert usage.present
    assert usage.input_tokens == 120
    assert usage.cache_creation_tokens == 2000
    assert usage.cached_input_tokens == 51000
    assert usage.output_tokens == 1500
    assert usage.reasoning_output_tokens is None      # Claude does not separate reasoning
    assert usage.cost_usd == 0.42
    assert usage.turns == 7
    assert usage.duration_ms == 91234
    assert usage.model == "claude-opus-4"
    assert "service_tier" in usage.unrecognized       # unmodeled field preserved, not dropped


def test_codex_usage_sums_turns_and_nets_out_cached_input():
    usage = codex_usage(CODEX_STREAM)
    assert usage.present
    # input_tokens is reported *including* cached, so the normalized non-cached input nets it out:
    # (100000 + 64000) gross - (40000 + 20000) cached = 104000.
    assert usage.input_tokens == 104000
    assert usage.cached_input_tokens == 60000
    assert usage.cache_creation_tokens is None        # Codex has no cache-creation notion
    assert usage.output_tokens == 13000
    assert usage.reasoning_output_tokens == 5000
    assert usage.cost_usd is None                     # Codex reports no provider dollar cost
    assert usage.turns == 2
    assert "tool_tokens" in usage.unrecognized


def test_missing_usage_stays_explicit_never_zero():
    # A stream with no result / no completed turn reports nothing — the attempt's spend is
    # genuinely unknown, not zero.
    empty_claude = claude_usage([{"type": "assistant"}])
    empty_codex = codex_usage([{"type": "thread.started", "thread_id": "x"}])
    for usage in (empty_claude, empty_codex):
        assert not usage.present
        assert usage.input_tokens is None and usage.output_tokens is None
        assert usage.cost_usd is None


def test_provider_observation_carries_normalized_usage():
    # The usage rides on the observation the coordinator reads, so raw provider shapes stay
    # behind the adapter seam.
    assert classify_claude([CLAUDE_RESULT]).usage.cost_usd == 0.42
    assert classify_codex(events=CODEX_STREAM).usage.output_tokens == 13000


# --- durable persistence -------------------------------------------------------------------

def _entry(token="tok", stage="build", model="opus", verified=True, outcome="pr opened",
           usage=None, cause="none"):
    return AttemptTelemetry(
        token=token, identity=f"o/r|5|{stage}|", repo="o/r", subject="5", stage=stage,
        pool="claude", model=model, complexity="deep", effort="high", reasoning_effort=None,
        attempt=1, continuation=False, restart_resumes=0, round=0, conflict_round=0,
        verified=verified, outcome=outcome, cause=cause, classification="incomplete",
        started_at=100, finalized_at=200, usage=usage or AttemptUsage())


def test_record_attempt_is_idempotent_by_launch_token(tmp_path):
    store = tmp_path / "records.db"
    usage = AttemptUsage(input_tokens=120, output_tokens=1500, cost_usd=0.42)
    record_attempt(store, _entry(usage=usage))
    record_attempt(store, _entry(usage=usage))        # a restart re-observes the same family
    files = list(telemetry_dir(store).iterdir())
    assert len(files) == 1                            # persisted exactly once, keyed by the token
    (only,) = read_attempts(store)
    assert only.usage.cost_usd == 0.42 and only.outcome == "pr opened"


def test_no_prompt_or_message_content_is_ever_persisted(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(usage=AttemptUsage(output_tokens=10)))
    raw = (telemetry_dir(store) / "tok.json").read_text()
    data = json.loads(raw)
    # Only numbers and stage identity — no message, event, or partial-output text.
    assert "final_message" not in raw and "partial_output" not in raw and "events" not in raw
    assert set(data["usage"]) <= {
        "input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens",
        "reasoning_output_tokens", "cost_usd", "turns", "duration_ms", "model", "unrecognized"}


def test_projection_totals_and_missing_counts_by_stage_model_outcome(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(token="a", stage="build", model="opus", verified=True,
                                 outcome="pr opened",
                                 usage=AttemptUsage(output_tokens=1000, cost_usd=0.40)))
    record_attempt(store, _entry(token="b", stage="build", model="opus", verified=True,
                                 outcome="pr opened",
                                 usage=AttemptUsage(output_tokens=500, cost_usd=0.20)))
    # A Codex-style attempt: no provider cost, and one with no usage at all.
    record_attempt(store, _entry(token="c", stage="review", model="sol", verified=False,
                                 outcome="", cause="capacity",
                                 usage=AttemptUsage(output_tokens=300)))
    record_attempt(store, _entry(token="d", stage="review", model="sol", verified=False,
                                 outcome="", cause="capacity", usage=AttemptUsage()))

    proj = project(store)
    assert proj.total.attempts == 4
    assert proj.total.output_tokens == 1800
    assert proj.total.cost_usd == pytest.approx(0.60)
    assert proj.total.cost_missing == 2               # the two Codex-style attempts
    assert proj.total.missing_usage == 1              # the one attempt that reported nothing
    cells = {(c.stage, c.model, c.outcome): c.totals for c in proj.cells}
    assert cells[("build", "opus", "pr opened")].attempts == 2
    assert cells[("build", "opus", "pr opened")].verified == 2
    assert cells[("review", "sol", "unverified:capacity")].missing_usage == 1


def test_projection_ignores_a_corrupt_entry(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(usage=AttemptUsage(output_tokens=10)))
    (telemetry_dir(store) / "bad.json").write_text("{not json")
    assert project(store).total.attempts == 1         # the corrupt tail is skipped, not fatal


# --- through the coordinator's public seam -------------------------------------------------

def test_completed_attempt_persists_its_spend_through_the_seam(make_coord, coord_state):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="5", stage="build",
                                             pool="claude", complexity="deep", effort="high"))
    assert coord.cycle("claude") == []                # the build starts
    fake.end(identity, success=True,
             usage=AttemptUsage(input_tokens=120, output_tokens=1500, cost_usd=0.42, turns=7))
    outcomes = coord.cycle("claude")
    assert [o.status for o in outcomes] == ["completed"]

    (entry,) = read_attempts(coord._store.path)
    assert entry.stage == "build" and entry.model == "opus" and entry.effort == "high"
    assert entry.verified is True
    assert entry.usage.cost_usd == 0.42 and entry.usage.output_tokens == 1500
    assert entry.attempt == 1


def test_retries_remain_separately_attributable(make_coord, coord_state):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build",
                                             pool="claude", complexity="deep"))
    # First attempt burns spend then hits a recoverable interruption → an automatic continuation.
    coord.cycle("claude")
    fake.end(identity, cause=ProviderCause.CAPACITY,
             usage=AttemptUsage(output_tokens=400, cost_usd=0.10))
    assert coord.cycle("claude") == []                # continuation waits, then re-admits
    # Second attempt completes.
    coord.cycle("claude")
    fake.end(identity, success=True, usage=AttemptUsage(output_tokens=900, cost_usd=0.30))
    coord.cycle("claude")

    entries = sorted(read_attempts(coord._store.path), key=lambda e: e.attempt)
    assert len(entries) == 2                           # the superseded attempt and the winner
    assert [e.attempt for e in entries] == [1, 2]
    assert entries[0].verified is False and entries[0].usage.cost_usd == 0.10
    assert entries[1].verified is True and entries[1].usage.cost_usd == 0.30
    # The superseded spend is not lost — it lands in the total (ADR 0040's anti-gaming core).
    assert project(coord._store.path).total.cost_usd == pytest.approx(0.40)


def test_restart_replay_of_the_same_family_records_spend_once(make_coord, coord_state):
    """A daemon restart that re-observes the same ended family must not double-count its spend."""
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="8", stage="review",
                                             pool="claude", complexity="deep"))
    coord.cycle("claude")
    fake.end(identity, success=True, usage=AttemptUsage(output_tokens=200, cost_usd=0.05))
    coord.cycle("claude")
    # A fresh coordinator over the same durable store re-runs reconciliation (a crash replay).
    replay = make_coord(fake)
    replay.cycle("claude")
    assert len(read_attempts(coord._store.path)) == 1        # exactly once across the restart
