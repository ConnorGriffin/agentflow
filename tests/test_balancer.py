"""Balancer decision + gate-output parsing — the pure surfaces."""

import json
import os
import time

import pytest

from agentflow import balancer
from agentflow.balancer import PoolStatus, choose_pair, parse_pct, pick_pair
from agentflow.dashboard_data import pools

RUNNERS = {"claude": "CLAUDE", "codex": "CODEX"}

# The observed 80%-weekly lockout from issue #75: at this instant only ~4.1% of the
# unattended weekly allowance has been released, so 80% used correctly blocks — yet the
# pool can never dispatch a session to learn its limits have reset.
_NOW = 1784027236
_STARVED_WEEKLY = {"used_percent": 80, "window_minutes": 10080, "resets_at": 1784601070}


def _limits(observed_at, windows):
    return json.dumps({"observed_at": observed_at, "windows": windows})


class _FakeCodex:
    """A CodexRunner stand-in whose probe records the call and optionally lands fresh
    facts (by rewriting the stub gate's reported limits), the way a real probe would."""

    def __init__(self, on_probe=None):
        self.calls = 0
        self._on_probe = on_probe

    def probe(self):
        self.calls += 1
        return self._on_probe() if self._on_probe else True


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
    monkeypatch.setenv("TEST_SPEND_PCT", "88")   # both pools genuinely over the ceiling
    _, _, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert "claude" in block_msg
    assert "codex" in block_msg
    # Each pool should carry something from the gate's check output
    assert "spend" in block_msg or "trailing" in block_msg


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


@pytest.mark.parametrize("field", ["used_percent", "window_minutes", "resets_at"])
def test_oversized_codex_limit_facts_fail_closed(stub_gate, monkeypatch, field):
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    facts = {
        "used_percent": 10,
        "window_minutes": 10080,
        "resets_at": 1784566349,
    }
    facts[field] = 10**1000
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [facts]}))

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


def test_ceiling_policy_is_named_config():
    """The idle/active ceilings and pace are the ADR 0025 policy dials — 85 / 50 / 1."""
    assert balancer.IDLE_CEILING_PCT == 85.0
    assert balancer.ACTIVE_CEILING_PCT == 50.0
    assert balancer.ACTIVE_PACE == 1
    assert balancer.ceiling_for(active=False) == 85.0
    assert balancer.ceiling_for(active=True) == 50.0


def test_idle_pool_reports_not_active(stub_gate, monkeypatch):
    """With no operator activity a pool reports idle and dispatches under the 85% ceiling."""
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    status = balancer._query_pool("claude")
    assert status.active is False
    assert status.ceiling == 85.0
    assert status.clear is True


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


def test_active_operator_lowers_the_ceiling_but_does_not_stop_dispatch(stub_gate, monkeypatch):
    """ADR 0025: an operator working interactively no longer hard-stops the daemon — it
    yields to a lower ceiling. With spend well under the active ceiling, unattended dispatch
    still gets a full pair, and each pool reports the operator-active fact."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert builder is not None
    assert balancer._query_pool("claude").active is True


def test_active_operator_defers_above_the_active_ceiling_yielding_not_stopping(
        stub_gate, monkeypatch):
    """Above the (lowered) active ceiling the daemon defers — but the reason says it is
    yielding to the operator at the active ceiling, not a mute 'busy' (ADR 0025)."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    monkeypatch.setattr(balancer, "ACTIVE_CEILING_PCT", 10.0)   # push spend above it
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert builder is None
    assert "yielding to operator" in block_msg
    assert "ceiling 10%" in block_msg


def test_operator_dispatch_still_honors_spend_ceiling(stub_gate, monkeypatch):
    """The spend ceiling is NOT bypassed: a genuinely rate-limited pool still defers
    even for a by-hand build."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "88")   # over the 40% ceiling
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=True)
    assert builder is None


@pytest.fixture
def codex_refresh(stub_gate, tmp_path, monkeypatch):
    """Common wiring for the active-refresh path: claude blocked so codex is the sole
    builder, the gate's check clear, a fixed clock, and probe-attempt state isolated to
    a temp file so the cooldown persists within the test but never touches real state."""
    monkeypatch.setattr(balancer, "_PROBE_STATE", tmp_path / "codex-refresh.json")
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    clock = {"now": float(_NOW)}
    monkeypatch.setattr(time, "time", lambda: clock["now"])
    return clock


def test_stale_weekly_block_refreshes_facts_and_dispatches(codex_refresh, monkeypatch):
    """The lockout recovery: a weekly-paced block on a fact older than the refresh age
    runs one probe, re-reads the now-reset facts once, and dispatches codex — no operator."""
    monkeypatch.setenv("TEST_LIMITS", _limits(_NOW - 22000, [_STARVED_WEEKLY]))
    fresh = _limits(_NOW - 10, [{"used_percent": 0, "window_minutes": 10080,
                                 "resets_at": 1784601071}])

    def land_fresh():
        os.environ["TEST_LIMITS"] = fresh
        return True

    codex = _FakeCodex(land_fresh)
    builder, reviewer, block_msg = pick_pair("CLAUDE", codex)

    assert codex.calls == 1
    assert builder is codex


def test_exhausted_pool_probes_at_most_once_per_refresh_window(codex_refresh, monkeypatch):
    """A genuinely exhausted pool whose probe cannot clear it (facts stay stale) is
    probed at most once per window: no second probe until the full window has elapsed."""
    monkeypatch.setenv("TEST_LIMITS", _limits(_NOW - 22000, [_STARVED_WEEKLY]))
    codex = _FakeCodex(lambda: False)   # probe runs but the pool is really exhausted

    builder, _, _ = pick_pair("CLAUDE", codex)
    assert builder is None and codex.calls == 1

    codex_refresh["now"] = _NOW + balancer.CODEX_LIMIT_REFRESH_SECONDS - 1
    builder, _, _ = pick_pair("CLAUDE", codex)
    assert builder is None and codex.calls == 1   # still within the cooldown

    codex_refresh["now"] = _NOW + balancer.CODEX_LIMIT_REFRESH_SECONDS
    builder, _, _ = pick_pair("CLAUDE", codex)
    assert builder is None and codex.calls == 2   # window elapsed → a new attempt


def test_block_just_under_refresh_age_does_not_probe(codex_refresh, monkeypatch):
    """A blocking fact one second short of the refresh age is left alone."""
    just_fresh = _NOW - (balancer.CODEX_LIMIT_REFRESH_SECONDS - 1)
    monkeypatch.setenv("TEST_LIMITS", _limits(just_fresh, [_STARVED_WEEKLY]))
    codex = _FakeCodex()

    builder, _, _ = pick_pair("CLAUDE", codex)

    assert builder is None and codex.calls == 0


def test_operator_activity_block_does_not_probe(codex_refresh, monkeypatch):
    """Even with an old, pacing-blocked fact, an operator working on the pool is never
    displaced by a probe — the fleet yields entirely."""
    monkeypatch.delenv("TEST_CODEX_CLEAR", raising=False)   # let the activity check run
    monkeypatch.setenv("TEST_ACTIVE", "1")
    paced = {"used_percent": 30, "window_minutes": 10080, "resets_at": 1784601070}
    monkeypatch.setenv("TEST_LIMITS", _limits(_NOW - 22000, [paced]))
    codex = _FakeCodex()

    builder, _, _ = pick_pair("CLAUDE", codex)

    assert codex.calls == 0


def test_short_window_capacity_block_does_not_probe(codex_refresh, monkeypatch):
    """An independently exhausted 300-minute window is real no-capacity, not stale
    pacing — it never triggers a probe however old the fact is."""
    monkeypatch.setenv("TEST_LIMITS", _limits(_NOW - 22000, [
        {"used_percent": 90, "window_minutes": 300, "resets_at": 1784601070},
        {"used_percent": 5, "window_minutes": 10080, "resets_at": 1784601070},
    ]))
    codex = _FakeCodex()

    builder, _, _ = pick_pair("CLAUDE", codex)

    assert builder is None and codex.calls == 0


def test_malformed_facts_do_not_probe(codex_refresh, monkeypatch):
    """Unparseable facts fail closed to blocked without a probe."""
    monkeypatch.setenv("TEST_LIMITS", "not json")
    codex = _FakeCodex()

    builder, _, block_msg = pick_pair("CLAUDE", codex)

    assert builder is None and codex.calls == 0
    assert "limit facts unavailable" in block_msg


def test_refresh_default_window_is_six_hours():
    assert balancer.CODEX_LIMIT_REFRESH_SECONDS == 21600.0


@pytest.mark.parametrize("stdout,rc,expected", [
    ("clear: trailing-5h spend at 23% of peak", 0, 23.0),
    ("clear: trailing-5h spend at 7.5% of limit", 0, 7.5),
    ("blocked: trailing-5h spend at 88% of peak (threshold 40%)", 1, 88.0),
    ("blocked: interactive session on a tty", 1, 100.0),   # unparsed + blocked -> no headroom
    ("clear: something unparsed", 0, 100.0),               # unknown facts fail closed
])
def test_parse_pct(stdout, rc, expected):
    assert parse_pct(stdout, rc) == expected
