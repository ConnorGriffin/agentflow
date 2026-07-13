"""Balancer decision + gate-output parsing — the pure surfaces."""

import json
import time

import pytest

from agentflow import balancer
from agentflow.balancer import PoolStatus, choose_pair, parse_pct, pick_pair
from agentflow.dashboard_data import pools

RUNNERS = {"claude": "CLAUDE", "codex": "CODEX"}


# A stand-in for `triage-gate.sh` that models the two gates independently: `check`
# blocks on a recent-active session (unless the operator skip signal is set) AND on
# spend over the ceiling; `spend` always reports the real % (never gated). The test
# drives it via TEST_SPEND_PCT / TEST_ACTIVE.
_STUB_GATE = """#!/bin/bash
pct="${TEST_SPEND_PCT:-0}"
if [ "$1" = "limits" ]; then
  if [ -n "$TEST_LIMITS" ]; then
    printf '%s\n' "$TEST_LIMITS"
  else
    echo "{\"windows\":[{\"used_percent\":19,\"window_minutes\":300,\"resets_at\":$(($(date +%s) + 18000))}]}"
  fi
  exit 0
fi
if [ "$1" = "spend" ]; then
  echo "spend: trailing-5h spend at ${pct}% of peak"; exit 0
fi
if [ "$TRIAGE_AGENT" = "claude" ] && [ -n "$TEST_CLAUDE_BLOCKED" ]; then
  echo "blocked: no claude capacity"; exit 1
fi
if [ "$TRIAGE_AGENT" = "codex" ] && [ -n "$TEST_CODEX_CLEAR" ]; then
  echo "burn-clear: stale weekly fallback"; exit 0
fi
if [ -z "$TRIAGE_SKIP_ACTIVITY" ] && [ -n "$TEST_ACTIVE" ]; then
  echo "blocked: a session was active in the last 10m"; exit 1
fi
if awk "BEGIN{exit !($pct>=40)}"; then
  echo "blocked: trailing-5h spend at ${pct}% of peak (threshold 40%)"; exit 1
fi
echo "clear: trailing-5h spend at ${pct}% of peak"; exit 0
"""


@pytest.fixture
def stub_gate(tmp_path, monkeypatch):
    gate = tmp_path / "triage-gate.sh"
    gate.write_text(_STUB_GATE)
    gate.chmod(0o755)
    monkeypatch.setattr(balancer, "_GATE", str(gate))
    return gate


def test_more_headroom_builds_other_reviews():
    claude = PoolStatus("claude", True, 20.0)   # more headroom
    codex = PoolStatus("codex", True, 60.0)
    assert choose_pair(claude, codex, RUNNERS) == ("CLAUDE", "CODEX")


def test_flips_when_codex_has_more_headroom():
    claude = PoolStatus("claude", True, 70.0)
    codex = PoolStatus("codex", True, 30.0)     # more headroom
    assert choose_pair(claude, codex, RUNNERS) == ("CODEX", "CLAUDE")


def test_single_pool_clear_is_single_tool_no_reviewer():
    claude = PoolStatus("claude", True, 50.0)
    codex = PoolStatus("codex", False, 100.0)
    assert choose_pair(claude, codex, RUNNERS) == ("CLAUDE", None)


def test_neither_clear_is_no_capacity():
    claude = PoolStatus("claude", False, 100.0)
    codex = PoolStatus("codex", False, 100.0)
    assert choose_pair(claude, codex, RUNNERS) == (None, None)


def test_pick_pair_block_msg_names_blocked_pools(stub_gate, monkeypatch):
    """When both pools are blocked the third return value names each pool and its reason
    so callers can surface it in deferral log lines instead of a bare 'no headroom'."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")   # activity gate fires; spend is fine
    _, _, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert "claude" in block_msg
    assert "codex" in block_msg
    # Each pool should carry something from the gate's check output
    assert "session" in block_msg or "active" in block_msg


def test_pick_pair_block_msg_empty_when_headroom(stub_gate, monkeypatch):
    """block_msg is empty string when at least one pool is clear and a builder is returned."""
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX")
    assert builder is not None
    assert block_msg == ""


def test_weekly_only_codex_ahead_of_pace_cannot_start_unattended_work(
        stub_gate, monkeypatch):
    """The observed weekly-only report must not become a new Codex session."""
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "64")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [{
        "used_percent": 64,
        "window_minutes": 10080,
        "resets_at": 1784566349,
    }]}))
    monkeypatch.setattr(time, "time", lambda: 1783985520.468)

    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert reviewer is None
    assert "codex" in block_msg
    assert "3.2% released" in block_msg


def test_weekly_pacing_does_not_replace_dashboard_raw_usage(stub_gate, monkeypatch):
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "64")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [{
        "used_percent": 64,
        "window_minutes": 10080,
        "resets_at": 1784566349,
    }]}))
    monkeypatch.setattr(time, "time", lambda: 1783985520.468)

    codex = next(pool for pool in pools() if pool["tool"] == "codex")

    assert codex == {
        "tool": "codex",
        "clear": True,
        "spent_pct": 64.0,
        "headroom_pct": 36.0,
    }


@pytest.mark.parametrize(("elapsed", "used_percent", "eligible"), [
    (0.25, 19, True),
    (0.25, 20, False),
    (0.50, 39, True),
    (0.50, 40, False),
    (1.00 - 1e-9, 79, True),
    (1.00 - 1e-9, 80, False),
])
def test_weekly_codex_dispatch_stays_below_released_allowance(
        stub_gate, monkeypatch, elapsed, used_percent, eligible):
    resets_at = 1784566349
    duration = 10080 * 60
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [{
        "used_percent": used_percent,
        "window_minutes": 10080,
        "resets_at": resets_at,
    }]}))
    monkeypatch.setattr(time, "time", lambda: resets_at - duration + elapsed * duration)

    builder, _, _ = pick_pair("CLAUDE", "CODEX")

    assert (builder == "CODEX") is eligible


@pytest.mark.parametrize("windows", [
    [
        {"used_percent": 10, "window_minutes": 300, "resets_at": 1784281949},
        {"used_percent": 40, "window_minutes": 10080, "resets_at": 1784566349},
    ],
    [
        {"used_percent": 40, "window_minutes": 10080, "resets_at": 1784566349},
        {"used_percent": 10, "window_minutes": 300, "resets_at": 1784281949},
    ],
])
def test_weekly_pacing_blocks_regardless_of_reported_window_order(
        stub_gate, monkeypatch, windows):
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": windows}))
    monkeypatch.setattr(time, "time", lambda: 1784263949)

    builder, _, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert "weekly spend" in block_msg


def test_short_window_block_wins_while_weekly_window_is_below_pace(
        stub_gate, monkeypatch):
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "40")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [
        {"used_percent": 39, "window_minutes": 10080, "resets_at": 1784566349},
        {"used_percent": 40, "window_minutes": 300, "resets_at": 1784281949},
    ]}))
    monkeypatch.setattr(time, "time", lambda: 1784263949)

    builder, _, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert "trailing-5h" in block_msg


@pytest.mark.parametrize("facts", [
    "not json",
    "{}",
    '{"windows":[]}',
    '{"windows":[{"used_percent":10,"window_minutes":60,"resets_at":1784566349}]}',
    '{"windows":[{"used_percent":"10","window_minutes":10080,"resets_at":1784566349}]}',
    '{"windows":[{"used_percent":10,"window_minutes":1e309,"resets_at":1784566349}]}',
    '{"windows":[{"used_percent":10,"window_minutes":10080}]}',
])
def test_unknown_codex_limit_facts_fail_closed(stub_gate, monkeypatch, facts):
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_LIMITS", facts)

    builder, _, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert "limit facts unavailable" in block_msg


def test_temporally_impossible_short_window_fails_closed(stub_gate, monkeypatch):
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [{
        "used_percent": 10,
        "window_minutes": 300,
        "resets_at": 4102444800,
    }]}))
    monkeypatch.setattr(time, "time", lambda: 1783985520.468)

    builder, _, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert "300-minute limit facts are stale" in block_msg


def test_operator_dispatch_ignores_active_session(stub_gate, monkeypatch):
    """A by-hand build fired while a session is active (and spend well under the
    ceiling) still gets a full pair — the recent-activity guard is skipped. Fails
    for the daemon (operator=False) below, which is the point of the flag."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=True)
    assert builder is not None


def test_operator_dispatch_is_not_weekly_paced(stub_gate, monkeypatch):
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "64")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [{
        "used_percent": 64,
        "window_minutes": 10080,
        "resets_at": 1784566349,
    }]}))
    monkeypatch.setattr(time, "time", lambda: 1783985520.468)

    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=True)

    assert builder == "CODEX"


def test_daemon_dispatch_still_respects_active_session(stub_gate, monkeypatch):
    """Unattended dispatch (operator=False) keeps the full guard: an active session
    defers both pools, so no capacity this cycle."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert builder is None


def test_operator_dispatch_still_honors_spend_ceiling(stub_gate, monkeypatch):
    """The spend ceiling is NOT bypassed: a genuinely rate-limited pool still defers
    even for a by-hand build."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "88")   # over the 40% ceiling
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=True)
    assert builder is None


@pytest.mark.parametrize("stdout,rc,expected", [
    ("clear: trailing-5h spend at 23% of peak", 0, 23.0),
    ("clear: trailing-5h spend at 7.5% of limit", 0, 7.5),
    ("blocked: trailing-5h spend at 88% of peak (threshold 40%)", 1, 88.0),
    ("blocked: interactive session on a tty", 1, 100.0),   # unparsed + blocked -> no headroom
    ("clear: something unparsed", 0, 100.0),               # unknown facts fail closed
])
def test_parse_pct(stdout, rc, expected):
    assert parse_pct(stdout, rc) == expected
