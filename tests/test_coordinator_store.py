"""The continuation store creates itself atomically and otherwise fails closed (ADR 0030).

These exercise the store through its interface: an absent store is created versioned; a
corrupt, newer-schema, or otherwise unreadable store raises ``StoreUnavailable`` so the
coordinator starts nothing and clears no claim.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentflow.coordinator import Coordinator, Record, StoreUnavailable
from agentflow.coordinator.store import SCHEMA_VERSION, Store


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


def test_coordinator_over_unreadable_store_starts_nothing(tmp_path):
    """A coordinator cannot be constructed on an unreadable store, so no cycle can run —
    the fail-closed guarantee: no provider starts and no claim is cleared."""
    path = tmp_path / "coord.db"
    path.write_bytes(b"corrupt")

    with pytest.raises(StoreUnavailable):
        Coordinator(path)
