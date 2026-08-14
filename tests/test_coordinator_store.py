"""The continuation store creates itself atomically, reserves atomically, and otherwise
fails closed (ADR 0030).

These exercise the private store adapter directly: an absent store is created versioned; a
corrupt, newer-schema, or otherwise unreadable store raises ``StoreUnavailable`` so the
coordinator starts nothing and clears no claim; and a permit reservation reads availability
and writes the running row under one lock, so instances racing on the same file can never
push a pool past its budget.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from agentflow.coordinator import Coordinator
from agentflow.coordinator.record import RUNNING, WAITING, Record
from agentflow.coordinator.store import (
    LegacyReservationIntent, ReservationLimits, SCHEMA_VERSION, Store, StoreUnavailable,
    default_store_path)


def intent(identity, *, token=None, revision=1, limits=None, budget=5):
    return LegacyReservationIntent(identity, token, revision, 100, "daemon-test", budget,
                                   limits, None)


def test_default_store_lives_under_the_state_directory(coord_state):
    # coord_state points AGENTFLOW_STATE at an isolated directory; the store is placed there,
    # never at a caller-supplied path.
    assert default_store_path().is_relative_to(coord_state)


def test_coordinator_begin_start_persists_waiting_then_uses_only_no_admission_intent(
        coord_state, monkeypatch):
    coordinator = Coordinator(daemon_generation="daemon-compat")
    record = Record(
        "compat", "review", "codex", 1, state=WAITING, lineage="codex",
        launch_token="prior-token", start_fact="not_started", family="old-family",
        process_alive=True, attempt_committed=True, started_at=4, deadline=5,
        model="legacy-model", refusal="checkout-failed: old", refusals=3,
        stall_refusal_id="checkout-failed", stall_started_at=7,
        stall_last_observed_at=8)
    assert coordinator._store.upsert(record)
    coordinator._records[record.identity] = record
    record.model = "prepared-model"
    record.refusal = ""
    record.refusals = 0
    record.stall_refusal_id = ""
    record.stall_started_at = 0
    record.stall_last_observed_at = 0
    reserve = coordinator._store.reserve_legacy
    captured = []

    def observe(intent):
        captured.append(intent)
        assert type(intent) is LegacyReservationIntent
        prepared = coordinator._store.record_of(record.identity)
        assert prepared == record
        assert prepared is not None and prepared.state == WAITING
        assert prepared.revision == 2 and prepared.launch_token == "prior-token"
        return reserve(intent)

    monkeypatch.setattr(coordinator._store, "reserve_legacy", observe)
    assert coordinator._begin_start(record, 100)
    assert captured == [LegacyReservationIntent(
        "compat", "prior-token", 2, 100, "daemon-compat", 5, None, None)]
    durable = coordinator._store.record_of("compat")
    assert durable == record and record.state == RUNNING
    assert record.revision == 3 and record.model == "prepared-model"
    assert record.refusal == "" and record.refusals == 0
    assert record.stall_refusal_id == "" and record.stall_started_at == 0
    assert record.stall_last_observed_at == 0
    assert record.launch_token != "prior-token"
    assert coordinator._store.permits_used("codex") == 1
    coordinator._store.close()


def _corrupt_gate_snapshot(record):
    record.state = RUNNING
    record.identity = "forged-identity"
    record.repo = "forged/repo"
    record.stage = "build"
    record.pool = "claude"
    record.model = "forged-model"
    record.demand = 99
    record.revision = 999
    record.launch_token = "forged-token"
    record.descendants.add("forged-child")


def _authoritative_gate_facts(record):
    return (
        record.state, record.identity, record.repo, record.stage, record.pool, record.model,
        record.demand, record.revision, record.launch_token, frozenset(record.descendants),
    )


def test_hostile_gate_and_limits_mutations_cannot_change_waiting_authority_or_successor(
        coord_state, monkeypatch):
    authoritative_facts = (
        WAITING, "authority", "owner/repo", "review", "codex", "prepared-model", 1, 1,
        "prepared-token", frozenset({"owned-child"}),
    )
    callback_inputs = []
    limits = ReservationLimits(10, 10, "review", {"review": "review"})

    class HostileGate:
        def __call__(self, snapshot):
            callback_inputs.append(("gate", _authoritative_gate_facts(snapshot)))
            _corrupt_gate_snapshot(snapshot)
            return True

        def reservation_limits(self, snapshot):
            callback_inputs.append(("limits", _authoritative_gate_facts(snapshot)))
            _corrupt_gate_snapshot(snapshot)
            return limits

    coordinator = Coordinator(gate=HostileGate(), daemon_generation="daemon-authority")
    record = Record(
        "authority", "review", "codex", 1, repo="owner/repo", model="prepared-model",
        state=WAITING, lineage="codex", launch_token="prepared-token",
        descendants={"owned-child"})
    assert coordinator._store.upsert(record)
    coordinator._records[record.identity] = record
    reserve = coordinator._store.reserve_legacy
    captured = []

    def observe(intent):
        captured.append(intent)
        assert type(intent) is LegacyReservationIntent
        durable = coordinator._store.record_of("authority")
        assert durable is not None
        assert _authoritative_gate_facts(durable) == (
            WAITING, "authority", "owner/repo", "review", "codex", "prepared-model", 1, 2,
            "prepared-token", frozenset({"owned-child"}),
        )
        # These are the only durable facts from which Store may build an admission context.
        assert (durable.identity, durable.repo, durable.stage, durable.pool, durable.model) == (
            "authority", "owner/repo", "review", "codex", "prepared-model")
        return reserve(intent)

    monkeypatch.setattr(coordinator._store, "reserve_legacy", observe)
    assert coordinator._begin_start(record, 100)

    assert callback_inputs == [("gate", authoritative_facts), ("limits", authoritative_facts)]
    assert captured == [LegacyReservationIntent(
        "authority", "prepared-token", 2, 100, "daemon-authority", 5, limits, None)]
    successor = coordinator._store.record_of("authority")
    assert successor == record and successor is not None
    assert successor.state == RUNNING and successor.revision == 3
    assert (successor.identity, successor.repo, successor.stage, successor.pool,
            successor.model, successor.demand, successor.descendants) == (
                "authority", "owner/repo", "review", "codex", "prepared-model", 1,
                {"owned-child"})
    assert successor.launch_token not in {"prepared-token", "forged-token"}
    coordinator._store.close()


def test_hostile_false_gate_hooks_preserve_declared_deferral_without_importing_mutations(
        coord_state):
    seen = []
    lines = []

    class HostileGate:
        def __call__(self, snapshot):
            seen.append(("gate", _authoritative_gate_facts(snapshot)))
            _corrupt_gate_snapshot(snapshot)
            return False

        def deferral_reason(self, snapshot):
            seen.append(("reason", _authoritative_gate_facts(snapshot)))
            _corrupt_gate_snapshot(snapshot)
            return "declared capacity"

        def should_emit_deferral(self, snapshot):
            seen.append(("emit", _authoritative_gate_facts(snapshot)))
            _corrupt_gate_snapshot(snapshot)
            return True

    coordinator = Coordinator(gate=HostileGate(), log=lines.append)
    record = Record(
        "deferral", "review", "codex", 1, repo="owner/repo", model="prepared-model",
        state=WAITING, lineage="codex", launch_token="prepared-token",
        descendants={"owned-child"})
    assert coordinator._store.upsert(record)
    coordinator._records[record.identity] = record
    prepared_facts = _authoritative_gate_facts(record)

    assert coordinator._admit(record, 100) == "blocked"
    durable = coordinator._store.record_of("deferral")
    assert durable is not None and durable.state == WAITING
    assert (durable.identity, durable.repo, durable.stage, durable.pool, durable.model,
            durable.demand, durable.launch_token, durable.descendants) == (
                "deferral", "owner/repo", "review", "codex", "prepared-model", 1,
                "prepared-token", {"owned-child"})
    assert durable.refusal == "declared capacity"
    assert seen[0] == ("gate", prepared_facts)
    assert seen[1] == ("reason", prepared_facts)
    assert seen[2][0] == "emit"
    assert seen[2][1][1:7] == prepared_facts[1:7]
    assert any("declared capacity" in line for line in lines)
    coordinator._store.close()


def test_absent_store_is_created_versioned_and_round_trips(tmp_path):
    path = tmp_path / "state" / "coord.db"  # a directory that does not exist yet
    assert not path.exists()

    store = Store(path)
    store.upsert(Record("R1", "review", "claude", 1, state="running"))
    store.close()

    assert path.exists()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()

    reopened = Store(path)
    loaded = reopened.load()
    assert loaded["R1"].stage == "review"
    assert reopened.permits_used("claude") == 1


def test_reserve_is_atomic_across_instances(tmp_path):
    """Many instances racing to reserve demand-2 reviews on one pool reserve at most two
    (four permits) between them — availability and the running write share one critical
    section, so a sixth permit is impossible."""
    path = tmp_path / "coord.db"
    seed = Store(path)
    for i in range(8):
        seed.upsert(Record(f"R{i}", "review", "codex", 2, state="waiting"))
    seed.close()

    records = [f"R{i}" for i in range(8)]
    barrier = threading.Barrier(len(records))
    reserved: list[str] = []
    lock = threading.Lock()

    def race(identity):
        store = Store(path)  # a distinct instance/connection per racer
        barrier.wait()
        if store.reserve_legacy(intent(identity)) is not None:
            with lock:
                reserved.append(identity)
        store.close()

    threads = [threading.Thread(target=race, args=(r,)) for r in records]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(reserved) == 2
    assert Store(path).permits_used("codex") == 4  # never over the five-permit budget


def test_checkpoint_callback_blocks_concurrent_store_mutation(
        tmp_path, monkeypatch):
    path = tmp_path / "same-store.db"
    store = Store(path)
    store.upsert(Record("A", "build", "codex", 1, state=WAITING))
    store.upsert(Record("B", "build", "codex", 1, state=WAITING))
    entered = threading.Event()
    release = threading.Event()
    attempting = threading.Event()
    results = {}
    errors = []

    def checkpoint(name):
        if name == "after-successor-before-commit":
            entered.set()
            assert release.wait(2)

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(checkpoint))

    def first_reservation():
        try:
            results["first"] = store.reserve_legacy(intent("A"))
        except BaseException as error:
            errors.append(error)

    def serialized_operations():
        try:
            attempting.set()
            results["upsert"] = store.upsert(
                Record("C", "review", "codex", 1, state=WAITING))
            results["second"] = store.reserve_legacy(intent("B"))
        except BaseException as error:
            errors.append(error)

    first = threading.Thread(target=first_reservation)
    first.start()
    assert entered.wait(1)
    assert store._admission_callback_active.is_set()
    second = threading.Thread(target=serialized_operations)
    second.start()
    assert attempting.wait(1)
    second.join(1)
    assert not second.is_alive()
    release.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert len(errors) == 1 and type(errors[0]) is StoreUnavailable
    assert str(errors[0]) == "reentrant Store mutation during admission"
    assert results["first"] is not None
    assert store.record_of("C") is None
    assert store.permits_used("codex") == 1
    store.close()


def test_reserve_refuses_a_foreign_running_reservation(tmp_path):
    """The same-identity compare-and-set: once one instance holds a record's running
    reservation under its launch token, a second instance still holding the stale waiting view
    cannot reserve the same identity with its own token, and the winner's token is untouched."""
    path = tmp_path / "coord.db"
    store = Store(path)
    store.upsert(Record("R", "build", "claude", 1, state="waiting"))

    winner = store.reserve_legacy(intent("R"))
    assert winner is not None
    # A racer that loaded the record while it was still waiting now tries its own reservation.
    assert store.reserve_legacy(intent("R")) is None
    assert store.record_of("R").launch_token == winner.successor.launch_token
    assert store.permits_used("claude") == 1           # exactly one running reservation
    store.close()


def test_reserve_never_overwrites_a_terminal_same_identity(tmp_path):
    path = tmp_path / "coord.db"
    store = Store(path)
    store.upsert(Record("R", "intake", "claude", 1, state="completed",
                        launch_token="winner", outcome="route"))

    assert store.reserve_legacy(intent("R")) is None
    durable = store.record_of("R")
    assert durable.state == "completed" and durable.launch_token == "winner"
    assert durable.outcome == "route"
    store.close()


def test_reserve_requires_the_loaded_waiting_token_generation(tmp_path):
    path = tmp_path / "coord.db"
    store = Store(path)
    store.upsert(Record("R", "review", "claude", 1, state="waiting",
                        launch_token="newer", attempts=1, eligible_at=100))

    assert store.reserve_legacy(intent("R")) is None
    durable = store.record_of("R")
    assert durable.attempts == 1 and durable.launch_token == "newer"
    assert durable.eligible_at == 100
    store.close()


def test_two_processes_racing_one_waiting_record_yield_one_reservation(tmp_path):
    """Two coordinator instances that both loaded the same waiting record and each flipped it to
    running with its own fresh launch token race to reserve. Exactly one wins; the surviving
    running row carries the winner's token, and no second permit is charged (ADR 0030)."""
    path = tmp_path / "coord.db"
    seed = Store(path)
    seed.upsert(Record("R", "build", "claude", 1, state="waiting"))
    seed.close()

    barrier = threading.Barrier(2)
    results: dict[str, bool] = {}
    lock = threading.Lock()

    def race(token):
        store = Store(path)  # a distinct instance/connection per process
        barrier.wait()
        result = store.reserve_legacy(intent("R"))
        with lock:
            results[token] = result
        store.close()

    threads = [threading.Thread(target=race, args=(t,)) for t in ("TA", "TB")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(result is not None for result in results.values()) == 1
    winner = next(result.successor.launch_token for result in results.values()
                  if result is not None)
    final = Store(path)
    reread = final.record_of("R")
    assert reread.state == "running" and reread.launch_token == winner
    assert final.permits_used("claude") == 1        # one running reservation, one launch token
    final.close()


def test_global_stage_cap_is_atomic_across_pools(tmp_path):
    """Review and Build share Build's lane. Distinct coordinators racing on distinct pools
    still reserve at most the lane cap because the decision lives in the shared transaction."""
    path = tmp_path / "coord.db"
    seed = Store(path)
    records = [
        Record(f"R{i}", "review" if i % 2 else "build",
               "claude" if i % 2 else "codex", 1, state="waiting")
        for i in range(8)
    ]
    for record in records:
        seed.upsert(record)
    seed.close()
    limits = ReservationLimits(
        machine_ceiling=8, stage_cap=2, stage_lane="build",
        lane_by_stage={"build": "build", "review": "build", "revise": "build"},
    )
    barrier = threading.Barrier(len(records))
    reserved = []
    lock = threading.Lock()

    def race(record):
        store = Store(path)
        barrier.wait()
        if store.reserve_legacy(intent(record.identity, limits=limits)) is not None:
            with lock:
                reserved.append(record.identity)
        store.close()

    threads = [threading.Thread(target=race, args=(record,)) for record in records]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(reserved) == 2


def test_machine_ceiling_is_atomic_across_stage_lanes_and_pools(tmp_path):
    path = tmp_path / "coord.db"
    seed = Store(path)
    stages = ("intake", "build", "mockup", "respond")
    records = [
        Record(f"R{i}", stages[i % len(stages)],
               "claude" if i % 2 else "codex", 1, state="waiting")
        for i in range(8)
    ]
    for record in records:
        seed.upsert(record)
    seed.close()
    barrier = threading.Barrier(len(records))
    reserved = []
    lock = threading.Lock()

    def race(record):
        store = Store(path)
        lane = {"intake": "triage"}.get(record.stage, record.stage)
        limits = ReservationLimits(
            machine_ceiling=3, stage_cap=8, stage_lane=lane,
            lane_by_stage={"intake": "triage"},
        )
        barrier.wait()
        if store.reserve_legacy(intent(record.identity, limits=limits)) is not None:
            with lock:
                reserved.append(record.identity)
        store.close()

    threads = [threading.Thread(target=race, args=(record,)) for record in records]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(reserved) == 3


def test_existing_zero_byte_store_fails_closed(tmp_path):
    """Only absence means fresh state. An existing empty file is ambiguous and must not be
    replaced as though no coordinator had ever owned the path."""
    path = tmp_path / "coord.db"
    path.write_bytes(b"")

    with pytest.raises(StoreUnavailable):
        Store(path)


def test_concurrent_first_openers_share_one_store_inode(tmp_path):
    """Coordinators racing on an absent path all write through the one published ledger.
    No late creator may replace the database underneath an earlier open connection."""
    path = tmp_path / "coord.db"
    count = 16
    start = threading.Barrier(count)
    opened = threading.Barrier(count)
    errors: list[BaseException] = []

    def open_and_write(index):
        try:
            start.wait()
            store = Store(path)
            opened.wait()
            store.upsert(Record(f"R{index}", "review", "codex", 1))
            store.close()
        except BaseException as error:  # surfaced after every thread joins
            errors.append(error)

    threads = [threading.Thread(target=open_and_write, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(Store(path).load()) == {f"R{i}" for i in range(count)}


def test_child_start_and_disown_are_mutually_exclusive(tmp_path):
    """The launcher handshake's two atomic halves never both win. A child that records
    ``started`` under its token wins, and a later timeout disown yields that same start. A
    launch disowned first rotates the token, so a still-running child's late write is refused
    and it must not become a provider — closing the timeout race (ADR 0030)."""
    from agentflow.coordinator.record import NOT_STARTED, STARTED
    path = tmp_path / "coord.db"
    store = Store(path)

    # The child wins the race: it records started before the coordinator gives up.
    store.upsert(Record("R-win", "review", "codex", 2, state="running", launch_token="T1"))
    assert store.child_start("R-win", "T1", 4242) is True
    assert store.disown_launch("R-win", "T1") == (STARTED, "4242")

    # The timeout wins the race: disown rotates the token first, so an uncancelled child's
    # late guarded write is refused and no unreserved, uncounted provider can start.
    store.upsert(Record("R-lose", "review", "codex", 2, state="running", launch_token="T2"))
    assert store.disown_launch("R-lose", "T2") == (NOT_STARTED, None)
    assert store.child_start("R-lose", "T2", 5252) is False
    reread = store.record_of("R-lose")
    assert reread.start_fact != STARTED and reread.family is None

    # A delayed timeout from an older launch cannot rotate or disown a newer generation.
    newer = Record("R-stale", "review", "codex", 2, state="running", launch_token="T-new")
    store.upsert(newer)
    before = store.record_of("R-stale")
    assert store.disown_launch("R-stale", "T-old") == (NOT_STARTED, None)
    after = store.record_of("R-stale")
    assert after.launch_token == before.launch_token == "T-new"
    assert after.revision == before.revision and after.start_fact is None
    store.close()


def test_newer_schema_fails_closed(tmp_path):
    path = tmp_path / "coord.db"
    Store(path).close()
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    conn.commit()
    conn.close()

    with pytest.raises(StoreUnavailable):
        Store(path)


def test_corrupt_store_fails_closed(tmp_path):
    path = tmp_path / "coord.db"
    path.write_bytes(b"this is not a sqlite database at all, it is garbage bytes")

    with pytest.raises(StoreUnavailable):
        Store(path)


def test_coordinator_over_unreadable_store_starts_nothing(coord_state):
    """A coordinator cannot be constructed on an unreadable store, so no cycle can run —
    the fail-closed guarantee: no provider starts and no claim is cleared."""
    path = default_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"corrupt")

    with pytest.raises(StoreUnavailable):
        Coordinator()
