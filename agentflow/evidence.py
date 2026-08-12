"""Redacted, provider-neutral evidence behind one governed interface.

The database is deliberately private.  Callers use :class:`EvidenceStore` and
its five verbs; a later GitHub adapter can satisfy ``AuthorityVerifier`` without
changing the durable evidence model.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
import re
import sqlite3

from agentflow.state import state_path

SCHEMA_VERSION = 2
FAILURE_CLASSES = frozenset({"original_defect", "plan_gap", "slice_scope_error",
                             "reviewer_false_claim", "speculative_preference",
                             "fix_introduced_defect"})
VALIDATION_STATES = frozenset({"observed", "reproduced", "refuted", "model_judged",
                               "human_validated", "unvalidated"})
_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{32,128}$")
_SHA = re.compile(r"^[a-f0-9]{40,64}$")
_CONTENT_REVISION = re.compile(r"^sha256:([a-f0-9]{64})$")

_V1_SCHEMA = """
CREATE TABLE events (event_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
  subject TEXT NOT NULL, revision TEXT NOT NULL, failure_class TEXT NOT NULL,
  signature TEXT NOT NULL, normalizer TEXT NOT NULL,
  UNIQUE(repository,subject,revision,failure_class,signature,normalizer));
CREATE TABLE observations (observation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
  source_kind TEXT NOT NULL, source_repository TEXT NOT NULL, source_locator TEXT NOT NULL,
  source_revision TEXT NOT NULL, source_hash_algorithm TEXT NOT NULL, source_hash TEXT NOT NULL,
  source_scope TEXT NOT NULL, validation_state TEXT NOT NULL, observed_at INTEGER NOT NULL,
  parent_revision TEXT NOT NULL, fixer_revision TEXT NOT NULL);
CREATE TABLE evaluations (evaluation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
  validation_state TEXT NOT NULL, evaluated_at INTEGER NOT NULL);
CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY, proposal_digest TEXT NOT NULL,
  policy_version INTEGER NOT NULL, nominated_at INTEGER NOT NULL);
CREATE TABLE candidate_events (candidate_id TEXT NOT NULL, event_id TEXT NOT NULL,
  PRIMARY KEY(candidate_id,event_id));
CREATE TABLE receipts (candidate_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL,
  approval_id TEXT NOT NULL, policy_version INTEGER NOT NULL, promoted_at INTEGER NOT NULL);
"""

_V2_SCHEMA = """
CREATE TABLE events (event_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
  subject TEXT NOT NULL, revision TEXT NOT NULL, failure_class TEXT NOT NULL,
  signature TEXT NOT NULL, normalizer TEXT NOT NULL,
  UNIQUE(repository,subject,revision,failure_class,signature,normalizer));
CREATE TABLE observations (observation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
  source_kind TEXT NOT NULL, source_repository TEXT NOT NULL, source_locator TEXT NOT NULL,
  source_revision TEXT NOT NULL, source_hash_algorithm TEXT NOT NULL, source_hash TEXT NOT NULL,
  source_scope TEXT NOT NULL, validation_state TEXT NOT NULL, observed_at INTEGER NOT NULL,
  parent_revision TEXT NOT NULL, fixer_revision TEXT NOT NULL,
  FOREIGN KEY(event_id) REFERENCES events(event_id));
CREATE TABLE evaluations (evaluation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
  validation_state TEXT NOT NULL, evaluated_at INTEGER NOT NULL,
  FOREIGN KEY(event_id) REFERENCES events(event_id));
CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY, proposal_digest TEXT NOT NULL,
  policy_version INTEGER NOT NULL, nominated_at INTEGER NOT NULL);
CREATE TABLE candidate_events (candidate_id TEXT NOT NULL, event_id TEXT NOT NULL,
  PRIMARY KEY(candidate_id,event_id),
  FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id),
  FOREIGN KEY(event_id) REFERENCES events(event_id));
CREATE TABLE receipts (candidate_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL UNIQUE,
  approval_id TEXT NOT NULL, policy_version INTEGER NOT NULL, promoted_at INTEGER NOT NULL,
  binding_status TEXT NOT NULL CHECK(binding_status IN ('verified', 'legacy_unverifiable')),
  authority_kind TEXT, authority_repository TEXT, authority_locator TEXT,
  authority_revision TEXT, authority_hash_algorithm TEXT, authority_hash TEXT,
  authority_scope TEXT, verifier_id TEXT, verifier_version TEXT, verifier_outcome TEXT,
  approved_revision TEXT, approved_hash TEXT, approved_scope TEXT,
  FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id));
CREATE INDEX observations_by_event ON observations(event_id);
CREATE INDEX evaluations_by_event ON evaluations(event_id);
CREATE INDEX candidate_events_by_event ON candidate_events(event_id);
"""


class EvidenceError(ValueError):
    """Evidence input or durable state was rejected fail-closed."""


def _token(value: str, name: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value) or "?" in value or "#" in value:
        raise EvidenceError(f"invalid {name}")


def _digest(value: str, name: str) -> None:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise EvidenceError(f"invalid {name}")


def _locator(value: str, name: str) -> None:
    _token(value, name)
    if "/" not in value:
        raise EvidenceError(f"invalid {name}")


def _schema_fingerprint(conn: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                        "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
                        "ORDER BY type, name").fetchall()
    return tuple((row[0], row[1], row[2], re.sub(r"\s+", "", row[3] or "").lower()) for row in rows)


def _schema_fingerprint_for(schema: str) -> tuple[tuple[str, str, str, str], ...]:
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(schema)
        return _schema_fingerprint(conn)
    finally:
        conn.close()


def _execute_schema(conn: sqlite3.Connection, schema: str) -> None:
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)


@dataclass(frozen=True)
class AuthorityPointer:
    authority_kind: str
    repository: str
    locator: str
    revision: str
    content_hash_algorithm: str
    content_hash: str
    scope: str

    def __post_init__(self) -> None:
        for name in ("authority_kind", "repository", "revision", "content_hash_algorithm", "scope"):
            _token(getattr(self, name), name)
        _locator(self.locator, "locator")
        _digest(self.content_hash, "content_hash")
        if self.authority_kind == "github":
            immutable = _SHA.fullmatch(self.revision)
        elif self.authority_kind == "repository":
            match = _CONTENT_REVISION.fullmatch(self.revision)
            immutable = match is not None and match.group(1) == self.content_hash
        else:
            immutable = False
        if not immutable:
            raise EvidenceError("invalid immutable revision for authority_kind")


@dataclass(frozen=True)
class ApprovedAuthority:
    pointer: AuthorityPointer
    approval_id: str
    approved_revision: str
    approved_hash: str
    approved_scope: str
    verifier_id: str
    verifier_version: str
    outcome: str

    def __post_init__(self) -> None:
        for name in ("approval_id", "approved_revision", "approved_scope", "verifier_id",
                     "verifier_version"):
            _token(getattr(self, name), name)
        _digest(self.approved_hash, "approved_hash")
        if self.outcome != "verified":
            raise EvidenceError("authority outcome must be verified")
        if (self.approved_revision != self.pointer.revision or
                self.approved_hash != self.pointer.content_hash or
                self.approved_scope != self.pointer.scope):
            raise EvidenceError("approval does not bind the exact authority pointer")


class AuthorityVerifier(Protocol):
    def verify(self, authority: AuthorityPointer) -> ApprovedAuthority | None: ...


class FakeAuthorityVerifier:
    """Public-test adapter; production authority verification belongs to #584."""
    def __init__(self, approvals: tuple[ApprovedAuthority, ...] = ()) -> None:
        self._approvals = {approval.pointer: approval for approval in approvals}

    def verify(self, authority: AuthorityPointer) -> ApprovedAuthority | None:
        return self._approvals.get(authority)


@dataclass(frozen=True)
class SubjectRevision:
    subject_kind: str
    subject: str
    revision: str
    locator: str = ""
    content_digest: str = ""

    def __post_init__(self) -> None:
        if self.subject_kind not in {"review", "issue", "document"}:
            raise EvidenceError("invalid subject_kind")
        _token(self.subject, "subject")
        if self.subject_kind == "review":
            if not _SHA.fullmatch(self.revision):
                raise EvidenceError("review evidence requires exact reviewed SHA")
            if self.locator or self.content_digest:
                raise EvidenceError("review subject revision has no content locator")
        else:
            _locator(self.locator, "locator")
            _digest(self.content_digest, "content_digest")
            _token(self.revision, "revision")


@dataclass(frozen=True)
class Observation:
    observation_id: str
    subject: SubjectRevision
    failure_class: str
    validation_state: str
    signature_digest: str
    normalizer_version: str
    source: AuthorityPointer
    observed_at: int
    reviewed_parent_revision: str = ""
    fixer_revision: str = ""

    def __post_init__(self) -> None:
        _token(self.observation_id, "observation_id")
        if self.failure_class not in FAILURE_CLASSES:
            raise EvidenceError("invalid failure_class")
        if self.validation_state not in VALIDATION_STATES:
            raise EvidenceError("invalid validation_state")
        _digest(self.signature_digest, "signature_digest")
        _token(self.normalizer_version, "normalizer_version")
        if not isinstance(self.observed_at, int) or self.observed_at < 0:
            raise EvidenceError("invalid observed_at")
        for name in ("reviewed_parent_revision", "fixer_revision"):
            value = getattr(self, name)
            if value and not _SHA.fullmatch(value):
                raise EvidenceError(f"invalid {name}")
        if self.failure_class != "fix_introduced_defect" and (
                self.reviewed_parent_revision or self.fixer_revision):
            raise EvidenceError("only fix-introduced defects carry fixer lineage")


@dataclass(frozen=True)
class Event:
    event_id: str
    recurrence_count: int
    observation_ids: tuple[str, ...]


@dataclass(frozen=True)
class Evaluation:
    evaluation_id: str
    event_id: str
    validation_state: str
    evaluated_at: int

    def __post_init__(self) -> None:
        _token(self.evaluation_id, "evaluation_id")
        _token(self.event_id, "event_id")
        if self.validation_state not in VALIDATION_STATES:
            raise EvidenceError("invalid validation_state")
        if not isinstance(self.evaluated_at, int) or self.evaluated_at < 0:
            raise EvidenceError("invalid evaluated_at")


@dataclass(frozen=True)
class LessonCandidate:
    candidate_id: str
    event_ids: tuple[str, ...]
    proposal_digest: str
    policy_version: int
    nominated_at: int

    def __post_init__(self) -> None:
        _token(self.candidate_id, "candidate_id")
        if not 1 <= len(self.event_ids) <= 32:
            raise EvidenceError("candidate needs bounded event references")
        for event_id in self.event_ids:
            _token(event_id, "event_id")
        if len(set(self.event_ids)) != len(self.event_ids):
            raise EvidenceError("candidate event references must be unique")
        object.__setattr__(self, "event_ids", tuple(sorted(self.event_ids)))
        _digest(self.proposal_digest, "proposal_digest")
        if not isinstance(self.policy_version, int) or self.policy_version < 1:
            raise EvidenceError("invalid policy_version")
        if not isinstance(self.nominated_at, int) or self.nominated_at < 0:
            raise EvidenceError("invalid nominated_at")


@dataclass(frozen=True)
class PromotionReceipt:
    receipt_id: str
    candidate_id: str
    approval_id: str
    policy_version: int
    authority: ApprovedAuthority | None
    authoritative: bool


class EvidenceStore:
    """The sole caller-facing Evidence interface; its schema is fail-closed."""
    def __init__(self, *, path: Path | None = None, verifier: AuthorityVerifier | None = None) -> None:
        self.path = path or state_path("evidence", "evidence.db")
        self.verifier = verifier or FakeAuthorityVerifier()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn: self._initialize(conn)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _initialize(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1, SCHEMA_VERSION):
            raise EvidenceError("unsupported evidence schema version")
        existing = _schema_fingerprint(conn)
        v1 = _schema_fingerprint_for(_V1_SCHEMA)
        v2 = _schema_fingerprint_for(_V2_SCHEMA)
        if version == 0 and existing:
            raise EvidenceError("unversioned evidence database is not safe to open")
        if version == 1 and existing != v1:
            raise EvidenceError("evidence v1 schema does not match the known migration source")
        if version == SCHEMA_VERSION and existing != v2:
            raise EvidenceError("evidence schema does not match its version")
        if version == 0:
            _execute_schema(conn, _V2_SCHEMA)
            if _schema_fingerprint(conn) != v2:
                raise EvidenceError("evidence schema did not initialize exactly")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version == 1:
            EvidenceStore._migrate_v1_to_v2(conn, v2)

    @staticmethod
    def _migration_checkpoint() -> None:
        """Private test seam for proving the migration transaction rolls back."""

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection, expected: tuple[tuple[str, str, str, str], ...]) -> None:
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("events", "observations", "evaluations", "candidates", "candidate_events", "receipts"):
                conn.execute(f"ALTER TABLE {table} RENAME TO v1_{table}")
            _execute_schema(conn, _V2_SCHEMA)
            for table in ("events", "observations", "evaluations", "candidates", "candidate_events"):
                conn.execute(f"INSERT INTO {table} SELECT * FROM v1_{table}")
            conn.execute("""INSERT INTO receipts (candidate_id, receipt_id, approval_id, policy_version,
                promoted_at, binding_status) SELECT candidate_id, receipt_id, approval_id, policy_version,
                promoted_at, 'legacy_unverifiable' FROM v1_receipts""")
            EvidenceStore._migration_checkpoint()
            for table in ("events", "observations", "evaluations", "candidates", "candidate_events", "receipts"):
                conn.execute(f"DROP TABLE v1_{table}")
            if _schema_fingerprint(conn) != expected:
                raise EvidenceError("evidence migration did not produce v2 exactly")
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    @staticmethod
    def _event_id(observation: Observation) -> str:
        import hashlib
        parts = (observation.source.repository, observation.subject.subject, observation.subject.revision,
                 observation.failure_class, observation.signature_digest, observation.normalizer_version)
        return "event-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]

    def observe(self, observation: Observation) -> Event:
        event_id = self._event_id(observation)
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)", (event_id, observation.source.repository,
                observation.subject.subject, observation.subject.revision, observation.failure_class,
                observation.signature_digest, observation.normalizer_version))
            existing = conn.execute("SELECT * FROM observations WHERE observation_id=?", (observation.observation_id,)).fetchone()
            immutable = (event_id, observation.source.authority_kind, observation.source.repository,
                         observation.source.locator, observation.source.revision,
                         observation.source.content_hash_algorithm, observation.source.content_hash,
                         observation.source.scope, observation.validation_state, observation.observed_at,
                         observation.reviewed_parent_revision, observation.fixer_revision)
            if existing is not None and tuple(existing)[1:] != immutable:
                raise EvidenceError("observation_id already names a different immutable observation")
            conn.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (observation.observation_id,
                event_id, observation.source.authority_kind, observation.source.repository, observation.source.locator,
                observation.source.revision, observation.source.content_hash_algorithm, observation.source.content_hash,
                observation.source.scope, observation.validation_state, observation.observed_at,
                observation.reviewed_parent_revision, observation.fixer_revision))
            return self._event(conn, event_id)

    def _event(self, conn: sqlite3.Connection, event_id: str) -> Event:
        row = conn.execute("SELECT event_id FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None: raise EvidenceError("unknown event")
        ids = tuple(row[0] for row in conn.execute("SELECT observation_id FROM observations WHERE event_id=? ORDER BY observation_id", (event_id,)))
        return Event(event_id, 1, ids)

    def evaluate(self, evaluation: Evaluation) -> Evaluation:
        with self._connect() as conn:
            self._event(conn, evaluation.event_id)
            existing = conn.execute("SELECT event_id, validation_state, evaluated_at FROM evaluations "
                                    "WHERE evaluation_id=?", (evaluation.evaluation_id,)).fetchone()
            facts = (evaluation.event_id, evaluation.validation_state, evaluation.evaluated_at)
            if existing is not None:
                if tuple(existing) != facts:
                    raise EvidenceError("evaluation_id already names different immutable facts")
                return Evaluation(evaluation.evaluation_id, *existing)
            conn.execute("INSERT INTO evaluations VALUES (?,?,?,?)", (evaluation.evaluation_id, *facts))
        return evaluation

    def nominate(self, candidate: LessonCandidate) -> LessonCandidate:
        with self._connect() as conn:
            for event_id in candidate.event_ids:
                self._event(conn, event_id)
            existing = conn.execute("SELECT proposal_digest, policy_version, nominated_at FROM candidates "
                                    "WHERE candidate_id=?", (candidate.candidate_id,)).fetchone()
            facts = (candidate.proposal_digest, candidate.policy_version, candidate.nominated_at)
            if existing is not None:
                relations = tuple(row[0] for row in conn.execute(
                    "SELECT event_id FROM candidate_events WHERE candidate_id=? ORDER BY event_id",
                    (candidate.candidate_id,)))
                if tuple(existing) != facts or relations != candidate.event_ids:
                    raise EvidenceError("candidate_id already names different immutable facts or relations")
                return LessonCandidate(candidate.candidate_id, relations, *existing)
            conn.execute("INSERT INTO candidates VALUES (?,?,?,?)", (candidate.candidate_id, *facts))
            for event_id in candidate.event_ids:
                conn.execute("INSERT INTO candidate_events VALUES (?,?)", (candidate.candidate_id, event_id))
        return candidate

    def promote(self, candidate_id: str, authority: AuthorityPointer, *, promoted_at: int) -> PromotionReceipt:
        _token(candidate_id, "candidate_id")
        approved = self.verifier.verify(authority)
        if approved is None: raise EvidenceError("authority was not verified")
        if not isinstance(approved, ApprovedAuthority):
            raise EvidenceError("verifier returned an invalid authority approval")
        if (approved.pointer != authority or approved.approved_revision != authority.revision or
                approved.approved_hash != authority.content_hash or
                approved.approved_scope != authority.scope):
            raise EvidenceError("verifier did not approve the requested authority")
        if not isinstance(promoted_at, int) or promoted_at < 0: raise EvidenceError("invalid promoted_at")
        with self._connect() as conn:
            candidate = conn.execute("SELECT policy_version FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone()
            if candidate is None: raise EvidenceError("unknown candidate")
            prior = conn.execute("SELECT * FROM receipts WHERE candidate_id=?", (candidate_id,)).fetchone()
            if prior:
                receipt = self._receipt(prior)
                if not receipt.authoritative:
                    raise EvidenceError("legacy receipt is unverifiable and cannot be promotion-active")
                if receipt.authority.pointer != authority:
                    raise EvidenceError("candidate was promoted under a different authority")
                return receipt
            receipt_id = f"receipt-{candidate_id}"
            conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                candidate_id, receipt_id, approved.approval_id, candidate[0], promoted_at,
                "verified", authority.authority_kind, authority.repository, authority.locator, authority.revision,
                authority.content_hash_algorithm, authority.content_hash, authority.scope,
                approved.verifier_id, approved.verifier_version, approved.outcome,
                approved.approved_revision, approved.approved_hash, approved.approved_scope))
            return PromotionReceipt(receipt_id, candidate_id, approved.approval_id, candidate[0], approved, True)

    @staticmethod
    def _receipt(row: sqlite3.Row) -> PromotionReceipt:
        if row["binding_status"] == "legacy_unverifiable":
            return PromotionReceipt(row["receipt_id"], row["candidate_id"], row["approval_id"],
                                    row["policy_version"], None, False)
        pointer = AuthorityPointer(row["authority_kind"], row["authority_repository"],
            row["authority_locator"], row["authority_revision"], row["authority_hash_algorithm"],
            row["authority_hash"], row["authority_scope"])
        approved = ApprovedAuthority(pointer, row["approval_id"], row["approved_revision"],
            row["approved_hash"], row["approved_scope"], row["verifier_id"],
            row["verifier_version"], row["verifier_outcome"])
        return PromotionReceipt(row["receipt_id"], row["candidate_id"], row["approval_id"],
                                row["policy_version"], approved, True)

    def brief_for(self, subject: str, *, now: int, effective_policy_versions: tuple[int, ...] = ()) -> tuple[Event, ...]:
        _token(subject, "subject")
        if not isinstance(now, int) or now < 0: raise EvidenceError("invalid now")
        self._expire(now, frozenset(effective_policy_versions))
        with self._connect() as conn:
            rows = conn.execute("SELECT event_id FROM events WHERE subject=? ORDER BY event_id", (subject,)).fetchall()
            return tuple(self._event(conn, row[0]) for row in rows)

    def _expire(self, now: int, effective_versions: frozenset[int]) -> None:
        """Private retention work performed as part of briefing, never a caller verb."""
        cutoff = now - 90 * 24 * 60 * 60
        with self._connect() as conn:
            # A promoted candidate is pinned only by an effective policy version and its successor.
            pinned = tuple(sorted(v for v in effective_versions for v in (v - 1, v) if v >= 1))
            marks = ",".join("?" for _ in pinned) or "NULL"
            stale = conn.execute(f"SELECT candidate_id FROM candidates WHERE nominated_at<? AND (candidate_id NOT IN (SELECT candidate_id FROM receipts) OR policy_version NOT IN ({marks}))", (cutoff, *pinned)).fetchall()
            for row in stale:
                cid = row[0]
                conn.execute("DELETE FROM candidate_events WHERE candidate_id=?", (cid,)); conn.execute("DELETE FROM receipts WHERE candidate_id=?", (cid,)); conn.execute("DELETE FROM candidates WHERE candidate_id=?", (cid,))
            conn.execute("DELETE FROM evaluations WHERE event_id NOT IN (SELECT event_id FROM candidate_events) AND evaluated_at<?", (cutoff,))
            conn.execute("DELETE FROM observations WHERE event_id NOT IN (SELECT event_id FROM candidate_events) AND observed_at<?", (cutoff,))
            conn.execute("DELETE FROM events WHERE event_id NOT IN (SELECT event_id FROM observations) AND event_id NOT IN (SELECT event_id FROM candidate_events)")
