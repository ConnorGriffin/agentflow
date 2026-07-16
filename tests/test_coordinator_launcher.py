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

from conftest import FakeSession, NeverStartsLauncher, permits, starts_until_held

from agentflow.coordinator import Coordinator, Submission
from agentflow.coordinator.launcher import LocalLauncher, pid_family_alive
from agentflow.coordinator.providers import ProviderCause


def review(subject: str = "7", pool: str = "codex") -> Submission:
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool)


def test_reservation_that_never_started_recovers_with_the_full_budget(make_coord):
    fake = FakeSession()
    fake.crash_start = True
    crashed = make_coord(fake)
    identity = crashed.submit_stage(review())
    with pytest.raises(RuntimeError):
        crashed.cycle("codex")
    assert permits(crashed, "codex") == 2  # ambiguous running reservation fails closed

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
    assert permits(recovered, "codex") == 2
    assert recovered.cycle("codex") == []       # idempotent across repeated reconciliation
    assert permits(recovered, "codex") == 2


def test_launch_that_never_creates_a_family_records_not_started(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, launcher=NeverStartsLauncher())
    identity = coord.submit_stage(review())
    assert coord.cycle("codex") == []
    assert permits(coord, "codex") == 0  # nothing started, so nothing reserved and no attempt


def test_real_launcher_spawns_a_provider_and_the_start_is_durable(coord_state):
    """The production launcher genuinely spawns a child that records a durable ``started``
    before ``exec``-replacing itself, so a fresh coordinator recovers a real, live family."""
    alive_provider = lambda record: [sys.executable, "-c", "import time; time.sleep(30)"]
    coord = Coordinator(launcher=LocalLauncher(alive_provider, timeout=5))
    identity = coord.submit_stage(review(pool="claude"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 1  # a real provider family is alive and reserved

    # A fresh coordinator over the same store reconciles the durable start and real liveness.
    recovered = Coordinator(launcher=LocalLauncher(alive_provider, timeout=5))
    assert recovered.cycle("claude") == []
    assert permits(recovered, "claude") == 1


def test_real_launcher_releases_when_the_spawned_provider_exits(coord_state):
    """A provider that exits is detected as a dead family and its permit is released."""
    gate = {"open": True}
    exiting_provider = lambda record: [sys.executable, "-c", ""]
    coord = Coordinator(launcher=LocalLauncher(exiting_provider, timeout=5),
                        gate=lambda record: gate["open"])
    coord.submit_stage(review(pool="claude"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 1  # started

    time.sleep(0.5)                      # the provider exits
    gate["open"] = False                 # do not immediately re-admit the continuation
    coord.cycle("claude")
    assert permits(coord, "claude") == 0  # the dead family's reservation is released


def test_launched_session_is_observed_from_its_durable_artifacts(coord_state):
    """The wired provider path is real, not a no-op: a launched Claude family redirects its
    structured stream and exit to durable per-attempt artifacts, and the default provider
    observer reconstructs the full observation from them — a typed capacity cause with its
    reset, not a guess — which then drives the seam's continuation decision (ADR 0030)."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    emitted = [{"type": "assistant", "text": "working"},
               {"type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 900}}]
    script = ("import json,sys\n"
              + "\n".join(f"print(json.dumps({e!r}))" for e in emitted) + "\nsys.exit(0)\n")
    provider = lambda record: [sys.executable, "-c", script]

    coord = Coordinator(launcher=LocalLauncher(provider, timeout=5))
    identity = coord.submit_stage(Submission(repo="o/r", subject="obs", stage="review",
                                             pool="claude", input_ptr="a-brief"))
    assert coord.cycle("claude") == []       # launched; a real family recorded started
    time.sleep(0.5)                          # the provider emits its events and exits

    record = Store(default_store_path()).load()[identity]
    obs = ClaudeProviderAdapter().observe(record)
    assert obs.cause is ProviderCause.CAPACITY and obs.reset_at == 900
    assert any(e.get("type") == "assistant" for e in obs.events)   # events preserved
    assert obs.exit_status == 0

    # The same observation, through the seam, defers the stage as an eligible continuation.
    assert coord.cycle("claude", now=0) == []
    assert permits(coord, "claude") == 0     # released, waiting for its reset


def test_real_supervisor_preserves_partial_output_signal_and_timeout(coord_state):
    """The production path, not a fixture classifier, retains output written before a
    deadline and records both the supervisor timeout and the terminating signal."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    script = (
        "import sys,time\n"
        "print('partial stdout', flush=True)\n"
        "print('partial stderr', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n"
    )
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, session_timeout=0.1))
    identity = coord.submit_stage(review(subject="timed-out", pool="claude"))
    coord.cycle("claude")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = Store(default_store_path()).load()[identity]
        if not pid_family_alive(record.family):
            break
        time.sleep(0.02)
    else:
        pytest.fail("provider supervisor did not finish after its timeout")

    observation = ClaudeProviderAdapter().observe(record)
    assert observation.timed_out is True
    assert observation.signal in {15, 9}
    assert "partial stdout" in observation.partial_output
    assert "partial stderr" in observation.partial_output
    assert observation.cause is ProviderCause.TIMEOUT


def test_real_supervisor_starts_provider_in_the_submitted_source(coord_state, tmp_path):
    """The path named in the boundary is also the provider process's real working directory,
    which is what the Claude project settings and OS workspace sandbox confine."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    source = tmp_path / "owned-worktree"
    source.mkdir()
    provider = lambda record: [sys.executable, "-c", "import os; print(os.getcwd())"]
    coord = Coordinator(launcher=LocalLauncher(provider, timeout=5))
    identity = coord.submit_stage(Submission(
        repo="o/r", subject="cwd", stage="review", pool="claude", source=str(source)))
    coord.cycle("claude")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = Store(default_store_path()).load()[identity]
        if not pid_family_alive(record.family):
            break
        time.sleep(0.02)
    else:
        pytest.fail("provider supervisor did not finish")

    observation = ClaudeProviderAdapter().observe(record)
    assert observation.partial_output == str(source)


def test_coordinator_supervisor_marks_its_worktree_active(tmp_path, monkeypatch):
    """Startup recovery sees a coordinator provider through the same PID marker it already
    trusts for legacy sessions, so it cannot remove a clean worktree before reconciliation."""
    from agentflow import runner
    from agentflow.coordinator._launch_child import _clear_active, _mark_active

    marker = tmp_path / "agentflow-active"
    monkeypatch.setattr(runner, "_active_marker", lambda path: marker)

    written = _mark_active(str(tmp_path))
    assert written == marker
    assert marker.read_text().strip().isdigit()

    _clear_active(marker)
    assert not marker.exists()
