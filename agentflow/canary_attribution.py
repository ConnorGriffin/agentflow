"""Append-only attribution for canaries already activated by OperationalSafety.

This module records an existing treatment; it never chooses a treatment or changes routing.
Its participant joins a caller-owned coordinator Store transaction, while ``read`` is the
content-free resolver used by later reporting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import sqlite3
import unicodedata
from typing import Protocol

from agentflow.coordinator.record import ENABLED_STAGES
from agentflow.evidence import (
    PROMOTION_RECEIPT_ID_GRAMMAR_VERSION,
    EvidenceError,
    PromotionReceipt,
    valid_promotion_receipt_id,
)
from agentflow.operational_safety import (
    CanaryState,
    OperationalSafety,
    OPERATIONAL_SAFETY_CONTRACT_DIGEST,
    PROMOTION_VERIFIER,
    ROUTE_CELL_CONTRACT_DIGEST,
)
from agentflow.promotion_contract import PromotionAuthorityError, parse_promotion_scope


ATTRIBUTION_CONTRACT_VERSION = "agentflow-canary-attribution-v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_REVISION = re.compile(r"^(?:[a-f0-9]{40}|sha256:[a-f0-9]{64})$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ISSUE_SUBJECT = re.compile(r"^[1-9][0-9]{0,19}$")
_CONVERSATION_SUBJECT = re.compile(
    r"^(?:[1-9][0-9]{0,19}|[a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[1-5][a-f0-9]{3}-"
    r"[89ab][a-f0-9]{3}-[a-f0-9]{12}|(?:conv|conversation)-[1-9][0-9]{0,19})$")
_STAGE_VOCABULARY = "|".join(re.escape(stage) for stage in ENABLED_STAGES)
_STAGE_IDENTITY = re.compile(
    r"^(?P<repository>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\|"
    r"(?P<subject>[A-Za-z0-9-]{1,64})\|"
    rf"(?P<stage>{_STAGE_VOCABULARY})\|"
    r"(?P<target>-|[a-f0-9]{40}|[0-9]{1,20}|"
    r"(?:IC|PRRC|DC)_[A-Za-z0-9_-]{1,96})"
    r"(?:\|(?:r|c|s|p|q|u)[1-9][0-9]{0,9}|\|a(?:product|standards|fix))*$")

CANARY_ATTRIBUTION_REFUSAL_CODES = frozenset({
    "wrong_connection",
    "outside_transaction",
    "unreadable_canary_state",
    "missing_receipt",
    "unreadable_receipt",
    "wrong_verifier",
    "wrong_scope",
    "wrong_binding",
    "corrupt_attribution",
    "conflicting_attribution",
})

CANARY_ATTRIBUTION_SCHEMA = (
    "CREATE TABLE canary_attributions ("
    " stage_identity TEXT PRIMARY KEY,"
    " repository TEXT NOT NULL,"
    " subject_revision TEXT NOT NULL,"
    " route_cell_digest TEXT NOT NULL,"
    " receipt_id TEXT NOT NULL,"
    " method_revision TEXT NOT NULL,"
    " cohort_id TEXT NOT NULL,"
    " contract_version TEXT NOT NULL,"
    " attribution_digest TEXT NOT NULL,"
    " CONSTRAINT canary_attributions_closed CHECK (canary_attribution_row_valid("
    "stage_identity, repository, subject_revision, route_cell_digest, receipt_id,"
    " method_revision, cohort_id, contract_version, attribution_digest) = 1))"
)

_NO_UPDATE_SCHEMA = (
    "CREATE TRIGGER canary_attributions_no_update BEFORE UPDATE ON canary_attributions "
    "BEGIN SELECT RAISE(ABORT, 'canary_attributions is append-only'); END"
)
_NO_DELETE_SCHEMA = (
    "CREATE TRIGGER canary_attributions_no_delete BEFORE DELETE ON canary_attributions "
    "BEGIN SELECT RAISE(ABORT, 'canary_attributions is append-only'); END"
)
CANARY_ATTRIBUTION_SCHEMA_STATEMENTS = (
    ("v2-to-v3:create:canary_attributions", CANARY_ATTRIBUTION_SCHEMA),
    ("v2-to-v3:create:no-update-trigger", _NO_UPDATE_SCHEMA),
    ("v2-to-v3:create:no-delete-trigger", _NO_DELETE_SCHEMA),
)

CANARY_ATTRIBUTION_SCHEMA_FINGERPRINT = (
    ("table", "canary_attributions", "canary_attributions",
     "createtablecanary_attributions(stage_identitytextprimarykey,repositorytextnotnull,"
     "subject_revisiontextnotnull,route_cell_digesttextnotnull,receipt_idtextnotnull,"
     "method_revisiontextnotnull,cohort_idtextnotnull,contract_versiontextnotnull,"
     "attribution_digesttextnotnull,constraintcanary_attributions_closedcheck("
     "canary_attribution_row_valid(stage_identity,repository,subject_revision,"
     "route_cell_digest,receipt_id,method_revision,cohort_id,contract_version,"
     "attribution_digest)=1))"),
    ("trigger", "canary_attributions_no_delete", "canary_attributions",
     "createtriggercanary_attributions_no_deletebeforedeleteoncanary_attributionsbegin"
     "selectraise(abort,'canary_attributionsisappend-only');end"),
    ("trigger", "canary_attributions_no_update", "canary_attributions",
     "createtriggercanary_attributions_no_updatebeforeupdateoncanary_attributionsbegin"
     "selectraise(abort,'canary_attributionsisappend-only');end"),
)

# Exact coordinator Store schema-v2 migration source at #585 merge bd818fa.  The Store
# compares these canonical sqlite_master bytes before this module's DDL is allowed to run.
STORE_V2_SCHEMA_FINGERPRINT = (
    ("table", "records", "records",
     "createtablerecords(identitytextprimarykey,pooltextnotnull,statetextnotnull,"
     "demandintegernotnull,datatextnotnull)"),
    ("table", "safety_action_results", "safety_action_results",
     "createtablesafety_action_results(action_idtextprimarykey,evidence_reftextnotnull,"
     "prooftextnotnull,foreignkey(action_id)referencessafety_actions(action_id))"),
    ("table", "safety_actions", "safety_actions",
     "createtablesafety_actions(action_idtextprimarykey,idempotency_keytextuniquenotnull,"
     "kindtextnotnull,route_cell_digesttextnotnull,declaration_digesttextnotnull,"
     "evidence_reftextnotnull,exit_conditiontextnotnull,payloadtextnotnull)"),
    ("table", "safety_alerts", "safety_alerts",
     "createtablesafety_alerts(alert_idtextprimarykey,idempotency_keytextuniquenotnull,"
     "kindtextnotnull,route_cell_digesttextnotnull,evidence_reftextnotnull)"),
    ("table", "safety_canary_state", "safety_canary_state",
     "createtablesafety_canary_state(cell_keytextprimarykey,active_digesttextnotnull,"
     "active_receipt_idtext,active_receipt_digesttext,predecessor_digesttext,"
     "disabled_generationintegernotnull,generationintegernotnull)"),
    ("table", "safety_launch_configs", "safety_launch_configs",
     "createtablesafety_launch_configs(digesttextprimarykey,contentblobnotnull)"),
    ("table", "safety_observations", "safety_observations",
     "createtablesafety_observations(observation_idtextprimarykey,scope_identitytextnotnull,"
     "route_cell_digesttextnotnull,outcometextnotnull,verifiedintegernotnull,"
     "evidence_reftextnotnull,declaration_digesttextnotnull)"),
    ("table", "safety_rerun_claims", "safety_rerun_claims",
     "createtablesafety_rerun_claims(action_idtextprimarykey,owner_tokentextnotnull,"
     "generationintegernotnull,expires_at_nsintegernotnull,"
     "foreignkey(action_id)referencessafety_actions(action_id))"),
    ("table", "safety_route_cells", "safety_route_cells",
     "createtablesafety_route_cells(digesttextprimarykey,cell_keytextnotnull,"
     "repositorytextnotnull,stagetextnotnull,providertextnotnull,modeltextnotnull,"
     "route_idtextnotnull,launch_config_digesttextnotnull,datatextnotnull,"
     "foreignkey(launch_config_digest)referencessafety_launch_configs(digest))"),
    ("table", "safety_route_state", "safety_route_state",
     "createtablesafety_route_state(cell_keytextprimarykey,active_digesttextnotnull,"
     "quarantined_digesttext,quarantine_action_idtext,safety_state_idtextnotnull,"
     "generationintegernotnull)"),
)
STORE_V3_SCHEMA_FINGERPRINT = tuple(sorted(
    STORE_V2_SCHEMA_FINGERPRINT + CANARY_ATTRIBUTION_SCHEMA_FINGERPRINT))


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


STORE_V2_SCHEMA_FINGERPRINT_DIGEST = _digest(STORE_V2_SCHEMA_FINGERPRINT)
STORE_V3_SCHEMA_FINGERPRINT_DIGEST = _digest(STORE_V3_SCHEMA_FINGERPRINT)

DEPENDENCY_PINS = {
    "issue_584_merge": "ef08dd3d2f691aa154ddaa193e6161b559099396",
    "evidence_schema": 4,
    "promotion_contract": "github-merged-pr-v1",
    "issue_584_evidence_blob": "abe7473358c646d85ebc2bb51ea0154fff89bb19",
    "issue_585_merge": "bd818fa1d65c92def671192464207e6bc3904a34",
    "issue_585_receipt_reader_blob": "02e7d525a4cba5c4cdd95e26143673ea186e5519",
    "promotion_verifier": "/".join(PROMOTION_VERIFIER),
    "route_cell_contract_digest": ROUTE_CELL_CONTRACT_DIGEST,
    "operational_safety_contract_digest": OPERATIONAL_SAFETY_CONTRACT_DIGEST,
    "coordinator_store_schema": 2,
    "coordinator_store_schema_fingerprint_digest": STORE_V2_SCHEMA_FINGERPRINT_DIGEST,
    "coordinator_store_target_schema": 3,
    "coordinator_store_target_fingerprint_digest": STORE_V3_SCHEMA_FINGERPRINT_DIGEST,
    "coordinator_stage_vocabulary_digest": _digest(ENABLED_STAGES),
    "evidence_promotion_receipt_id_grammar": PROMOTION_RECEIPT_ID_GRAMMAR_VERSION,
}

CANARY_ATTRIBUTION_CONTRACT = {
    "schema": ATTRIBUTION_CONTRACT_VERSION,
    "dependencies": DEPENDENCY_PINS,
    "identity": "coordinator Record.identity",
    "transaction": "exact Store connection; caller-owned open transaction",
    "active_authority": "OperationalSafety.canary_state exact active digest",
    "receipt_authority": "PromotionReceiptReader.read(active_receipt_id)",
    "mapping": {
        "receipt_id": "CanaryState.active_receipt_id",
        "method_revision": "PromotionReceipt.authority.approved_revision",
        "cohort_id": "CanaryState.cell_key",
    },
    "selector_version": ATTRIBUTION_CONTRACT_VERSION,
    "persistence": "append-only digest-verified content-free fields",
    "replay": "committed attribution before external authority reads",
    "refusal_codes": sorted(CANARY_ATTRIBUTION_REFUSAL_CODES),
}
CANARY_ATTRIBUTION_CONTRACT_DIGEST = _digest(CANARY_ATTRIBUTION_CONTRACT)

# Fixed code-owned vectors pin the two non-obvious mappings independently of runtime state.
CANARY_ATTRIBUTION_VECTORS = (
    {
        "receipt": {"receipt_id": "receipt-vector-fleet",
                    "approved_revision": "1" * 40},
        "active_route_cell": {"cell_key": "2" * 64},
        "attribution": {"receipt_id": "receipt-vector-fleet",
                        "method_revision": "1" * 40, "cohort_id": "2" * 64},
    },
    {
        "receipt": {"receipt_id": "receipt-vector-overlay",
                    "approved_revision": "sha256:" + "3" * 64},
        "active_route_cell": {"cell_key": "4" * 64},
        "attribution": {"receipt_id": "receipt-vector-overlay",
                        "method_revision": "sha256:" + "3" * 64,
                        "cohort_id": "4" * 64},
    },
)


class CanaryAttributionRefused(RuntimeError):
    """A closed, content-free refusal from the attribution authority."""

    def __init__(self, code: str) -> None:
        if code not in CANARY_ATTRIBUTION_REFUSAL_CODES:
            raise ValueError("unknown canary attribution refusal code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CanaryAttribution:
    stage_identity: str
    repository: str
    subject_revision: str
    route_cell_digest: str
    receipt_id: str
    method_revision: str
    cohort_id: str
    contract_version: str
    attribution_digest: str


class PromotionReceiptAuthority(Protocol):
    def read(self, receipt_id: str) -> PromotionReceipt: ...


def register_sql_functions(conn: sqlite3.Connection) -> None:
    """Install the deterministic validator referenced by the closed v3 table schema."""
    conn.create_function(
        "canary_attribution_row_valid", 9, _schema_row_valid, deterministic=True)


def initialize_schema(conn: sqlite3.Connection, *, checkpoint=None) -> None:
    """Create only the attribution-owned append-only table and guards."""
    for name, statement in CANARY_ATTRIBUTION_SCHEMA_STATEMENTS:
        if checkpoint is not None:
            checkpoint(name)
        conn.execute(statement)


class CanaryAttributionAuthority:
    """The sole writer and resolver for coordinator canary attribution."""

    def __init__(self, store: object, operational_safety: OperationalSafety,
                 promotion_receipts: PromotionReceiptAuthority) -> None:
        try:
            conn = getattr(store, "_conn")
            lock = getattr(store, "_lock")
        except AttributeError as error:
            raise CanaryAttributionRefused("wrong_connection") from error
        if (not isinstance(conn, sqlite3.Connection)
                or not isinstance(operational_safety, OperationalSafety)
                or operational_safety._conn is not conn
                or operational_safety._lock is not lock):
            raise CanaryAttributionRefused("wrong_connection")
        self._conn = conn
        self._lock = lock
        self._operational_safety = operational_safety
        self._promotion_receipts = promotion_receipts

    def participate_in_admission(
            self, connection: sqlite3.Connection, logical_stage_identity: str,
            repository: str, subject_revision: str,
            route_cell_digest: str) -> CanaryAttribution | None:
        """Join one caller-owned transaction; never begin, commit, or roll it back."""
        if connection is not self._conn:
            raise CanaryAttributionRefused("wrong_connection")
        transaction_authority = getattr(
            connection, "_agentflow_has_immediate_transaction_authority", None)
        if (not callable(transaction_authority) or not transaction_authority()):
            raise CanaryAttributionRefused("outside_transaction")
        supplied = _validate_supplied(
            logical_stage_identity, repository, subject_revision, route_cell_digest,
            bind_identity=False)
        with self._lock:
            existing = self._row(logical_stage_identity)
            if existing is not None:
                if _supplied(existing) != supplied:
                    raise CanaryAttributionRefused("conflicting_attribution")
                return existing

            _stage_identity(logical_stage_identity, repository, "wrong_binding")

            try:
                state = self._operational_safety.canary_state(route_cell_digest)
            except Exception as error:
                raise CanaryAttributionRefused("unreadable_canary_state") from error
            if not isinstance(state, CanaryState):
                raise CanaryAttributionRefused("unreadable_canary_state")
            if state.active_route_cell_digest != route_cell_digest:
                raise CanaryAttributionRefused("wrong_binding")
            if state.active_receipt_id is None:
                return None
            _receipt_id(state.active_receipt_id, "wrong_binding")
            receipt = self._receipt(state.active_receipt_id)
            method_revision = self._validate_receipt(receipt, state, repository)
            facts = {
                "stage_identity": logical_stage_identity,
                "repository": repository,
                "subject_revision": subject_revision,
                "route_cell_digest": route_cell_digest,
                "receipt_id": state.active_receipt_id,
                "method_revision": method_revision,
                "cohort_id": state.cell_key,
                "contract_version": ATTRIBUTION_CONTRACT_VERSION,
            }
            attribution = CanaryAttribution(**facts, attribution_digest=_digest(facts))
            _validate_attribution(attribution)
            try:
                connection.execute(
                    "INSERT INTO canary_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    tuple(asdict(attribution).values()),
                )
            except sqlite3.IntegrityError:
                winner = self._row(logical_stage_identity)
                if winner is None or winner != attribution:
                    raise CanaryAttributionRefused("conflicting_attribution")
                return winner
            except sqlite3.DatabaseError as error:
                raise CanaryAttributionRefused("conflicting_attribution") from error
            return attribution

    def read(self, stage_identity: str) -> CanaryAttribution | None:
        """Resolve one committed attribution without consulting external authorities."""
        with self._lock:
            attribution = self._row(stage_identity)
            if attribution is None:
                _stage_identity(stage_identity, None, "wrong_binding")
            return attribution

    def _row(self, stage_identity: str) -> CanaryAttribution | None:
        try:
            row = self._conn.execute(
                "SELECT stage_identity, repository, subject_revision, route_cell_digest,"
                " receipt_id, method_revision, cohort_id, contract_version,"
                " attribution_digest FROM canary_attributions WHERE stage_identity = ?",
                (stage_identity,),
            ).fetchone()
        except sqlite3.DatabaseError as error:
            raise CanaryAttributionRefused("corrupt_attribution") from error
        if row is None:
            return None
        try:
            attribution = CanaryAttribution(*row)
            _validate_attribution(attribution)
        except (TypeError, ValueError, CanaryAttributionRefused) as error:
            raise CanaryAttributionRefused("corrupt_attribution") from error
        return attribution

    def _receipt(self, receipt_id: str) -> PromotionReceipt:
        try:
            return self._promotion_receipts.read(receipt_id)
        except KeyError as error:
            raise CanaryAttributionRefused("missing_receipt") from error
        except EvidenceError as error:
            code = ("missing_receipt" if str(error) == "unknown promotion receipt"
                    else "unreadable_receipt")
            raise CanaryAttributionRefused(code) from error
        except Exception as error:
            raise CanaryAttributionRefused("unreadable_receipt") from error

    @staticmethod
    def _validate_receipt(receipt: PromotionReceipt, state: CanaryState,
                          repository: str) -> str:
        if not isinstance(receipt, PromotionReceipt):
            raise CanaryAttributionRefused("unreadable_receipt")
        if (receipt.receipt_id != state.active_receipt_id
                or not receipt.authoritative or receipt.authority is None):
            raise CanaryAttributionRefused("missing_receipt")
        authority = receipt.authority
        if (authority.verifier_id, authority.verifier_version) != PROMOTION_VERIFIER:
            raise CanaryAttributionRefused("wrong_verifier")
        try:
            scope = parse_promotion_scope(authority.pointer.scope)
        except PromotionAuthorityError as error:
            raise CanaryAttributionRefused("wrong_scope") from error
        if (receipt.policy_version != scope.new
                or (scope.kind == "repository" and scope.repository != repository)):
            raise CanaryAttributionRefused("wrong_scope")
        pointer = authority.pointer
        if (pointer.content_hash_algorithm != "sha256"
                or pointer.content_hash != state.active_receipt_digest
                or authority.approved_revision != pointer.revision
                or authority.approved_hash != pointer.content_hash
                or authority.approved_scope != pointer.scope
                or authority.outcome != "verified"):
            raise CanaryAttributionRefused("wrong_binding")
        _revision(authority.approved_revision, "wrong_binding")
        _sha256(state.cell_key, "wrong_binding")
        return authority.approved_revision


def _validate_supplied(stage_identity: str, repository: str, subject_revision: str,
                       route_cell_digest: str, *,
                       bind_identity: bool = True) -> tuple[str, str, str, str]:
    if not isinstance(repository, str) or not _REPOSITORY.fullmatch(repository):
        raise CanaryAttributionRefused("wrong_scope")
    _stage_identity(
        stage_identity, repository if bind_identity else None, "wrong_binding")
    _revision(subject_revision, "wrong_binding", commit_only=True)
    _sha256(route_cell_digest, "wrong_binding")
    return stage_identity, repository, subject_revision, route_cell_digest


def _supplied(value: CanaryAttribution) -> tuple[str, str, str, str]:
    return (value.stage_identity, value.repository, value.subject_revision,
            value.route_cell_digest)


def _validate_attribution(value: CanaryAttribution) -> None:
    _validate_supplied(*_supplied(value))
    _receipt_id(value.receipt_id, "corrupt_attribution")
    _revision(value.method_revision, "corrupt_attribution")
    _sha256(value.cohort_id, "corrupt_attribution")
    if value.contract_version != ATTRIBUTION_CONTRACT_VERSION:
        raise CanaryAttributionRefused("corrupt_attribution")
    facts = asdict(value)
    stored_digest = facts.pop("attribution_digest")
    _sha256(stored_digest, "corrupt_attribution")
    if stored_digest != _digest(facts):
        raise CanaryAttributionRefused("corrupt_attribution")


def _stage_identity(value: object, repository: str | None, code: str) -> None:
    if (not isinstance(value, str) or unicodedata.normalize("NFC", value) != value
            or (match := _STAGE_IDENTITY.fullmatch(value)) is None
            or (repository is not None and match.group("repository") != repository)):
        raise CanaryAttributionRefused(code)
    subject = match.group("subject")
    target = match.group("target")
    if match.group("stage") == "converse":
        if (_CONVERSATION_SUBJECT.fullmatch(subject) is None
                or re.fullmatch(r"(?:0|[1-9][0-9]{0,19})", target) is None):
            raise CanaryAttributionRefused(code)
    elif _ISSUE_SUBJECT.fullmatch(subject) is None or target == "0":
        raise CanaryAttributionRefused(code)


def _receipt_id(value: object, code: str) -> None:
    if (not isinstance(value, str) or unicodedata.normalize("NFC", value) != value
            or not valid_promotion_receipt_id(value)):
        raise CanaryAttributionRefused(code)


def _revision(value: object, code: str, *, commit_only: bool = False) -> None:
    pattern = re.compile(r"^[a-f0-9]{40}$") if commit_only else _REVISION
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise CanaryAttributionRefused(code)


def _sha256(value: object, code: str) -> None:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise CanaryAttributionRefused(code)


def _schema_row_valid(*values: object) -> int:
    try:
        _validate_attribution(CanaryAttribution(*values))
    except (CanaryAttributionRefused, TypeError, ValueError):
        return 0
    return 1
