"""The Build tracer rollout switch (issue #103): a durable legacy↔coordinated intent whose
phase is re-derived every cycle from that intent plus the observed world. Driven through its
public surface — ``request_coordinated`` / ``request_legacy`` / ``phase`` — never by poking the
JSON file, except the one corruption case that proves the fail-closed read.
"""

from __future__ import annotations

from agentflow.coordinator.rollout import (COORDINATED, DRAINING, LEGACY,
                                           MODE_COORDINATED, MODE_LEGACY, Rollout)


def _rollout(tmp_path, **kw):
    return Rollout(tmp_path / "rollout.json", **kw)


def test_starts_in_legacy_and_launches_legacy_when_clean(tmp_path):
    roll = _rollout(tmp_path)
    assert roll.mode == MODE_LEGACY
    phase = roll.phase()
    assert phase.name == LEGACY
    assert phase.launch_legacy and not phase.submit_coordinated


def test_requesting_coordinated_survives_a_restart(tmp_path):
    _rollout(tmp_path).request_coordinated()
    # A fresh Rollout over the same durable file is the daemon restart.
    assert _rollout(tmp_path).mode == MODE_COORDINATED


def test_forward_drain_waits_on_legacy_evidence_and_names_it(tmp_path):
    lines: list[str] = []
    roll = _rollout(tmp_path, log=lines.append)
    roll.request_coordinated()
    evidence = ("#12 building live", "dirty worktree issue-9")
    phase = roll.phase(legacy_evidence=evidence)
    assert phase.name == DRAINING
    assert phase.blocked_by == evidence
    assert not phase.launch_legacy and not phase.submit_coordinated  # neither side launches
    assert any("draining to coordinated" in line and "#12 building live" in line
               for line in lines)


def test_coordinated_reached_only_once_legacy_evidence_clears(tmp_path):
    roll = _rollout(tmp_path)
    roll.request_coordinated()
    assert roll.phase(legacy_evidence=("#3 building live",)).name == DRAINING
    clean = roll.phase(legacy_evidence=())
    assert clean.name == COORDINATED
    assert clean.submit_coordinated and not clean.launch_legacy


def test_rollback_keeps_draining_while_a_record_still_owns_work(tmp_path):
    roll = _rollout(tmp_path)
    roll.request_coordinated()
    assert roll.phase().name == COORDINATED
    # Roll back: a record still owns in-flight work, so legacy launching stays suspended.
    roll.request_legacy()
    draining = roll.phase(coordinator_active=True)
    assert draining.name == DRAINING
    assert not draining.launch_legacy and not draining.submit_coordinated
    # Once nothing owns work, legacy launching resumes.
    assert roll.phase(coordinator_active=False).name == LEGACY


def test_no_phase_ever_launches_both_sides(tmp_path):
    roll = _rollout(tmp_path)
    for mode in (MODE_LEGACY, MODE_COORDINATED):
        (roll.request_coordinated if mode == MODE_COORDINATED else roll.request_legacy)()
        for evidence in ((), ("x",)):
            for active in (False, True):
                phase = roll.phase(legacy_evidence=evidence, coordinator_active=active)
                assert not (phase.launch_legacy and phase.submit_coordinated)


def test_cycle_can_pin_the_intent_it_read_before_reclaim(tmp_path):
    roll = _rollout(tmp_path)
    roll.request_coordinated()
    # Even if the durable file is toggled after the cycle's first read, cleanup and dispatch use
    # the same pinned intent. The new request takes effect on the next cycle.
    roll.request_legacy()
    phase = roll.phase(requested_mode=MODE_COORDINATED)
    assert phase.name == COORDINATED


def test_corrupt_durable_file_refuses_to_guess_a_mode(tmp_path):
    path = tmp_path / "rollout.json"
    path.write_text("{not json")
    roll = Rollout(path)
    # The dispatch seam catches this ambiguity and derives a named drain. The state object must
    # not silently reinterpret a partially-written coordinated request as legacy launching.
    import pytest
    with pytest.raises(ValueError):
        _ = roll.mode
