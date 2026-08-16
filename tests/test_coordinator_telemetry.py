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
    AttemptTelemetry, AttemptUsage, ModelCost, claude_usage, codex_usage, format_spend_report,
    project, read_attempts, record_attempt, spend_report, telemetry_dir)

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
    assert usage.model_costs == (ModelCost(model="claude-opus-4", cost_usd=0.42),)
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
    assert usage.model_costs == ()


def test_claude_usage_keeps_every_valid_provider_model_cost():
    result = {
        "type": "result", "usage": {"input_tokens": 1},
        "modelUsage": {
            "claude-opus-4": {"costUSD": 0.2},
            "claude-sonnet-4": {"costUSD": 0.1},
        },
    }

    usage = claude_usage([result])

    assert usage.model is None
    assert usage.model_costs == (
        ModelCost(model="claude-opus-4", cost_usd=0.2),
        ModelCost(model="claude-sonnet-4", cost_usd=0.1),
    )


def test_claude_usage_keeps_three_valid_provider_model_costs():
    usage = claude_usage([{
        "type": "result", "usage": {"input_tokens": 1},
        "modelUsage": {
            "claude-opus-4": {"costUSD": 0.2},
            "claude-sonnet-4": {"costUSD": 0.1},
            "claude-haiku-4": {"costUSD": 0.05},
        },
    }])

    assert usage.model_costs == (
        ModelCost(model="claude-opus-4", cost_usd=0.2),
        ModelCost(model="claude-sonnet-4", cost_usd=0.1),
        ModelCost(model="claude-haiku-4", cost_usd=0.05),
    )


def test_claude_usage_omits_malformed_provider_model_costs_without_losing_siblings():
    result = {
        "type": "result", "usage": {"input_tokens": 1},
        "modelUsage": {
            "missing": {}, "string": {"costUSD": "0.1"}, "boolean": {"costUSD": True},
            "bad": None, 9: {"costUSD": 0.1}, "claude-good": {"costUSD": 0.2},
        },
    }

    usage = claude_usage([result])

    assert usage.model is None
    assert usage.model_costs == (ModelCost(model="claude-good", cost_usd=0.2),)


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


def test_model_costs_round_trip_as_typed_provider_attribution(tmp_path):
    store = tmp_path / "records.db"
    usage = AttemptUsage(
        cost_usd=0.3,
        model_costs=(ModelCost("claude-opus-4", 0.2), ModelCost("claude-sonnet-4", 0.1)),
    )
    record_attempt(store, _entry(token="mixed", usage=usage))

    (loaded,) = [entry for entry in read_attempts(store) if entry.token == "mixed"]

    assert loaded.usage.model_costs == (
        ModelCost("claude-opus-4", 0.2), ModelCost("claude-sonnet-4", 0.1))


def test_legacy_and_malformed_model_costs_preserve_other_usage(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(token="legacy", usage=AttemptUsage(input_tokens=12, cost_usd=0.4)))
    written = json.loads((telemetry_dir(store) / "legacy.json").read_text())
    del written["usage"]["model_costs"]                # a record written before the breakdown existed
    (telemetry_dir(store) / "legacy.json").write_text(json.dumps(written))
    malformed = json.loads((telemetry_dir(store) / "legacy.json").read_text())
    malformed["token"] = "malformed"
    malformed["usage"]["model_costs"] = [
        None, {"model": "claude-good", "cost_usd": 0.2}, {"model": 3, "cost_usd": 0.1},
        {"model": "boolean", "cost_usd": True},
    ]
    (telemetry_dir(store) / "malformed.json").write_text(json.dumps(malformed))
    non_list = json.loads((telemetry_dir(store) / "legacy.json").read_text())
    non_list["token"] = "non-list"
    non_list["usage"]["model_costs"] = {"claude-good": 0.2}
    (telemetry_dir(store) / "non-list.json").write_text(json.dumps(non_list))

    entries = {entry.token: entry for entry in read_attempts(store)}

    assert entries["legacy"].usage.model_costs == ()  # unavailable historical attribution
    assert entries["legacy"].usage.input_tokens == 12 and entries["legacy"].usage.cost_usd == 0.4
    assert entries["malformed"].usage.model_costs == (ModelCost("claude-good", 0.2),)
    assert entries["malformed"].usage.input_tokens == 12 and entries["malformed"].usage.cost_usd == 0.4
    assert entries["non-list"].usage.model_costs == ()
    assert entries["non-list"].usage.input_tokens == 12 and entries["non-list"].usage.cost_usd == 0.4


def test_no_prompt_or_message_content_is_ever_persisted(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(usage=AttemptUsage(output_tokens=10)))
    raw = (telemetry_dir(store) / "tok.json").read_text()
    data = json.loads(raw)
    # Only numbers and stage identity — no message, event, or partial-output text.
    assert "final_message" not in raw and "partial_output" not in raw and "events" not in raw
    assert set(data["usage"]) <= {
        "input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens",
        "reasoning_output_tokens", "cost_usd", "turns", "duration_ms", "model", "model_costs",
        "unrecognized"}


def test_projection_totals_and_missing_counts_by_stage_model_outcome(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(token="a", stage="build", model="opus", verified=True,
                                 outcome="pr opened",
                                 usage=AttemptUsage(output_tokens=1000, cost_usd=0.40)))
    record_attempt(store, _entry(token="b", stage="build", model="opus", verified=True,
                                 outcome="pr opened",
                                 usage=AttemptUsage(output_tokens=500, cost_usd=0.20)))
    # A Codex-style attempt: its token facts price from the rate card; the other has no usage.
    record_attempt(store, _entry(token="c", stage="review", model="sol", verified=False,
                                 outcome="", cause="capacity",
                                 usage=AttemptUsage(output_tokens=300)))
    record_attempt(store, _entry(token="d", stage="review", model="sol", verified=False,
                                 outcome="", cause="capacity", usage=AttemptUsage()))

    proj = project(store)
    assert proj.total.attempts == 4
    assert proj.total.output_tokens == 1800
    assert proj.total.cost_usd == pytest.approx(0.60)
    assert proj.total.estimated_cost_usd == pytest.approx(0.009)
    assert proj.total.cost_missing == 1               # only the attempt that reported nothing
    assert proj.total.missing_usage == 1              # the one attempt that reported nothing
    cells = {(c.stage, c.model, c.outcome): c.totals for c in proj.cells}
    assert cells[("build", "opus", "pr opened")].attempts == 2
    assert cells[("build", "opus", "pr opened")].verified == 2
    assert cells[("review", "sol", "unverified:capacity")].missing_usage == 1


def test_rolling_projection_prices_codex_from_the_rate_card_and_marks_it_estimated(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="codex", stage="review", model="luna",
        usage=AttemptUsage(input_tokens=200, output_tokens=40)))

    totals = project(store).total

    expected = 200 * 1 / 1_000_000 + 40 * 6 / 1_000_000
    assert totals.cost_usd == 0
    assert totals.estimated_cost_usd == pytest.approx(expected)
    assert totals.cost_missing == 0


def test_rolling_projection_prices_an_unbilled_attributed_codex_worker(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="worker", stage="build", model="fable",
        usage=AttemptUsage(model_costs=(
            ModelCost("gpt-5.6-terra", None, input_tokens=200, output_tokens=40),))))

    totals = project(store).total

    expected = 200 * 2.5 / 1_000_000 + 40 * 15 / 1_000_000
    assert totals.cost_usd == 0
    assert totals.estimated_cost_usd == pytest.approx(expected)
    assert totals.cost_missing == 0


def test_rolling_projection_does_not_double_count_an_attributed_provider_bill(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="billed", stage="review", model="sonnet",
        usage=AttemptUsage(cost_usd=0.05, model_costs=(
            ModelCost("sonnet", 0.05, output_tokens=1),))))

    totals = project(store).total

    assert totals.cost_usd == pytest.approx(0.05)
    assert totals.estimated_cost_usd == 0


def test_rolling_projection_uses_attributed_bills_without_an_attempt_total(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="attributed-bill", stage="review", model="sonnet",
        usage=AttemptUsage(model_costs=(ModelCost("sonnet", 0.05, output_tokens=1),))))

    totals = project(store).total

    assert totals.cost_usd == pytest.approx(0.05)
    assert totals.estimated_cost_usd == 0
    assert totals.cost_missing == 0


def test_rolling_projection_counts_uncaptured_delegate_spend(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="uncaptured", stage="build", model="fable",
        usage=AttemptUsage(model_costs=(ModelCost("sonnet", 0.05, output_tokens=1),))))

    totals = project(store).total

    assert totals.cost_usd == pytest.approx(0.05)
    assert totals.delegate_uncaptured_attempts == 1


def test_projection_ignores_a_corrupt_entry(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(usage=AttemptUsage(output_tokens=10)))
    (telemetry_dir(store) / "bad.json").write_text("{not json")
    assert project(store).total.attempts == 1         # the corrupt tail is skipped, not fatal


def test_spend_report_uses_delegate_models_and_keeps_token_only_codex_rows(tmp_path):
    store = tmp_path / "records.db"
    delegated = AttemptUsage(model_costs=(
        ModelCost("fable", 0.03, input_tokens=100, output_tokens=20),
        ModelCost("sonnet", 0.12, input_tokens=400, output_tokens=80),
        ModelCost("gpt-5.6-terra", None, input_tokens=300, output_tokens=60),
    ))
    record_attempt(store, _entry(token="delegated", model="fable", usage=delegated))
    record_attempt(store, _entry(
        token="codex", stage="review", model="luna",
        usage=AttemptUsage(input_tokens=200, output_tokens=40)))
    old = _entry(token="old", model="opus", usage=AttemptUsage(output_tokens=999))
    object.__setattr__(old, "started_at", 10)
    record_attempt(store, old)

    report = spend_report(store, start=50, end=150)
    rows = {(row.stage, row.model): row for row in report.rows}

    assert set(rows) == {
        ("build", "fable"), ("build", "sonnet"),
        ("build", "gpt-5.6-terra"), ("review", "luna"),
    }
    assert rows[("build", "fable")].tokens == 120
    assert rows[("build", "sonnet")].cost_usd == pytest.approx(0.12)
    assert rows[("build", "gpt-5.6-terra")].tokens == 360
    # Terra ($2.50/$15 per million in/out) prices from the rate card since it is not billed.
    assert rows[("build", "gpt-5.6-terra")].cost_usd == pytest.approx(
        300 * 2.5 / 1_000_000 + 60 * 15 / 1_000_000)
    assert rows[("build", "gpt-5.6-terra")].estimated is True
    assert rows[("review", "luna")].tokens == 240
    # Luna prices from the card too (input/output tokens only, from the fallback usage row).
    assert rows[("review", "luna")].cost_usd == pytest.approx(
        200 * 1 / 1_000_000 + 40 * 6 / 1_000_000)
    assert rows[("review", "luna")].estimated is True


def test_format_spend_report_flags_every_estimated_row_and_a_total_would_mix_estimates(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="codex", stage="review", model="luna",
        usage=AttemptUsage(input_tokens=200, output_tokens=40)))
    record_attempt(store, _entry(
        token="billed", stage="review", model="sonnet",
        usage=AttemptUsage(model_costs=(
            ModelCost("sonnet", 0.05, input_tokens=10, output_tokens=5),))))

    report = spend_report(store, start=50, end=150)
    rendered = format_spend_report(report)
    rows_by_key = {(r.stage, r.model): r for r in report.rows}

    # The estimated Codex row must render with the "est" flag — a test that fails if an
    # estimated dollar figure ever renders unflagged (indistinguishable from a billed one).
    assert rows_by_key[("review", "luna")].estimated is True
    for line in rendered.splitlines():
        if line.startswith("review\tluna\t"):
            assert "est" in line
            assert "~" in line
            assert "cached input not priced" not in line
        if line.startswith("review\tsonnet\t"):
            assert "est" not in line   # provider-billed, never flagged


def test_spend_report_discloses_cached_input_omitted_from_an_estimate(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="cached", stage="review", model="luna",
        usage=AttemptUsage(model_costs=(
            ModelCost("luna", None, input_tokens=5, cached_input_tokens=95, output_tokens=1),))))

    report = spend_report(store, start=50, end=150)
    (row,) = report.rows

    assert row.estimated is True
    assert row.unpriced_cached_input_tokens == 95
    assert "cached input not priced (95 tokens)" in format_spend_report(report)


def test_spend_report_discloses_when_a_dollar_total_covers_only_some_attempts(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="priced", stage="build", model="fable",
        usage=AttemptUsage(output_tokens=1, cost_usd=0.05)))
    record_attempt(store, _entry(
        token="unpriced", stage="build", model="fable",
        usage=AttemptUsage(output_tokens=99)))

    report = spend_report(store, start=50, end=150)
    (row,) = report.rows

    assert row.attempts == 2
    assert row.dollar_covered_attempts == 1
    assert "dollar total covers 1 of 2 attempts" in format_spend_report(report)


def test_spend_report_keeps_delegate_and_dollar_coverage_notes(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="priced", stage="build", model="fable",
        usage=AttemptUsage(output_tokens=1, cost_usd=0.05)))
    record_attempt(store, _entry(
        token="unpriced", stage="build", model="fable",
        usage=AttemptUsage(output_tokens=99)))

    rendered = format_spend_report(spend_report(store, start=50, end=150))

    assert "delegate spend not counted (2); dollar total covers 1 of 2 attempts" in rendered


def test_fully_billed_and_dollar_covered_spend_row_has_no_qualification(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="billed", stage="review", model="sonnet",
        usage=AttemptUsage(output_tokens=1, cost_usd=0.05)))

    report = spend_report(store, start=50, end=150)
    (row,) = report.rows
    rendered = format_spend_report(report)

    assert row.dollar_covered_attempts == row.attempts == 1
    assert row.estimated is False
    assert "not priced" not in rendered
    assert "dollar total covers" not in rendered
    assert "delegate spend not counted" not in rendered


def test_unknown_dollar_figure_is_never_rendered_as_zero(tmp_path):
    store = tmp_path / "records.db"
    # A model this routing table has never heard of: no rate card entry, no billed cost.
    record_attempt(store, _entry(
        token="unpriced", stage="review", model="mystery-model",
        usage=AttemptUsage(model_costs=(
            ModelCost("mystery-model", None, input_tokens=10, output_tokens=5),))))

    report = spend_report(store, start=50, end=150)
    (row,) = report.rows
    assert row.cost_usd is None
    assert row.dollar_covered_attempts == 0
    assert "—" in format_spend_report(report)
    assert "0.000000" not in format_spend_report(report)
    assert "dollar total covers" not in format_spend_report(report)


def test_lead_run_attempt_without_worker_capture_shows_the_not_counted_mark(tmp_path):
    store = tmp_path / "records.db"
    # A build attempt run by fable whose usage carries only Claude-side model costs — no
    # Codex worker spend has been merged in yet.
    record_attempt(store, _entry(
        token="uncaptured", stage="build", model="fable",
        usage=AttemptUsage(model_costs=(
            ModelCost("fable", 0.03, input_tokens=100, output_tokens=20),))))

    report = spend_report(store, start=50, end=150)
    (row,) = report.rows
    assert row.delegate_uncaptured_attempts == 1
    assert "delegate spend not counted" in format_spend_report(report)


def test_lead_run_attempt_with_merged_worker_capture_has_no_not_counted_mark(tmp_path):
    store = tmp_path / "records.db"
    # Same shape, but a Codex worker entry (slice 2's merge) is now present.
    record_attempt(store, _entry(
        token="captured", stage="build", model="fable",
        usage=AttemptUsage(model_costs=(
            ModelCost("fable", 0.03, input_tokens=100, output_tokens=20),
            ModelCost("codex", None, input_tokens=50, output_tokens=10),))))

    report = spend_report(store, start=50, end=150)
    for row in report.rows:
        assert row.delegate_uncaptured_attempts == 0
    assert "delegate spend not counted" not in format_spend_report(report)


def test_sol_lead_helper_usage_is_captured_without_counting_its_parent(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="sol-missing", stage="build", model="sol",
        usage=AttemptUsage(model_costs=(
            ModelCost("sol", None, input_tokens=100, output_tokens=20),
            ModelCost("gpt-5.6-sol", None, input_tokens=5, output_tokens=1)))))
    record_attempt(store, _entry(
        token="sol-captured", stage="revise", model="sol",
        usage=AttemptUsage(model_costs=(
            ModelCost("sol", None, input_tokens=100, output_tokens=20),
            ModelCost("terra", None, input_tokens=50, output_tokens=10)))))
    record_attempt(store, _entry(
        token="sol-claude-helper", stage="build", model="sol",
        usage=AttemptUsage(model_costs=(
            ModelCost("sol", None, input_tokens=100, output_tokens=20),
            ModelCost("sonnet", 0.03, input_tokens=50, output_tokens=10)))))
    record_attempt(store, _entry(
        token="sol-opus-helper", stage="revise", model="sol",
        usage=AttemptUsage(model_costs=(
            ModelCost("gpt-5.6-sol", None, input_tokens=100, output_tokens=20),
            ModelCost("opus", 0.05, input_tokens=50, output_tokens=10)))))

    rows = {(row.stage, row.model): row for row in spend_report(store, start=50, end=150).rows}
    assert rows[("build", "sol")].delegate_uncaptured_attempts == 1
    assert rows[("revise", "sol")].delegate_uncaptured_attempts == 0


def test_spend_report_does_not_rewrite_historical_entry_files(tmp_path):
    store = tmp_path / "records.db"
    record_attempt(store, _entry(
        token="historical", stage="build", model="opus",
        usage=AttemptUsage(model_costs=(ModelCost("gpt-5.6-terra", None, input_tokens=100,
                                                   output_tokens=20),))))
    path = telemetry_dir(store) / "historical.json"
    before = path.read_bytes()

    spend_report(store, start=50, end=150)

    assert path.read_bytes() == before


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
    assert entry.stage == "build" and entry.model == "fable" and entry.effort == "high"
    assert entry.verified is True
    assert entry.usage.cost_usd == 0.42 and entry.usage.output_tokens == 1500
    assert entry.attempt == 1


def test_attempt_records_the_session_leads_low_reasoning_effort(make_coord, coord_state):
    """The parent stays low even when the worker instructions carry the extra/xhigh dial."""
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="5", stage="build",
                                             pool="claude", complexity="deep", effort="extra"))
    assert coord.cycle("claude") == []
    fake.end(identity, success=True, usage=AttemptUsage(output_tokens=900))
    coord.cycle("claude")

    (entry,) = read_attempts(coord._store.path)
    assert entry.reasoning_effort == "low"


def test_non_build_attempt_records_reasoning_effort_as_explicit_none(make_coord, coord_state):
    """A Review keeps the provider default, so its telemetry records reasoning effort as an
    explicit ``None`` — the recalibration pass reads "unset", not a mapped rung."""
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="8", stage="review",
                                             pool="claude", complexity="deep", effort="extra"))
    coord.cycle("claude")
    fake.end(identity, success=True, usage=AttemptUsage(output_tokens=200))
    coord.cycle("claude")

    (entry,) = read_attempts(coord._store.path)
    assert entry.reasoning_effort is None


def test_retries_remain_separately_attributable(make_coord, coord_state):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build",
                                             pool="claude", complexity="deep"))
    # First attempt burns spend then hits a recoverable interruption → an automatic continuation.
    # A non-capacity recoverable end keeps consuming the budget, so the two attempts stay distinctly
    # numbered — a provider capacity reset would refund the attempt (#305) and muddy this test.
    coord.cycle("claude")
    fake.end(identity, cause=ProviderCause.SERVER,
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
