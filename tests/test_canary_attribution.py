"""The superseding #641 Store-owned canary-attribution contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict, replace
import inspect
import json
import re
import shutil
import sqlite3
import threading

import pytest

from agentflow.canary_attribution import (
    ATTRIBUTION_CONTRACT_VERSION,
    CANARY_ATTRIBUTION_CONTRACT,
    CANARY_ATTRIBUTION_CONTRACT_DIGEST,
    CANARY_ATTRIBUTION_RECEIPT_BINDING_VECTORS,
    CANARY_ATTRIBUTION_REFUSAL_CODES,
    CANARY_ATTRIBUTION_SCHEMA_FINGERPRINT,
    DEPENDENCY_PINS,
    ROW_DIGEST_DOMAIN,
    STORE_V2_SCHEMA_FINGERPRINT,
    STORE_V2_SCHEMA_FINGERPRINT_DIGEST,
    STORE_V3_SCHEMA_FINGERPRINT,
    STORE_V3_SCHEMA_FINGERPRINT_DIGEST,
    CanaryAttribution,
    CanaryAttributionAuthority,
    CanaryAttributionRefused,
    _canonical_bytes,
    _digest,
    _receipt_binding_source,
    _schema_row_valid,
    register_sql_functions,
)
from agentflow.coordinator.record import RUNNING, WAITING, Record, logical_stage_identity
from agentflow.coordinator.store import (
    AdmissionResult,
    NoAdmission,
    OperationalSafetyAndCanary,
    OperationalSafetyOnly,
    ReservationIntent,
    ReservationLimits,
    SCHEMA_VERSION,
    SUPERVISOR_WINDOW,
    SafetySources,
    Store,
    StoreUnavailable,
    V2_TO_V3_FAULT_OBSERVATIONS,
    _RECORDS_SCHEMA,
    _schema_fingerprint,
)
from agentflow.evidence import ApprovedAuthority, AuthorityPointer, EvidenceError, PromotionReceipt
from agentflow.operational_safety import (
    CanaryActivationRequest,
    CanaryState,
    OperationalSafety,
    SafetyRefused,
)


IDENTITY = logical_stage_identity("octo/app", "641", "build", None)


class Receipts:
    def __init__(self) -> None:
        self.values: dict[str, PromotionReceipt] = {}
        self.reads: list[str] = []
        self.failure: Exception | None = None

    def issue(self, request: CanaryActivationRequest, *,
              receipt_id: str | None = None, revision: str = "a" * 40,
              scope: str = "fleet-policy/0-to-1",
              verifier=("github-authority", "v1")) -> PromotionReceipt:
        receipt_id = receipt_id or request.promotion_receipt_id
        pointer = AuthorityPointer(
            "github", "octo/governance", "pulls/584/files/canary.json",
            revision, "sha256", request.digest, scope)
        approved = ApprovedAuthority(
            pointer, "approval-641", pointer.revision, pointer.content_hash,
            pointer.scope, verifier[0], verifier[1], "verified")
        receipt = PromotionReceipt(
            receipt_id, receipt_id.removeprefix("receipt-"), approved.approval_id,
            1 if scope == "fleet-policy/0-to-1" else 3, approved, True)
        self.values[receipt_id] = receipt
        return receipt

    def read(self, receipt_id: str) -> PromotionReceipt:
        self.reads.append(receipt_id)
        if self.failure is not None:
            raise self.failure
        return self.values[receipt_id]


def intent(identity=IDENTITY, *, revision=1, token=None, digest=None, now=1_000,
           generation="daemon-641", budget=5, limits=None):
    return ReservationIntent(
        identity, token, revision, now, generation, budget, limits, digest)


def seed(path, receipts: Receipts, *, with_receipt=True,
         receipt_id="receipt-candidate-alpha", record: Record | None = None):
    store = Store(path)
    safety = OperationalSafety(store, promotion_receipts=receipts)
    predecessor = safety.register_route_cell(
        "octo/app", "build", "codex", "gpt-5", "primary",
        {"model": "gpt-5", "effort": "medium", "timeout": 900})
    active = predecessor
    if with_receipt:
        active = safety.register_route_cell(
            "octo/app", "build", "codex", "gpt-5", "primary",
            {"model": "gpt-5", "effort": "high", "timeout": 900})
        request = CanaryActivationRequest(receipt_id, active.digest, predecessor.digest, 0)
        receipts.issue(request, receipt_id=receipt_id)
        safety.approve_canary(request)
    record = record or Record(
        IDENTITY, "build", "codex", 1, repo="octo/app", subject="641",
        model="gpt-5", state=WAITING)
    assert store.upsert(record)
    store.close()
    # Activation itself reads the #584 receipt. Admission assertions start from the public
    # Store call boundary and therefore count only reads caused by reservation.
    receipts.reads.clear()
    return active, record


def composed(path, receipts):
    sources = SafetySources(promotion_receipts=receipts)
    return Store(path, admission_mode=OperationalSafetyAndCanary(sources, receipts))


def make_v2(path, *, record=None):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(_RECORDS_SCHEMA)
    OperationalSafety.initialize_schema(conn)
    if record is not None:
        conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (
            record.identity, record.pool, record.state, record.demand, Store._encode(record)))
    conn.execute("PRAGMA user_version = 2")
    conn.execute("COMMIT")
    assert _schema_fingerprint(conn) == STORE_V2_SCHEMA_FINGERPRINT
    conn.close()


def test_contract_schema_pins_and_closed_interfaces_are_exact():
    assert SCHEMA_VERSION == 3
    assert STORE_V2_SCHEMA_FINGERPRINT_DIGEST == (
        "9039da12f2376a5078ae067bbe91bfc1b1bae5dffdc469d9ac7d7afbfb2ea05e")
    assert STORE_V3_SCHEMA_FINGERPRINT_DIGEST == (
        "135795b5c28ade801c7a2687eda89370e92d5bee5b16049baa4e17392cf0602b")
    assert CANARY_ATTRIBUTION_CONTRACT_DIGEST == (
        "993403fa31faf2445044bce73c9d94c8e693667dd998ad82eb4b6fca218820b6")
    assert _digest(STORE_V2_SCHEMA_FINGERPRINT) == STORE_V2_SCHEMA_FINGERPRINT_DIGEST
    assert _digest(STORE_V3_SCHEMA_FINGERPRINT) == STORE_V3_SCHEMA_FINGERPRINT_DIGEST
    assert _digest(CANARY_ATTRIBUTION_CONTRACT) == CANARY_ATTRIBUTION_CONTRACT_DIGEST
    assert DEPENDENCY_PINS == CANARY_ATTRIBUTION_CONTRACT["dependencies"]
    assert DEPENDENCY_PINS["issue_584_merge"] == (
        "ef08dd3d2f691aa154ddaa193e6161b559099396")
    assert DEPENDENCY_PINS["issue_585_merge"] == (
        "bd818fa1d65c92def671192464207e6bc3904a34")
    assert [field.name for field in inspect.signature(ReservationIntent).parameters.values()] == [
        "identity", "expected_launch_token", "expected_revision", "now",
        "daemon_generation", "budget", "limits", "route_cell_digest"]
    assert list(inspect.signature(Store.reserve).parameters) == ["self", "intent"]
    assert list(inspect.signature(Store.resolve_route_cell).parameters) == [
        "self", "stage_identity", "expected_revision", "route_id"]
    assert list(inspect.signature(Store.read_canary_attribution).parameters) == [
        "self", "stage_identity"]
    assert not hasattr(CanaryAttributionAuthority, "participate_in_admission")
    assert not hasattr(OperationalSafety, "participate_in_admission")
    assert CANARY_ATTRIBUTION_REFUSAL_CODES == {
        "unreadable_canary_state", "missing_receipt", "unreadable_receipt",
        "wrong_verifier", "wrong_scope", "wrong_binding", "corrupt_attribution",
        "conflicting_attribution"}


def test_store_modes_are_exact_frozen_values_and_unconfigured_delegation_refuses(tmp_path):
    with pytest.raises(FrozenInstanceError):
        NoAdmission().anything = True
    sources = SafetySources()
    with pytest.raises(FrozenInstanceError):
        sources.check_evidence = object()
    store = Store(tmp_path / "none.db")
    with pytest.raises(StoreUnavailable, match="route resolution is not configured"):
        store.resolve_route_cell(IDENTITY, 1, "primary")
    with pytest.raises(StoreUnavailable, match="canary attribution is not configured"):
        store.read_canary_attribution(IDENTITY)
    store.close()


def test_no_admission_returns_store_owned_ten_field_successor(tmp_path):
    path = tmp_path / "none.db"
    before = Record(
        IDENTITY, "build", "codex", 2, repo="octo/app", subject="641", target="abc",
        model="gpt-5", state=WAITING, start_fact="not_started", launch_token="old",
        family="55", process_alive=True, attempt_committed=True,
        daemon_generation="old-daemon", started_at=4, deadline=5,
        outcome="preserved", attempts=2, descendants={"child"})
    store = Store(path)
    assert store.upsert(before)
    durable_before = store.record_of(IDENTITY)
    result = store.reserve(intent(token="old", now=20, generation="new-daemon"))
    assert type(result) is AdmissionResult
    assert result.safety_state_id is None and result.canary_attribution is None
    successor = result.successor
    assert successor.state == RUNNING and successor.revision == 2
    assert successor.start_fact is None and re.fullmatch(r"[a-f0-9]{32}", successor.launch_token)
    assert successor.family is None and successor.process_alive is False
    assert successor.attempt_committed is False
    assert successor.daemon_generation == "new-daemon"
    assert successor.started_at == 20 and successor.deadline == 20 + SUPERVISOR_WINDOW
    changed = {"state", "revision", "start_fact", "launch_token", "family",
               "process_alive", "attempt_committed", "daemon_generation",
               "started_at", "deadline"}
    assert {key: value for key, value in asdict(successor).items() if key not in changed} == {
        key: value for key, value in asdict(durable_before).items() if key not in changed}
    assert store.record_of(IDENTITY) == successor
    store.close()


def test_composed_admission_is_safety_first_and_binds_durable_record(tmp_path, monkeypatch):
    path = tmp_path / "composed.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    calls = []
    safety_method = OperationalSafety._participate_in_admission
    canary_method = CanaryAttributionAuthority._participate_in_admission

    def safety(self, context):
        calls.append(("safety", context))
        return safety_method(self, context)

    def canary(self, context):
        calls.append(("canary", context))
        return canary_method(self, context)

    monkeypatch.setattr(OperationalSafety, "_participate_in_admission", safety)
    monkeypatch.setattr(CanaryAttributionAuthority, "_participate_in_admission", canary)
    store = composed(path, receipts)
    result = store.reserve(intent(digest=active.digest))
    assert result is not None and result.safety_state_id
    assert result.canary_attribution == store.read_canary_attribution(IDENTITY)
    assert [name for name, _ in calls] == ["safety", "canary"]
    context = calls[0][1]
    assert asdict(context) == {
        "stage_identity": IDENTITY, "repository": "octo/app", "stage": "build",
        "provider": "codex", "model": "gpt-5", "route_cell_digest": active.digest}
    store.close()


def test_missing_digest_and_stale_intent_stop_before_any_participant_or_write(tmp_path, monkeypatch):
    path = tmp_path / "stop.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    calls = []
    monkeypatch.setattr(OperationalSafety, "_participate_in_admission",
                        lambda *_: calls.append("safety"))
    store = composed(path, receipts)
    assert store.reserve(intent(digest=None)) is None
    assert store.reserve(intent(revision=0, digest=active.digest)) is None
    assert calls == [] and receipts.reads == []
    assert store.record_of(IDENTITY).state == WAITING
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    store.close()


def test_safety_record_to_route_binding_refuses_before_receipt_read(tmp_path):
    path = tmp_path / "binding.db"
    receipts = Receipts()
    record = Record(
        IDENTITY, "build", "claude", 1, repo="octo/app", subject="641",
        model="opus", state=WAITING)
    active, _ = seed(path, receipts, record=record)
    store = composed(path, receipts)
    with pytest.raises(SafetyRefused, match="durable record"):
        store.reserve(intent(digest=active.digest))
    assert receipts.reads == []
    assert store.record_of(IDENTITY).state == WAITING
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    store.close()


def test_active_cell_without_receipt_admits_without_attribution(tmp_path):
    path = tmp_path / "no-receipt.db"
    receipts = Receipts()
    active, _ = seed(path, receipts, with_receipt=False)
    store = composed(path, receipts)
    result = store.reserve(intent(digest=active.digest))
    assert result is not None and result.safety_state_id
    assert result.canary_attribution is None and receipts.reads == []
    assert store.read_canary_attribution(IDENTITY) is None
    store.close()


@pytest.mark.parametrize("failure,code", [
    (KeyError("lost"), "missing_receipt"),
    (EvidenceError("receipt storage unavailable"), "unreadable_receipt"),
])
def test_receipt_failure_rolls_back_successor_and_attribution(tmp_path, failure, code):
    path = tmp_path / (code + ".db")
    receipts = Receipts()
    active, _ = seed(path, receipts)
    receipts.failure = failure
    store = composed(path, receipts)
    with pytest.raises(CanaryAttributionRefused) as refused:
        store.reserve(intent(digest=active.digest))
    assert refused.value.code == code
    assert store.record_of(IDENTITY).state == WAITING
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("cutpoint", [
    "after-attribution-before-successor", "after-successor-before-commit"])
def test_precommit_crash_cutpoints_roll_back_both_rows(tmp_path, monkeypatch, cutpoint):
    path = tmp_path / (cutpoint + ".db")
    receipts = Receipts()
    active, _ = seed(path, receipts)

    def crash(name):
        if name == cutpoint:
            raise RuntimeError("crash")

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(crash))
    store = composed(path, receipts)
    with pytest.raises(RuntimeError, match="crash"):
        store.reserve(intent(digest=active.digest))
    assert store.record_of(IDENTITY).state == WAITING
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    store.close()


def test_commit_lost_ack_reopens_with_successor_and_attribution(tmp_path, monkeypatch):
    path = tmp_path / "lost-ack.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)

    def lost_ack(name):
        if name == "after-commit":
            raise RuntimeError("lost ack")

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lost_ack))
    store = composed(path, receipts)
    with pytest.raises(RuntimeError, match="lost ack"):
        store.reserve(intent(digest=active.digest))
    store.close()
    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lambda _name: None))
    receipts.failure = EvidenceError("source lost")
    reopened = composed(path, receipts)
    assert reopened.record_of(IDENTITY).state == RUNNING
    attribution = reopened.read_canary_attribution(IDENTITY)
    assert attribution is not None and attribution.route_cell_digest == active.digest
    assert receipts.reads == ["receipt-candidate-alpha"]
    reopened.close()


def test_receipt_id_is_opaque_and_only_its_canonical_binding_is_persisted(tmp_path):
    path = tmp_path / "opaque.db"
    receipts = Receipts()
    raw = "receipt-secret:token-123"
    active, _ = seed(path, receipts, receipt_id=raw)
    store = composed(path, receipts)
    result = store.reserve(intent(digest=active.digest))
    assert result is not None and result.canary_attribution is not None
    columns = [row[1] for row in store._conn.execute("PRAGMA table_info(canary_attributions)")]
    assert columns == ["stage_identity", "repository", "route_cell_digest", "receipt_binding",
                       "method_revision", "cohort_id", "contract_version",
                       "attribution_digest"]
    persisted = store._conn.execute("SELECT * FROM canary_attributions").fetchone()
    assert raw not in json.dumps(persisted)
    assert persisted[3] == result.canary_attribution.receipt_binding
    store.close()


def test_fixed_receipt_binding_vectors_are_executable_and_mutation_sensitive():
    expected = {
        "fleet": "4c646f5570ebb5490786f6ce1aaff7920f0ead4bdd42b49c1523ff0c98536be4",
        "repository-overlay":
            "512d07706dc708b9f1293ec8ff04f707b02c10d6b570888459d60b9a5618e4be"}
    for vector in CANARY_ATTRIBUTION_RECEIPT_BINDING_VECTORS:
        assert _digest(vector["source"]) == expected[vector["name"]] == vector["binding"]
        assert _canonical_bytes(vector["source"]) == json.dumps(
            vector["source"], sort_keys=True, separators=(",", ":"),
            ensure_ascii=True, allow_nan=False).encode()
        changed = json.loads(json.dumps(vector["source"]))
        changed["active_declaration"]["generation"] += 1
        assert _digest(changed) != vector["binding"]


def test_runtime_receipt_binding_uses_every_receipt_and_declaration_field():
    source = CANARY_ATTRIBUTION_RECEIPT_BINDING_VECTORS[0]["source"]
    receipt_source = source["receipt"]
    authority_source = receipt_source["authority"]
    pointer = AuthorityPointer(*(
        authority_source[name] for name in (
            "authority_kind", "repository", "locator", "revision",
            "content_hash_algorithm", "content_hash", "scope")))
    approved = ApprovedAuthority(
        pointer, authority_source["approval_id"], authority_source["approved_revision"],
        authority_source["approved_hash"], authority_source["approved_scope"],
        authority_source["verifier_id"], authority_source["verifier_version"],
        authority_source["outcome"])
    receipt = PromotionReceipt(
        receipt_source["receipt_id"], receipt_source["candidate_id"],
        receipt_source["approval_id"], receipt_source["policy_version"], approved,
        receipt_source["authoritative"])
    declaration = source["active_declaration"]
    state = CanaryState(
        declaration["cell_key"], declaration["active_route_cell_digest"],
        declaration["active_receipt_id"], declaration["active_receipt_digest"],
        declaration["predecessor_route_cell_digest"], declaration["disabled_generation"],
        declaration["generation"])
    assert _receipt_binding_source(receipt, state) == source


def test_schema_is_insertion_only_source_bound_and_content_free(tmp_path):
    path = tmp_path / "closed.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    result = store.reserve(intent(digest=active.digest))
    assert result is not None
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE canary_attributions SET cohort_id = ?",
                            ("f" * 64,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM canary_attributions")
    for values in (
        ("ignore-previous-instructions", "a" * 40, "b" * 64),
        ("a" * 64, "prompt/findings/provider/source", "b" * 64),
        ("a" * 64, "a" * 40, "secret:token-123"),
    ):
        fields = {
            "stage_identity": IDENTITY, "repository": "octo/app",
            "route_cell_digest": active.digest, "receipt_binding": values[0],
            "method_revision": values[1], "cohort_id": values[2],
            "contract_version": ATTRIBUTION_CONTRACT_VERSION}
        digest = _digest({"domain": ROW_DIGEST_DOMAIN, **fields})
        assert _schema_row_valid(*fields.values(), digest) == 0
    assert _schema_fingerprint(store._conn) == STORE_V3_SCHEMA_FINGERPRINT
    store.close()


def test_public_read_revalidates_a_tampered_row_without_external_reads(tmp_path):
    path = tmp_path / "tamper.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    assert store.reserve(intent(digest=active.digest)) is not None
    reads = list(receipts.reads)
    store._conn.create_function("canary_attribution_row_valid", 8, lambda *_: 1,
                                deterministic=True)
    store._conn.execute("DROP TRIGGER canary_attributions_no_update")
    store._conn.execute("UPDATE canary_attributions SET attribution_digest = ?",
                        ("f" * 64,))
    with pytest.raises(CanaryAttributionRefused) as refused:
        store.read_canary_attribution(IDENTITY)
    assert refused.value.code == "corrupt_attribution"
    assert receipts.reads == reads
    store.close()


def test_reservation_write_set_is_only_attribution_insert_and_successor_write(tmp_path):
    path = tmp_path / "writes.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    writes = []

    def authorizer(action, table, column, database, trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            writes.append((action, table))
        return sqlite3.SQLITE_OK

    store._conn.set_authorizer(authorizer)
    try:
        assert store.reserve(intent(digest=active.digest)) is not None
    finally:
        store._conn.set_authorizer(None)
    assert set(writes) == {
        (sqlite3.SQLITE_INSERT, "canary_attributions"),
        (sqlite3.SQLITE_INSERT, "records"),
        (sqlite3.SQLITE_UPDATE, "records")}
    store.close()


def test_two_composed_stores_racing_one_intent_publish_one_winner_row(tmp_path):
    path = tmp_path / "race.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    barrier = threading.Barrier(2)
    results = []
    errors = []
    lock = threading.Lock()

    def race():
        store = composed(path, receipts)
        try:
            barrier.wait()
            value = store.reserve(intent(digest=active.digest))
            with lock:
                results.append(value)
        except BaseException as error:
            with lock:
                errors.append(error)
        finally:
            store.close()

    threads = [threading.Thread(target=race) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sum(value is not None for value in results) == 1
    final = composed(path, receipts)
    assert final.record_of(IDENTITY).state == RUNNING
    assert final._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 1
    assert len(receipts.reads) == 1
    final.close()


def test_same_owned_instances_handle_admission_resolution_and_read(tmp_path, monkeypatch):
    path = tmp_path / "same.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    seen = {}
    safety_admit = OperationalSafety._participate_in_admission
    safety_resolve = OperationalSafety.resolve
    canary_admit = CanaryAttributionAuthority._participate_in_admission
    canary_read = CanaryAttributionAuthority._read

    def remember_safety_admit(self, context):
        seen["safety_admit"] = id(self)
        return safety_admit(self, context)

    def remember_safety_resolve(self, *args):
        seen["safety_resolve"] = id(self)
        return safety_resolve(self, *args)

    def remember_canary_admit(self, context):
        seen["canary_admit"] = id(self)
        return canary_admit(self, context)

    def remember_canary_read(self, identity):
        seen["canary_read"] = id(self)
        return canary_read(self, identity)

    monkeypatch.setattr(OperationalSafety, "_participate_in_admission", remember_safety_admit)
    monkeypatch.setattr(OperationalSafety, "resolve", remember_safety_resolve)
    monkeypatch.setattr(CanaryAttributionAuthority, "_participate_in_admission", remember_canary_admit)
    monkeypatch.setattr(CanaryAttributionAuthority, "_read", remember_canary_read)
    store = composed(path, receipts)
    store.resolve_route_cell(IDENTITY, 1, "primary")
    assert store.reserve(intent(digest=active.digest)) is not None
    store.read_canary_attribution(IDENTITY)
    assert seen["safety_admit"] == seen["safety_resolve"]
    assert seen["canary_admit"] == seen["canary_read"]
    store.close()


def test_direct_v2_to_v3_migration_preserves_records_and_has_zero_attributions(tmp_path):
    path = tmp_path / "migration.db"
    record = Record(
        IDENTITY, "build", "codex", 1, repo="octo/app", subject="641",
        model="gpt-5", state=WAITING, revision=7)
    make_v2(path, record=record)
    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert _schema_fingerprint(store._conn) == STORE_V3_SCHEMA_FINGERPRINT
    assert store.record_of(IDENTITY) == record
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("observation", V2_TO_V3_FAULT_OBSERVATIONS)
def test_every_declared_migration_fault_observation_is_atomic(tmp_path, monkeypatch, observation):
    path = tmp_path / (observation.replace(":", "-") + ".db")
    make_v2(path)

    def crash(name):
        if name == observation:
            raise RuntimeError("migration fault")

    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(crash))
    with pytest.raises(RuntimeError, match="migration fault"):
        Store(path)
    conn = sqlite3.connect(path, isolation_level=None)
    register_sql_functions(conn)
    if observation == "v2-to-v3:after-commit":
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
        assert _schema_fingerprint(conn) == STORE_V3_SCHEMA_FINGERPRINT
    else:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert _schema_fingerprint(conn) == STORE_V2_SCHEMA_FINGERPRINT
    conn.close()


def test_v2_migration_rejects_wrong_source_fingerprint_without_ddl(tmp_path):
    path = tmp_path / "wrong-source.db"
    make_v2(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE records ADD COLUMN forged TEXT")
    conn.commit()
    conn.close()
    with pytest.raises(StoreUnavailable, match="migration source"):
        Store(path)
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 2
    assert check.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='canary_attributions'").fetchone()[0] == 0
    check.close()
