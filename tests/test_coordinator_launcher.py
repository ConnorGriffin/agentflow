"""The crash-safe launcher handshake survives daemon death at every boundary (ADR 0030).

A launch is driven through the coordinator's public ``submit_stage`` / ``cycle`` seam with a
launcher told to die at each of the four boundaries. After the death a fresh coordinator
over the same store reconciles: ``not_started`` releases the permits and preserves the
attempt count; ``started`` consumes exactly one attempt and keeps its reservation while the
family is alive. Repeated reconciliation is idempotent — it never frees an attempt,
duplicates a start, or releases a live reservation.
"""

from __future__ import annotations

import pytest

from agentflow.coordinator import Coordinator, Submission
from agentflow.coordinator.launcher import (
    AFTER_RESERVATION, AFTER_START, BEFORE_REPLACEMENT, NOT_STARTED, LocalLauncher,
    _InjectedDeath)

FAMILY = "777001"


def submission(pool: str = "codex") -> Submission:
    return Submission(repo="o/r", subject="7", stage="review", pool=pool)


def start_and_die(tmp_path, crash_at, *, alive: bool):
    """Reserve and launch through a coordinator whose launcher dies at ``crash_at``; return
    a fresh coordinator over the same store with ``alive`` deciding family liveness."""
    launcher = LocalLauncher(family=FAMILY, crash_at=crash_at)
    coord = Coordinator(tmp_path / "s.db", launcher=launcher, is_alive=lambda f: alive)
    identity = coord.submit_stage(submission())
    with pytest.raises(_InjectedDeath):
        coord.cycle("codex")
    recovered = Coordinator(tmp_path / "s.db", is_alive=lambda f: alive)
    return identity, recovered


def test_death_after_reservation_releases_permits_and_keeps_attempt_at_zero(tmp_path):
    identity, recovered = start_and_die(tmp_path, AFTER_RESERVATION, alive=False)
    assert recovered.permits("codex") == 2  # ambiguous running state fails closed on load

    recovered.reconcile()
    record = recovered.records[identity]
    assert (record.state, record.attempts, recovered.permits("codex")) == ("waiting", 0, 0)

    recovered.reconcile()  # idempotent — no free attempt, no duplicate release
    assert (record.state, record.attempts) == ("waiting", 0)


def test_death_before_replacement_consumes_one_attempt_and_classifies_incomplete(tmp_path):
    identity, recovered = start_and_die(tmp_path, BEFORE_REPLACEMENT, alive=False)
    recovered.reconcile()
    record = recovered.records[identity]
    # `started` was durable, so the attempt counts; the dead family classifies incomplete and
    # returns to eligible waiting with budget remaining, permits released.
    assert (record.state, record.attempts, record.claim, recovered.permits("codex")) == (
        "waiting", 1, True, 0)

    recovered.reconcile()  # no duplicate start, no double-count
    assert record.attempts == 1


def test_death_after_start_keeps_reservation_while_family_is_alive(tmp_path):
    identity, recovered = start_and_die(tmp_path, AFTER_START, alive=True)
    recovered.reconcile()
    record = recovered.records[identity]
    assert (record.state, record.attempts, recovered.permits("codex")) == ("running", 1, 2)

    recovered.reconcile()  # a live reservation is never released or duplicated
    assert (record.state, record.attempts, recovered.permits("codex")) == ("running", 1, 2)


def test_launch_that_never_creates_a_family_records_not_started(tmp_path):
    launcher = LocalLauncher(fact=NOT_STARTED)
    coord = Coordinator(tmp_path / "s.db", launcher=launcher, is_alive=lambda f: False)
    identity = coord.submit_stage(submission())
    assert coord.cycle("codex") == []  # nothing started
    record = coord.records[identity]
    assert (record.state, record.attempts, coord.permits("codex")) == ("waiting", 0, 0)
