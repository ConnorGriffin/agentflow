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

SCHEMA_VERSION = 3
FAILURE_CLASSES = frozenset({"original_defect", "plan_gap", "slice_scope_error",
                             "reviewer_false_claim", "speculative_preference",
                             "fix_introduced_defect"})
VALIDATION_STATES = frozenset({"observed", "reproduced", "refuted", "model_judged",
                               "human_validated", "unvalidated"})
ALL_VALIDATION_STATES = (
    "human_validated", "model_judged", "observed",
    "refuted", "reproduced", "unvalidated",
)
PRODUCER_KINDS = frozenset({"claim", "criterion", "decision", "decline", "delegation",
                            "disposition", "finding", "fix", "objection", "review_action",
                            "revision", "settlement", "slice", "verification", "verdict"})
REVIEW_ACTIONS = frozenset({"ask_maintainer", "discard_preference", "fix_before_completion",
                            "necessary_follow_up"})
LINEAGE_RELATIONS = frozenset({"addresses", "delegates", "derives_from", "governs",
                               "implements", "refutes", "revises", "settles", "verifies"})
_LINEAGE_MATRIX = {
    "derives_from": (
        PRODUCER_KINDS,
        PRODUCER_KINDS | {"failure_observation"},
    ),
    "governs": (
        frozenset({"decision", "disposition", "verdict"}),
        frozenset({"claim", "criterion", "delegation", "slice", "finding",
                   "review_action", "fix", "verification"}),
    ),
    "addresses": (
        frozenset({"finding", "review_action", "fix"}),
        frozenset({"failure_observation", "finding", "objection"}),
    ),
    "delegates": (
        frozenset({"delegation", "slice"}),
        frozenset({"claim", "criterion", "decision", "delegation"}),
    ),
    "implements": (
        frozenset({"revision", "fix"}),
        frozenset({"criterion", "decision", "finding", "review_action"}),
    ),
    "verifies": (
        frozenset({"verification", "verdict"}),
        frozenset({"claim", "criterion", "decision", "finding", "fix", "verification"}),
    ),
    "refutes": (
        frozenset({"verification", "verdict"}),
        frozenset({"claim", "criterion", "decision", "finding", "fix", "verification"}),
    ),
    "revises": (
        frozenset({"revision", "decision", "disposition", "objection", "fix"}),
        frozenset({"claim", "criterion", "decision", "disposition", "objection",
                   "revision", "finding", "fix"}),
    ),
    "settles": (
        frozenset({"settlement"}),
        frozenset({"claim", "decision", "disposition", "verdict", "fix", "verification"}),
    ),
}
_REQUIRED_RELATION = {"fix": "addresses", "settlement": "settles",
                      "delegation": "delegates", "slice": "derives_from"}
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

_V3_SCHEMA = """
CREATE TABLE events (event_id TEXT PRIMARY KEY, event_kind TEXT NOT NULL,
  repository TEXT NOT NULL, subject_kind TEXT NOT NULL, subject TEXT NOT NULL,
  revision TEXT NOT NULL, locator TEXT NOT NULL, content_digest TEXT NOT NULL,
  failure_class TEXT NOT NULL, producer_kind TEXT NOT NULL, fact_digest TEXT NOT NULL,
  normalizer TEXT NOT NULL, review_action TEXT NOT NULL,
  CHECK(event_kind IN ('failure_observation','producer_fact')),
  CHECK((event_kind='failure_observation' AND failure_class<>'' AND producer_kind='' AND review_action='')
     OR (event_kind='producer_fact' AND failure_class='' AND producer_kind<>'')));
CREATE TABLE observations (observation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL,
  source_kind TEXT NOT NULL, source_repository TEXT NOT NULL, source_locator TEXT NOT NULL,
  source_revision TEXT NOT NULL, source_hash_algorithm TEXT NOT NULL, source_hash TEXT NOT NULL,
  source_scope TEXT NOT NULL, validation_state TEXT NOT NULL, observed_at INTEGER NOT NULL,
  parent_revision TEXT NOT NULL, fixer_revision TEXT NOT NULL,
  subject_kind TEXT NOT NULL, subject_locator TEXT NOT NULL, subject_content_digest TEXT NOT NULL,
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
  binding_status TEXT NOT NULL CHECK(binding_status IN ('verified','legacy_unverifiable')),
  authority_kind TEXT, authority_repository TEXT, authority_locator TEXT,
  authority_revision TEXT, authority_hash_algorithm TEXT, authority_hash TEXT,
  authority_scope TEXT, verifier_id TEXT, verifier_version TEXT, verifier_outcome TEXT,
  approved_revision TEXT, approved_hash TEXT, approved_scope TEXT,
  FOREIGN KEY(candidate_id) REFERENCES candidates(candidate_id));
CREATE TABLE event_links (source_event_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  relation TEXT NOT NULL, target_event_id TEXT NOT NULL,
  PRIMARY KEY(source_event_id,ordinal), UNIQUE(source_event_id,relation,target_event_id),
  CHECK(ordinal>=0 AND ordinal<=31),
  FOREIGN KEY(source_event_id) REFERENCES events(event_id) ON DELETE CASCADE,
  FOREIGN KEY(target_event_id) REFERENCES events(event_id) ON DELETE RESTRICT);
CREATE UNIQUE INDEX events_failure_identity ON events(
  repository,subject,revision,failure_class,fact_digest,normalizer)
  WHERE event_kind='failure_observation';
CREATE INDEX observations_by_event ON observations(event_id);
CREATE INDEX evaluations_by_event ON evaluations(event_id);
CREATE INDEX candidate_events_by_event ON candidate_events(event_id);
CREATE INDEX event_links_by_target ON event_links(target_event_id);
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
class EvidenceLink:
    relation: str
    target_event_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if self.relation not in LINEAGE_RELATIONS:
            raise EvidenceError("invalid lineage relation")
        _token(self.target_event_id, "target_event_id")
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int) or not 0 <= self.ordinal <= 31:
            raise EvidenceError("invalid link ordinal")


@dataclass(frozen=True)
class FailureFacts:
    failure_class: str
    validation_state: str
    signature_digest: str
    normalizer_version: str
    reviewed_parent_revision: str | None = None
    fixer_revision: str | None = None

    def __post_init__(self) -> None:
        if self.failure_class not in FAILURE_CLASSES:
            raise EvidenceError("invalid failure_class")
        if self.validation_state not in VALIDATION_STATES:
            raise EvidenceError("invalid validation_state")
        _digest(self.signature_digest, "signature_digest")
        _token(self.normalizer_version, "normalizer_version")
        revisions = (self.reviewed_parent_revision, self.fixer_revision)
        if self.failure_class == "fix_introduced_defect":
            if not all(isinstance(value, str) and _SHA.fullmatch(value) for value in revisions):
                raise EvidenceError("fix-introduced defect requires both fixer revisions")
        elif any(value is not None for value in revisions):
            raise EvidenceError("only fix-introduced defects carry fixer lineage")


@dataclass(frozen=True)
class ProducerFacts:
    producer_kind: str
    fact_digest: str
    normalizer_version: str
    validation_state: str
    review_action: str | None = None

    def __post_init__(self) -> None:
        if self.producer_kind not in PRODUCER_KINDS:
            raise EvidenceError("invalid producer_kind")
        _digest(self.fact_digest, "fact_digest")
        _token(self.normalizer_version, "normalizer_version")
        if self.validation_state not in VALIDATION_STATES:
            raise EvidenceError("invalid validation_state")
        if self.producer_kind == "review_action":
            if self.review_action not in REVIEW_ACTIONS:
                raise EvidenceError("review_action producer requires a review action")
        elif self.review_action is not None:
            raise EvidenceError("review action is forbidden for this producer kind")


@dataclass(frozen=True)
class EvidenceEnvelopeV2:
    envelope_kind: str
    observation_id: str
    subject: SubjectRevision
    source: AuthorityPointer
    observed_at: int
    links: tuple[EvidenceLink, ...] = ()
    failure: FailureFacts | None = None
    producer: ProducerFacts | None = None

    def __post_init__(self) -> None:
        _token(self.observation_id, "observation_id")
        if not isinstance(self.subject, SubjectRevision) or not isinstance(self.source, AuthorityPointer):
            raise EvidenceError("evidence envelope requires immutable subject and authority pointers")
        if isinstance(self.observed_at, bool) or not isinstance(self.observed_at, int) or self.observed_at < 0:
            raise EvidenceError("invalid observed_at")
        if not isinstance(self.links, tuple):
            raise EvidenceError("links must be a tuple")
        if self.envelope_kind == "failure_observation":
            valid = isinstance(self.failure, FailureFacts) and self.producer is None and not self.links
        elif self.envelope_kind == "producer_fact":
            valid = isinstance(self.producer, ProducerFacts) and self.failure is None
        else:
            valid = False
        if not valid:
            raise EvidenceError("invalid tagged evidence envelope")
        if len(self.links) > 32:
            raise EvidenceError("producer lineage is bounded to 32 links")
        for position, link in enumerate(self.links):
            if not isinstance(link, EvidenceLink) or link.ordinal != position:
                raise EvidenceError("producer lineage ordinals must be dense and ordered")
        pairs = tuple((link.relation, link.target_event_id) for link in self.links)
        if len(set(pairs)) != len(pairs):
            raise EvidenceError("producer lineage links must be unique")


@dataclass(frozen=True)
class Event:
    event_id: str
    recurrence_count: int
    observation_ids: tuple[str, ...]
    contextual: bool = False


@dataclass(frozen=True)
class ProducerEvent:
    event_id: str
    observation_ids: tuple[str, ...]
    producer_kind: str
    review_action: str
    validation_states: tuple[str, ...]
    links: tuple[EvidenceLink, ...]
    contextual: bool = False


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
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise EvidenceError("unsupported evidence schema version")
        existing = _schema_fingerprint(conn)
        v1 = _schema_fingerprint_for(_V1_SCHEMA)
        v2 = _schema_fingerprint_for(_V2_SCHEMA)
        v3 = _schema_fingerprint_for(_V3_SCHEMA)
        if version == 0 and existing:
            raise EvidenceError("unversioned evidence database is not safe to open")
        if version == 1 and existing != v1:
            raise EvidenceError("evidence v1 schema does not match the known migration source")
        if version == 2 and existing != v2:
            raise EvidenceError("evidence v2 schema does not match the known migration source")
        if version == SCHEMA_VERSION and existing != v3:
            raise EvidenceError("evidence schema does not match its version")
        if version == 0:
            _execute_schema(conn, _V3_SCHEMA)
            if _schema_fingerprint(conn) != v3:
                raise EvidenceError("evidence schema did not initialize exactly")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif version == 1:
            EvidenceStore._migrate_v1_to_v2(conn, v2)
            EvidenceStore._migrate_v2_to_v3(conn, v3)
        elif version == 2:
            EvidenceStore._migrate_v2_to_v3(conn, v3)
        EvidenceStore._validate_graph(conn)

    @staticmethod
    def _validate_graph(conn: sqlite3.Connection) -> None:
        events = {row["event_id"]: row for row in conn.execute(
            "SELECT event_id, event_kind, repository, producer_kind FROM events")}
        links_by_source: dict[str, list[sqlite3.Row]] = {}
        for link in conn.execute(
                "SELECT source_event_id, ordinal, relation, target_event_id "
                "FROM event_links ORDER BY source_event_id, ordinal"):
            links_by_source.setdefault(link["source_event_id"], []).append(link)
        for source_id, links in links_by_source.items():
            source = events.get(source_id)
            if source is None or source["event_kind"] != "producer_fact" or len(links) > 32:
                raise EvidenceError("invalid persisted Evidence lineage")
            pairs: set[tuple[str, str]] = set()
            for position, link in enumerate(links):
                target = events.get(link["target_event_id"])
                relation = link["relation"]
                pair = (relation, link["target_event_id"])
                if (link["ordinal"] != position or pair in pairs or target is None
                        or source["repository"] != target["repository"]
                        or relation not in _LINEAGE_MATRIX):
                    raise EvidenceError("invalid persisted Evidence lineage")
                pairs.add(pair)
                target_kind = ("failure_observation"
                               if target["event_kind"] == "failure_observation"
                               else target["producer_kind"])
                sources, targets = _LINEAGE_MATRIX[relation]
                if source["producer_kind"] not in sources or target_kind not in targets:
                    raise EvidenceError("invalid persisted Evidence lineage")
        for event in events.values():
            links = links_by_source.get(event["event_id"], [])
            if event["event_kind"] == "failure_observation" and links:
                raise EvidenceError("invalid persisted Evidence lineage")
            required = _REQUIRED_RELATION.get(event["producer_kind"])
            if required is not None and all(link["relation"] != required for link in links):
                raise EvidenceError("invalid persisted Evidence lineage")

    @staticmethod
    def _migration_checkpoint(label: str) -> None:
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
            EvidenceStore._migration_checkpoint("v1-to-v2:after-copy-receipts")
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
    def _migrate_v2_to_v3(conn: sqlite3.Connection,
                          expected: tuple[tuple[str, str, str, str], ...]) -> None:
        tables = ("events", "observations", "evaluations", "candidates",
                  "candidate_events", "receipts")
        statements = tuple(statement.strip() for statement in _V3_SCHEMA.split(";")
                           if statement.strip())
        table_names = ("events", "observations", "evaluations", "candidates",
                       "candidate_events", "receipts", "event_links")
        index_names = ("events_failure_identity", "observations_by_event",
                       "evaluations_by_event", "candidate_events_by_event",
                       "event_links_by_target")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            before = {
                table: tuple(sorted((tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
                                    key=repr))
                for table in tables
            }
            expected_rows = {
                "events": tuple(sorted((
                    (row[0], "failure_observation", row[1], "", row[2], row[3], "", "",
                     row[4], "", row[5], row[6], "") for row in before["events"]), key=repr)),
                "observations": tuple(sorted((
                    (*row, "", "", "") for row in before["observations"]), key=repr)),
                **{table: before[table] for table in tables[2:]},
            }
            for table in ("receipts", "candidate_events", "evaluations", "observations",
                          "candidates", "events"):
                conn.execute(f"ALTER TABLE {table} RENAME TO v2_{table}")
                EvidenceStore._migration_checkpoint(f"rename:{table}")
            for index in ("candidate_events_by_event", "evaluations_by_event",
                          "observations_by_event"):
                conn.execute(f"DROP INDEX {index}")
                EvidenceStore._migration_checkpoint(f"drop-old-index:{index}")
            for table, statement in zip(table_names, statements[:7], strict=True):
                conn.execute(statement)
                EvidenceStore._migration_checkpoint(f"create-table:{table}")
            conn.execute("""INSERT INTO events
                SELECT event_id, 'failure_observation', repository, '', subject, revision, '', '',
                       failure_class, '', signature, normalizer, '' FROM v2_events""")
            EvidenceStore._migration_checkpoint("copy:events")
            conn.execute("""INSERT INTO observations
                SELECT observation_id, event_id, source_kind, source_repository, source_locator,
                       source_revision, source_hash_algorithm, source_hash, source_scope,
                       validation_state, observed_at, parent_revision, fixer_revision, '', '', ''
                FROM v2_observations""")
            EvidenceStore._migration_checkpoint("copy:observations")
            for table in ("evaluations", "candidates", "candidate_events", "receipts"):
                conn.execute(f"INSERT INTO {table} SELECT * FROM v2_{table}")
                EvidenceStore._migration_checkpoint(f"copy:{table}")
            copied = {
                table: tuple(sorted((tuple(row) for row in conn.execute(f"SELECT * FROM {table}")),
                                    key=repr))
                for table in tables
            }
            if copied != expected_rows:
                raise EvidenceError("evidence migration did not preserve every v2 value")
            EvidenceStore._migration_checkpoint("verify:copied-values")
            for table in ("receipts", "candidate_events", "evaluations", "observations",
                          "candidates", "events"):
                conn.execute(f"DROP TABLE v2_{table}")
                EvidenceStore._migration_checkpoint(f"drop-old-table:{table}")
            for index, statement in zip(index_names, statements[7:], strict=True):
                conn.execute(statement)
                EvidenceStore._migration_checkpoint(f"create-index:{index}")
            if _schema_fingerprint(conn) != expected:
                raise EvidenceError("evidence migration did not produce v3 exactly")
            EvidenceStore._migration_checkpoint("verify:fingerprint")
            conn.execute("PRAGMA user_version = 3")
            EvidenceStore._migration_checkpoint("set:user-version")
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

    @staticmethod
    def _producer_event_id(envelope: EvidenceEnvelopeV2) -> str:
        import hashlib
        producer = envelope.producer
        assert producer is not None
        parts = ["agentflow-evidence-producer-v2", envelope.source.repository,
                 envelope.subject.subject_kind, envelope.subject.subject, envelope.subject.revision,
                 envelope.subject.locator, envelope.subject.content_digest, producer.producer_kind,
                 producer.fact_digest, producer.normalizer_version, producer.review_action or ""]
        for link in envelope.links:
            parts.extend((str(link.ordinal), link.relation, link.target_event_id))
        return "event-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:32]

    def observe(self, observation: Observation | EvidenceEnvelopeV2) -> Event | ProducerEvent:
        if isinstance(observation, EvidenceEnvelopeV2):
            if observation.envelope_kind == "producer_fact":
                return self._observe_producer(observation)
            failure = observation.failure
            assert failure is not None
            observation = Observation(
                observation.observation_id, observation.subject, failure.failure_class,
                failure.validation_state, failure.signature_digest, failure.normalizer_version,
                observation.source, observation.observed_at,
                failure.reviewed_parent_revision or "", failure.fixer_revision or "",
            )
        if not isinstance(observation, Observation):
            raise EvidenceError("observe requires an Evidence envelope")
        event_id = self._event_id(observation)
        with self._connect() as conn:
            event_values = (event_id, "failure_observation", observation.source.repository,
                observation.subject.subject_kind, observation.subject.subject, observation.subject.revision,
                observation.subject.locator, observation.subject.content_digest, observation.failure_class, "",
                observation.signature_digest, observation.normalizer_version, "")
            existing_event = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing_event is not None:
                stored_event = tuple(existing_event)
                legacy_unknown = (stored_event[3], stored_event[6], stored_event[7]) == ("", "", "")
                historical_fields = (0, 1, 2, 4, 5, 8, 9, 10, 11, 12)
                if ((not legacy_unknown and stored_event != event_values)
                        or (legacy_unknown and any(
                            stored_event[index] != event_values[index]
                            for index in historical_fields))):
                    raise EvidenceError("event_id already names different immutable failure facts")
            conn.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", event_values)
            existing = conn.execute("SELECT * FROM observations WHERE observation_id=?", (observation.observation_id,)).fetchone()
            immutable = (event_id, observation.source.authority_kind, observation.source.repository,
                         observation.source.locator, observation.source.revision,
                         observation.source.content_hash_algorithm, observation.source.content_hash,
                         observation.source.scope, observation.validation_state, observation.observed_at,
                         observation.reviewed_parent_revision, observation.fixer_revision,
                         observation.subject.subject_kind, observation.subject.locator,
                         observation.subject.content_digest)
            if existing is not None:
                stored = tuple(existing)[1:]
                legacy_unknown = stored[-3:] == ("", "", "")
                if (stored[:12] != immutable[:12]
                        or (not legacy_unknown and stored[12:] != immutable[12:])):
                    raise EvidenceError("observation_id already names a different immutable observation")
            conn.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (observation.observation_id,
                event_id, observation.source.authority_kind, observation.source.repository, observation.source.locator,
                observation.source.revision, observation.source.content_hash_algorithm, observation.source.content_hash,
                observation.source.scope, observation.validation_state, observation.observed_at,
                observation.reviewed_parent_revision, observation.fixer_revision,
                observation.subject.subject_kind, observation.subject.locator, observation.subject.content_digest))
            return self._event(conn, event_id)

    def _observe_producer(self, envelope: EvidenceEnvelopeV2) -> ProducerEvent:
        producer = envelope.producer
        assert producer is not None
        event_id = self._producer_event_id(envelope)
        event_values = (event_id, "producer_fact", envelope.source.repository,
                        envelope.subject.subject_kind, envelope.subject.subject,
                        envelope.subject.revision, envelope.subject.locator,
                        envelope.subject.content_digest, "", producer.producer_kind,
                        producer.fact_digest, producer.normalizer_version,
                        producer.review_action or "")
        observation_values = (envelope.observation_id, event_id, envelope.source.authority_kind,
                              envelope.source.repository, envelope.source.locator,
                              envelope.source.revision, envelope.source.content_hash_algorithm,
                              envelope.source.content_hash, envelope.source.scope,
                              producer.validation_state, envelope.observed_at, "", "",
                              envelope.subject.subject_kind, envelope.subject.locator,
                              envelope.subject.content_digest)
        with self._connect() as conn:
            required = _REQUIRED_RELATION.get(producer.producer_kind)
            if required is not None and all(link.relation != required for link in envelope.links):
                raise EvidenceError(f"{producer.producer_kind} requires {required} lineage")
            for link in envelope.links:
                target = conn.execute(
                    "SELECT event_kind, repository, producer_kind FROM events WHERE event_id=?",
                    (link.target_event_id,),
                ).fetchone()
                if target is None:
                    raise EvidenceError("lineage target does not resolve in this Evidence store")
                if target["repository"] != envelope.source.repository:
                    raise EvidenceError("lineage target belongs to a different repository")
                target_kind = ("failure_observation" if target["event_kind"] == "failure_observation"
                               else target["producer_kind"])
                sources, targets = _LINEAGE_MATRIX[link.relation]
                if producer.producer_kind not in sources or target_kind not in targets:
                    raise EvidenceError("lineage relation is not applicable to source and target kinds")
            existing_event = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if existing_event is not None and tuple(existing_event) != event_values:
                raise EvidenceError("event_id already names different immutable producer facts")
            if existing_event is not None:
                stored_links = tuple(tuple(row) for row in conn.execute(
                    "SELECT ordinal, relation, target_event_id FROM event_links "
                    "WHERE source_event_id=? ORDER BY ordinal", (event_id,)))
                supplied_links = tuple((link.ordinal, link.relation, link.target_event_id)
                                       for link in envelope.links)
                if stored_links != supplied_links:
                    raise EvidenceError("event_id already names different immutable producer lineage")
            conn.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", event_values)
            existing = conn.execute("SELECT * FROM observations WHERE observation_id=?",
                                    (envelope.observation_id,)).fetchone()
            if existing is not None and tuple(existing) != observation_values:
                raise EvidenceError("observation_id already names a different immutable observation")
            conn.execute("INSERT OR IGNORE INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         observation_values)
            for link in envelope.links:
                conn.execute("INSERT OR IGNORE INTO event_links VALUES (?,?,?,?)",
                             (event_id, link.ordinal, link.relation, link.target_event_id))
            event = self._event(conn, event_id)
            assert isinstance(event, ProducerEvent)
            return event

    def _event(self, conn: sqlite3.Connection, event_id: str, *, contextual: bool = False) -> Event | ProducerEvent:
        row = conn.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        if row is None: raise EvidenceError("unknown event")
        ids = () if contextual else tuple(item[0] for item in conn.execute(
            "SELECT observation_id FROM observations WHERE event_id=? ORDER BY observation_id", (event_id,)))
        if row["event_kind"] == "failure_observation":
            return Event(event_id, 1, ids, contextual)
        states = () if contextual else tuple(item[0] for item in conn.execute(
            "SELECT DISTINCT validation_state FROM observations WHERE event_id=? ORDER BY validation_state",
            (event_id,)))
        links = tuple(EvidenceLink(item[0], item[1], item[2]) for item in conn.execute(
            "SELECT relation, target_event_id, ordinal FROM event_links WHERE source_event_id=? ORDER BY ordinal",
            (event_id,)))
        return ProducerEvent(event_id, ids, row["producer_kind"], row["review_action"], states,
                             links, contextual)

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

    def brief_for(self, subject: str, *, repository: str = "", now: int,
                  effective_policy_versions: tuple[int, ...] = (),
                  accepted_validation_states: tuple[str, ...] = ALL_VALIDATION_STATES
                  ) -> tuple[Event | ProducerEvent, ...]:
        _token(subject, "subject")
        if repository:
            _token(repository, "repository")
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise EvidenceError("invalid now")
        if (not isinstance(accepted_validation_states, tuple)
                or len(set(accepted_validation_states)) != len(accepted_validation_states)
                or any(state not in VALIDATION_STATES for state in accepted_validation_states)):
            raise EvidenceError("accepted validation states must be a unique tuple of known states")
        self._expire(now, frozenset(effective_policy_versions))
        if not accepted_validation_states:
            return ()
        with self._connect() as conn:
            marks = ",".join("?" for _ in accepted_validation_states)
            if not repository:
                rows = conn.execute(
                    f"SELECT event_id FROM events WHERE event_kind='failure_observation' AND subject=? "
                    f"AND EXISTS (SELECT 1 FROM observations WHERE observations.event_id=events.event_id "
                    f"AND validation_state IN ({marks})) ORDER BY event_id",
                    (subject, *accepted_validation_states),
                ).fetchall()
                return tuple(self._event(conn, row[0]) for row in rows)
            roots = {row[0] for row in conn.execute(
                f"SELECT event_id FROM events WHERE repository=? AND subject=? "
                f"AND EXISTS (SELECT 1 FROM observations WHERE observations.event_id=events.event_id "
                f"AND validation_state IN ({marks}))",
                (repository, subject, *accepted_validation_states),
            )}
            selected = set(roots)
            frontier = list(roots)
            while frontier:
                source = frontier.pop()
                for row in conn.execute(
                        "SELECT links.target_event_id FROM event_links AS links "
                        "JOIN events AS targets ON targets.event_id=links.target_event_id "
                        "WHERE links.source_event_id=? AND targets.repository=?",
                        (source, repository)):
                    if row[0] not in selected:
                        selected.add(row[0])
                        frontier.append(row[0])
            return tuple(self._event(conn, event_id, contextual=event_id not in roots)
                         for event_id in sorted(selected))

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
            conn.execute("CREATE TEMP TABLE retained_event_marks (event_id TEXT PRIMARY KEY)")
            conn.execute("INSERT OR IGNORE INTO retained_event_marks SELECT event_id FROM observations")
            conn.execute("INSERT OR IGNORE INTO retained_event_marks SELECT event_id FROM evaluations")
            conn.execute("INSERT OR IGNORE INTO retained_event_marks SELECT event_id FROM candidate_events")
            while True:
                inserted = conn.execute(
                    "INSERT OR IGNORE INTO retained_event_marks "
                    "SELECT links.target_event_id FROM event_links AS links "
                    "JOIN retained_event_marks AS marks ON marks.event_id=links.source_event_id"
                ).rowcount
                if not inserted:
                    break
            conn.execute("DELETE FROM event_links WHERE source_event_id NOT IN "
                         "(SELECT event_id FROM retained_event_marks)")
            conn.execute("DELETE FROM events WHERE event_id NOT IN "
                         "(SELECT event_id FROM retained_event_marks)")
