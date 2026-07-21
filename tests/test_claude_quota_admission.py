"""Claude admission on provider quota facts (#305), through the public dispatch/coordinator seam.

Two failures the retired transcript proxy allowed are reproduced here:

* a *new provider window* opens while the trailing-five-hour transcript proxy still reads above
  its old threshold — the proxy must not keep Claude blocked past the reset; and
* *concurrent admitted work* would overshoot the configured ceiling — several launches must not
  all pass one below-ceiling reading, so admission reserves headroom for work already running.

Both drive the real balancer (`pick_pair`) and the real composed production gate through the
coordinator's `cycle`, not a private hook.
"""

from __future__ import annotations

import time

from conftest import FakeSession, permits

from agentflow import balancer, coordinated_build
from agentflow.coordinator import Submission, quota
from agentflow.coordinator.store import default_store_path


def _clear_activity_gate(tmp_path, monkeypatch):
    """Point the balancer's activity gate at a stub that always reports idle+clear, so only the
    provider quota fact (not the personal-tooling gate) decides Claude headroom."""
    gate = tmp_path / "triage-gate.sh"
    gate.write_text("#!/bin/bash\necho clear\nexit 0\n")
    gate.chmod(0o755)
    monkeypatch.setattr(balancer, "_GATE", str(gate))


def _seed_claude_quota(used_percent, *, now, resets_at, observed_at):
    quota.record_quota(default_store_path(), quota.QuotaFact(
        pool="claude", used_percent=used_percent, resets_at=resets_at,
        observed_at=observed_at, provenance="claude:rate_limit_event"))


def test_a_new_provider_window_admits_claude_despite_a_high_transcript_proxy(
        coord_state, monkeypatch):
    """Reproduction #1: the provider's five-hour window has reset, so Claude is eligible on the next
    cycle — even though the trailing-five-hour transcript proxy still reads 90% (well over its old
    40% threshold). The proxy is diagnostic only and cannot keep the pool blocked past the reset."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = 1_000_000
    monkeypatch.setattr(time, "time", lambda: now)
    monkeypatch.setenv("TEST_SPEND_PCT", "90")             # transcript proxy still high
    # The last observed reading was near-full, but its window has already reset.
    _seed_claude_quota(90.0, now=now, resets_at=now - 60, observed_at=now - 3600)

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder == "CLAUDE"                              # eligible on the fresh window
    assert block_msg == ""


def test_a_stale_below_ceiling_reading_cannot_be_reused_to_overshoot_the_ceiling(
        make_coord, coord_state, monkeypatch):
    """Reproduction #2: three Claude stages read one below-ceiling observation (30% under an 85%
    ceiling). Admission reserves conservative headroom for work already running, so they cannot all
    admit and collectively drive the pool to 100% — two start, the third defers. The permit and
    stage-lane caps do not bind here (three intakes, machine ceiling four, triage lane cap three),
    so the reservation is the only thing that holds the third back."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    monkeypatch.setattr(balancer, "CLAUDE_INFLIGHT_RESERVE_PCT", 30.0)
    now = int(time.time())
    _seed_claude_quota(30.0, now=now, resets_at=now + 4 * 3600, observed_at=now)

    fake = FakeSession()
    coord = make_coord(fake, gate=coordinated_build._production_gate())
    for subject in ("1", "2", "3"):
        coord.submit_stage(Submission(repo="o/r", subject=subject, stage="intake",
                                      pool="claude", source="/ro"))

    coord.cycle("claude")

    # 30 + 0 admits, 30 + 30 = 60 admits, 30 + 60 = 90 > 85 — the third is reserved out.
    assert permits(coord, "claude") == 2


def test_without_the_reservation_the_same_three_all_overshoot(
        make_coord, coord_state, monkeypatch):
    """The failing-first baseline: with no in-flight reservation the same three stages all admit
    against one 30% reading — exactly the overshoot the reservation prevents above."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    monkeypatch.setattr(balancer, "CLAUDE_INFLIGHT_RESERVE_PCT", 0.0)   # reservation disabled
    now = int(time.time())
    _seed_claude_quota(30.0, now=now, resets_at=now + 4 * 3600, observed_at=now)

    fake = FakeSession()
    coord = make_coord(fake, gate=coordinated_build._production_gate())
    for subject in ("1", "2", "3"):
        coord.submit_stage(Submission(repo="o/r", subject=subject, stage="intake",
                                      pool="claude", source="/ro"))

    coord.cycle("claude")

    assert permits(coord, "claude") == 3                   # all three admit — the overshoot


def test_the_named_ceiling_owns_admission_not_an_independent_lower_gate(
        coord_state, monkeypatch):
    """Reproduction of AC7: a Claude utilization of 60% is over the retired independent 40% gate
    but under the 85% idle policy ceiling. The single activity-adaptive policy (`ceiling_for`) owns
    the effective ceiling, so the pool admits — the personal-tooling gate blocking at 40% no longer
    silently overrides it."""
    tmp_path = coord_state
    # A gate that blocks the way the old independent 40% threshold did — it must not govern Claude.
    gate = tmp_path / "triage-gate.sh"
    gate.write_text("#!/bin/bash\necho 'blocked: trailing-5h spend over 40%'\nexit 1\n")
    gate.chmod(0o755)
    monkeypatch.setattr(balancer, "_GATE", str(gate))
    now = int(time.time())
    _seed_claude_quota(60.0, now=now, resets_at=now + 4 * 3600, observed_at=now)

    status = balancer._query_pool("claude")

    assert status.clear is True                            # admits under the 85% policy ceiling
    assert status.ceiling == 85.0 and status.spent_pct == 60.0


def test_a_persisted_quota_fact_drives_admission_after_a_restart(
        make_coord, coord_state, monkeypatch):
    """The fact a Claude attempt observed survives into a fresh coordinator/balancer read: an
    over-ceiling reading blocks the pool after a restart with no new session required (#305)."""
    tmp_path = coord_state
    _clear_activity_gate(tmp_path, monkeypatch)
    now = int(time.time())
    _seed_claude_quota(95.0, now=now, resets_at=now + 4 * 3600, observed_at=now)

    builder, _reviewer, block_msg = balancer.pick_pair("CLAUDE", "CODEX")

    assert builder is None                                 # the over-ceiling provider fact defers it
    assert "claude" in block_msg and "utilization" in block_msg
