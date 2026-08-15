"""Immutable, content-free canary reports derived from attribution and attempt telemetry."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import math
from pathlib import Path
import sqlite3
from time import time
from typing import Protocol

from agentflow.canary_attribution import (
    CANARY_ATTRIBUTION_CONTRACT_DIGEST,
    CanaryAttribution,
    CanaryAttributionRefused,
    validate_canary_attribution,
)
from agentflow.coordinator.errors import StoreUnavailable
from agentflow.coordinator.telemetry import read_attempts


REPORT_VERSION = "canary-report-v1"
REPORT_MANIFEST = (b'{"fields":["stage_identity","report_version","receipt_binding","method_revision","'
                   b'cohort_id","result","attempt_count","verified_count","terminal_failure_count",'
                   b'"duration_seconds","duration_missing","token_count","token_missing","cost_usd",'
                   b'"cost_missing","evidence_finalized_at","evidence_age_missing","committed_at"],'
                   b'"report_version":"canary-report-v1","results":["observation",'
                   b'"rollback_recommendation","block_recommendation"],"schema_version":1}')
REPORT_MANIFEST_DIGEST = "d80ad3d7e1819f09856d2421e25c4199d55016e2f2afb6b8be7ebdd63a81557b"
if sha256(REPORT_MANIFEST).hexdigest() != REPORT_MANIFEST_DIGEST:
    raise RuntimeError("canary report manifest changed")

_TABLE_SQL = """CREATE TABLE canary_reports (
  stage_identity TEXT NOT NULL,
  report_version TEXT NOT NULL CHECK (report_version = 'canary-report-v1'),
  receipt_binding TEXT NOT NULL,
  method_revision TEXT NOT NULL,
  cohort_id TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('observation', 'rollback_recommendation', 'block_recommendation')),
  attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
  verified_count INTEGER NOT NULL CHECK (verified_count >= 0 AND verified_count <= attempt_count),
  terminal_failure_count INTEGER NOT NULL CHECK (terminal_failure_count >= 0 AND terminal_failure_count <= attempt_count),
  duration_seconds INTEGER CHECK (duration_seconds >= 0),
  duration_missing INTEGER NOT NULL CHECK (duration_missing IN (0, 1)) CHECK ((duration_seconds IS NULL) = (duration_missing = 1)),
  token_count INTEGER CHECK (token_count >= 0),
  token_missing INTEGER NOT NULL CHECK (token_missing IN (0, 1)) CHECK ((token_count IS NULL) = (token_missing = 1)),
  cost_usd REAL CHECK (cost_usd >= 0),
  cost_missing INTEGER NOT NULL CHECK (cost_missing IN (0, 1)) CHECK ((cost_usd IS NULL) = (cost_missing = 1)),
  evidence_finalized_at INTEGER CHECK (evidence_finalized_at >= 0),
  evidence_age_missing INTEGER NOT NULL CHECK (evidence_age_missing IN (0, 1)) CHECK ((evidence_finalized_at IS NULL) = (evidence_age_missing = 1)),
  committed_at INTEGER NOT NULL CHECK (committed_at >= 0),
  PRIMARY KEY (stage_identity, report_version)
)"""
_UPDATE_SQL = "CREATE TRIGGER canary_reports_no_update BEFORE UPDATE ON canary_reports BEGIN SELECT RAISE(ABORT, 'canary_reports are immutable'); END"
_DELETE_SQL = "CREATE TRIGGER canary_reports_no_delete BEFORE DELETE ON canary_reports BEGIN SELECT RAISE(ABORT, 'canary_reports are immutable'); END"
_SCHEMA_SQL = (_TABLE_SQL, _UPDATE_SQL, _DELETE_SQL)
SCHEMA_FINGERPRINT = "72b9dfc4ac98d3ce17fa9a3d0db7c3af764686b2df21c08e65c93a927edfb91c"

CANARY_REPORT_REFUSAL_CODES = frozenset({
    "unsupported_report_version", "attribution_absent", "attribution_unavailable",
    "attribution_invalid", "telemetry_invalid", "report_store_unavailable",
})


class CanaryReportRefused(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in CANARY_REPORT_REFUSAL_CODES:
            raise ValueError("unknown canary report refusal code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CanaryAttemptFact:
    stage_identity: str
    attempt_token: str
    verified: bool
    cause: str
    classification: str
    started_at: int
    finalized_at: int
    token_count: int | None
    cost_usd: float | None


@dataclass(frozen=True, slots=True)
class CanaryAttemptProjection:
    stage_identity: str
    attempts: tuple[CanaryAttemptFact, ...]


class CanaryTelemetryReader(Protocol):
    def read(self, stage_identity: str) -> CanaryAttemptProjection: ...


@dataclass(frozen=True, slots=True)
class CanaryMeasures:
    attempt_count: int
    verified_count: int
    terminal_failure_count: int
    duration_seconds: int | None
    duration_missing: bool
    token_count: int | None
    token_missing: bool
    cost_usd: float | None
    cost_missing: bool
    evidence_finalized_at: int | None
    evidence_age_missing: bool


@dataclass(frozen=True, slots=True)
class CanaryReport:
    stage_identity: str
    report_version: str
    receipt_binding: str
    method_revision: str
    cohort_id: str
    result: str
    measures: CanaryMeasures
    committed_at: int


class AttemptTelemetryReader:
    """The single production adapter from durable attempt JSON to report facts."""

    def __init__(self, store_path: Path | str) -> None:
        self._store_path = Path(store_path)

    def read(self, stage_identity: str) -> CanaryAttemptProjection:
        facts = []
        for entry in read_attempts(self._store_path):
            fact = _attempt_fact(entry)
            if fact is not None and fact.stage_identity == stage_identity:
                facts.append(fact)
        return CanaryAttemptProjection(
            stage_identity, tuple(sorted(facts, key=lambda item: item.attempt_token)))


def _attempt_fact(entry: object) -> CanaryAttemptFact | None:
    try:
        identity, token, verified = entry.identity, entry.token, entry.verified
        cause, classification = entry.cause, entry.classification
        started, finalized, usage = entry.started_at, entry.finalized_at, entry.usage
    except AttributeError:
        return None
    if (not all(isinstance(item, str) for item in (identity, token, cause, classification))
            or type(verified) is not bool or any(type(item) is not int for item in (started, finalized))):
        return None
    try:
        tokens = [getattr(usage, key) for key in (
            "input_tokens", "cached_input_tokens", "cache_creation_tokens", "output_tokens",
            "reasoning_output_tokens") if getattr(usage, key) is not None]
        cost = usage.cost_usd
    except AttributeError:
        return None
    if any(type(item) is not int or item < 0 for item in tokens):
        return None
    if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int, float))
                             or not math.isfinite(cost) or cost < 0):
        return None
    return CanaryAttemptFact(identity, token, verified, cause, classification, started, finalized,
                             sum(tokens) if tokens else None, float(cost) if cost is not None else None)


class CanaryReporter:
    """Durably report one logical canary stage through a single immutable-row operation."""

    def __init__(self, store, *, telemetry: CanaryTelemetryReader | None = None,
                 report_store_path: Path | str | None = None, now=None) -> None:
        self._store = store
        store_path = Path(store.path)
        self._telemetry = telemetry or AttemptTelemetryReader(store_path)
        self._path = Path(report_store_path) if report_store_path is not None else store_path.parent / "canary-reports.db"
        self._now = now or time

    @staticmethod
    def _checkpoint(_name: str) -> None:
        """A test-only crash boundary around the final immutable-row commit."""

    def report(self, stage_identity: str, report_version: str) -> CanaryReport:
        if report_version != REPORT_VERSION:
            raise CanaryReportRefused("unsupported_report_version")
        try:
            if self._path.exists():
                conn = self._open()
                existing = _row(conn, stage_identity, report_version)
                if existing is not None:
                    return existing
        except (sqlite3.Error, OSError):
            raise CanaryReportRefused("report_store_unavailable") from None
        finally:
            if 'conn' in locals():
                conn.close()
        attribution = self._attribution(stage_identity)
        try:
            projection = self._telemetry.read(stage_identity)
            measures = _measures(projection, stage_identity)
        except CanaryReportRefused:
            raise
        except Exception:
            raise CanaryReportRefused("telemetry_invalid") from None
        result = ("observation" if measures.verified_count else "rollback_recommendation"
                  if measures.terminal_failure_count else "block_recommendation")
        candidate = CanaryReport(stage_identity, report_version, attribution.receipt_binding,
                                 attribution.method_revision, attribution.cohort_id, result, measures,
                                 int(self._now()))
        try:
            conn = self._open()
            conn.execute("BEGIN IMMEDIATE")
            existing = _row(conn, stage_identity, report_version)
            if existing is None:
                conn.execute("INSERT INTO canary_reports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             _values(candidate))
                self._checkpoint("before-commit")
                conn.execute("COMMIT")
                self._checkpoint("after-commit")
                return candidate
            conn.execute("ROLLBACK")
            return existing
        except (sqlite3.Error, OSError):
            if 'conn' in locals():
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise CanaryReportRefused("report_store_unavailable") from None
        finally:
            if 'conn' in locals():
                conn.close()

    def _attribution(self, stage_identity: str) -> CanaryAttribution:
        if CANARY_ATTRIBUTION_CONTRACT_DIGEST != "f7f64e3fb9a3913713d121d24af39c3f208d39b3cb6afb04b1457dd54b8d0d2f":
            raise CanaryReportRefused("attribution_invalid")
        try:
            value = self._store.read_canary_attribution(stage_identity)
        except StoreUnavailable:
            raise CanaryReportRefused("attribution_unavailable") from None
        except CanaryAttributionRefused:
            raise CanaryReportRefused("attribution_invalid") from None
        if value is None:
            raise CanaryReportRefused("attribution_absent")
        try:
            value = validate_canary_attribution(value)
        except CanaryAttributionRefused:
            raise CanaryReportRefused("attribution_invalid") from None
        if value.stage_identity != stage_identity:
            raise CanaryReportRefused("attribution_invalid")
        return value

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, isolation_level=None)
        try:
            conn.execute("BEGIN IMMEDIATE")
            objects = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchone()[0]
            if objects == 0:
                for statement in _SCHEMA_SQL:
                    conn.execute(statement)
                conn.execute("PRAGMA user_version = 1")
                conn.execute("COMMIT")
            else:
                conn.execute("ROLLBACK")
            _check_schema(conn)
            return conn
        except BaseException:
            conn.close()
            raise


def _check_schema(conn: sqlite3.Connection) -> None:
    if conn.execute("PRAGMA user_version").fetchone() != (1,):
        raise sqlite3.DatabaseError("report store version")
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY CASE name WHEN 'canary_reports' THEN 0 WHEN 'canary_reports_no_update' THEN 1 WHEN 'canary_reports_no_delete' THEN 2 END").fetchall()
    if [(row[0], row[1], row[2]) for row in rows] != [
            ("table", "canary_reports", "canary_reports"),
            ("trigger", "canary_reports_no_update", "canary_reports"),
            ("trigger", "canary_reports_no_delete", "canary_reports")]:
        raise sqlite3.DatabaseError("report store objects")
    source = "\n".join(row[3] for row in rows)
    if tuple(row[3] for row in rows) != _SCHEMA_SQL or sha256(source.encode()).hexdigest() != SCHEMA_FINGERPRINT:
        raise sqlite3.DatabaseError("report store schema")


def _measures(projection: CanaryAttemptProjection, stage_identity: str) -> CanaryMeasures:
    if type(projection) is not CanaryAttemptProjection or projection.stage_identity != stage_identity:
        raise CanaryReportRefused("telemetry_invalid")
    attempts = projection.attempts
    if not isinstance(attempts, tuple) or tuple(sorted(attempts, key=lambda item: item.attempt_token)) != attempts:
        raise CanaryReportRefused("telemetry_invalid")
    tokens = set()
    for item in attempts:
        if (type(item) is not CanaryAttemptFact or item.stage_identity != stage_identity
                or not isinstance(item.attempt_token, str) or not item.attempt_token or item.attempt_token in tokens
                or type(item.verified) is not bool or not isinstance(item.cause, str)
                or not isinstance(item.classification, str) or type(item.started_at) is not int
                or type(item.finalized_at) is not int or item.started_at < 0 or item.started_at > item.finalized_at
                or (item.token_count is not None and (type(item.token_count) is not int or item.token_count < 0))
                or (item.cost_usd is not None and (isinstance(item.cost_usd, bool) or not isinstance(item.cost_usd, (int, float))
                                                   or not math.isfinite(item.cost_usd) or item.cost_usd < 0))):
            raise CanaryReportRefused("telemetry_invalid")
        tokens.add(item.attempt_token)
    if not attempts:
        return CanaryMeasures(0, 0, 0, None, True, None, True, None, True, None, True)
    verified = sum(item.verified for item in attempts)
    terminal = sum(not item.verified and item.cause == "permanent" and item.classification == "permanent"
                   for item in attempts)
    token_count = sum(item.token_count for item in attempts) if all(item.token_count is not None for item in attempts) else None
    cost = sum(item.cost_usd for item in attempts) if all(item.cost_usd is not None for item in attempts) else None
    return CanaryMeasures(len(attempts), verified, terminal,
                          sum(item.finalized_at - item.started_at for item in attempts), False,
                          token_count, token_count is None, cost, cost is None,
                          max(item.finalized_at for item in attempts), False)


def _values(report: CanaryReport) -> tuple:
    measures = report.measures
    return (report.stage_identity, report.report_version, report.receipt_binding, report.method_revision,
            report.cohort_id, report.result, measures.attempt_count, measures.verified_count,
            measures.terminal_failure_count, measures.duration_seconds, int(measures.duration_missing),
            measures.token_count, int(measures.token_missing), measures.cost_usd, int(measures.cost_missing),
            measures.evidence_finalized_at, int(measures.evidence_age_missing), report.committed_at)


def _row(conn: sqlite3.Connection, stage_identity: str, report_version: str) -> CanaryReport | None:
    row = conn.execute("SELECT * FROM canary_reports WHERE stage_identity = ? AND report_version = ?",
                       (stage_identity, report_version)).fetchone()
    if row is None:
        return None
    measures = CanaryMeasures(row[6], row[7], row[8], row[9], bool(row[10]), row[11], bool(row[12]),
                              row[13], bool(row[14]), row[15], bool(row[16]))
    return CanaryReport(*row[:6], measures, row[17])
