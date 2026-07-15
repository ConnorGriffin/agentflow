"""The crash-safe launcher handshake survives daemon death at every boundary (ADR 0030).

Every boundary is driven through the coordinator's public ``submit_stage`` / ``cycle`` seam:
a launch is admitted, the world (an injected :class:`FakeSession`) is left in the state a
crash would leave it, and a fresh coordinator over the same durable store reconciles. A
reservation that never durably started releases its permits and keeps the full attempt
budget; a durable ``started`` consumes exactly one attempt and keeps its reservation while
its family is alive. A separate test exercises the real spawning launcher end to end.
"""

from __future__ import annotations

import sys
import time

import pytest

from conftest import FakeSession, NeverStartsLauncher, starts_until_held

from agentflow.coordinator import Coordinator, Submission
from agentflow.coordinator.launcher import LocalLauncher


def review(subject: str = "7", pool: str = "codex") -> Submission:
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool)


def test_reservation_that_never_started_recovers_with_the_full_budget(make_coord):
    fake = FakeSession()
    fake.crash_start = True
    crashed = make_coord(fake)
    identity = crashed.submit_stage(review())
    with pytest.raises(RuntimeError):
        crashed.cycle("codex")
    assert crashed.permits("codex") == 2  # ambiguous running reservation fails closed

    fake.crash_start = False
    recovered = make_coord(fake)
    # No durable start existed, so no attempt was consumed: the record still has all three.
    assert starts_until_held(recovered, fake, identity, "codex") == 3


def test_durable_started_then_dead_recovery_consumes_exactly_one_attempt(make_coord):
    fake = FakeSession()
    started = make_coord(fake)
    identity = started.submit_stage(review())
    started.cycle("codex")            # a durable `started` is written and the family is alive
    fake.kill(identity)               # the provider died before the daemon could observe it

    recovered = make_coord(fake)
    # The durable `started` counts, so only two attempts remain before the hold.
    assert starts_until_held(recovered, fake, identity, "codex") == 2


def test_durable_started_and_alive_recovery_keeps_the_reservation(make_coord):
    fake = FakeSession()
    started = make_coord(fake)
    identity = started.submit_stage(review())
    started.cycle("codex")            # started and alive

    recovered = make_coord(fake)
    assert recovered.cycle("codex") == []       # a live family is neither released nor duplicated
    assert recovered.permits("codex") == 2
    assert recovered.cycle("codex") == []       # idempotent across repeated reconciliation
    assert recovered.permits("codex") == 2


def test_launch_that_never_creates_a_family_records_not_started(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, launcher=NeverStartsLauncher())
    identity = coord.submit_stage(review())
    assert coord.cycle("codex") == []
    assert coord.permits("codex") == 0  # nothing started, so nothing reserved and no attempt


def test_real_launcher_spawns_a_provider_and_the_start_is_durable(coord_state):
    """The production launcher genuinely spawns a child that records a durable ``started``
    before ``exec``-replacing itself, so a fresh coordinator recovers a real, live family."""
    alive_provider = lambda record: [sys.executable, "-c", "import time; time.sleep(30)"]
    coord = Coordinator(launcher=LocalLauncher(alive_provider, timeout=5))
    identity = coord.submit_stage(review(pool="claude"))
    assert coord.cycle("claude") == []
    assert coord.permits("claude") == 1  # a real provider family is alive and reserved

    # A fresh coordinator over the same store reconciles the durable start and real liveness.
    recovered = Coordinator(launcher=LocalLauncher(alive_provider, timeout=5))
    assert recovered.cycle("claude") == []
    assert recovered.permits("claude") == 1


def test_real_launcher_releases_when_the_spawned_provider_exits(coord_state):
    """A provider that exits is detected as a dead family and its permit is released."""
    gate = {"open": True}
    exiting_provider = lambda record: [sys.executable, "-c", ""]
    coord = Coordinator(launcher=LocalLauncher(exiting_provider, timeout=5),
                        gate=lambda record: gate["open"])
    coord.submit_stage(review(pool="claude"))
    assert coord.cycle("claude") == []
    assert coord.permits("claude") == 1  # started

    time.sleep(0.5)                      # the provider exits
    gate["open"] = False                 # do not immediately re-admit the continuation
    coord.cycle("claude")
    assert coord.permits("claude") == 0  # the dead family's reservation is released
