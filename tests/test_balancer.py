"""Balancer decision + gate-output parsing — the pure surfaces."""

import json
import os
import time

import pytest

from agentflow import balancer
from agentflow.balancer import (PoolStatus, RateLimitWindow, _codex_dispatch_status,
                                 choose_pair, choose_reviewer, parse_pct, pick_pair)
from agentflow.coordinator import quota
from agentflow.coordinator.store import default_store_path
from agentflow.dashboard_data import pools

RUNNERS = {"claude": "CLAUDE", "codex": "CODEX"}

def _limits(observed_at, windows):
    return json.dumps({"observed_at": observed_at, "windows": windows})


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Point the durable store (and the Claude quota fact beside it) at a scratch dir, for *every*
    balancer test. Claude's clear/blocked status is now read from the persisted five-hour quota
    fact (#305/#307), so a test that leaves the store to the ambient `~/.agentflow` reads whatever
    the live daemon last wrote there — order-dependent and machine-dependent. Autouse makes each
    test hermetic: it starts with an empty store (Claude bootstrapping/blocked) and seeds a fact
    with `_seed_claude_quota` only when it wants Claude to have headroom."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    return tmp_path


def _seed_claude_quota(used_percent, *, now=None, resets_at=None, observed_at=None,
                       weekly_percent=1.0):
    """Persist a fresh Claude five-hour quota fact — the provider-authored dispatch authority the
    balancer reads instead of the retired transcript proxy (#305) — plus a fresh seven-day fact
    under its released weekly allowance, so a pool with headroom clears both windows the balancer
    now enforces (#315). A test that means to block on weekly pacing seeds a high `weekly_percent`
    or its own seven-day fact with `_seed_claude_weekly`."""
    now = int(time.time()) if now is None else int(now)
    resets_at = now + 4 * 60 * 60 if resets_at is None else int(resets_at)
    observed_at = now if observed_at is None else int(observed_at)
    quota.record_quota(default_store_path(), quota.QuotaFact(
        pool="claude", used_percent=used_percent, resets_at=resets_at,
        observed_at=observed_at, provenance="claude:rate_limit_event"))
    _seed_claude_weekly(weekly_percent, now=now)


def _seed_claude_weekly(used_percent, *, now=None, resets_at=None, observed_at=None):
    """Persist a fresh Claude seven-day quota fact — the paced weekly allowance window (#315)."""
    now = int(time.time()) if now is None else int(now)
    resets_at = now + 3 * 24 * 60 * 60 if resets_at is None else int(resets_at)
    observed_at = now if observed_at is None else int(observed_at)
    quota.record_quota(default_store_path(), quota.QuotaFact(
        pool="claude", used_percent=used_percent, resets_at=resets_at,
        observed_at=observed_at, provenance="oauth:seven_day", window=quota.SEVEN_DAY))


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


# --- choose_reviewer: ADR 0020 review under partial availability -----------------
_CS = PoolStatus("claude", True, 20.0)
_XS = PoolStatus("codex", True, 60.0)


def test_reviewer_prefers_cross_tool_when_both_free():
    assert choose_reviewer("claude", _CS, _XS) == "codex"
    assert choose_reviewer("codex", _CS, _XS) == "claude"


def test_reviewer_falls_back_same_tool_when_cross_tool_exhausted():
    # Codex out of headroom (the real symptom): a claude-built PR reviews same-tool
    # rather than stalling — the gate parks it, so it is reviewed but never auto-merged.
    codex_dead = PoolStatus("codex", False, 100.0)
    assert choose_reviewer("claude", _CS, codex_dead) == "claude"


def test_reviewer_uses_cross_tool_when_builders_own_pool_is_the_dead_one():
    codex_dead = PoolStatus("codex", False, 100.0)
    # A codex-built PR still reviews cross-tool on the free claude pool.
    assert choose_reviewer("codex", _CS, codex_dead) == "claude"


def test_reviewer_defers_when_neither_pool_free():
    claude_dead = PoolStatus("claude", False, 100.0)
    codex_dead = PoolStatus("codex", False, 100.0)
    assert choose_reviewer("claude", claude_dead, codex_dead) is None
    assert choose_reviewer("codex", claude_dead, codex_dead) is None


def test_pick_pair_block_msg_names_blocked_pools(stub_gate, isolate_state, monkeypatch):
    """When both pools are blocked the third return value names each pool and its reason
    so callers can surface it in deferral log lines instead of a bare 'no headroom'."""
    monkeypatch.setenv("TEST_SPEND_PCT", "88")   # codex genuinely over the ceiling
    _seed_claude_quota(95)                        # Claude's provider fact is over its ceiling too
    _, _, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert "claude" in block_msg
    assert "codex" in block_msg
    # Claude's block cites the provider five-hour utilization, not the retired transcript proxy.
    assert "utilization" in block_msg


def test_pick_pair_block_msg_empty_when_headroom(stub_gate, isolate_state, monkeypatch):
    """block_msg is empty string when at least one pool is clear and a builder is returned."""
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    _seed_claude_quota(19)                         # Claude's provider fact is well under its ceiling
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX")
    assert builder is not None
    assert block_msg == ""


# --- issue #309: a cold/parked fleet must seed its own Claude dispatch --------------------
def test_cold_pool_bootstraps_from_transcript_estimate(stub_gate, isolate_state, monkeypatch):
    """A cold daemon has never persisted a Claude quota fact (empty quota/). Before #309 this
    wedged the pool closed ('pool bootstrapping') until an operator ran an interactive turn — an
    unattended fleet never did, so the whole thing deadlocked. Now the trailing-five-hour proxy
    seeds a bootstrap estimate so a cold pool dispatches on its own."""
    monkeypatch.setenv("TEST_SPEND_PCT", "20")   # proxy well under the idle ceiling
    claude = next(p for p in pools() if p["tool"] == "claude")
    assert claude["clear"]
    assert claude["spent_pct"] == 20.0


def test_cold_pool_bootstrap_estimate_still_respects_ceiling(
        stub_gate, isolate_state, monkeypatch):
    """The bootstrap estimate is a real gate, not an open door: an over-ceiling proxy still holds
    a cold pool closed rather than dispatching blind, and the deferral reason marks it an estimate
    so it is not mistaken for a measured provider utilization."""
    monkeypatch.setenv("TEST_SPEND_PCT", "90")
    _, _, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert "claude" in block_msg
    assert "bootstrap estimate" in block_msg


def test_provider_fact_wins_over_bootstrap_estimate(stub_gate, isolate_state, monkeypatch):
    """A real provider fact always wins: a healthy 19% provider reading keeps the pool clear even
    though the trailing-five-hour estimate alone would read over-ceiling."""
    monkeypatch.setenv("TEST_SPEND_PCT", "90")   # estimate alone would block
    _seed_claude_quota(19)                        # but the provider fact says there is headroom
    claude = next(p for p in pools() if p["tool"] == "claude")
    assert claude["clear"]
    assert claude["spent_pct"] == 19.0


def test_provider_fact_blocks_even_when_estimate_would_clear(
        stub_gate, isolate_state, monkeypatch):
    """The converse: an over-ceiling provider fact holds the pool closed even when the estimate
    alone would clear — the estimate is only consulted when there is no provider fact."""
    monkeypatch.setenv("TEST_SPEND_PCT", "10")   # estimate alone would clear
    _seed_claude_quota(95)                        # provider fact is over its ceiling
    claude = next(p for p in pools() if p["tool"] == "claude")
    assert not claude["clear"]


def test_parked_pool_resumes_after_window_reset(stub_gate, isolate_state, monkeypatch):
    """Acceptance #2: a fully parked fleet writes no new fact, but once the observed five-hour
    window has reset the last fact reads as fresh (0%), so dispatch resumes on the next cycle
    without a human seeding it — regardless of the estimate proxy."""
    now = time.time()
    _seed_claude_quota(95, now=now - 6 * 3600, resets_at=int(now - 3600),
                       observed_at=int(now - 6 * 3600))
    monkeypatch.setenv("TEST_SPEND_PCT", "90")   # estimate is irrelevant once the fact resets
    claude = next(p for p in pools() if p["tool"] == "claude")
    assert claude["clear"]
    assert claude["spent_pct"] == 0.0


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
    assert "11.4% released" in block_msg


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


@pytest.mark.parametrize(("elapsed_days", "used_percent", "eligible"), [
    (0, 10, True),
    (0, 80 / 7, False),
    (1 - 1 / 86400, 11.43, False),
    (1, 11.43, True),
    (1, 160 / 7, False),
    (6, 79, True),
    (6, 80, False),
])
def test_weekly_codex_dispatch_stays_below_released_allowance(
        stub_gate, monkeypatch, elapsed_days, used_percent, eligible):
    resets_at = 1784566349
    duration = 10080 * 60
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [{
        "used_percent": used_percent,
        "window_minutes": 10080,
        "resets_at": resets_at,
    }]}))
    monkeypatch.setattr(
        time, "time", lambda: resets_at - duration + elapsed_days * 24 * 60 * 60)

    builder, _, _ = pick_pair("CLAUDE", "CODEX")

    assert (builder == "CODEX") is eligible


@pytest.mark.parametrize("windows", [
    [
        {"used_percent": 10, "window_minutes": 300, "resets_at": 1784281949},
        {"used_percent": 46, "window_minutes": 10080, "resets_at": 1784566349},
    ],
    [
        {"used_percent": 46, "window_minutes": 10080, "resets_at": 1784566349},
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
def test_unknown_codex_limit_facts_fail_closed(stub_gate, isolate_state, monkeypatch, facts):
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_LIMITS", facts)

    builder, _, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert "limit facts unavailable" in block_msg


@pytest.mark.parametrize("field", ["used_percent", "window_minutes", "resets_at"])
def test_oversized_codex_limit_facts_fail_closed(stub_gate, isolate_state, monkeypatch, field):
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


def test_idle_pool_reports_not_active(stub_gate, isolate_state, monkeypatch):
    """With no operator activity a pool reports idle and dispatches under the 85% ceiling."""
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    _seed_claude_quota(19)
    status = balancer._query_pool("claude")
    assert status.active is False
    assert status.ceiling == 85.0
    assert status.clear is True


def test_operator_dispatch_ignores_active_session(stub_gate, isolate_state, monkeypatch):
    """A by-hand build fired while a session is active (and spend well under the
    ceiling) still gets a full pair — the recent-activity guard is skipped. Fails
    for the daemon (operator=False) below, which is the point of the flag."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    _seed_claude_quota(19)
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


def test_active_operator_lowers_the_ceiling_but_does_not_stop_dispatch(
        stub_gate, isolate_state, monkeypatch):
    """ADR 0025: an operator working interactively no longer hard-stops the daemon — it
    yields to a lower ceiling. With spend well under the active ceiling, unattended dispatch
    still gets a full pair, and each pool reports the operator-active fact."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    _seed_claude_quota(19)
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert builder is not None
    assert balancer._query_pool("claude").active is True


def test_active_operator_defers_above_the_active_ceiling_yielding_not_stopping(
        stub_gate, isolate_state, monkeypatch):
    """Above the (lowered) active ceiling the daemon defers — but the reason says it is
    yielding to the operator at the active ceiling, not a mute 'busy' (ADR 0025)."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "19")
    _seed_claude_quota(19)
    monkeypatch.setattr(balancer, "ACTIVE_CEILING_PCT", 10.0)   # push utilization above it
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=False)
    assert builder is None
    assert "yielding to operator" in block_msg
    assert "ceiling 10%" in block_msg


def test_operator_dispatch_still_honors_spend_ceiling(stub_gate, isolate_state, monkeypatch):
    """The utilization ceiling is NOT bypassed: a genuinely rate-limited pool still defers
    even for a by-hand build."""
    monkeypatch.setenv("TEST_ACTIVE", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "88")   # codex over its ceiling
    _seed_claude_quota(95)                        # Claude's provider fact over its ceiling
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", operator=True)
    assert builder is None


def test_expired_weekly_window_clears_without_transcript_write():
    """Regression for #319: a 10080-minute window at 63% whose resets_at has passed must
    dispatch-clear without any new transcript write.  This is the exact reproduction from
    the issue — it failed as 'stale' before the rollover normalization was added."""
    now = 2000.0
    window = RateLimitWindow(used_percent=63.0, window_minutes=10080, resets_at=1900.0)
    status = PoolStatus("codex", True, 63.0, "", (window,))

    result = _codex_dispatch_status(status, now)

    assert result.clear, f"post-reset Codex should be clear, got: {result.reason!r}"
    assert result.spent_pct == 0.0


def test_expired_short_window_also_normalizes(stub_gate, monkeypatch):
    """An expired 300-minute window must not keep the pool blocked via the utilization ceiling
    after reset (trap called out in the issue)."""
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    resets_at = 1000.0
    monkeypatch.setattr(time, "time", lambda: 2000.0)
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [
        {"used_percent": 90, "window_minutes": 300, "resets_at": resets_at},
        {"used_percent": 90, "window_minutes": 10080, "resets_at": resets_at},
    ]}))

    builder, _, _ = pick_pair("CLAUDE", "CODEX")

    assert builder == "CODEX"


def test_live_short_window_and_expired_weekly_window_are_normalized_independently(
        stub_gate, monkeypatch):
    """A live 300-minute window retains its reported headroom while an expired 10080-minute
    window contributes 0% to weekly pacing, and vice versa."""
    monkeypatch.setenv("TEST_CLAUDE_BLOCKED", "1")
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    now = 2000.0
    monkeypatch.setattr(time, "time", lambda: now)
    # 300-minute window is live; 10080-minute window has expired
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [
        {"used_percent": 20, "window_minutes": 300, "resets_at": now + 1000},
        {"used_percent": 90, "window_minutes": 10080, "resets_at": now - 100},
    ]}))

    builder, _, block_msg = pick_pair("CLAUDE", "CODEX")

    # Codex should dispatch: short window is live at 20% (well under ceiling),
    # and the weekly window has expired so it reads as 0%.
    assert builder == "CODEX", f"expected CODEX to dispatch, got block: {block_msg!r}"


@pytest.mark.parametrize("stdout,rc,expected", [
    ("clear: trailing-5h spend at 23% of peak", 0, 23.0),
    ("clear: trailing-5h spend at 7.5% of limit", 0, 7.5),
    ("blocked: trailing-5h spend at 88% of peak (threshold 40%)", 1, 88.0),
    ("blocked: interactive session on a tty", 1, 100.0),   # unparsed + blocked -> no headroom
    ("clear: something unparsed", 0, 100.0),               # unknown facts fail closed
])
def test_parse_pct(stdout, rc, expected):
    assert parse_pct(stdout, rc) == expected
