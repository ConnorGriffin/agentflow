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
from hashlib import sha256
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Literal, Mapping, TypeAlias
from uuid import uuid4

from agentflow.state import state_dir as _state_dir, state_path
from agentflow.coordinator.errors import StoreUnavailable
from agentflow.coordinator.record import COMPLETED, NOT_STARTED, RUNNING, STARTED, WAITING, Record
from agentflow.operational_safety import (
    AdmittedLaunch,
    ActionIdempotentRerunEffect,
    CheckEvidenceAuthority,
    OperationalSafety,
    PromotionReceiptAuthority as SafetyPromotionReceiptAuthority,
    SafetyRefused,
    decode_admitted_launch,
    materialize_route_cell,
    _SafetyIdentityMismatch,
    _SafetyMissing,
    _AdmissionContext,
    _SafetyAdmissionResult,
)

if TYPE_CHECKING:
    from agentflow.canary_attribution import CanaryAttribution, PromotionReceiptAuthority
    from agentflow.capability_contracts import CapabilityReadyFact
    from agentflow.effective_policy import NotApplicableBriefing, ReadyBriefing

SCHEMA_VERSION = 4
_RECORDS_SCHEMA = (
    "CREATE TABLE records ("
    " identity TEXT PRIMARY KEY,"
    " pool TEXT NOT NULL,"
    " state TEXT NOT NULL,"
    " demand INTEGER NOT NULL,"
    " data TEXT NOT NULL)"
)
_ADMISSION_RECEIPTS_SCHEMA = (
    "CREATE TABLE admission_receipts ("
    " stage_identity TEXT PRIMARY KEY,"
    " subject_revision TEXT NOT NULL,"
    " briefing_id TEXT,"
    " briefing_digest TEXT,"
    " capability_id TEXT NOT NULL,"
    " capability_digest TEXT NOT NULL,"
    " route_id TEXT NOT NULL,"
    " route_cell_digest TEXT NOT NULL,"
    " launch_config_digest TEXT NOT NULL,"
    " safety_state_id TEXT NOT NULL)"
)
_ADMISSION_RECEIPTS_NO_UPDATE = (
    "CREATE TRIGGER admission_receipts_no_update BEFORE UPDATE ON admission_receipts "
    "BEGIN SELECT RAISE(ABORT, 'admission_receipts is append-only'); END"
)
_ADMISSION_RECEIPTS_NO_DELETE = (
    "CREATE TRIGGER admission_receipts_no_delete BEFORE DELETE ON admission_receipts "
    "BEGIN SELECT RAISE(ABORT, 'admission_receipts is append-only'); END"
)
_ADMISSION_RECEIPTS_FINGERPRINT = (
    ("table", "admission_receipts", "admission_receipts",
     "createtableadmission_receipts(stage_identitytextprimarykey,subject_revisiontextnotnull,"
     "briefing_idtext,briefing_digesttext,capability_idtextnotnull,capability_digesttextnotnull,"
     "route_idtextnotnull,route_cell_digesttextnotnull,launch_config_digesttextnotnull,"
     "safety_state_idtextnotnull)"),
    ("trigger", "admission_receipts_no_delete", "admission_receipts",
     "createtriggeradmission_receipts_no_deletebeforedeleteonadmission_receiptsbeginselectraise("
     "abort,'admission_receiptsisappend-only');end"),
    ("trigger", "admission_receipts_no_update", "admission_receipts",
     "createtriggeradmission_receipts_no_updatebeforeupdateonadmission_receiptsbeginselectraise("
     "abort,'admission_receiptsisappend-only');end"),
)


def _store_v4_fingerprint():
    from agentflow.canary_attribution import STORE_V3_SCHEMA_FINGERPRINT
    return tuple(sorted(STORE_V3_SCHEMA_FINGERPRINT + _ADMISSION_RECEIPTS_FINGERPRINT))


STORE_V4_SCHEMA_FINGERPRINT = _store_v4_fingerprint()
STORE_V4_SCHEMA_FINGERPRINT_DIGEST = sha256(json.dumps(
    STORE_V4_SCHEMA_FINGERPRINT, sort_keys=True, separators=(",", ":"),
    ensure_ascii=True, allow_nan=False).encode()).hexdigest()
# Bounded wait for a busy database. Beyond this we fail closed rather than block a whole
# daemon cycle on a lock we cannot prove will clear.
_BUSY_TIMEOUT_MS = int(os.environ.get("AGENTFLOW_COORD_BUSY_MS", "2000"))

_SET_FIELDS = {"descendants"}
_COLUMNS = [f.name for f in fields(Record)]
SUPERVISOR_WINDOW = 2 * 3600
V2_TO_V3_FAULT_OBSERVATIONS = (
    "v2-to-v3:before-begin",
    "v2-to-v3:after-begin",
    "v2-to-v3:create:canary_attributions:before",
    "v2-to-v3:create:canary_attributions:after",
    "v2-to-v3:create:no-update-trigger:before",
    "v2-to-v3:create:no-update-trigger:after",
    "v2-to-v3:create:no-delete-trigger:before",
    "v2-to-v3:create:no-delete-trigger:after",
    "v2-to-v3:before-fingerprint",
    "v2-to-v3:after-fingerprint",
    "v2-to-v3:after-user-version",
    "v2-to-v3:before-commit",
    "v2-to-v3:after-commit",
)
V4_ADMISSION_PRECOMMIT_CUTPOINTS = (
    "after-begin",
    "after-waiting-cas",
    "after-capacity",
    "after-identity-validation",
    "after-route-validation",
    "after-safety",
    "after-attribution-before-successor",
    "after-receipt-before-successor",
    "after-successor-before-commit",
)


def state_dir() -> Path:
    """agentflow's local state directory, honoring ``AGENTFLOW_STATE`` like the rest of the
    daemon. The coordinator's store lives beneath this; callers never choose a path."""
    return _state_dir()


def default_store_path() -> Path:
    """Where the coordinator privately keeps its continuation store (ADR 0030). There is one
    store per state directory; a fresh coordinator over the same directory recovers it."""
    return state_path("coordinator", "records.db")


RouteAdmissionCode = Literal["missing", "stale", "mismatched", "unreadable", "quarantined"]
ROUTE_ADMISSION_REFUSAL_CODES = {
    "missing", "stale", "mismatched", "unreadable", "quarantined",
}


class RouteAdmissionRefused(RuntimeError):
    """Closed Store-boundary refusal; private diagnostics never cross this seam."""

    __slots__ = ("code",)

    def __init__(self, code: RouteAdmissionCode) -> None:
        if type(code) is not str or code not in ROUTE_ADMISSION_REFUSAL_CODES:
            raise TypeError("unknown route admission refusal code")
        self.code = code
        super().__init__(code)


ADMISSION_REFUSAL_CODES = frozenset({
    "admission_identity_migration_required",
    "briefing_mismatch",
    "capability_mismatch",
    "route_mismatch",
    "safety_refused",
    "briefing_overflow", "incompatible_policy", "invalid_briefing", "invalid_overlay",
    "invalid_receipt", "missing_policy", "missing_receipt",
})


class AdmissionRefused(RuntimeError):
    """One content-free refusal vocabulary crossing Store's admission boundary."""

    __slots__ = ("code",)

    def __init__(self, code: str) -> None:
        if type(code) is not str or code not in ADMISSION_REFUSAL_CODES:
            raise TypeError("unknown admission refusal code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class RouteSelectionIdentity:
    route_id: str
    route_cell_digest: str
    launch_config_digest: str


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


@dataclass(frozen=True, slots=True)
class ReservationIntent:
    identity: str
    expected_launch_token: str | None
    expected_revision: int
    now: int
    daemon_generation: str
    budget: int
    limits: ReservationLimits | None
    route_cell_digest: str | None
    # These facts are deliberately optional at the type boundary so v3 Store databases and
    # their historical tests remain readable.  OperationalSafetyAndCanary admission rejects
    # their absence; only a legacy NoAdmission Store may reserve without them.
    briefing: "ReadyBriefing | NotApplicableBriefing | None" = None
    capability: "CapabilityReadyFact | None" = None


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    """The immutable authority tuple committed with one logical-stage admission."""

    subject_revision: str
    briefing_id: str | None
    briefing_digest: str | None
    capability_id: str
    capability_digest: str
    route_id: str
    route_cell_digest: str
    launch_config_digest: str
    safety_state_id: str


@dataclass(frozen=True, slots=True)
class AdmissionResult:
    successor: Record
    safety_state_id: str | None
    canary_attribution: CanaryAttribution | None
    admission_receipt: AdmissionReceipt | None = None
    admitted_launch: AdmittedLaunch | None = None


@dataclass(frozen=True, slots=True)
class SafetySources:
    check_evidence: CheckEvidenceAuthority | None = None
    promotion_receipts: SafetyPromotionReceiptAuthority | None = None
    rerun_effect: ActionIdempotentRerunEffect | None = None


@dataclass(frozen=True)
class NoAdmission:
    pass


@dataclass(frozen=True, slots=True)
class OperationalSafetyOnly:
    safety_sources: SafetySources


@dataclass(frozen=True, slots=True)
class OperationalSafetyAndCanary:
    safety_sources: SafetySources
    promotion_receipts: PromotionReceiptAuthority


StoreAdmissionMode: TypeAlias = NoAdmission | OperationalSafetyOnly | OperationalSafetyAndCanary


class Store:
    """A thin durable table of continuation records keyed by stage identity.

    Records are loaded into the coordinator's working set on open and written through on
    every transition, so a fresh coordinator over the same file recovers the same state —
    that is how the crash-recovery boundaries are exercised.
    """

    def __init__(self, path: Path | str, *,
                 admission_mode: StoreAdmissionMode = NoAdmission()) -> None:
        self.path = Path(path)
        # The daemon dispatches concurrent chains through one coordinator, so the single
        # connection is shared across threads and serialized by this lock — the reservation
        # critical section is one place, matching the one-ledger design (ADR 0030).
        self._lock = threading.RLock()
        self._promotion_receipt_callback_active = threading.Event()
        self._conn = self._connect()
        self._operational_safety: OperationalSafety | None = None
        self._canary_attribution = None
        try:
            if type(admission_mode) is NoAdmission:
                return
            if type(admission_mode) not in {
                    OperationalSafetyOnly, OperationalSafetyAndCanary}:
                raise TypeError("unknown Store admission mode")
            sources = admission_mode.safety_sources
            if type(sources) is not SafetySources:
                raise TypeError("Store safety sources must be the exact frozen value")
            self._operational_safety = OperationalSafety(
                self, check_evidence=sources.check_evidence,
                promotion_receipts=sources.promotion_receipts,
                rerun_effect=sources.rerun_effect)
            if type(admission_mode) is OperationalSafetyAndCanary:
                from agentflow.canary_attribution import CanaryAttributionAuthority
                self._canary_attribution = CanaryAttributionAuthority(
                    self, self._operational_safety, admission_mode.promotion_receipts)
        except BaseException:
            self._conn.close()
            raise

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
            if version == 1:
                if _schema_fingerprint(conn) != _expected_schema_fingerprint(1):
                    conn.close()
                    raise StoreUnavailable("store schema 1 does not match the migration source")
                self._migrate_v1_to_v2(conn)
                version = 2
            if version == 2:
                if _schema_fingerprint(conn) != _expected_schema_fingerprint(2):
                    conn.close()
                    raise StoreUnavailable("store schema 2 does not match the migration source")
                self._migrate_v2_to_v3(conn)
                version = 3
            if version == 3:
                if _schema_fingerprint(conn) != _expected_schema_fingerprint(3):
                    conn.close()
                    raise StoreUnavailable("store schema 3 does not match the migration source")
                self._migrate_v3_to_v4(conn)
                version = 4
            if version != SCHEMA_VERSION:
                conn.close()
                raise StoreUnavailable(f"store schema {version} is not readable")
            if _schema_fingerprint(conn) != _expected_schema_fingerprint(4):
                conn.close()
                raise StoreUnavailable("store schema 4 does not match the accepted schema")
        except sqlite3.DatabaseError as e:  # corrupt file, locked-beyond-wait, unreadable
            raise StoreUnavailable(f"cannot open continuation store: {e}") from e
        return conn

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        # Autocommit mode (isolation_level=None) so the reservation can hold a single
        # explicit BEGIN IMMEDIATE across its read and its write.
        conn = sqlite3.connect(
            path, timeout=_BUSY_TIMEOUT_MS / 1000, isolation_level=None,
            check_same_thread=False)
        from agentflow.canary_attribution import register_sql_functions
        register_sql_functions(conn)
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA recursive_triggers = ON")
        if conn.execute("PRAGMA recursive_triggers").fetchone()[0] != 1:
            conn.close()
            raise StoreUnavailable("recursive trigger enforcement is unavailable")
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
            conn.execute(_RECORDS_SCHEMA)
            from agentflow.operational_safety import OperationalSafety
            OperationalSafety.initialize_schema(conn)
            from agentflow.canary_attribution import initialize_schema
            initialize_schema(conn)
            conn.execute(_ADMISSION_RECEIPTS_SCHEMA)
            conn.execute(_ADMISSION_RECEIPTS_NO_UPDATE)
            conn.execute(_ADMISSION_RECEIPTS_NO_DELETE)
            if _schema_fingerprint(conn) != _expected_schema_fingerprint(4):
                raise sqlite3.DatabaseError("initialized coordinator schema was not accepted")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
        """Add OperationalSafety-owned tables without rewriting continuation rows."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            from agentflow.operational_safety import OperationalSafety
            OperationalSafety.initialize_schema(conn)
            if _schema_fingerprint(conn) != _expected_schema_fingerprint(2):
                raise sqlite3.DatabaseError("migrated coordinator schema was not accepted")
            conn.execute("PRAGMA user_version = 2")
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _migration_checkpoint(_name: str) -> None:
        """Fault-injection seam for the Store v2-to-v3 migration tests."""

    @staticmethod
    def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
        """Add append-only CanaryAttribution state without rewriting existing owners."""
        committed = False
        try:
            Store._migration_checkpoint("v2-to-v3:before-begin")
            conn.execute("BEGIN IMMEDIATE")
            Store._migration_checkpoint("v2-to-v3:after-begin")
            from agentflow.canary_attribution import initialize_schema
            initialize_schema(conn, checkpoint=Store._migration_checkpoint)
            Store._migration_checkpoint("v2-to-v3:before-fingerprint")
            if _schema_fingerprint(conn) != _expected_schema_fingerprint(3):
                raise sqlite3.DatabaseError("migrated coordinator schema was not accepted")
            Store._migration_checkpoint("v2-to-v3:after-fingerprint")
            conn.execute("PRAGMA user_version = 3")
            Store._migration_checkpoint("v2-to-v3:after-user-version")
            Store._migration_checkpoint("v2-to-v3:before-commit")
            conn.execute("COMMIT")
            committed = True
            Store._migration_checkpoint("v2-to-v3:after-commit")
        except BaseException:
            if not committed and conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
        """Add the insert-only authority receipt without rewriting historical Records."""
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_ADMISSION_RECEIPTS_SCHEMA)
            conn.execute(_ADMISSION_RECEIPTS_NO_UPDATE)
            conn.execute(_ADMISSION_RECEIPTS_NO_DELETE)
            if _schema_fingerprint(conn) != _expected_schema_fingerprint(4):
                raise sqlite3.DatabaseError("migrated coordinator schema was not accepted")
            conn.execute("PRAGMA user_version = 4")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
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

    def lead_availability(self, now: int) -> tuple[dict[str, int], dict[str, bool]]:
        """One durable snapshot for cold session-lead selection.

        The hard permit counts and the PR-bound drain-first barrier must describe
        the same instant.  This is a read-only convenience over the existing
        ledger, not a second reservation ledger; coordinator admission remains
        the atomic authority when the selected submission starts.
        """
        from agentflow.coordinator.admission import pr_bound_waiting

        with self._lock:
            try:
                rows = self._conn.execute("SELECT data FROM records").fetchall()
            except sqlite3.DatabaseError as e:
                raise StoreUnavailable(f"cannot read continuation store: {e}") from e
        records = [self._decode(row[0]) for row in rows]
        permits = {
            pool: sum(record.demand for record in records
                      if record.pool == pool and record.state == RUNNING)
            for pool in ("claude", "codex")
        }
        waiting = {pool: pr_bound_waiting(records, pool, now) for pool in permits}
        return permits, waiting

    def _refuse_promotion_receipt_callback_mutation(self) -> None:
        if self._promotion_receipt_callback_active.is_set():
            raise StoreUnavailable("reentrant Store mutation during admission")

    @contextmanager
    def _promotion_receipt_callback(self):
        """Mark only the untrusted receipt-reader call as non-reentrant."""
        if self._promotion_receipt_callback_active.is_set():
            raise StoreUnavailable("promotion receipt callback is already active")
        self._promotion_receipt_callback_active.set()
        try:
            yield
        finally:
            self._promotion_receipt_callback_active.clear()

    def upsert(self, record: Record, *, retire_descendants: bool = False) -> bool:
        """Persist one record only if its durable revision is still current.

        The caller's object is advanced to the committed revision. A stale writer returns
        ``False`` without changing durable state, so an old cycle can never replace a newer
        continuation or terminal transition.
        """
        self._refuse_promotion_receipt_callback_mutation()
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
                if retire_descendants:
                    self._retire_descendants(record)
                self._conn.execute("COMMIT")
                return True
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot write continuation store: {e}") from e

    def transition(self, expected: Record,
                   operation: Callable[[Record], bool], *,
                   retire_descendants: bool = False) -> Record | None:
        """Serialize one externally-backed terminal transition.

        The durable revision is claimed under ``BEGIN IMMEDIATE`` before ``operation`` runs.
        Therefore only one coordinator may perform the external finalization for a generation.
        A process crash releases SQLite's transaction; the stage adapter's idempotent durable
        proof can then be retried by a fresh coordinator. Returning false rolls back cleanly.
        """
        self._refuse_promotion_receipt_callback_mutation()
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
                if retire_descendants:
                    self._retire_descendants(current)
                self._conn.execute("COMMIT")
                return current
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot transition continuation store: {e}") from e
            except BaseException:
                self._rollback_quietly()
                raise

    def submit(self, record: Record,
               prior_identity: str | None = None, *,
               supersede: bool = False
               ) -> tuple[Record, Record | None, bool, Record | None]:
        """Persist an idempotent stage submission. When ``prior_identity`` is supplied, the
        successor insert and completed predecessor's claim transfer are one transaction.
        Returns the durable successor, predecessor (if any), whether this call transferred
        ownership, and the updated root (for a descendant). Successor creation, claim transfer,
        and descendant registration share one transaction; no crash or concurrent root write can
        expose an orphaned descendant. Any missing or ineligible predecessor/root aborts without
        exposing a successor.

        ``supersede`` relaxes the predecessor eligibility to any claim-holding, not-yet-retired
        record — not only a completed one — so a Review stranded at a head that has moved off its
        immutable target (#208) can atomically hand its claim to a bounded successor at the live
        head. The superseded predecessor is left completed-and-retired, exactly as a normal
        claim-transfer leaves its completed predecessor, so it leaves the running ledger and is
        never re-admitted or re-reconciled."""
        self._refuse_promotion_receipt_callback_mutation()
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
                        eligible = (prior.claim and not prior.retired if supersede
                                    else prior.state == COMPLETED and not prior.retired
                                    and prior.claim)
                        if not eligible:
                            raise StoreUnavailable(
                                "cannot transfer claim: predecessor does not own a completed stage")
                        successor.claim = True
                        prior.claim = False
                        prior.retired = True
                        if supersede:
                            prior.state = COMPLETED  # leave the running ledger; never re-admitted
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
                    already_registered = (
                        successor_row is not None
                        and successor.identity in root.descendants)
                    if (not already_registered
                            and (root.state != RUNNING or root.retired or root.hold_pending)):
                        raise StoreUnavailable(
                            "cannot register descendant: root is not actively running")
                    if not already_registered:
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

    @staticmethod
    def _admission_checkpoint(_name: str) -> None:
        """Fault-observation seam around the two atomic admission cutpoints."""

    def reserve(self, intent: ReservationIntent) -> AdmissionResult | None:
        """Atomically admit the exact durable WAITING Record named by ``intent``.

        Store owns the immediate transaction, derives the participant context and ten-field
        successor, and publishes a result only after commit.  Ordinary ineligibility returns
        ``None`` without calling either admission owner or writing any row.
        """
        self._refuse_promotion_receipt_callback_mutation()
        if type(intent) is not ReservationIntent:
            raise TypeError("reserve requires the exact ReservationIntent")
        if (not isinstance(intent.identity, str) or not intent.identity
                or (intent.expected_launch_token is not None
                    and not isinstance(intent.expected_launch_token, str))
                or isinstance(intent.expected_revision, bool)
                or not isinstance(intent.expected_revision, int)
                or intent.expected_revision < 0
                or isinstance(intent.now, bool) or not isinstance(intent.now, int)
                or intent.now < 0
                or not isinstance(intent.daemon_generation, str)
                or not intent.daemon_generation
                or (intent.limits is not None
                    and type(intent.limits) is not ReservationLimits)):
            return None
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._admission_checkpoint("after-begin")
                existing_row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (intent.identity,)).fetchone()
                if existing_row is None:
                    self._conn.execute("ROLLBACK")
                    return None
                existing = self._decode(existing_row[0])
                if (existing.state != WAITING
                        or existing.launch_token != intent.expected_launch_token
                        or existing.revision != intent.expected_revision
                        or isinstance(intent.budget, bool) or not isinstance(intent.budget, int)
                        or intent.budget < 0):
                    self._conn.execute("ROLLBACK")
                    return None
                self._admission_checkpoint("after-waiting-cas")
                if intent.limits is not None:
                    limits = intent.limits
                    rows = self._conn.execute(
                        "SELECT data FROM records WHERE state = ? AND identity != ?",
                        (RUNNING, existing.identity),
                    ).fetchall()
                    running = [self._decode(row[0]) for row in rows]
                    roots = [item for item in running if item.root is None]
                    if len(roots) >= limits.machine_ceiling:
                        self._conn.execute("ROLLBACK")
                        return None
                    lane_count = sum(
                        limits.lane_by_stage.get(item.stage, item.stage) == limits.stage_lane
                        for item in roots
                    )
                    if lane_count >= limits.stage_cap:
                        self._conn.execute("ROLLBACK")
                        return None
                row = self._conn.execute(
                    "SELECT COALESCE(SUM(demand), 0) FROM records"
                    " WHERE pool = ? AND state = ? AND identity != ?",
                    (existing.pool, RUNNING, existing.identity),
                ).fetchone()
                if int(row[0]) + existing.demand > intent.budget:
                    self._conn.execute("ROLLBACK")
                    return None
                self._admission_checkpoint("after-capacity")
                composed = self._canary_attribution is not None
                admitted_launch = None
                if composed:
                    if (not existing.subject_revision or not existing.route_id
                            or not existing.route_cell_digest
                            or not existing.launch_config_digest):
                        raise AdmissionRefused("admission_identity_migration_required")
                    if intent.route_cell_digest != existing.route_cell_digest:
                        raise AdmissionRefused("route_mismatch")
                    briefing_id, briefing_digest = self._validate_briefing(existing, intent.briefing)
                    capability = self._validate_capability(existing, intent.capability)
                    self._admission_checkpoint("after-identity-validation")
                    try:
                        admitted_launch = self._resolve_active_admitted(
                            existing.identity, existing.revision, existing.route_id,
                            conn=self._conn)
                    except (RouteAdmissionRefused, SafetyRefused) as error:
                        raise AdmissionRefused("safety_refused") from error
                    if (admitted_launch.route_cell.digest != existing.route_cell_digest
                            or admitted_launch.route_cell.launch_config_digest
                            != existing.launch_config_digest
                            or admitted_launch.route_cell.route_id != existing.route_id):
                        raise AdmissionRefused("route_mismatch")
                    self._admission_checkpoint("after-route-validation")
                if (self._operational_safety is not None
                        and (not isinstance(intent.route_cell_digest, str)
                             or not intent.route_cell_digest)):
                    self._conn.execute("ROLLBACK")
                    return None
                safety_state_id = None
                attribution = None
                if self._operational_safety is not None:
                    context = _AdmissionContext(
                        stage_identity=existing.identity,
                        repository=existing.repo,
                        stage=existing.stage,
                        provider=existing.pool,
                        model=existing.model,
                        route_cell_digest=intent.route_cell_digest)  # type: ignore[arg-type]
                    try:
                        safety = self._operational_safety._participate_in_admission(context)
                    except SafetyRefused as error:
                        if composed:
                            raise AdmissionRefused("safety_refused") from error
                        raise
                    if (type(safety) is not _SafetyAdmissionResult
                            or not isinstance(safety.safety_state_id, str)
                            or not safety.safety_state_id):
                        if composed:
                            raise AdmissionRefused("safety_refused")
                        raise SafetyRefused("OperationalSafety returned an invalid admission result")
                    safety_state_id = safety.safety_state_id
                    self._admission_checkpoint("after-safety")
                    if self._canary_attribution is not None:
                        from agentflow.canary_attribution import (
                            CanaryAttributionRefused, _CanaryAdmissionResult,
                        )
                        try:
                            canary = self._canary_attribution._participate_in_admission(context)
                        except CanaryAttributionRefused as error:
                            raise AdmissionRefused("safety_refused") from error
                        if type(canary) is not _CanaryAdmissionResult:
                            raise AdmissionRefused("safety_refused")
                        attribution = canary.attribution
                self._admission_checkpoint("after-attribution-before-successor")
                receipt = None
                if composed:
                    receipt = AdmissionReceipt(
                        existing.subject_revision, briefing_id, briefing_digest,
                        capability.capability_id, capability.capability_digest,
                        existing.route_id, existing.route_cell_digest,
                        existing.launch_config_digest, safety_state_id)  # type: ignore[arg-type]
                    self._insert_or_validate_admission_receipt(existing.identity, receipt)
                    self._admission_checkpoint("after-receipt-before-successor")
                successor = replace(
                    existing,
                    state=RUNNING,
                    revision=existing.revision + 1,
                    start_fact=None,
                    launch_token=uuid4().hex,
                    family=None,
                    process_alive=False,
                    attempt_committed=False,
                    daemon_generation=intent.daemon_generation,
                    started_at=intent.now,
                    deadline=intent.now + SUPERVISOR_WINDOW,
                )
                self._write(successor)
                self._admission_checkpoint("after-successor-before-commit")
                self._conn.execute("COMMIT")
                self._admission_checkpoint("after-commit")
                return AdmissionResult(
                    successor, safety_state_id, attribution, receipt, admitted_launch)
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot reserve on continuation store: {e}") from e
            except BaseException:
                self._rollback_quietly()
                raise

    @staticmethod
    def _validate_briefing(existing: Record, briefing) -> tuple[str | None, str | None]:
        from agentflow.effective_policy import (
            NotApplicableBriefing, ReadyBriefing, validate_briefing,
        )
        if existing.stage == "converse":
            if briefing is not None:
                raise AdmissionRefused("briefing_mismatch")
            return None, None
        if type(briefing) not in {ReadyBriefing, NotApplicableBriefing}:
            raise AdmissionRefused("briefing_mismatch")
        if (briefing.repository != existing.repo or briefing.stage != existing.stage
                or briefing.subject_revision != existing.subject_revision):
            raise AdmissionRefused("briefing_mismatch")
        if not validate_briefing(briefing):
            raise AdmissionRefused("briefing_mismatch")
        return briefing.briefing_id, briefing.briefing_digest

    @staticmethod
    def _validate_capability(existing: Record, capability):
        from agentflow.capability_contracts import validate_capability_ready_fact
        if (not validate_capability_ready_fact(capability)
                or capability.stage != existing.stage
                or capability.provider != existing.pool):
            raise AdmissionRefused("capability_mismatch")
        return capability

    def _insert_or_validate_admission_receipt(
            self, stage_identity: str, receipt: AdmissionReceipt) -> None:
        row = self._conn.execute(
            "SELECT subject_revision, briefing_id, briefing_digest, capability_id, "
            "capability_digest, route_id, route_cell_digest, launch_config_digest, "
            "safety_state_id FROM admission_receipts WHERE stage_identity = ?",
            (stage_identity,),
        ).fetchone()
        values = tuple(getattr(receipt, item.name) for item in fields(AdmissionReceipt))
        if row is not None:
            prior = AdmissionReceipt(*row)
            if (prior.subject_revision, prior.briefing_id, prior.briefing_digest) != (
                    receipt.subject_revision, receipt.briefing_id, receipt.briefing_digest):
                raise AdmissionRefused("briefing_mismatch")
            if (prior.capability_id, prior.capability_digest) != (
                    receipt.capability_id, receipt.capability_digest):
                raise AdmissionRefused("capability_mismatch")
            if (prior.route_id, prior.route_cell_digest, prior.launch_config_digest) != (
                    receipt.route_id, receipt.route_cell_digest, receipt.launch_config_digest):
                raise AdmissionRefused("route_mismatch")
            if prior.safety_state_id != receipt.safety_state_id:
                raise AdmissionRefused("safety_refused")
            return
        self._conn.execute(
            "INSERT INTO admission_receipts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (stage_identity, *values),
        )

    def read_admission_receipt(self, stage_identity: str) -> AdmissionReceipt | None:
        """Read one immutable admission authority tuple through Store's public interface."""
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT subject_revision, briefing_id, briefing_digest, capability_id, "
                    "capability_digest, route_id, route_cell_digest, launch_config_digest, "
                    "safety_state_id FROM admission_receipts WHERE stage_identity = ?",
                    (stage_identity,),
                ).fetchone()
            except sqlite3.DatabaseError as error:
                raise StoreUnavailable(f"cannot read admission receipt: {error}") from error
        return AdmissionReceipt(*row) if row is not None else None

    def resolve_admitted_launch(self, stage_identity: str, expected_revision: int,
                                route_id: str) -> AdmittedLaunch:
        """Resolve and decode one active route, exposing only a closed refusal code."""
        if self._operational_safety is None:
            raise RouteAdmissionRefused("unreadable")
        with self._lock:
            try:
                return self._resolve_active_admitted(
                    stage_identity, expected_revision, route_id, conn=self._conn)
            except RouteAdmissionRefused:
                raise
            except (SafetyRefused, StoreUnavailable, sqlite3.DatabaseError, ValueError, TypeError):
                raise RouteAdmissionRefused("unreadable") from None

    def _resolve_active_admitted(self, stage_identity: str, expected_revision: int,
                                 route_id: str, *, conn: sqlite3.Connection) -> AdmittedLaunch:
        row = conn.execute(
            "SELECT data FROM records WHERE identity = ?", (stage_identity,)).fetchone()
        if row is None:
            raise RouteAdmissionRefused("missing")
        record = self._decode(row[0])
        if record.revision != expected_revision:
            raise RouteAdmissionRefused("stale")
        identity = (record.repo, record.stage, record.pool, record.model)
        from agentflow.routing import logical_route_id_for_record
        if logical_route_id_for_record(record) != route_id:
            raise RouteAdmissionRefused("mismatched")
        route = conn.execute(
            "SELECT 1 FROM safety_route_cells WHERE repository = ? AND stage = ?"
            " AND provider = ? AND route_id = ?",
            (record.repo, record.stage, record.pool, route_id)).fetchone()
        if route is None:
            raise RouteAdmissionRefused("missing")
        try:
            resolved = self._operational_safety.resolve(  # type: ignore[union-attr]
                *identity, route_id, conn=conn)
        except _SafetyIdentityMismatch:
            raise RouteAdmissionRefused("mismatched") from None
        except SafetyRefused:
            raise
        cell = resolved.route_cell
        if (cell.repository, cell.stage, cell.provider, cell.model, cell.route_id) != (
                *identity, route_id):
            raise RouteAdmissionRefused("mismatched")
        state = self._operational_safety.route_state(  # type: ignore[union-attr]
            resolved.route_cell.digest)
        if state.quarantined:
            raise RouteAdmissionRefused("quarantined")
        return decode_admitted_launch(resolved)

    def decode_committed_launch(self, route_cell_digest: str) -> AdmittedLaunch:
        """Decode one exact durable RouteCell version without requiring it to stay active."""
        if self._operational_safety is None:
            raise RouteAdmissionRefused("unreadable")
        with self._lock:
            try:
                return decode_admitted_launch(
                    self._operational_safety.resolve_digest(route_cell_digest, conn=self._conn))
            except _SafetyIdentityMismatch:
                raise RouteAdmissionRefused("mismatched") from None
            except _SafetyMissing:
                raise RouteAdmissionRefused("missing") from None
            except SafetyRefused:
                raise RouteAdmissionRefused("unreadable") from None
            except (sqlite3.DatabaseError, ValueError, TypeError):
                raise RouteAdmissionRefused("unreadable") from None

    def consume_admitted_launch(self, stage_identity: str, expected_revision: int,
                                route_id: str, *, reserve, before_reserve=None):
        """Reserve only after the selected active pointer is verified under SQLite write lock."""
        admitted = self.resolve_admitted_launch(
            stage_identity, expected_revision, route_id)
        if before_reserve is not None:
            before_reserve()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    current = self._resolve_active_admitted(
                        stage_identity, expected_revision, route_id, conn=self._conn)
                except RouteAdmissionRefused as error:
                    if error.code in {"missing", "stale", "mismatched", "quarantined"}:
                        raise RouteAdmissionRefused("stale") from None
                    raise
                except SafetyRefused:
                    raise RouteAdmissionRefused("unreadable") from None
                if current.route_cell.digest != admitted.route_cell.digest:
                    raise RouteAdmissionRefused("stale") from None
                result = reserve(admitted)
                self._conn.execute("COMMIT")
                return result
            except (json.JSONDecodeError, sqlite3.DatabaseError):
                self._rollback_quietly()
                raise RouteAdmissionRefused("unreadable") from None
            except BaseException:
                self._rollback_quietly()
                raise

    def route_selection_identity(self, selection) -> RouteSelectionIdentity:
        """Return current-routing-validated immutable digests without touching SQLite."""
        current = self._rematerialize_route_selection(selection)
        cell, _ = materialize_route_cell(
            current.repository, current.stage, current.provider, current.model,
            current.route_id, current.launch_config)
        return RouteSelectionIdentity(
            cell.route_id, cell.digest, cell.launch_config_digest)

    @staticmethod
    def _rematerialize_route_selection(selection):
        from agentflow.routing import (
            RouteSelection, RoutingConfigError, rematerialize_route_selection,
        )
        if type(selection) is not RouteSelection:
            raise TypeError("route registration requires the exact frozen selection")
        try:
            current = rematerialize_route_selection(selection)
        except RoutingConfigError as error:
            raise SafetyRefused("route selection is not admitted by current routing") from error
        if current != selection:
            raise SafetyRefused("route selection differs from current routing authority")
        return current

    def register_route_selection(self, selection):
        """Register one exact routing-owned selection through this Store's sealed owner."""
        if self._operational_safety is None:
            raise StoreUnavailable("route registration is not configured")
        current = self._rematerialize_route_selection(selection)
        return self._operational_safety.register_route_cell(
            current.repository, current.stage, current.provider, current.model,
            current.route_id, current.launch_config)

    def route_cell_state(self, route_cell_digest: str):
        """Read verified RouteCell state through this Store's sealed owner."""
        if self._operational_safety is None:
            raise StoreUnavailable("route state is not configured")
        return self._operational_safety.route_state(route_cell_digest)

    def read_canary_attribution(
            self, stage_identity: str) -> CanaryAttribution | None:
        """Read one committed attribution through this Store's sealed owner."""
        if self._canary_attribution is None:
            raise StoreUnavailable("canary attribution is not configured")
        return self._canary_attribution._read(stage_identity)

    def discard(self, expected: Record) -> bool:
        """Remove a never-started record from the ledger under a revision compare-and-set, freeing
        its identity so a later cold submission opens a genuinely fresh stage rather than colliding
        with a terminal tombstone (#251).

        The delete is guarded exactly like a reservation: the durable row must still be the
        never-started ``waiting`` record the caller loaded — same revision, no attempt consumed, no
        successful start fact, no live family. ``not_started`` is safe to discard: the guarded
        launcher proved that no provider family came into existence. A concurrent instance that
        already advanced this identity (a cycle that admitted it, a completed transfer) fails the
        compare-and-set, so genuine in-flight or completed work is never freed. Returns whether the
        row was removed.
        """
        self._refuse_promotion_receipt_callback_mutation()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT data FROM records WHERE identity = ?", (expected.identity,)).fetchone()
                if row is None:
                    self._conn.execute("ROLLBACK")
                    return False
                current = self._decode(row[0])
                if (current.revision != expected.revision or current.state != WAITING
                        or current.attempts != 0 or current.start_fact not in {None, NOT_STARTED}
                        or current.process_alive):
                    self._conn.execute("ROLLBACK")
                    return False
                self._conn.execute(
                    "DELETE FROM records WHERE identity = ?", (expected.identity,))
                self._conn.execute("COMMIT")
                return True
            except sqlite3.DatabaseError as e:
                self._rollback_quietly()
                raise StoreUnavailable(f"cannot discard continuation: {e}") from e

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
        self._refuse_promotion_receipt_callback_mutation()
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
        self._refuse_promotion_receipt_callback_mutation()
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
        self._refuse_promotion_receipt_callback_mutation()
        with self._lock:
            self._refuse_promotion_receipt_callback_mutation()
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

    def _retire_descendants(self, root: Record) -> None:
        """Retire a root's registered descendants inside the caller's transaction."""
        for identity in root.descendants:
            row = self._conn.execute(
                "SELECT data FROM records WHERE identity = ?", (identity,)).fetchone()
            if row is None:
                continue
            child = self._decode(row[0])
            if child.retired:
                continue
            child.state = COMPLETED
            child.retired = True
            child.claim = False
            child.revision += 1
            self._write(child)

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


def _schema_fingerprint(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    """Canonical SQL for every caller-owned schema object; SQLite internals are excluded."""
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' AND sql IS NOT NULL "
        "ORDER BY type, name"
    ).fetchall()
    return tuple((kind, name, table, re.sub(r"\s+", "", sql).lower())
                 for kind, name, table, sql in rows)


def _expected_schema_fingerprint(version: int) -> tuple[tuple[str, str, str, str], ...]:
    if version in (2, 3, 4):
        from agentflow.canary_attribution import (
            STORE_V2_SCHEMA_FINGERPRINT,
            STORE_V3_SCHEMA_FINGERPRINT,
        )
        if version == 2:
            return STORE_V2_SCHEMA_FINGERPRINT
        if version == 3:
            return STORE_V3_SCHEMA_FINGERPRINT
        return STORE_V4_SCHEMA_FINGERPRINT
    conn = sqlite3.connect(":memory:", isolation_level=None)
    try:
        conn.execute(_RECORDS_SCHEMA)
        return _schema_fingerprint(conn)
    finally:
        conn.close()
