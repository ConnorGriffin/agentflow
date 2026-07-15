"""The coordinator's public seam (ADR 0030): idempotent logical-stage submission and pool
cycling, with the admission matrix, continuation priority, and provider observations kept
private. Also asserts the dormant guarantee — nothing here is wired into the daemon yet.
"""

from __future__ import annotations

import threading

from agentflow.coordinator import Coordinator, Record, Submission
from agentflow.coordinator.providers import classify_claude


def test_submit_stage_is_idempotent_on_the_logical_stage_identity(tmp_path):
    coord = Coordinator(tmp_path / "s.db")
    first = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    again = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    assert first == again
    assert len(coord.records) == 1


def test_legacy_lane_alias_never_turns_revise_into_build(tmp_path):
    coord = Coordinator(tmp_path / "s.db")
    # A revise reported on the ambiguous `building` lane must submit as revise, not build.
    identity = coord.submit_stage(Submission(repo="o/r", subject="9", stage="building",
                                             pool="claude", complexity="deep"))
    assert coord.records[identity].stage == "build"  # `building` normalizes to the build stage
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise",
                                           pool="claude", complexity="deep"))
    assert coord.records[revise].stage == "revise" and coord.records[revise].demand == 3


def test_cycle_admits_intake_and_charges_one_permit(tmp_path):
    coord = Coordinator(tmp_path / "s.db")
    identity = coord.submit_stage(Submission(repo="o/r", subject="1", stage="intake",
                                             pool="claude"))
    assert coord.cycle("claude") == [identity]
    assert coord.permits("claude") == 1
    assert coord.cycle("codex") == []  # the other pool has no work


def test_unknown_pool_submission_is_inadmissible(tmp_path):
    coord = Coordinator(tmp_path / "s.db")
    identity = coord.submit_stage(Submission(repo="o/r", subject="2", stage="review",
                                             pool="gemini"))
    # No ledger to charge an unknown pool, so it fills the exclusive fallback and never starts.
    assert coord.cycle("gemini") == []
    assert coord.records[identity].state == "waiting"


def test_capacity_reset_defers_a_continuation_until_it_is_eligible(tmp_path):
    coord = Coordinator(tmp_path / "s.db")
    identity = coord.submit_stage(Submission(repo="o/r", subject="3", stage="review",
                                             pool="claude"))
    assert coord.cycle("claude", now=0) == [identity]

    # A capacity interruption with a future reset returns the stage to waiting and defers it.
    record = coord.records[identity]
    obs = classify_claude([{"type": "error", "subtype": "capacity", "reset_at": 50}])
    coord._finish(record, provider=obs.classification(), outcome=False,
                  eligible_at=obs.reset_at)
    assert coord.permits("claude") == 0

    assert coord.cycle("claude", now=49) == []          # not eligible yet
    assert coord.cycle("claude", now=50) == [identity]  # reset reached
    assert coord.records[identity].attempts == 2


def test_concurrent_reservations_never_exceed_the_five_permit_budget(tmp_path):
    """Reservation is all-or-nothing on the shared ledger: many cycles racing to start
    demand-2 reviews on one pool can reserve at most two (four permits), never a sixth."""
    coord = Coordinator(tmp_path / "s.db")
    records = [coord.submit(Record(f"R{i}", "review", "codex", 2, model="sol"))
               for i in range(8)]
    barrier = threading.Barrier(len(records))
    started: list[str] = []
    lock = threading.Lock()

    def race(record):
        barrier.wait()  # release all threads into the reservation at once
        if coord._begin_start(record):
            coord._commit_start(record, "started", family=str(id(record)))
            with lock:
                started.append(record.identity)

    threads = [threading.Thread(target=race, args=(r,)) for r in records]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(started) == 2
    assert coord.permits("codex") == 4  # two reservations of two, never over budget


def test_coordinator_module_is_dormant_no_daemon_wiring():
    """Guardrail for ADR 0030's dormant slice: no pipeline module imports the coordinator,
    so the current legacy pipeline behavior cannot change."""
    import agentflow.daemon
    import agentflow.dispatch
    import agentflow.loop
    import agentflow.runner
    for module in (agentflow.daemon, agentflow.dispatch, agentflow.loop, agentflow.runner):
        assert "coordinator" not in getattr(module, "__dict__", {})
        source = module.__loader__.get_source(module.__name__) or ""
        assert "agentflow.coordinator" not in source
