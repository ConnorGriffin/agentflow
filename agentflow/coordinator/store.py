"""The coordinator's private, versioned continuation store (ADR 0030).

One SQLite database under agentflow's existing local state directory holds every
continuation record. The ``running`` rows are the permit ledger, so the reservation and
continuation state share one critical section and one file. This store is a private
implementation detail of the coordinator — there is no public storage seam, and there
will not be one until a second real representation exists (ADR 0030 alternatives).

Fail-closed is the whole point of the safety story. An absent store is created
atomically. An unreadable, corrupt, locked-beyond-the-bounded-wait, or newer-schema
store raises :class:`StoreUnavailable`; the coordinator then starts no provider and
clears no claim (ADR 0028's "unreadable store fails closed").
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
            conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000,
                                   check_same_thread=False)
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
        with conn:
            conn.execute(
                "CREATE TABLE records ("
                " identity TEXT PRIMARY KEY,"
                " pool TEXT NOT NULL,"
                " state TEXT NOT NULL,"
                " demand INTEGER NOT NULL,"
                " data TEXT NOT NULL)"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

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
                with self._conn:
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
