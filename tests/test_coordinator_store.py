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
from agentflow.coordinator.record import Record
from agentflow.coordinator.store import (
    SCHEMA_VERSION, Store, StoreUnavailable, default_store_path)


def test_default_store_lives_under_the_state_directory(coord_state):
    # coord_state points AGENTFLOW_STATE at an isolated directory; the store is placed there,
    # never at a caller-supplied path.
    assert default_store_path().is_relative_to(coord_state)


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
    Store(path).close()  # initialize the schema once

    records = [Record(f"R{i}", "review", "codex", 2, state="running") for i in range(8)]
    barrier = threading.Barrier(len(records))
    reserved: list[str] = []
    lock = threading.Lock()

    def race(record):
        store = Store(path)  # a distinct instance/connection per racer
        barrier.wait()
        if store.reserve(record, budget=5):
            with lock:
                reserved.append(record.identity)
        store.close()

    threads = [threading.Thread(target=race, args=(r,)) for r in records]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(reserved) == 2
    assert Store(path).permits_used("codex") == 4  # never over the five-permit budget


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
