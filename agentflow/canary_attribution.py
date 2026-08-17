"""Store-owned attribution for already-active canary routing.

Canary attribution observes authority selected by OperationalSafety.  It never selects a
RouteCell, changes routing, or owns a transaction.  The coordinator Store constructs the
single owner, supplies durable Record facts, and commits the attribution with its successor.
Rows are append-only except at ADR 627's atomic never-started reservation retirement boundary.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import sqlite3
from typing import Protocol

from agentflow.evidence import EvidenceError, PromotionReceipt
from agentflow.operational_safety import (
    CanaryState,
    OperationalSafety,
    OPERATIONAL_SAFETY_CONTRACT_DIGEST,
    OPERATIONAL_SAFETY_CONTRACT_V1_DIGEST,
    PROMOTION_VERIFIER,
    ROUTE_CELL_CONTRACT_DIGEST,
    ROUTE_CELL_CONTRACT_V1_DIGEST,
    _AdmissionContext,
)
from agentflow.promotion_contract import PromotionAuthorityError, parse_promotion_scope


ATTRIBUTION_CONTRACT_VERSION = "agentflow-canary-attribution-v1"
RECEIPT_BINDING_DOMAIN = "agentflow-canary-attribution-receipt-binding-v1"
ROW_DIGEST_DOMAIN = "agentflow-canary-attribution-row-v1"
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_COMMIT_REVISION = re.compile(r"^[a-f0-9]{40}$")

CANARY_ATTRIBUTION_REFUSAL_CODES = frozenset({
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
    " route_cell_digest TEXT NOT NULL,"
    " receipt_binding TEXT NOT NULL,"
    " method_revision TEXT NOT NULL,"
    " cohort_id TEXT NOT NULL,"
    " contract_version TEXT NOT NULL,"
    " attribution_digest TEXT NOT NULL,"
    " CONSTRAINT canary_attributions_closed CHECK (canary_attribution_row_valid("
    "stage_identity, repository, route_cell_digest, receipt_binding, method_revision,"
    " cohort_id, contract_version, attribution_digest) = 1))"
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


def rollback_never_started_canary_attribution(
        conn: sqlite3.Connection, stage_identity: str,
) -> None:
    """Remove one attribution after its provider launch proved never started.

    The caller owns the transaction that also removes the coordinator record. SQLite has no
    statement-scoped bypass for an unconditional delete trigger, so this owner must suspend and
    restore its own trigger inside that transaction. SQLite's transactional DDL restores the
    trigger with the row if any later step rolls back.
    """
    if not conn.in_transaction:
        raise sqlite3.OperationalError(
            "never-started canary attribution rollback requires an active transaction")
    conn.execute("DROP TRIGGER canary_attributions_no_delete")
    conn.execute(
        "DELETE FROM canary_attributions WHERE stage_identity = ?", (stage_identity,))
    conn.execute(_NO_DELETE_SCHEMA)


# Exact coordinator Store schema-v2 migration source at #585 merge bd818fa.  Store compares
# canonical sqlite_master bytes before any v3 DDL is allowed to run.
STORE_V2_SCHEMA_FINGERPRINT = (
    ("table", "records", "records", "createtablerecords(identitytextprimarykey,pooltextnotnull,state"
     "textnotnull,demandintegernotnull,datatextnotnull)"),
    ("table", "safety_action_results", "safety_action_results", "createtablesafety_action_results("
     "action_idtextprimarykey,evidence_reftextnotnull,prooftextnotnull,foreignkey(action_id)references"
     "safety_actions(action_id))"),
    ("table", "safety_actions", "safety_actions", "createtablesafety_actions(action_idtextprimarykey,"
     "idempotency_keytextuniquenotnull,kindtextnotnull,route_cell_digesttextnotnull,declaration_digest"
     "textnotnull,evidence_reftextnotnull,exit_conditiontextnotnull,payloadtextnotnull)"),
    ("table", "safety_alerts", "safety_alerts", "createtablesafety_alerts(alert_idtextprimarykey,"
     "idempotency_keytextuniquenotnull,kindtextnotnull,route_cell_digesttextnotnull,evidence_reftextnotnull)"),
    ("table", "safety_canary_state", "safety_canary_state", "createtablesafety_canary_state(cell_key"
     "textprimarykey,active_digesttextnotnull,active_receipt_idtext,active_receipt_digesttext,"
     "predecessor_digesttext,disabled_generationintegernotnull,generationintegernotnull)"),
    ("table", "safety_launch_configs", "safety_launch_configs", "createtablesafety_launch_configs("
     "digesttextprimarykey,contentblobnotnull)"),
    ("table", "safety_observations", "safety_observations", "createtablesafety_observations("
     "observation_idtextprimarykey,scope_identitytextnotnull,route_cell_digesttextnotnull,outcome"
     "textnotnull,verifiedintegernotnull,evidence_reftextnotnull,declaration_digesttextnotnull)"),
    ("table", "safety_rerun_claims", "safety_rerun_claims", "createtablesafety_rerun_claims(action_id"
     "textprimarykey,owner_tokentextnotnull,generationintegernotnull,expires_at_nsintegernotnull,"
     "foreignkey(action_id)referencessafety_actions(action_id))"),
    ("table", "safety_route_cells", "safety_route_cells", "createtablesafety_route_cells(digesttext"
     "primarykey,cell_keytextnotnull,repositorytextnotnull,stagetextnotnull,providertextnotnull,model"
     "textnotnull,route_idtextnotnull,launch_config_digesttextnotnull,datatextnotnull,foreignkey("
     "launch_config_digest)referencessafety_launch_configs(digest))"),
    ("table", "safety_route_state", "safety_route_state", "createtablesafety_route_state(cell_keytext"
     "primarykey,active_digesttextnotnull,quarantined_digesttext,quarantine_action_idtext,safety_state_id"
     "textnotnull,generationintegernotnull)"),
)


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("utf-8")


def _digest(value: object) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


CANARY_ATTRIBUTION_SCHEMA_FINGERPRINT = (
    ("table", "canary_attributions", "canary_attributions",
     "createtablecanary_attributions(stage_identitytextprimarykey,repositorytextnotnull,"
     "route_cell_digesttextnotnull,receipt_bindingtextnotnull,method_revisiontextnotnull,"
     "cohort_idtextnotnull,contract_versiontextnotnull,attribution_digesttextnotnull,"
     "constraintcanary_attributions_closedcheck(canary_attribution_row_valid(stage_identity,"
     "repository,route_cell_digest,receipt_binding,method_revision,cohort_id,contract_version,"
     "attribution_digest)=1))"),
    ("trigger", "canary_attributions_no_delete", "canary_attributions",
     "createtriggercanary_attributions_no_deletebeforedeleteoncanary_attributionsbeginselectraise("
     "abort,'canary_attributionsisappend-only');end"),
    ("trigger", "canary_attributions_no_update", "canary_attributions",
     "createtriggercanary_attributions_no_updatebeforeupdateoncanary_attributionsbeginselectraise("
     "abort,'canary_attributionsisappend-only');end"),
)
STORE_V3_SCHEMA_FINGERPRINT = tuple(sorted(
    STORE_V2_SCHEMA_FINGERPRINT + CANARY_ATTRIBUTION_SCHEMA_FINGERPRINT))
STORE_V2_SCHEMA_FINGERPRINT_DIGEST = (
    "9039da12f2376a5078ae067bbe91bfc1b1bae5dffdc469d9ac7d7afbfb2ea05e")
STORE_V3_SCHEMA_FINGERPRINT_DIGEST = (
    "3a51988512b246ec34c469fc469b63cbcdabaf5d537c9a8552ae7c75d127bda5")
if (_digest(STORE_V2_SCHEMA_FINGERPRINT) != STORE_V2_SCHEMA_FINGERPRINT_DIGEST
        or _digest(STORE_V3_SCHEMA_FINGERPRINT) != STORE_V3_SCHEMA_FINGERPRINT_DIGEST):
    raise RuntimeError(
        "coordinator Store schema fingerprint changed: "
        + _digest(STORE_V3_SCHEMA_FINGERPRINT))

DEPENDENCY_PINS = {
    "issue_584_merge": "ef08dd3d2f691aa154ddaa193e6161b559099396",
    "issue_584_evidence_blob": "abe7473358c646d85ebc2bb51ea0154fff89bb19",
    "evidence_schema": 4,
    "promotion_contract": "github-merged-pr-v1",
    "issue_585_merge": "bd818fa1d65c92def671192464207e6bc3904a34",
    "issue_585_evidence_reader_blob": "02e7d525a4cba5c4cdd95e26143673ea186e5519",
    "issue_585_operational_safety_blob": "b1e10904b1ecf177a6cfafc218122f77f5261f30",
    "issue_585_store_blob": "1993afa56520777a0bb1391f476b3888e02c83f8",
    "promotion_verifier": "/".join(PROMOTION_VERIFIER),
    "route_cell_contract_v1_digest": ROUTE_CELL_CONTRACT_V1_DIGEST,
    "route_cell_contract_digest": ROUTE_CELL_CONTRACT_DIGEST,
    "operational_safety_contract_v1_digest": OPERATIONAL_SAFETY_CONTRACT_V1_DIGEST,
    "operational_safety_contract_digest": OPERATIONAL_SAFETY_CONTRACT_DIGEST,
    "coordinator_store_schema": 2,
    "coordinator_store_schema_fingerprint_digest": STORE_V2_SCHEMA_FINGERPRINT_DIGEST,
    "coordinator_store_target_schema": 3,
    "coordinator_store_target_fingerprint_digest": STORE_V3_SCHEMA_FINGERPRINT_DIGEST,
}

CANARY_ATTRIBUTION_CONTRACT = {
    "schema": "agentflow-canary-attribution-contract-v2",
    "dependencies": DEPENDENCY_PINS,
    "store_modes": ["NoAdmission", "OperationalSafetyOnly", "OperationalSafetyAndCanary"],
    "transaction": "Store-owned BEGIN IMMEDIATE through successor commit",
    "context": ["stage_identity", "repository", "stage", "provider", "model",
                "route_cell_digest"],
    "authority_order": ["OperationalSafety", "CanaryAttribution"],
    "receipt_binding_domain": RECEIPT_BINDING_DOMAIN,
    "row_digest_domain": ROW_DIGEST_DOMAIN,
    "persistence": ["stage_identity", "repository", "route_cell_digest", "receipt_binding",
                    "method_revision", "cohort_id", "contract_version", "attribution_digest"],
    "writes": ["INSERT canary_attributions", "successor records write"],
    "immutability": "recursive delete/update triggers enabled and verified per Store connection",
    "refusal_codes": sorted(CANARY_ATTRIBUTION_REFUSAL_CODES),
}
CANARY_ATTRIBUTION_CONTRACT_V1_DIGEST = (
    "4c0ff263ee994228ffae0641a26959ca8f5f497285f800d0b7d980399e508157")
CANARY_ATTRIBUTION_CONTRACT_DIGEST = (
    "f7f64e3fb9a3913713d121d24af39c3f208d39b3cb6afb04b1457dd54b8d0d2f")
if _digest(CANARY_ATTRIBUTION_CONTRACT) != CANARY_ATTRIBUTION_CONTRACT_DIGEST:
    raise RuntimeError("CanaryAttribution contract changed")


def _binding_vector(revision: str, receipt_digest: str, cell_key: str,
                    route_digest: str, predecessor: str, *, receipt_id: str,
                    candidate_id: str, approval_id: str, policy_version: int,
                    repository: str, locator: str, scope: str,
                    disabled_generation: int, generation: int) -> dict[str, object]:
    return {
        "domain": RECEIPT_BINDING_DOMAIN,
        "receipt": {
            "receipt_id": receipt_id, "candidate_id": candidate_id,
            "approval_id": approval_id, "policy_version": policy_version,
            "authoritative": True,
            "authority": {
                "authority_kind": "github", "repository": repository, "locator": locator,
                "revision": revision, "content_hash_algorithm": "sha256",
                "content_hash": receipt_digest, "scope": scope,
                "approval_id": approval_id, "approved_revision": revision,
                "approved_hash": receipt_digest, "approved_scope": scope,
                "verifier_id": "github-authority", "verifier_version": "v1",
                "outcome": "verified",
            },
        },
        "active_declaration": {
            "cell_key": cell_key, "active_route_cell_digest": route_digest,
            "active_receipt_id": receipt_id, "active_receipt_digest": receipt_digest,
            "predecessor_route_cell_digest": predecessor,
            "disabled_generation": disabled_generation, "generation": generation,
        },
    }


CANARY_ATTRIBUTION_RECEIPT_BINDING_VECTORS = (
    {
        "name": "fleet",
        "source": _binding_vector(
            "a" * 40, "b" * 64, "c" * 64, "d" * 64, "e" * 64,
            receipt_id="receipt-candidate-alpha", candidate_id="candidate-alpha",
            approval_id="approval-641", policy_version=1,
            repository="octo/governance", locator="pulls/584/files/canary.json",
            scope="fleet-policy/0-to-1", disabled_generation=0, generation=1),
        "binding": "4c646f5570ebb5490786f6ce1aaff7920f0ead4bdd42b49c1523ff0c98536be4",
    },
    {
        "name": "repository-overlay",
        "source": _binding_vector(
            "f" * 40, "1" * 64, "2" * 64, "3" * 64, "4" * 64,
            receipt_id="receipt-candidate-overlay", candidate_id="candidate-overlay",
            approval_id="approval-overlay-641", policy_version=3,
            repository="octo/app", locator="pulls/641/files/.agentflow/overlay.json",
            scope="repository-policy/octo/app/2-to-3", disabled_generation=7, generation=8),
        "binding": "512d07706dc708b9f1293ec8ff04f707b02c10d6b570888459d60b9a5618e4be",
    },
)


class CanaryAttributionRefused(RuntimeError):
    """A closed, content-free refusal from the attribution owner."""

    def __init__(self, code: str) -> None:
        if code not in CANARY_ATTRIBUTION_REFUSAL_CODES:
            raise ValueError("unknown canary attribution refusal code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class CanaryAttribution:
    stage_identity: str
    repository: str
    route_cell_digest: str
    receipt_binding: str
    method_revision: str
    cohort_id: str
    contract_version: str
    attribution_digest: str


@dataclass(frozen=True, slots=True)
class _CanaryAdmissionResult:
    attribution: CanaryAttribution | None


class PromotionReceiptAuthority(Protocol):
    def read(self, receipt_id: str) -> PromotionReceipt: ...


def register_sql_functions(conn: sqlite3.Connection) -> None:
    conn.create_function(
        "canary_attribution_row_valid", 8, _schema_row_valid, deterministic=True)


def initialize_schema(conn: sqlite3.Connection, *, checkpoint=None) -> None:
    for name, statement in CANARY_ATTRIBUTION_SCHEMA_STATEMENTS:
        if checkpoint is not None:
            checkpoint(name + ":before")
        conn.execute(statement)
        if checkpoint is not None:
            checkpoint(name + ":after")


class CanaryAttributionAuthority:
    """Private Store-owned participant and committed-row reader."""

    def __init__(self, store: object, operational_safety: OperationalSafety,
                 promotion_receipts: PromotionReceiptAuthority) -> None:
        self._conn: sqlite3.Connection = getattr(store, "_conn")
        self._lock = getattr(store, "_lock")
        self._operational_safety = operational_safety
        self._promotion_receipts = promotion_receipts

    def _participate_in_admission(self, context: _AdmissionContext) -> _CanaryAdmissionResult:
        existing = self._row(context.stage_identity)
        if existing is not None:
            if (existing.repository != context.repository
                    or existing.route_cell_digest != context.route_cell_digest):
                raise CanaryAttributionRefused("conflicting_attribution")
            return _CanaryAdmissionResult(existing)
        try:
            state = self._operational_safety.canary_state(context.route_cell_digest)
        except Exception as error:
            raise CanaryAttributionRefused("unreadable_canary_state") from error
        if type(state) is not CanaryState:
            raise CanaryAttributionRefused("unreadable_canary_state")
        if state.active_route_cell_digest != context.route_cell_digest:
            raise CanaryAttributionRefused("wrong_binding")
        if state.active_receipt_id is None:
            return _CanaryAdmissionResult(None)
        receipt = self._receipt(state.active_receipt_id)
        method_revision = self._validate_receipt(receipt, state, context.repository)
        receipt_binding = _digest(_receipt_binding_source(receipt, state))
        facts = {
            "stage_identity": context.stage_identity,
            "repository": context.repository,
            "route_cell_digest": context.route_cell_digest,
            "receipt_binding": receipt_binding,
            "method_revision": method_revision,
            "cohort_id": state.cell_key,
            "contract_version": ATTRIBUTION_CONTRACT_VERSION,
        }
        attribution = CanaryAttribution(
            **facts, attribution_digest=_digest({"domain": ROW_DIGEST_DOMAIN, **facts}))
        validate_canary_attribution(attribution)
        try:
            self._conn.execute(
                "INSERT INTO canary_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                tuple(asdict(attribution).values()))
        except sqlite3.IntegrityError as error:
            winner = self._row(context.stage_identity)
            if winner is None or winner != attribution:
                raise CanaryAttributionRefused("conflicting_attribution") from error
            return _CanaryAdmissionResult(winner)
        return _CanaryAdmissionResult(attribution)

    def _read(self, stage_identity: str) -> CanaryAttribution | None:
        with self._lock:
            return self._row(stage_identity)

    def _row(self, stage_identity: str) -> CanaryAttribution | None:
        try:
            row = self._conn.execute(
                "SELECT stage_identity, repository, route_cell_digest, receipt_binding,"
                " method_revision, cohort_id, contract_version, attribution_digest"
                " FROM canary_attributions WHERE stage_identity = ?", (stage_identity,)).fetchone()
        except sqlite3.DatabaseError as error:
            raise CanaryAttributionRefused("corrupt_attribution") from error
        if row is None:
            return None
        try:
            attribution = CanaryAttribution(*row)
            validate_canary_attribution(attribution)
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
        if type(receipt) is not PromotionReceipt:
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
                or receipt.approval_id != authority.approval_id
                or authority.approved_revision != pointer.revision
                or authority.approved_hash != pointer.content_hash
                or authority.approved_scope != pointer.scope
                or authority.outcome != "verified"
                or _COMMIT_REVISION.fullmatch(authority.approved_revision) is None
                or _SHA256.fullmatch(state.cell_key) is None):
            raise CanaryAttributionRefused("wrong_binding")
        return authority.approved_revision


def _receipt_binding_source(receipt: PromotionReceipt, state: CanaryState) -> dict[str, object]:
    authority = receipt.authority
    if authority is None:
        raise CanaryAttributionRefused("wrong_binding")
    pointer = authority.pointer
    return {
        "domain": RECEIPT_BINDING_DOMAIN,
        "receipt": {
            "receipt_id": receipt.receipt_id,
            "candidate_id": receipt.candidate_id,
            "approval_id": receipt.approval_id,
            "policy_version": receipt.policy_version,
            "authoritative": receipt.authoritative,
            "authority": {
                "authority_kind": pointer.authority_kind,
                "repository": pointer.repository,
                "locator": pointer.locator,
                "revision": pointer.revision,
                "content_hash_algorithm": pointer.content_hash_algorithm,
                "content_hash": pointer.content_hash,
                "scope": pointer.scope,
                "approval_id": authority.approval_id,
                "approved_revision": authority.approved_revision,
                "approved_hash": authority.approved_hash,
                "approved_scope": authority.approved_scope,
                "verifier_id": authority.verifier_id,
                "verifier_version": authority.verifier_version,
                "outcome": authority.outcome,
            },
        },
        "active_declaration": {
            "cell_key": state.cell_key,
            "active_route_cell_digest": state.active_route_cell_digest,
            "active_receipt_id": state.active_receipt_id,
            "active_receipt_digest": state.active_receipt_digest,
            "predecessor_route_cell_digest": state.predecessor_route_cell_digest,
            "disabled_generation": state.disabled_generation,
            "generation": state.generation,
        },
    }


def validate_canary_attribution(value: object) -> CanaryAttribution:
    """Validate one content-free attribution row without reading or mutating its owner."""
    if type(value) is not CanaryAttribution:
        raise CanaryAttributionRefused("corrupt_attribution")
    try:
        _validate_attribution(value)
    except (TypeError, ValueError) as error:
        raise CanaryAttributionRefused("corrupt_attribution") from error
    return value


def _validate_attribution(value: CanaryAttribution) -> None:
    facts = asdict(value)
    digest = facts.pop("attribution_digest")
    if (not isinstance(value.stage_identity, str) or not value.stage_identity
            or not isinstance(value.repository, str) or not value.repository
            or _SHA256.fullmatch(value.route_cell_digest) is None
            or _SHA256.fullmatch(value.receipt_binding) is None
            or _COMMIT_REVISION.fullmatch(value.method_revision) is None
            or _SHA256.fullmatch(value.cohort_id) is None
            or value.contract_version != ATTRIBUTION_CONTRACT_VERSION
            or not isinstance(digest, str)
            or digest != _digest({"domain": ROW_DIGEST_DOMAIN, **facts})):
        raise CanaryAttributionRefused("corrupt_attribution")


def _schema_row_valid(*values: object) -> int:
    try:
        _validate_attribution(CanaryAttribution(*values))
    except (CanaryAttributionRefused, TypeError, ValueError):
        return 0
    return 1


if any(_digest(vector["source"]) != vector["binding"]
       for vector in CANARY_ATTRIBUTION_RECEIPT_BINDING_VECTORS):
    raise RuntimeError("CanaryAttribution receipt-binding vector changed")
