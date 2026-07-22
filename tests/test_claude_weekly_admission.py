"""Claude's paced weekly allowance (#315), driven through the public dispatch/coordinator seam.

Claude now gates a new unattended session on two independent provider windows: the five-hour
headroom window (issue #305) AND a seven-day window whose 80% unattended allowance is released in
seven equal daily steps from the window's own start — the same pacing Codex uses (ADR 0006). These
tests exercise that second constraint through `pick_pair` (the assignment surface) and the real
production launch gate, never a private hook: independent resets, the daily boundaries, strict
equality, a missing/stale weekly window, and the five-hour-available/weekly-blocked case that
pre-#315 code (five-hour only) would have wrongly admitted.
"""

from __future__ import annotations

import time

from agentflow import balancer, coordinated_build
from agentflow.coordinator import quota
from agentflow.coordinator.quota import SEVEN_DAY_SECONDS
from agentflow.coordinator.record import Record
from agentflow.coordinator.store import default_store_path

_DAY = 24 * 60 * 60


def _clear_activity_gate(tmp_path, monkeypatch):
    """A stub gate that always reports idle+clear, so only the persisted quota facts (not the
    personal-tooling gate) decide Claude headroom. Codex has no `limits` facts under this stub, so
    it fails closed and Claude alone decides `pick_pair`."""
    gate = tmp_path / "triage-gate.sh"
    gate.write_text("#!/bin/bash\necho clear\nexit 0\n")
    gate.chmod(0o755)
    monkeypatch.setattr(balancer, "_GATE", str(gate))


def _seed_five_hour(used_percent, *, now):
    """A fresh, clearly-under-ceiling five-hour fact, so the five-hour gate never decides."""
    quota.record_quota(default_store_path(), quota.QuotaFact(
        pool="claude", used_percent=used_percent, resets_at=now + 4 * 60 * 60,
        observed_at=now, provenance="oauth:five_hour"))


def _seed_weekly(used_percent, *, now, elapsed=3600):
    """A seven-day fact observed ``elapsed`` seconds into its own window (default: one hour in,
    i.e. day 0). ``resets_at`` sits one full window past that start."""
    quota.record_quota(default_store_path(), quota.QuotaFact(
        pool="claude", used_percent=used_percent, resets_at=now + SEVEN_DAY_SECONDS - elapsed,
        observed_at=now, provenance="oauth:seven_day", window=quota.SEVEN_DAY))


def test_weekly_below_the_released_allowance_admits_claude(coord_state, monkeypatch):
    """Day 0 releases 80/7 ≈ 11.4%. Weekly usage of 5% is below it, so — with five-hour headroom —
    Claude is eligible."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(10.0, now=now)
    _seed_weekly(5.0, now=now)

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder == "CLAUDE" and block_msg == ""


def test_five_hour_headroom_cannot_admit_while_weekly_is_ahead_of_pace(coord_state, monkeypatch):
    """The load-bearing case: five-hour utilization is a healthy 10% (well under the 85% ceiling),
    but weekly usage of 20% is already past day 0's ~11.4% released allowance. Pre-#315 code, which
    gated Claude on the five-hour window alone, would have admitted here — the weekly constraint is
    what defers it, and the deferral names the weekly pacing block, not a headroom block."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(10.0, now=now)
    _seed_weekly(20.0, now=now)

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert "claude" in block_msg and "weekly spend at 20%" in block_msg
    assert "released for unattended work" in block_msg


def test_weekly_usage_equal_to_the_released_allowance_blocks(coord_state, monkeypatch):
    """Strict inequality: on the seventh day the full 80% is released, and usage of exactly 80%
    is *not* below it, so it blocks."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(10.0, now=now)
    _seed_weekly(80.0, now=now, elapsed=int(6.5 * _DAY))   # day 6 → 80% released

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder is None and "weekly spend at 80%" in block_msg


def test_a_daily_boundary_releases_more_weekly_allowance(coord_state, monkeypatch):
    """The same 15% weekly usage that is over day 0's 11.4% allowance is under day 1's 22.9%: one
    24-hour boundary releases the next tranche and unblocks the pool."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(10.0, now=now)

    _seed_weekly(15.0, now=now, elapsed=3600)              # 1h in → day 0, 11.4% released
    assert balancer.pick_pair("CLAUDE", "CODEX")[0] is None

    _seed_weekly(15.0, now=now, elapsed=_DAY + 3600)       # 25h in → day 1, 22.9% released
    assert balancer.pick_pair("CLAUDE", "CODEX")[0] == "CLAUDE"


def test_a_weekly_reset_cannot_fabricate_five_hour_availability(coord_state, monkeypatch):
    """Independent windows: a fresh, low weekly window does not rescue a pool whose five-hour
    utilization is over the ceiling. The deferral names the five-hour headroom block, not weekly."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(95.0, now=now)                         # over the 85% idle ceiling
    _seed_weekly(1.0, now=now)                             # weekly wide open

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder is None
    assert "utilization at 95%" in block_msg and "weekly" not in block_msg


def test_a_five_hour_reset_cannot_fabricate_weekly_availability(coord_state, monkeypatch):
    """The mirror: a fresh, low five-hour window does not rescue a pool whose weekly allowance is
    spent. Resetting either window only clears its own constraint."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(2.0, now=now)                          # five-hour wide open
    _seed_weekly(50.0, now=now)                            # day 0, far over 11.4% released

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder is None and "weekly spend at 50%" in block_msg


def test_a_missing_weekly_window_fails_closed(coord_state, monkeypatch):
    """The weekly window is a required fact: with a healthy five-hour fact but no seven-day fact at
    all, admission fails closed rather than admitting on the five-hour window alone."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(10.0, now=now)                         # no weekly fact seeded

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder is None and "weekly limit facts unavailable" in block_msg


def test_a_stale_weekly_window_fails_closed(coord_state, monkeypatch):
    """A weekly fact whose window has already rolled over (its reset is in the past) is stale, not
    fresh capacity — it blocks rather than fabricating a full allowance."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    _seed_five_hour(10.0, now=now)
    # Observed inside a window that has since reset: resets_at sits 100s in the past.
    quota.record_quota(default_store_path(), quota.QuotaFact(
        pool="claude", used_percent=5.0, resets_at=now - 100, observed_at=now - 200,
        provenance="oauth:seven_day", window=quota.SEVEN_DAY))

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder is None and "weekly limit facts are stale" in block_msg


def test_a_queued_claude_build_is_rechecked_and_deferred_at_launch(coord_state, monkeypatch):
    """A Claude build admitted while weekly headroom existed must not LAUNCH after that weekly
    allowance is consumed. The production launch gate re-reads both windows, so the same queued
    record that the gate would admit under a fresh weekly window is deferred once weekly is spent —
    the recheck between assignment and launch."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setattr(coordinated_build.time, "time", lambda: now)
    _seed_five_hour(10.0, now=now)
    record = Record(identity="315", stage="build", pool="claude", lineage="claude",
                    demand=5, repo="o/r", subject="315")

    _seed_weekly(5.0, now=now)                             # weekly headroom at assignment
    assert coordinated_build._production_gate()(record) is True

    _seed_weekly(40.0, now=now)                            # weekly consumed before launch
    assert coordinated_build._production_gate()(record) is False
