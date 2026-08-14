"""Contract tests for the Store-owned canary attribution admission participant."""

from __future__ import annotations

from dataclasses import asdict, replace
import inspect
import sqlite3
import threading

import pytest

from agentflow.canary_attribution import (
    ATTRIBUTION_CONTRACT_VERSION,
    CANARY_ATTRIBUTION_CONTRACT,
    CANARY_ATTRIBUTION_CONTRACT_DIGEST,
    CANARY_ATTRIBUTION_REFUSAL_CODES,
    CANARY_ATTRIBUTION_SCHEMA,
    CANARY_ATTRIBUTION_VECTORS,
    DEPENDENCY_PINS,
    STORE_V2_SCHEMA_FINGERPRINT,
    STORE_V2_SCHEMA_FINGERPRINT_DIGEST,
    STORE_V3_SCHEMA_FINGERPRINT,
    STORE_V3_SCHEMA_FINGERPRINT_DIGEST,
    CanaryAttributionAuthority,
    CanaryAttributionRefused,
    _digest,
)
from agentflow.coordinator.record import Record
from agentflow.coordinator.store import (
    SCHEMA_VERSION,
    Store,
    StoreUnavailable,
    _RECORDS_SCHEMA,
    _expected_schema_fingerprint,
    _schema_fingerprint,
)
from agentflow.evidence import ApprovedAuthority, AuthorityPointer, EvidenceError, PromotionReceipt
from agentflow.operational_safety import (
    CanaryActivationRequest,
    OperationalSafety,
    OPERATIONAL_SAFETY_CONTRACT_DIGEST,
    ROUTE_CELL_CONTRACT_DIGEST,
)


SUBJECT_REVISION = "b" * 40
STAGE_IDENTITY = "octo/app|641|build|" + SUBJECT_REVISION


class Receipts:
    def __init__(self) -> None:
        self.receipts: dict[str, PromotionReceipt] = {}
        self.calls: list[str] = []
        self.unavailable = False

    def issue(self, request: CanaryActivationRequest, *,
              scope: str = "fleet-policy/0-to-1",
              revision: str = "a" * 40) -> PromotionReceipt:
        pointer = AuthorityPointer(
            "github", "octo/governance", "pulls/584/files/canary.json",
            revision, "sha256", request.digest, scope,
        )
        authority = ApprovedAuthority(
            pointer, "approval-641", pointer.revision, pointer.content_hash,
            pointer.scope, "github-authority", "v1", "verified",
        )
        receipt = PromotionReceipt(
            request.promotion_receipt_id, "candidate-641", authority.approval_id,
            1, authority, True,
        )
        self.receipts[receipt.receipt_id] = receipt
        return receipt

    def read(self, receipt_id: str) -> PromotionReceipt:
        self.calls.append(receipt_id)
        if self.unavailable:
            raise EvidenceError("receipt storage unavailable")
        return self.receipts[receipt_id]


def route(owner: OperationalSafety, *, effort: str):
    return owner.register_route_cell(
        "octo/app", "build", "codex", "gpt-5", "primary",
        {"model": "gpt-5", "effort": effort, "timeout": 900},
    )


def canary(tmp_path, *, scope: str = "fleet-policy/0-to-1"):
    store = Store(tmp_path / "coordinator.db")
    receipts = Receipts()
    safety = OperationalSafety(store, promotion_receipts=receipts)
    predecessor = route(safety, effort="high")
    active = route(safety, effort="medium")
    request = CanaryActivationRequest(
        "receipt-641", active.digest, predecessor.digest, 0)
    receipt = receipts.issue(request, scope=scope)
    state = safety.approve_canary(request)
    receipts.calls.clear()
    authority = CanaryAttributionAuthority(store, safety, receipts)
    return store, safety, receipts, predecessor, active, state, receipt, authority


def participate(store: Store, authority: CanaryAttributionAuthority,
                identity: str = STAGE_IDENTITY, repository: str = "octo/app",
                revision: str = SUBJECT_REVISION, route_digest: str = ""):
    store._conn.execute("BEGIN IMMEDIATE")
    try:
        value = authority.participate_in_admission(
            store._conn, identity, repository, revision, route_digest)
        store._conn.execute("COMMIT")
        return value
    except BaseException:
        if store._conn.in_transaction:
            store._conn.execute("ROLLBACK")
        raise


def exact_v2(path) -> tuple[Record, Record]:
    waiting = Record("waiting", "build", "codex", 1, state="waiting", revision=3)
    running = Record("running", "review", "claude", 2, state="running", revision=7)
    conn = sqlite3.connect(path)
    conn.execute(_RECORDS_SCHEMA)
    OperationalSafety.initialize_schema(conn)
    for record in (waiting, running):
        conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (
            record.identity, record.pool, record.state, record.demand, Store._encode(record)))
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    assert _schema_fingerprint(conn) == STORE_V2_SCHEMA_FINGERPRINT
    conn.close()
    return waiting, running


def test_contract_dependencies_vectors_schema_and_public_interface_are_exact():
    assert DEPENDENCY_PINS == {
        "issue_584_merge": "ef08dd3d2f691aa154ddaa193e6161b559099396",
        "evidence_schema": 4,
        "promotion_contract": "github-merged-pr-v1",
        "issue_584_evidence_blob": "abe7473358c646d85ebc2bb51ea0154fff89bb19",
        "issue_585_merge": "bd818fa1d65c92def671192464207e6bc3904a34",
        "issue_585_receipt_reader_blob": "02e7d525a4cba5c4cdd95e26143673ea186e5519",
        "promotion_verifier": "github-authority/v1",
        "route_cell_contract_digest": ROUTE_CELL_CONTRACT_DIGEST,
        "operational_safety_contract_digest": OPERATIONAL_SAFETY_CONTRACT_DIGEST,
        "coordinator_store_schema": 2,
        "coordinator_store_schema_fingerprint_digest":
            "9039da12f2376a5078ae067bbe91bfc1b1bae5dffdc469d9ac7d7afbfb2ea05e",
        "coordinator_store_target_schema": 3,
        "coordinator_store_target_fingerprint_digest":
            "040dd13aa1108cb0f896893870a2cd563007be1d1c03b3ced96b92ab9f31f355",
    }
    assert STORE_V2_SCHEMA_FINGERPRINT == _expected_schema_fingerprint(2)
    assert STORE_V2_SCHEMA_FINGERPRINT_DIGEST == (
        "9039da12f2376a5078ae067bbe91bfc1b1bae5dffdc469d9ac7d7afbfb2ea05e")
    assert STORE_V3_SCHEMA_FINGERPRINT == _expected_schema_fingerprint(3)
    assert STORE_V3_SCHEMA_FINGERPRINT_DIGEST == (
        "040dd13aa1108cb0f896893870a2cd563007be1d1c03b3ced96b92ab9f31f355")
    assert CANARY_ATTRIBUTION_CONTRACT["schema"] == ATTRIBUTION_CONTRACT_VERSION
    assert CANARY_ATTRIBUTION_CONTRACT_DIGEST == _digest(CANARY_ATTRIBUTION_CONTRACT) == (
        "745fcdf2d8b358d2cb3418541d8c39bcb04c81bb913f7425866ae97ab4df2d38")
    for vector in CANARY_ATTRIBUTION_VECTORS:
        assert vector["attribution"]["receipt_id"] == vector["receipt"]["receipt_id"]
        assert (vector["attribution"]["method_revision"]
                == vector["receipt"]["approved_revision"])
        assert vector["attribution"]["cohort_id"] == vector["active_route_cell"]["cell_key"]
    assert CANARY_ATTRIBUTION_REFUSAL_CODES == {
        "wrong_connection", "outside_transaction", "unreadable_canary_state",
        "missing_receipt", "unreadable_receipt", "wrong_verifier", "wrong_scope",
        "wrong_binding", "corrupt_attribution", "conflicting_attribution",
    }
    assert list(inspect.signature(
        CanaryAttributionAuthority.participate_in_admission).parameters) == [
            "self", "connection", "logical_stage_identity", "repository",
            "subject_revision", "route_cell_digest",
        ]
    assert set(vars(CanaryAttributionAuthority)) & {
        "permit", "reserve", "transition", "report", "rollback", "promote", "nominate",
    } == set()


@pytest.mark.parametrize("scope", [
    "fleet-policy/0-to-1", "repository-policy/octo/app/0-to-1",
])
def test_fresh_attribution_maps_exact_receipt_revision_and_active_cell_key(tmp_path, scope):
    store, _safety, receipts, _predecessor, active, state, receipt, authority = canary(
        tmp_path, scope=scope)

    value = participate(store, authority, route_digest=active.digest)

    assert value is not None
    assert value.receipt_id == state.active_receipt_id == receipt.receipt_id
    assert value.method_revision == receipt.authority.approved_revision
    assert value.cohort_id == state.cell_key == active.key
    assert value.contract_version == ATTRIBUTION_CONTRACT_VERSION
    assert value.attribution_digest == _digest({
        key: item for key, item in asdict(value).items() if key != "attribution_digest"})
    assert receipts.calls == [receipt.receipt_id]
    assert authority.read(STAGE_IDENTITY) == value
    store.close()


def test_no_active_canary_returns_none_and_historical_predecessor_is_refused_first(tmp_path):
    store = Store(tmp_path / "none.db")
    receipts = Receipts()
    safety = OperationalSafety(store, promotion_receipts=receipts)
    only = route(safety, effort="high")
    authority = CanaryAttributionAuthority(store, safety, receipts)
    assert participate(store, authority, route_digest=only.digest) is None
    assert authority.read(STAGE_IDENTITY) is None and receipts.calls == []
    store.close()

    (store, _safety, receipts, predecessor, _active, _state,
     _receipt, authority) = canary(tmp_path / "active")
    store._conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(CanaryAttributionRefused) as refused:
        authority.participate_in_admission(
            store._conn, STAGE_IDENTITY, "octo/app", SUBJECT_REVISION, predecessor.digest)
    assert refused.value.code == "wrong_binding"
    assert receipts.calls == []
    assert store._conn.execute("SELECT count(*) FROM canary_attributions").fetchone()[0] == 0
    store._conn.execute("ROLLBACK")
    store.close()


def test_exact_connection_open_transaction_and_operational_safety_binding_are_required(tmp_path):
    store, safety, receipts, _predecessor, active, _state, _receipt, authority = canary(tmp_path)
    with pytest.raises(CanaryAttributionRefused) as refused:
        authority.participate_in_admission(
            store._conn, STAGE_IDENTITY, "octo/app", SUBJECT_REVISION, active.digest)
    assert refused.value.code == "outside_transaction"

    other = Store(tmp_path / "other.db")
    other._conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(CanaryAttributionRefused) as refused:
        authority.participate_in_admission(
            other._conn, STAGE_IDENTITY, "octo/app", SUBJECT_REVISION, active.digest)
    assert refused.value.code == "wrong_connection"
    other._conn.execute("ROLLBACK")
    with pytest.raises(CanaryAttributionRefused) as refused:
        CanaryAttributionAuthority(other, safety, receipts)
    assert refused.value.code == "wrong_connection"
    other.close()
    store.close()


def test_caller_rollback_crash_and_lost_ack_replay_the_transaction_boundary(tmp_path):
    store, safety, receipts, _predecessor, active, _state, _receipt, authority = canary(tmp_path)
    store._conn.execute("BEGIN IMMEDIATE")
    inserted = authority.participate_in_admission(
        store._conn, STAGE_IDENTITY, "octo/app", SUBJECT_REVISION, active.digest)
    assert inserted is not None
    store._conn.execute("ROLLBACK")
    assert authority.read(STAGE_IDENTITY) is None

    store._conn.execute("BEGIN IMMEDIATE")
    committed = authority.participate_in_admission(
        store._conn, STAGE_IDENTITY, "octo/app", SUBJECT_REVISION, active.digest)
    store._conn.execute("COMMIT")
    # Simulated acknowledgement loss happens after the caller's commit. Recovery must not
    # consult either external authority and must return the committed bytes.
    receipts.calls.clear()
    receipts.unavailable = True
    safety.canary_state = lambda _digest: (_ for _ in ()).throw(RuntimeError("source lost"))
    replayed = participate(store, authority, route_digest=active.digest)
    assert replayed == committed and receipts.calls == []
    store.close()


@pytest.mark.parametrize("change", ["repository", "revision", "route"])
def test_replay_rejects_different_caller_owned_facts_without_external_reads(tmp_path, change):
    store, safety, receipts, predecessor, active, _state, _receipt, authority = canary(tmp_path)
    participate(store, authority, route_digest=active.digest)
    receipts.calls.clear()
    safety.canary_state = lambda _digest: (_ for _ in ()).throw(RuntimeError("must not read"))
    arguments = {"repository": "octo/app", "revision": SUBJECT_REVISION,
                 "route_digest": active.digest}
    arguments[change if change != "route" else "route_digest"] = {
        "repository": "octo/other", "revision": "c" * 40,
        "route": predecessor.digest,
    }[change]
    with pytest.raises(CanaryAttributionRefused) as refused:
        participate(store, authority, **arguments)
    assert refused.value.code == "conflicting_attribution" and receipts.calls == []
    store.close()


def test_two_connections_race_one_identical_committed_row(tmp_path):
    (seed, _seed_safety, receipts, _predecessor, active, _state,
     _receipt, _seed_authority) = canary(tmp_path)
    seed.close()
    # canary() uses this exact path; separate Store objects model separate coordinators.
    path = tmp_path / "coordinator.db"
    start = threading.Barrier(2)
    values = []
    errors = []
    lock = threading.Lock()

    def race():
        store = Store(path)
        safety = OperationalSafety(store)
        authority = CanaryAttributionAuthority(store, safety, receipts)
        try:
            start.wait()
            value = participate(store, authority, route_digest=active.digest)
            with lock:
                values.append(value)
        except BaseException as error:
            with lock:
                errors.append(error)
        finally:
            store.close()

    threads = [threading.Thread(target=race), threading.Thread(target=race)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = Store(path)
    assert errors == [] and len(values) == 2 and values[0] == values[1]
    assert final._conn.execute("SELECT count(*) FROM canary_attributions").fetchone()[0] == 1
    final.close()


@pytest.mark.parametrize("fault", [
    "missing", "unreadable", "wrong_verifier", "wrong_scope", "wrong_policy",
    "wrong_binding",
])
def test_receipt_refusals_are_closed_and_write_nothing(tmp_path, fault):
    store, _safety, receipts, _predecessor, active, _state, receipt, authority = canary(tmp_path)
    assert receipt.authority is not None
    if fault == "missing":
        receipts.receipts.clear()
        expected = "missing_receipt"
    elif fault == "unreadable":
        receipts.unavailable = True
        expected = "unreadable_receipt"
    elif fault == "wrong_policy":
        receipts.receipts[receipt.receipt_id] = replace(receipt, policy_version=2)
        expected = "wrong_scope"
    else:
        old = receipt.authority
        scope = ("repository-policy/octo/other/0-to-1"
                 if fault == "wrong_scope" else old.pointer.scope)
        content_hash = "c" * 64 if fault == "wrong_binding" else old.pointer.content_hash
        pointer = replace(old.pointer, scope=scope, content_hash=content_hash)
        approved = ApprovedAuthority(
            pointer, old.approval_id, pointer.revision, pointer.content_hash,
            pointer.scope,
            "other" if fault == "wrong_verifier" else old.verifier_id,
            old.verifier_version, "verified",
        )
        receipts.receipts[receipt.receipt_id] = replace(receipt, authority=approved)
        expected = fault
    store._conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(CanaryAttributionRefused) as refused:
        authority.participate_in_admission(
            store._conn, STAGE_IDENTITY, "octo/app", SUBJECT_REVISION, active.digest)
    assert refused.value.code == expected
    assert store._conn.execute("SELECT count(*) FROM canary_attributions").fetchone()[0] == 0
    store._conn.execute("ROLLBACK")
    store.close()


def test_unreadable_canary_state_refuses_without_receipt_or_write(tmp_path):
    store, safety, receipts, _predecessor, active, _state, _receipt, authority = canary(tmp_path)
    safety.canary_state = lambda _digest: (_ for _ in ()).throw(RuntimeError("corrupt"))
    store._conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(CanaryAttributionRefused) as refused:
        authority.participate_in_admission(
            store._conn, STAGE_IDENTITY, "octo/app", SUBJECT_REVISION, active.digest)
    assert refused.value.code == "unreadable_canary_state"
    assert receipts.calls == []
    assert store._conn.execute("SELECT count(*) FROM canary_attributions").fetchone()[0] == 0
    store._conn.execute("ROLLBACK")
    store.close()


@pytest.mark.parametrize("column", [
    "stage_identity", "repository", "subject_revision", "route_cell_digest", "receipt_id",
    "method_revision", "cohort_id", "contract_version", "attribution_digest",
])
def test_every_persisted_field_is_digest_verified_and_tamper_fails_closed(tmp_path, column):
    store, _safety, _receipts, _predecessor, active, _state, _receipt, authority = canary(tmp_path)
    value = participate(store, authority, route_digest=active.digest)
    assert value is not None
    replacement = STAGE_IDENTITY + "-tampered" if column == "stage_identity" else "tampered"
    store._conn.execute(
        f"UPDATE canary_attributions SET {column} = ? WHERE stage_identity = ?",
        (replacement, STAGE_IDENTITY),
    )
    lookup = replacement if column == "stage_identity" else STAGE_IDENTITY
    with pytest.raises(CanaryAttributionRefused) as refused:
        authority.read(lookup)
    assert refused.value.code == "corrupt_attribution"
    store.close()


def test_sql_authorizer_allows_only_attribution_insert_and_interfaces_touch_no_adapters(
        tmp_path, monkeypatch):
    store, _safety, receipts, _predecessor, active, _state, _receipt, authority = canary(tmp_path)
    writes = []

    def authorizer(action, arg1, _arg2, _db, _trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            writes.append((action, arg1))
            return sqlite3.SQLITE_OK if (action, arg1) == (
                sqlite3.SQLITE_INSERT, "canary_attributions") else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def denied(*_args, **_kwargs):
        pytest.fail("out-of-scope adapter was touched")

    store._conn.set_authorizer(authorizer)
    monkeypatch.setattr("builtins.open", denied)
    monkeypatch.setattr("pathlib.Path.write_text", denied)
    monkeypatch.setattr("agentflow.evidence.EvidenceStore.promote", denied)
    value = participate(store, authority, route_digest=active.digest)
    assert value is not None and writes == [(sqlite3.SQLITE_INSERT, "canary_attributions")]
    assert receipts.calls == ["receipt-641"]
    store._conn.set_authorizer(None)
    store.close()


def test_schema_is_closed_content_free_and_has_no_update_delete_surface(tmp_path):
    store = Store(tmp_path / "schema.db")
    columns = tuple(row[1] for row in store._conn.execute(
        "PRAGMA table_info(canary_attributions)"))
    assert columns == (
        "stage_identity", "repository", "subject_revision", "route_cell_digest",
        "receipt_id", "method_revision", "cohort_id", "contract_version",
        "attribution_digest",
    )
    assert store._conn.execute(
        "SELECT sql FROM sqlite_master WHERE name='canary_attributions'").fetchone()[0] == (
            CANARY_ATTRIBUTION_SCHEMA)
    assert store._conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='trigger' AND tbl_name="
        "'canary_attributions'").fetchone()[0] == 0
    with pytest.raises(sqlite3.OperationalError):
        store._conn.execute(
            "INSERT INTO canary_attributions VALUES (?,?,?,?,?,?,?,?,?,?)", ("prose",) * 10)
    store.close()


@pytest.mark.parametrize("identity,repository,revision", [
    ("stage identity contains prose", "octo/app", SUBJECT_REVISION),
    (STAGE_IDENTITY, "octo/app/extra", SUBJECT_REVISION),
    (STAGE_IDENTITY, "octo/app", "not-a-commit"),
])
def test_content_bearing_or_noncanonical_caller_facts_never_persist(
        tmp_path, identity, repository, revision):
    store, _safety, _receipts, _predecessor, active, _state, _receipt, authority = canary(
        tmp_path)
    store._conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(CanaryAttributionRefused):
        authority.participate_in_admission(
            store._conn, identity, repository, revision, active.digest)
    assert store._conn.execute("SELECT count(*) FROM canary_attributions").fetchone()[0] == 0
    store._conn.execute("ROLLBACK")
    store.close()


def test_exact_v2_migrates_to_complete_v3_without_rewriting_records_or_safety(tmp_path):
    path = tmp_path / "v2.db"
    waiting, running = exact_v2(path)
    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 3
    assert _schema_fingerprint(store._conn) == _expected_schema_fingerprint(3)
    assert store.record_of(waiting.identity) == waiting
    assert store.record_of(running.identity) == running
    assert store.permits_used("claude") == 2
    assert store._conn.execute("SELECT count(*) FROM safety_route_state").fetchone()[0] == 0
    assert store._conn.execute("SELECT count(*) FROM canary_attributions").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("checkpoint", [
    "v2-to-v3:begin",
    "v2-to-v3:create:canary_attributions",
    "v2-to-v3:verify:fingerprint",
    "v2-to-v3:set:user-version",
    "v2-to-v3:commit",
])
def test_every_precommit_v2_to_v3_fault_rolls_back_to_exact_v2(
        tmp_path, monkeypatch, checkpoint):
    path = tmp_path / f"{checkpoint.rsplit(':', 1)[-1]}.db"
    exact_v2(path)

    def fail(name):
        if name == checkpoint:
            raise RuntimeError(name)

    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match=checkpoint):
        Store(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _schema_fingerprint(conn) == STORE_V2_SCHEMA_FINGERPRINT
    assert conn.execute(
        "SELECT name FROM sqlite_master WHERE name='canary_attributions'").fetchone() is None
    conn.close()


def test_postcommit_migration_lost_ack_recovers_exact_v3(tmp_path, monkeypatch):
    path = tmp_path / "lost-ack.db"
    exact_v2(path)

    def fail(name):
        if name == "v2-to-v3:committed":
            raise RuntimeError(name)

    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match="committed"):
        Store(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert _schema_fingerprint(conn) == _expected_schema_fingerprint(3)
    conn.close()
    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(lambda _name: None))
    Store(path).close()


def test_tampered_v2_is_not_a_migration_source_and_target_mismatch_rolls_back(
        tmp_path, monkeypatch):
    source = tmp_path / "tampered-source.db"
    exact_v2(source)
    conn = sqlite3.connect(source)
    conn.execute("ALTER TABLE safety_actions ADD COLUMN attacker TEXT")
    conn.commit()
    conn.close()
    with pytest.raises(StoreUnavailable, match="migration source"):
        Store(source)
    check = sqlite3.connect(source)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 2
    assert check.execute(
        "SELECT name FROM sqlite_master WHERE name='canary_attributions'").fetchone() is None
    check.close()

    target = tmp_path / "tampered-target.db"
    exact_v2(target)

    def wrong_schema(conn):
        conn.execute(CANARY_ATTRIBUTION_SCHEMA)
        conn.execute("CREATE TABLE attacker (value TEXT)")

    monkeypatch.setattr("agentflow.canary_attribution.initialize_schema", wrong_schema)
    with pytest.raises(StoreUnavailable, match="cannot open continuation store"):
        Store(target)
    check = sqlite3.connect(target)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _schema_fingerprint(check) == STORE_V2_SCHEMA_FINGERPRINT
    check.close()
