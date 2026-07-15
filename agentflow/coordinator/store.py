"""The coordinator's private, versioned continuation store (ADR 0030).

One SQLite database under agentflow's existing local state directory holds every
continuation record. The ``running`` rows are the permit ledger, so a permit reservation
is a single atomic transaction over that ledger — availability is read and the running
record is written under one ``BEGIN IMMEDIATE``, so two coordinator instances over the
same file can never reserve past the pool budget. This store is a private implementation
detail of the coordinator — there is no public storage seam, and there will not be one
until a second real representation exists (ADR 0030 alternatives).

Fail-closed is the whole point of the safety story. An absent store is created
atomically under the state directory. An unreadable, corrupt, locked-beyond-the-bounded-
wait, or newer-schema store raises :class:`StoreUnavailable`; the coordinator then starts
no provider and clears no claim (ADR 0028's "unreadable store fails closed").
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import fields
from pathlib import Path

from agentflow.coordinator.record import RUNNING, Record

SCHEMA_VERSION = 1
# Bounded wait for a busy database. Beyond this we fail closed rather than block a whole
# daemon cycle on a lock we cannot prove will clear.
_BUSY_TIMEOUT_MS = int(os.environ.get("AGENTFLOW_COORD_BUSY_MS", "2000"))

_SET_FIELDS = {"descendants"}
_COLUMNS = [f.name for f in fields(Record)]


def state_dir() -> Path:
    """agentflow's local state directory, honoring ``AGENTFLOW_STATE`` like the rest of the
    daemon. The coordinator's store lives beneath this; callers never choose a path."""
    return Path(os.environ.get("AGENTFLOW_STATE", os.path.expanduser("~/.agentflow")))


def default_store_path() -> Path:
    """Where the coordinator privately keeps its continuation store (ADR 0030). There is one
    store per state directory; a fresh coordinator over the same directory recovers it."""
    return state_dir() / "coordinator" / "records.db"


class StoreUnavailable(RuntimeError):
    """The store could not be read or is a schema this build does not understand. The
    coordinator treats this as fail-closed: no starts, no claim changes."""


class Store:
    """A thin durable table of continuation records keyed by stage identity.

    Records are loaded into the coordinator's working set on open and written through on
    every transition, so a fresh coordinator over the same file recovers the same state —
    that is how the crash-recovery boundaries are exercised.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        # The daemon dispatches concurrent chains through one coordinator, so the single
        # connection is shared across threads and serialized by this lock — the reservation
        # critical section is one place, matching the one-ledger design (ADR 0030).
        self._lock = threading.RLock()
        self._conn = self._connect()

    def _connect(self) -> sqlite3.Connection:
        created = not self.path.exists()
        if not created and self.path.stat().st_size == 0:
            created = True  # an empty file is a not-yet-initialized store, not a corrupt one
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # Autocommit mode (isolation_level=None) so the reservation can hold a single
            # explicit BEGIN IMMEDIATE across its read and its write.
            conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000,
                                   isolation_level=None, check_same_thread=False)
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0 and self._is_empty(conn):
                self._initialize(conn)
            elif version > SCHEMA_VERSION:
                conn.close()
                raise StoreUnavailable(
                    f"store schema {version} is newer than supported {SCHEMA_VERSION}")
            elif version != SCHEMA_VERSION:
                conn.close()
                raise StoreUnavailable(f"store schema {version} is not readable")
        except sqlite3.DatabaseError as e:  # corrupt file, locked-beyond-wait, unreadable
            raise StoreUnavailable(f"cannot open continuation store: {e}") from e
        return conn

    @staticmethod
    def _is_empty(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='records'"
        ).fetchone()
        return row[0] == 0

    @staticmethod
    def _initialize(conn: sqlite3.Connection) -> None:
        # One transaction: the whole schema and its version appear together or not at all.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "CREATE TABLE records ("
                " identity TEXT PRIMARY KEY,"
                " pool TEXT NOT NULL,"
                " state TEXT NOT NULL,"
                " demand INTEGER NOT NULL,"
                " data TEXT NOT NULL)"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            conn.execute("ROLLBACK")
            raise

    def load(self) -> dict[str, Record]:
        """Every persisted record, keyed by identity — the coordinator's working set."""
        with self._lock:
            try:
                rows = self._conn.execute("SELECT data FROM records").fetchall()
            except sqlite3.DatabaseError as e:
                raise StoreUnavailable(f"cannot read continuation store: {e}") from e
        return {r.identity: r for r in (self._decode(row[0]) for row in rows)}

    def upsert(self, record: Record) -> None:
        """Persist one record's current state. Called after every coordinator transition."""
        payload = self._encode(record)
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO records (identity, pool, state, demand, data)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(identity) DO UPDATE SET"
                    " pool=excluded.pool, state=excluded.state,"
                    " demand=excluded.demand, data=excluded.data",
                    (record.identity, record.pool, record.state, record.demand, payload),
                )
            except sqlite3.DatabaseError as e:
                raise StoreUnavailable(f"cannot write continuation store: {e}") from e

    def reserve(self, record: Record, budget: int) -> bool:
        """Atomically reserve ``record``'s demand on its pool, or refuse. Availability is read
        and the running row is written under one ``BEGIN IMMEDIATE``, so two coordinator
        instances racing on the same file serialize on the write lock and can never push a
        pool's ledger past ``budget`` (ADR 0029/0030). Returns whether the reservation was
        taken; on refusal nothing is written."""
        payload = self._encode(record)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(demand), 0) FROM records"
                    " WHERE pool = ? AND state = ? AND identity != ?",
                    (record.pool, RUNNING, record.identity),
                ).fetchone()
                if int(row[0]) + record.demand > budget:
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute(
                    "INSERT INTO records (identity, pool, state, demand, data)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(identity) DO UPDATE SET"
                    " pool=excluded.pool, state=excluded.state,"
                    " demand=excluded.demand, data=excluded.data",
                    (record.identity, record.pool, record.state, record.demand, payload),
                )
                self._conn.execute("COMMIT")
                return True
            except sqlite3.DatabaseError as e:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    pass
                raise StoreUnavailable(f"cannot reserve on continuation store: {e}") from e

    def record_of(self, identity: str) -> Record | None:
        """One record re-read from the ledger, or ``None``. The launcher polls this to observe
        the child's cross-process ``started`` write and its recorded family before it treats a
        launch as one that never started."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (identity,)).fetchone()
            except sqlite3.DatabaseError as e:
                raise StoreUnavailable(f"cannot read continuation store: {e}") from e
        return self._decode(row[0]) if row is not None else None

    def permits_used(self, pool: str) -> int:
        """The permits in use on ``pool``, derived from the durable running rows. There is
        no second counter — this is the ledger (ADR 0030)."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(demand), 0) FROM records"
                    " WHERE pool = ? AND state = ?",
                    (pool, RUNNING),
                ).fetchone()
            except sqlite3.DatabaseError as e:
                raise StoreUnavailable(f"cannot read permit ledger: {e}") from e
        return int(row[0])

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _encode(record: Record) -> str:
        data = {}
        for name in _COLUMNS:
            value = getattr(record, name)
            data[name] = sorted(value) if name in _SET_FIELDS else value
        return json.dumps(data)

    @staticmethod
    def _decode(payload: str) -> Record:
        data = json.loads(payload)
        for name in _SET_FIELDS:
            if name in data:
                data[name] = set(data[name])
        return Record(**{k: v for k, v in data.items() if k in _COLUMNS})
