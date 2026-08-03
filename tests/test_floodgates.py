"""Floodgates — the fleet-wide/per-dispatch headroom override (ADR 0025 amendment)."""

import json
import time

import pytest

from agentflow import balancer
from agentflow.balancer import floodgates_active, pick_pair
from agentflow.state import state_path

from tests.test_balancer import _STUB_GATE, _seed_claude_quota, _seed_claude_weekly


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    monkeypatch.delenv("AGENTFLOW_FLOODGATES", raising=False)
    return tmp_path


@pytest.fixture
def stub_gate(tmp_path, monkeypatch):
    gate = tmp_path / "triage-gate.sh"
    gate.write_text(_STUB_GATE)
    gate.chmod(0o755)
    monkeypatch.setattr(balancer, "_GATE", str(gate))
    return gate


def _weekly_ratchet_scenario(monkeypatch):
    """Both pools blocked purely on weekly pacing with healthy five-hour/short-window
    readings (mirrors test_balancer's ratchet tests) — the shape floodgates must clear."""
    monkeypatch.setenv("TEST_CODEX_CLEAR", "1")
    monkeypatch.setenv("TEST_SPEND_PCT", "10")
    monkeypatch.setattr(time, "time", lambda: 1783985520.468)
    monkeypatch.setenv("TEST_LIMITS", json.dumps({"windows": [{
        "used_percent": 64,
        "window_minutes": 10080,
        "resets_at": 1784566349,
    }]}))
    _seed_claude_quota(10, weekly_percent=60.0, now=1783985520, resets_at=1783985520 + 4 * 3600,
                       observed_at=1783985520)


# --- floodgates_active(): env + flag file --------------------------------------------------

def test_floodgates_active_false_by_default():
    assert floodgates_active() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_floodgates_active_true_for_truthy_env(monkeypatch, value):
    monkeypatch.setenv("AGENTFLOW_FLOODGATES", value)
    assert floodgates_active() is True


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_floodgates_active_false_for_non_truthy_env(monkeypatch, value):
    monkeypatch.setenv("AGENTFLOW_FLOODGATES", value)
    assert floodgates_active() is False


def test_floodgates_active_true_when_flag_file_present():
    flag = state_path("floodgates")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("123\n")
    assert floodgates_active() is True


def test_floodgates_active_evaluated_fresh_no_caching():
    """Toggling the flag file must be visible on the very next call — no memoization."""
    assert floodgates_active() is False
    flag = state_path("floodgates")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("123\n")
    assert floodgates_active() is True
    flag.unlink()
    assert floodgates_active() is False


# --- fleet-wide toggle lifts the weekly ratchet and ceiling --------------------------------

def test_flag_absent_weekly_ratchet_still_blocks_both_pools(stub_gate, monkeypatch):
    _weekly_ratchet_scenario(monkeypatch)
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX")
    assert builder is None
    assert "codex" in block_msg


def test_flag_file_open_dispatches_through_the_weekly_ratchet(stub_gate, monkeypatch):
    """This is the case that must fail without the feature: both pools are blocked purely on
    weekly pacing while comfortably under their five-hour/short-window ceilings, and the flag
    file lifts it so `pick_pair` dispatches anyway."""
    _weekly_ratchet_scenario(monkeypatch)
    flag = state_path("floodgates")
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("123\n")

    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is not None
    assert block_msg == ""


def test_env_toggle_lifts_the_idle_ceiling_to_100(stub_gate, monkeypatch):
    """A spend comfortably above the ordinary idle ceiling (85%) still dispatches under
    floodgates, because the ceiling itself is raised to 100."""
    monkeypatch.setenv("TEST_SPEND_PCT", "90")
    _seed_claude_quota(92, weekly_percent=1.0)
    monkeypatch.setenv("AGENTFLOW_FLOODGATES", "1")

    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX")

    assert builder is not None
    assert block_msg == ""


# --- per-dispatch bypass: scoped to one pick_pair call, global stays closed ----------------

def test_per_dispatch_floodgates_bypasses_while_global_stays_closed(stub_gate, monkeypatch):
    _weekly_ratchet_scenario(monkeypatch)
    assert floodgates_active() is False

    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX", floodgates=True)
    assert builder is not None
    assert block_msg == ""

    # The very next call with no override sees the ordinary (still-closed) policy again.
    builder, reviewer, block_msg = pick_pair("CLAUDE", "CODEX")
    assert builder is None


# --- the hard permit ledger is untouched ---------------------------------------------------

def test_floodgates_does_not_touch_ceiling_for_signature_when_not_active():
    assert balancer.ceiling_for(False) == balancer.IDLE_CEILING_PCT
    assert balancer.ceiling_for(True) == balancer.ACTIVE_CEILING_PCT
    assert balancer.ceiling_for(False, floodgates=True) == 100.0


# --- _ProductionGate: global toggle takes effect on the very next dispatch decision --------

class _Record:
    def __init__(self, pool, floodgates=False, interactive=False, stage="build"):
        self.pool = pool
        self.floodgates = floodgates
        self.interactive = interactive
        self.stage = stage


def test_global_floodgates_opening_mid_cycle_lifts_a_plain_records_cached_block(monkeypatch):
    """A pool status cached as blocked earlier in the cycle (for a plain, non-floodgates
    record) must not keep refusing a second plain record on the same pool once the operator
    opens the fleet-wide toggle mid-cycle — the toggle must take effect on the very next
    dispatch decision, not merely on the next cycle (ADR 0025 amendment)."""
    from agentflow.pipeline import _ProductionGate

    blocked = balancer.PoolStatus(tool="claude", clear=False, spent_pct=90.0, reason="blocked",
                                   active=False, ceiling=85.0)
    clear = balancer.PoolStatus(tool="claude", clear=True, spent_pct=10.0, active=False,
                                 ceiling=100.0)

    def fake_query_pool(tool, *, floodgates=False, **kwargs):
        return clear if floodgates else blocked

    monkeypatch.setattr(balancer, "_query_pool", fake_query_pool)
    monkeypatch.setattr(balancer, "_claude_dispatch_status", lambda status, now, floodgates=False: status)
    global_flag = {"on": False}
    monkeypatch.setattr(balancer, "floodgates_active", lambda: global_flag["on"])

    gate = _ProductionGate()

    first = _Record(pool="claude")
    assert gate(first) is False  # cached as blocked, global flag still closed

    global_flag["on"] = True  # operator opens the fleet-wide toggle mid-cycle

    second = _Record(pool="claude")  # a second plain record, same pool, same cycle
    assert gate(second) is True


# --- _ProductionGate: floodgates also skips the Claude in-flight reservation ---------------

def test_floodgates_skips_claude_inflight_reservation(monkeypatch):
    """3 permits in flight on Claude at 61% five-hour utilization reserve
    3 * CLAUDE_INFLIGHT_RESERVE_PCT (15%) = 45% of projected headroom, pushing the effective
    read to 106% against a 100% ceiling — blocked under the ordinary policy even though the
    raw status is clear. Floodgates must skip this reservation adjustment entirely, leaving
    only the hard permit ledger as a concurrency brake (ADR 0025 amendment)."""
    from agentflow.pipeline import _ProductionGate

    clear_but_tight = balancer.PoolStatus(tool="claude", clear=True, spent_pct=61.0,
                                           active=False, ceiling=100.0)

    monkeypatch.setattr(balancer, "_query_pool",
                         lambda tool, *, floodgates=False, **kwargs: clear_but_tight)
    monkeypatch.setattr(balancer, "_claude_dispatch_status",
                         lambda status, now, floodgates=False: status)
    monkeypatch.setattr(balancer, "floodgates_active", lambda: False)

    gate = _ProductionGate()
    gate._running_permits = lambda pool: 3

    plain = _Record(pool="claude")
    assert gate(plain) is False  # blocked: reservation pushes projected usage past ceiling

    gate.begin_cycle("claude")
    floodgated = _Record(pool="claude", floodgates=True)
    assert gate(floodgated) is True  # floodgates skips the reservation adjustment
