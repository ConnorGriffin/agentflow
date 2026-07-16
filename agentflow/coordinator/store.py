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
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Callable, Mapping
from uuid import uuid4

from agentflow.coordinator.record import COMPLETED, NOT_STARTED, RUNNING, STARTED, WAITING, Record

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


@dataclass(frozen=True)
class ReservationLimits:
    """Global limits that must be decided with the permit reservation.

    ``lane_by_stage`` preserves dispatch's deliberate grouping: Review and Revise consume
    Build concurrency, while Intake consumes Triage concurrency. The production gate owns
    those policy values; the store only enforces the supplied snapshot atomically.
    """

    machine_ceiling: int
    stage_cap: int
    stage_lane: str
    lane_by_stage: Mapping[str, str]


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
        # A fully-initialized store is published atomically: it is built in a private temp
        # file under the same directory, then linked into place without replacement. The final path therefore
        # only ever appears as a complete, versioned database — a crash mid-creation leaves a
        # temp file behind, never a zero-byte final path that a later open would misread as a
        # fresh not-yet-initialized store (ADR 0030 fail-closed). Only an absent final path
        # triggers creation; an existing empty/corrupt file fails closed.
        if not self.path.exists():
            self._create_atomically()
        try:
            conn = self._open(self.path)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                conn.close()
                raise StoreUnavailable(
                    f"store schema {version} is newer than supported {SCHEMA_VERSION}")
            if version != SCHEMA_VERSION:
                conn.close()
                raise StoreUnavailable(f"store schema {version} is not readable")
        except sqlite3.DatabaseError as e:  # corrupt file, locked-beyond-wait, unreadable
            raise StoreUnavailable(f"cannot open continuation store: {e}") from e
        return conn

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        # Autocommit mode (isolation_level=None) so the reservation can hold a single
        # explicit BEGIN IMMEDIATE across its read and its write.
        conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000,
                               isolation_level=None, check_same_thread=False)
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return conn

    def _create_atomically(self) -> None:
        """Build a complete, versioned store in a temp file and publish it with one atomic,
        no-clobber hard link. A crash before publication leaves only the temp file;
        the final path is only ever an absent or a fully-initialized store, never a half-built
        zero-byte file that later opens would treat as fresh absence. Concurrent creators all
        open the one winning inode; no initialized store is ever replaced underneath an
        already-open connection."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        try:
            conn = self._open(tmp)
            try:
                self._initialize(conn)
            finally:
                conn.close()
            _fsync_path(tmp)
            try:
                os.link(tmp, self.path)         # atomic create-if-absent; never replaces a winner
            except FileExistsError:
                pass                            # another creator published the shared ledger
            _unlink(tmp)
            _fsync_path(self.path.parent)
        except sqlite3.DatabaseError as e:
            _unlink(tmp)
            raise StoreUnavailable(f"cannot create continuation store: {e}") from e
        except BaseException:
            _unlink(tmp)
            raise

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

    def upsert(self, record: Record) -> bool:
        """Persist one record only if its durable revision is still current.

        The caller's object is advanced to the committed revision. A stale writer returns
        ``False`` without changing durable state, so an old cycle can never replace a newer
        continuation or terminal transition.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (record.identity,)).fetchone()
                if row is not None and self._decode(row[0]).revision != record.revision:
                    self._conn.execute("ROLLBACK")
                    return False
                if row is None and record.revision != 0:
                    self._conn.execute("ROLLBACK")
                    return False
                record.revision += 1
                self._write(record)
                self._conn.execute("COMMIT")
                return True
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot write continuation store: {e}") from e

    def transition(self, expected: Record,
                   operation: Callable[[Record], bool]) -> Record | None:
        """Serialize one externally-backed terminal transition.

        The durable revision is claimed under ``BEGIN IMMEDIATE`` before ``operation`` runs.
        Therefore only one coordinator may perform the external finalization for a generation.
        A process crash releases SQLite's transaction; the stage adapter's idempotent durable
        proof can then be retried by a fresh coordinator. Returning false rolls back cleanly.
        """
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (expected.identity,)).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return None
                current = self._decode(row[0])
                if current.revision != expected.revision:
                    self._conn.execute("ROLLBACK")
                    return None
                if not operation(current):
                    self._conn.execute("ROLLBACK")
                    return None
                current.revision += 1
                self._write(current)
                self._conn.execute("COMMIT")
                return current
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot transition continuation store: {e}") from e
            except BaseException:
                self._rollback_quietly()
                raise

    def submit(self, record: Record,
               prior_identity: str | None = None
               ) -> tuple[Record, Record | None, bool, Record | None]:
        """Persist an idempotent stage submission. When ``prior_identity`` is supplied, the
        successor insert and completed predecessor's claim transfer are one transaction.
        Returns the durable successor, predecessor (if any), whether this call transferred
        ownership, and the updated root (for a descendant). Successor creation, claim transfer,
        and descendant registration share one transaction; no crash or concurrent root write can
        expose an orphaned descendant. Any missing or ineligible predecessor/root aborts without
        exposing a successor."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                successor_row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (record.identity,)).fetchone()
                successor = self._decode(successor_row[0]) if successor_row is not None else record
                prior = None
                root = None
                transferred = False
                if prior_identity is not None:
                    prior_row = self._conn.execute(
                        "SELECT data FROM records WHERE identity = ?", (prior_identity,)).fetchone()
                    if prior_row is None:
                        raise StoreUnavailable("cannot transfer claim: predecessor is missing")
                    prior = self._decode(prior_row[0])
                    already_transferred = (
                        prior.state == COMPLETED and prior.retired and not prior.claim
                        and successor_row is not None and successor.claim)
                    if not already_transferred:
                        if prior.state != COMPLETED or prior.retired or not prior.claim:
                            raise StoreUnavailable(
                                "cannot transfer claim: predecessor does not own a completed stage")
                        successor.claim = True
                        prior.claim = False
                        prior.retired = True
                        prior.revision += 1
                        self._write(prior)
                        transferred = True
                if successor_row is None or transferred:
                    successor.revision += 1
                    self._write(successor)
                if successor.root is not None:
                    root_row = self._conn.execute(
                        "SELECT data FROM records WHERE identity = ?", (successor.root,)).fetchone()
                    if root_row is None:
                        raise StoreUnavailable("cannot register descendant: root is missing")
                    root = self._decode(root_row[0])
                    if root.state != RUNNING or root.retired or root.hold_pending:
                        raise StoreUnavailable(
                            "cannot register descendant: root is not actively running")
                    if successor.identity not in root.descendants:
                        root.descendants.add(successor.identity)
                        root.revision += 1
                        self._write(root)
                self._conn.execute("COMMIT")
                return successor, prior, transferred, root
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot submit continuation: {e}") from e
            except BaseException:
                self._rollback_quietly()
                raise

    def reserve(self, record: Record, budget: int,
                limits: ReservationLimits | None = None,
                expected_launch_token: str | None = None,
                expected_revision: int | None = None) -> bool:
        """Atomically reserve ``record``'s demand on its pool, or refuse. Availability is read
        and the running row is written under one ``BEGIN IMMEDIATE``, so two coordinator
        instances racing on the same file serialize on the write lock and can never push a
        pool's ledger past ``budget`` (ADR 0029/0030). When supplied, machine and dispatch-lane
        limits are checked from those same running rows in the same transaction, so racers on
        different pools cannot exceed either global cap. Returns whether the reservation was
        taken; on refusal nothing is written.

        The write is a same-identity compare-and-set: an existing row must still be ``waiting``
        at the launch-token generation the caller loaded.
        Two coordinator instances that both loaded the
        same ``waiting`` record and each flipped it to ``running`` with its own fresh launch token
        serialize on the write lock — the first commits its reservation, and the second finds the
        row already advanced and refuses instead of clobbering the winner's token or terminal
        outcome. Exactly one running reservation and one launch token survive the race."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing_row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (record.identity,)).fetchone()
                if existing_row is not None:
                    existing = self._decode(existing_row[0])
                    if (existing.state != WAITING
                            or existing.launch_token != expected_launch_token
                            or (expected_revision is not None
                                and existing.revision != expected_revision)):
                        # Reservation is a WAITING -> RUNNING compare-and-set. Any other durable
                        # state or token means another instance already advanced this identity;
                        # never overwrite newer attempts, completion, or hold state with a stale
                        # waiting generation.
                        self._conn.execute("ROLLBACK")
                        return False
                if limits is not None:
                    rows = self._conn.execute(
                        "SELECT data FROM records WHERE state = ? AND identity != ?",
                        (RUNNING, record.identity),
                    ).fetchall()
                    running = [self._decode(row[0]) for row in rows]
                    roots = [item for item in running if item.root is None]
                    if len(roots) >= limits.machine_ceiling:
                        self._conn.execute("ROLLBACK")
                        return False
                    lane_count = sum(
                        limits.lane_by_stage.get(item.stage, item.stage) == limits.stage_lane
                        for item in roots
                    )
                    if lane_count >= limits.stage_cap:
                        self._conn.execute("ROLLBACK")
                        return False
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(demand), 0) FROM records"
                    " WHERE pool = ? AND state = ? AND identity != ?",
                    (record.pool, RUNNING, record.identity),
                ).fetchone()
                if int(row[0]) + record.demand > budget:
                    self._conn.execute("ROLLBACK")
                    return False
                record.revision = (existing.revision + 1 if existing_row is not None else 1)
                self._write(record)
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

    def child_start(self, identity: str, token: str, pid: int) -> bool:
        """The launched child's guarded ``started`` write (ADR 0030). Atomically records
        ``started`` with ``pid`` as the family *only if* the record is still the ``running``
        reservation that stamped ``token``. If the coordinator already disowned this launch on
        a handshake timeout (rotating the token) or returned the record to ``waiting``, the
        write is refused and the caller must not become a provider — this is what stops an
        uncancelled bootstrap from starting an unreserved, uncounted provider."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (identity,)).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return False
                record = self._decode(row[0])
                if record.state != RUNNING or record.launch_token != token:
                    self._conn.execute("ROLLBACK")
                    return False
                record.start_fact = STARTED
                record.family = str(pid)
                record.process_alive = True
                record.revision += 1
                self._write(record)
                self._conn.execute("COMMIT")
                return True
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot record child start: {e}") from e

    def disown_launch(self, identity: str, token: str) -> tuple[str, str | None]:
        """The coordinator's atomic timeout finalize (ADR 0030). If the child already won —
        durably recorded ``started`` under ``token`` — return ``(started, family)`` and leave
        it. Otherwise rotate the reservation's launch token so any still-running child's late
        guarded write is refused, and return ``(not_started, None)``. Exactly one of this and
        :meth:`child_start` can win, so a launch never both times out and starts a provider."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (identity,)).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return (NOT_STARTED, None)
                record = self._decode(row[0])
                if record.start_fact == STARTED and record.launch_token == token:
                    self._conn.execute("ROLLBACK")
                    return (STARTED, record.family)
                if record.state != RUNNING or record.launch_token != token:
                    # A delayed timeout belongs to an older reservation generation. It has no
                    # authority over the current attempt and must not rotate its token.
                    self._conn.execute("ROLLBACK")
                    return (NOT_STARTED, None)
                record.start_fact = NOT_STARTED
                record.launch_token = uuid4().hex  # any late child write can no longer match
                record.revision += 1
                self._write(record)
                self._conn.execute("COMMIT")
                return (NOT_STARTED, None)
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot disown launch: {e}") from e

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

    def _write(self, record: Record) -> None:
        """The one INSERT-or-update statement, shared by every writer. The caller owns the
        transaction and lock, so this is safe to use inside a ``BEGIN IMMEDIATE`` section."""
        self._conn.execute(
            "INSERT INTO records (identity, pool, state, demand, data)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(identity) DO UPDATE SET"
            " pool=excluded.pool, state=excluded.state,"
            " demand=excluded.demand, data=excluded.data",
            (record.identity, record.pool, record.state, record.demand,
             self._encode(record)),
        )

    def _rollback_quietly(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass

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


def _fsync_path(path: Path) -> None:
    """Best-effort durability: flush the temp file (and, when it is a directory, the rename
    itself) so the atomic publish survives power loss. Platforms that refuse a directory
    fsync are tolerated — the rename is still atomic."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
