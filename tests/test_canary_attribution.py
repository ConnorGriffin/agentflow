"""The superseding #641 Store-owned canary-attribution contract."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, asdict
import inspect
import json
import re
import sqlite3
import threading

import pytest

from agentflow.canary_attribution import (
    ATTRIBUTION_CONTRACT_VERSION,
    CANARY_ATTRIBUTION_CONTRACT,
    CANARY_ATTRIBUTION_CONTRACT_DIGEST,
    CANARY_ATTRIBUTION_CONTRACT_V1_DIGEST,
    CANARY_ATTRIBUTION_RECEIPT_BINDING_VECTORS,
    CANARY_ATTRIBUTION_REFUSAL_CODES,
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
)
from agentflow.coordinator.record import RUNNING, WAITING, Record
from agentflow.coordinator.store import (
    AdmissionReceipt,
    AdmissionRefused,
    AdmissionResult,
    LegacyReservationIntent,
    LegacyReservationResult,
    NoAdmission,
    OperationalSafetyAndCanary,
    RouteAdmissionRefused,
    ReservationIntent,
    SCHEMA_VERSION,
    STORE_V4_SCHEMA_FINGERPRINT,
    STORE_V4_SCHEMA_FINGERPRINT_DIGEST,
    SUPERVISOR_WINDOW,
    SafetySources,
    Store,
    StoreUnavailable,
    V3_TO_V4_FAULT_OBSERVATIONS,
    V2_TO_V3_FAULT_OBSERVATIONS,
    V4_ADMISSION_PRECOMMIT_CUTPOINTS,
    _RECORDS_SCHEMA,
    _schema_fingerprint,
)
from agentflow.capability_contracts import _ready_fact
from agentflow.effective_policy import NotApplicableBriefing, _finish
from agentflow.evidence import ApprovedAuthority, AuthorityPointer, EvidenceError, PromotionReceipt
from agentflow.operational_safety import (
    CanaryActivationRequest,
    CanaryState,
    LaunchConfigV1,
    OperationalSafety,
    RouteCell,
    SafetyRefused,
)


IDENTITY = "octo/app|641|build|-"
PRIMARY_ROUTE_ID = "production/build/deep/default"


def launch_config(effort: str, *, stage_profile_id="build/deep/default") -> LaunchConfigV1:
    return LaunchConfigV1(
        schema="agentflow-launch-v1",
        provider="codex",
        internal_model="gpt-5",
        cli_model="gpt-5",
        stage_profile_id=stage_profile_id,
        reasoning_effort=effort,
        turn_ceiling=64,
        wall_ceiling_s=900,
        build_lease=(8, 12, 20),
        allowed_tools=None,
        sandbox_policy="workspace-write",
        result_schema_json=None,
        result_schema_digest=None,
    )


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
           generation="daemon-641", budget=5, limits=None, provider="codex"):
    subject_revision = "a" * 40
    value = {
        "briefing_digest": "", "briefing_id": "", "reason": "stage_not_applicable",
        "repository": "octo/app", "schema": "briefing-v1", "stage": "build",
        "status": "not_applicable", "subject_revision": subject_revision,
    }
    briefing_digest, briefing_id, _ = _finish(value)
    return ReservationIntent(
        identity, token, revision, now, generation, budget, limits,
        NotApplicableBriefing(
            "octo/app", "build", subject_revision, briefing_digest, briefing_id),
        _ready_fact("build", provider, b"manifest", ()), digest)


def seed(path, receipts: Receipts, *, with_receipt=True,
         receipt_id="receipt-candidate-alpha", record: Record | None = None):
    store = Store(path)
    safety = OperationalSafety(store, promotion_receipts=receipts)
    predecessor = safety.register_route_cell(
        "octo/app", "build", "codex", "gpt-5", PRIMARY_ROUTE_ID,
        launch_config("medium"))
    active = predecessor
    if with_receipt:
        active = safety.register_route_cell(
            "octo/app", "build", "codex", "gpt-5", PRIMARY_ROUTE_ID,
            launch_config("high"))
        request = CanaryActivationRequest(receipt_id, active.digest, predecessor.digest, 0)
        receipts.issue(request, receipt_id=receipt_id)
        safety.approve_canary(request)
    record = record or Record(
        IDENTITY, "build", "codex", 1, repo="octo/app", subject="641",
        model="gpt-5", state=WAITING)
    record.subject_revision = record.subject_revision or "a" * 40
    record.route_id = record.route_id or PRIMARY_ROUTE_ID
    record.route_cell_digest = record.route_cell_digest or active.digest
    record.launch_config_digest = (
        record.launch_config_digest or active.launch_config_digest)
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
    records = [
        Record("legacy-a", "review", "claude", 2, state=WAITING, revision=3),
        Record("legacy-b", "build", "codex", 1, state=RUNNING, revision=8),
    ]
    if record is not None:
        records.append(record)
    records.sort(key=lambda item: item.identity)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(_RECORDS_SCHEMA)
    OperationalSafety.initialize_schema(conn)
    for item in records:
        conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (
            item.identity, item.pool, item.state, item.demand, Store._encode(item)))
    conn.execute("PRAGMA user_version = 2")
    conn.execute("COMMIT")
    config_bytes = b'{"effort":"high","model":"gpt-5","timeout":900}'
    config_digest = _digest(json.loads(config_bytes))
    body = {
        "repository": "octo/app", "stage": "build", "provider": "codex",
        "model": "gpt-5", "route_id": "migration",
        "launch_config_digest": config_digest,
    }
    body_text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    cell = RouteCell(**body, digest=_digest(body))
    legacy_key = _digest({
        "repository": cell.repository, "stage": cell.stage,
        "provider": cell.provider, "model": cell.model, "route_id": cell.route_id,
    })
    state_id = _digest({
        "cell_key": legacy_key, "active": cell.digest,
        "quarantined": None, "generation": 0,
    })
    conn.execute("INSERT INTO safety_launch_configs VALUES (?, ?)",
                 (config_digest, config_bytes))
    conn.execute("INSERT INTO safety_route_cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        cell.digest, legacy_key, cell.repository, cell.stage, cell.provider, cell.model,
        cell.route_id, cell.launch_config_digest, body_text))
    conn.execute("INSERT INTO safety_route_state VALUES (?, ?, NULL, NULL, ?, 0)",
                 (legacy_key, cell.digest, state_id))
    conn.execute("INSERT INTO safety_canary_state VALUES (?, ?, NULL, NULL, NULL, 0, 0)",
                 (legacy_key, cell.digest))
    assert _schema_fingerprint(conn) == STORE_V2_SCHEMA_FINGERPRINT
    conn.close()
    return tuple(records), cell


def assert_v2_snapshot(path, records, cell):
    conn = sqlite3.connect(path, isolation_level=None)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _schema_fingerprint(conn) == STORE_V2_SCHEMA_FINGERPRINT
    payloads = conn.execute("SELECT data FROM records ORDER BY identity").fetchall()
    assert tuple(Store._decode(row[0]) for row in payloads) == records
    assert conn.execute(
        "SELECT active_digest FROM safety_route_state ORDER BY cell_key").fetchall() == [
            (cell.digest,)]
    assert conn.execute(
        "SELECT active_digest FROM safety_canary_state ORDER BY cell_key").fetchall() == [
            (cell.digest,)]
    conn.close()


def test_contract_schema_pins_and_closed_interfaces_are_exact():
    assert SCHEMA_VERSION == 4
    assert STORE_V2_SCHEMA_FINGERPRINT_DIGEST == (
        "9039da12f2376a5078ae067bbe91bfc1b1bae5dffdc469d9ac7d7afbfb2ea05e")
    assert STORE_V3_SCHEMA_FINGERPRINT_DIGEST == (
        "3a51988512b246ec34c469fc469b63cbcdabaf5d537c9a8552ae7c75d127bda5")
    assert STORE_V4_SCHEMA_FINGERPRINT_DIGEST == (
        "a2dd624722d0d4cbe93ffcf381f4de5cf6f52db1ebaa307453f51ede90986f7b")
    assert CANARY_ATTRIBUTION_CONTRACT_V1_DIGEST == (
        "4c0ff263ee994228ffae0641a26959ca8f5f497285f800d0b7d980399e508157")
    assert CANARY_ATTRIBUTION_CONTRACT_DIGEST == (
        "f7f64e3fb9a3913713d121d24af39c3f208d39b3cb6afb04b1457dd54b8d0d2f")
    assert _digest(STORE_V2_SCHEMA_FINGERPRINT) == STORE_V2_SCHEMA_FINGERPRINT_DIGEST
    assert _digest(STORE_V3_SCHEMA_FINGERPRINT) == STORE_V3_SCHEMA_FINGERPRINT_DIGEST
    assert _digest(STORE_V4_SCHEMA_FINGERPRINT) == STORE_V4_SCHEMA_FINGERPRINT_DIGEST
    assert _digest(CANARY_ATTRIBUTION_CONTRACT) == CANARY_ATTRIBUTION_CONTRACT_DIGEST
    assert DEPENDENCY_PINS == CANARY_ATTRIBUTION_CONTRACT["dependencies"]
    assert DEPENDENCY_PINS["issue_584_merge"] == (
        "ef08dd3d2f691aa154ddaa193e6161b559099396")
    assert DEPENDENCY_PINS["issue_585_merge"] == (
        "bd818fa1d65c92def671192464207e6bc3904a34")
    assert [field.name for field in inspect.signature(ReservationIntent).parameters.values()] == [
        "identity", "expected_launch_token", "expected_revision", "now",
        "daemon_generation", "budget", "limits", "briefing", "capability",
        "route_cell_digest"]
    assert list(inspect.signature(Store.reserve).parameters) == ["self", "intent"]
    assert list(inspect.signature(Store.reserve_legacy).parameters) == ["self", "intent"]
    assert all(parameter.default is inspect.Parameter.empty for parameter in
               inspect.signature(ReservationIntent).parameters.values())
    assert list(AdmissionResult.__dataclass_fields__) == [
        "successor", "admission_receipt", "safety_state_id", "canary_attribution"]
    assert "admitted_launch" not in AdmissionResult.__dataclass_fields__
    assert list(inspect.signature(Store.resolve_admitted_launch).parameters) == [
        "self", "stage_identity", "expected_revision", "route_id"]
    assert list(inspect.signature(Store.read_canary_attribution).parameters) == [
        "self", "stage_identity"]
    assert list(inspect.signature(Store.read_admission_receipt).parameters) == [
        "self", "stage_identity"]
    assert not hasattr(CanaryAttributionAuthority, "participate_in_admission")
    assert not hasattr(OperationalSafety, "participate_in_admission")
    assert not hasattr(OperationalSafety, "validate_admission_history")
    assert not hasattr(Store, "prune_admission_receipts")
    assert not hasattr(OperationalSafety, "prune_admission_history")
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
    with pytest.raises(RouteAdmissionRefused) as refused:
        store.resolve_admitted_launch(IDENTITY, 1, PRIMARY_ROUTE_ID)
    assert refused.value.code == "unreadable"
    with pytest.raises(StoreUnavailable, match="canary attribution is not configured"):
        store.read_canary_attribution(IDENTITY)
    store.close()


def test_legacy_waiting_record_requires_identity_migration_without_inference(tmp_path):
    path = tmp_path / "legacy-waiting.db"
    receipts = Receipts()
    active, record = seed(path, receipts)
    legacy = Store(path)
    current = legacy.record_of(record.identity)
    current.subject_revision = ""
    current.route_id = ""
    current.route_cell_digest = ""
    current.launch_config_digest = ""
    assert legacy.upsert(current)
    legacy.close()
    store = composed(path, receipts)
    with pytest.raises(AdmissionRefused) as refused:
        store.reserve(intent(revision=current.revision, digest=active.digest))
    assert refused.value.code == "admission_identity_migration_required"
    assert store.record_of(record.identity).state == WAITING
    assert store.read_admission_receipt(record.identity) is None
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
    result = store.reserve_legacy(LegacyReservationIntent(
        IDENTITY, "old", 1, 20, "new-daemon", 5, None, None))
    assert type(result) is LegacyReservationResult
    assert result.safety_state_id is None
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


def test_safety_refusal_stops_before_canary_attribution(tmp_path, monkeypatch):
    path = tmp_path / "safety-first-refusal.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    calls = []

    def refuse(_self, _context):
        calls.append("safety")
        raise SafetyRefused("blocked")

    def must_not_attribute(_self, _context):
        calls.append("attribution")
        raise AssertionError("Safety refusal must stop attribution")

    monkeypatch.setattr(OperationalSafety, "_participate_in_admission", refuse)
    monkeypatch.setattr(
        CanaryAttributionAuthority, "_participate_in_admission", must_not_attribute)
    store = composed(path, receipts)
    with pytest.raises(AdmissionRefused) as refused:
        store.reserve(intent(digest=active.digest))
    assert refused.value.code == "safety_refused"
    assert calls == ["safety"]
    assert store.record_of(IDENTITY).state == WAITING
    assert store.permits_used("codex") == 0
    assert store.read_admission_receipt(IDENTITY) is None
    assert store.read_canary_attribution(IDENTITY) is None
    store.close()


def test_missing_digest_and_stale_intent_stop_before_any_participant_or_write(tmp_path, monkeypatch):
    path = tmp_path / "stop.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    calls = []
    monkeypatch.setattr(OperationalSafety, "_participate_in_admission",
                        lambda *_: calls.append("safety"))
    store = composed(path, receipts)
    with pytest.raises(AdmissionRefused) as refused:
        store.reserve(intent(digest=None))
    assert refused.value.code == "route_cell:mismatched"
    assert store.reserve(intent(revision=0, digest=active.digest)) is None
    assert store.reserve(intent(token="stale-token", digest=active.digest)) is None
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
    with pytest.raises(AdmissionRefused) as refused:
        store.reserve(intent(digest=active.digest, provider="claude"))
    assert refused.value.code == "route_cell:missing"
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
    with pytest.raises(AdmissionRefused) as refused:
        store.reserve(intent(digest=active.digest))
    assert refused.value.code == "safety_refused"
    assert not store._admission_callback_active.is_set()
    assert store.record_of(IDENTITY).state == WAITING
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    store.close()


def test_hostile_receipt_reader_cannot_reenter_store_mutations_or_split_commit(tmp_path):
    path = tmp_path / "reentrant-reader.db"

    class HostileReceipts(Receipts):
        store = None

        def __init__(self):
            super().__init__()
            self.refusals = []

        def read(self, receipt_id):
            if self.store is not None:
                attacker = Record("attacker", "build", "codex", 1, state=WAITING)
                for operation in (
                    lambda: self.store.upsert(attacker),
                    lambda: self.store.reserve(intent(digest=active.digest)),
                ):
                    try:
                        operation()
                    except StoreUnavailable as error:
                        self.refusals.append(str(error))
            return super().read(receipt_id)

    receipts = HostileReceipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    receipts.store = store
    result = store.reserve(intent(digest=active.digest))
    assert type(result) is AdmissionResult
    assert receipts.refusals == [
        "reentrant Store mutation during admission",
        "reentrant Store mutation during admission",
    ]
    assert store.record_of("attacker") is None
    assert store.record_of(IDENTITY) == result.successor
    assert store.read_canary_attribution(IDENTITY) == result.canary_attribution
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 1
    assert not store._conn.in_transaction
    store.close()


def test_hostile_receipt_reader_threads_cannot_close_or_mutate_active_store(tmp_path):
    path = tmp_path / "threaded-reentrant-reader.db"

    class ThreadedHostileReceipts(Receipts):
        store = None

        def __init__(self):
            super().__init__()
            self.refusals = []

        def attack(self, operation):
            errors = []

            def run():
                try:
                    operation()
                except BaseException as error:
                    errors.append(error)

            thread = threading.Thread(target=run)
            thread.start()
            thread.join(1)
            assert not thread.is_alive()
            assert len(errors) == 1 and type(errors[0]) is StoreUnavailable
            self.refusals.append(str(errors[0]))

        def read(self, receipt_id):
            if self.store is not None:
                self.attack(self.store.close)
                self.attack(lambda: self.store.upsert(
                    Record("thread-attacker", "build", "codex", 1, state=WAITING)))
            return super().read(receipt_id)

    receipts = ThreadedHostileReceipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    receipts.store = store
    result = store.reserve(intent(digest=active.digest))
    assert type(result) is AdmissionResult
    assert receipts.refusals == [
        "reentrant Store mutation during admission",
        "reentrant Store mutation during admission",
    ]
    assert store.record_of("thread-attacker") is None
    assert store.record_of(IDENTITY) == result.successor
    assert store.read_canary_attribution(IDENTITY) == result.canary_attribution
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 1
    assert store.permits_used("codex") == 1
    assert not store._conn.in_transaction
    store.close()


@pytest.mark.parametrize("boundary", ("checkpoint", "safety", "attribution", "receipt"))
def test_every_in_transaction_callback_refuses_mutation_and_rolls_back_all_outputs(
        tmp_path, monkeypatch, boundary):
    path = tmp_path / f"reentrant-{boundary}.db"

    class ReentrantReceipts(Receipts):
        refusal = None
        mutation = None

        def read(self, receipt_id):
            if boundary == "receipt" and self.mutation is not None:
                try:
                    self.mutation()
                except StoreUnavailable as error:
                    self.refusal = error
                    raise
            return super().read(receipt_id)

    receipts = ReentrantReceipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)

    def reentrant_mutation():
        store.consume_admitted_launch(
            IDENTITY, 1, PRIMARY_ROUTE_ID,
            reserve=lambda _admitted: pytest.fail("reentrant reserve callback ran"))

    receipts.mutation = reentrant_mutation

    def checkpoint(name):
        if boundary == "checkpoint" and name == "after-safety":
            reentrant_mutation()

    def hostile_safety(_self, _context):
        reentrant_mutation()

    def hostile_attribution(_self, _context):
        reentrant_mutation()

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(checkpoint))
    if boundary == "safety":
        monkeypatch.setattr(OperationalSafety, "_participate_in_admission", hostile_safety)
    if boundary == "attribution":
        monkeypatch.setattr(
            CanaryAttributionAuthority, "_participate_in_admission", hostile_attribution)

    with pytest.raises((StoreUnavailable, AdmissionRefused)) as refused:
        store.reserve(intent(digest=active.digest))
    if boundary == "receipt":
        assert type(receipts.refusal) is StoreUnavailable
    else:
        assert "reentrant Store mutation during admission" in str(refused.value)
    assert store.record_of(IDENTITY).state == WAITING
    assert store.permits_used("codex") == 0
    assert store.read_admission_receipt(IDENTITY) is None
    assert store.read_canary_attribution(IDENTITY) is None
    store.close()


@pytest.mark.parametrize("cutpoint", V4_ADMISSION_PRECOMMIT_CUTPOINTS)
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
    store.close()
    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lambda _name: None))
    reopened = composed(path, receipts)
    assert reopened.record_of(IDENTITY).state == WAITING
    assert reopened._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    assert reopened._conn.execute("SELECT COUNT(*) FROM admission_receipts").fetchone()[0] == 0
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM safety_admission_history").fetchone()[0] == 0
    reopened.close()


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
    receipt = reopened.read_admission_receipt(IDENTITY)
    assert receipt is not None and receipt.route_cell_digest == active.digest
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM safety_admission_history").fetchone()[0] == 1
    assert receipts.reads == ["receipt-candidate-alpha"]
    reopened.close()


def test_admission_receipt_and_safety_history_are_insert_only_across_connections_and_reopen(
        tmp_path):
    path = tmp_path / "admission-receipt.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    result = store.reserve(intent(digest=active.digest))
    assert result is not None and result.admission_receipt == store.read_admission_receipt(IDENTITY)
    assert [row[1] for row in store._conn.execute(
        "PRAGMA table_info(admission_receipts)")] == [
            "stage_identity", "subject_revision", "briefing_id", "briefing_digest",
            "capability_id", "capability_digest", "route_id", "route_cell_digest",
            "launch_config_digest", "safety_state_id", "receipt_digest"]
    assert [row[1] for row in store._conn.execute(
        "PRAGMA table_info(safety_admission_history)")] == [
            "stage_identity", "route_cell_digest", "safety_state_id", "history_digest"]
    receipt_row = store._conn.execute(
        "SELECT * FROM admission_receipts WHERE stage_identity = ?", (IDENTITY,)
    ).fetchone()
    assert receipt_row[-1] == _digest({
        "stage_identity": IDENTITY,
        **asdict(result.admission_receipt),
    })
    history_row = store._conn.execute(
        "SELECT * FROM safety_admission_history WHERE stage_identity = ?", (IDENTITY,)
    ).fetchone()
    assert history_row[-1] == _digest({
        "stage_identity": IDENTITY,
        "route_cell_digest": result.admission_receipt.route_cell_digest,
        "safety_state_id": result.admission_receipt.safety_state_id,
    })
    peer = composed(path, receipts)
    assert peer.read_admission_receipt(IDENTITY) == result.admission_receipt
    peer.close()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "UPDATE admission_receipts SET route_cell_digest = ? WHERE stage_identity = ?",
            ("f" * 64, IDENTITY))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM admission_receipts WHERE stage_identity = ?", (IDENTITY,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "UPDATE safety_admission_history SET safety_state_id = ? WHERE stage_identity = ?",
            ("f" * 64, IDENTITY))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "DELETE FROM safety_admission_history WHERE stage_identity = ?", (IDENTITY,))
    store.close()
    reopened = composed(path, receipts)
    assert reopened.read_admission_receipt(IDENTITY) == result.admission_receipt
    reopened.close()


@pytest.mark.parametrize(("column", "value"), (
    ("safety_state_id", "d" * 64),
    ("receipt_digest", "e" * 64),
))
def test_public_receipt_read_fails_closed_on_second_connection_forgery(
        tmp_path, column, value):
    path = tmp_path / "forged-receipt.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    assert store.reserve(intent(digest=active.digest)) is not None

    attacker = sqlite3.connect(path)
    attacker.execute("DROP TRIGGER admission_receipts_no_update")
    attacker.execute(
        f"UPDATE admission_receipts SET {column} = ? WHERE stage_identity = ?",
        (value, IDENTITY))
    attacker.commit()
    tables = ("records", "admission_receipts", "canary_attributions", "safety_route_state")
    before = tuple(tuple(attacker.execute(f"SELECT * FROM {table}").fetchall())
                   for table in tables)
    attacker.close()

    with pytest.raises(StoreUnavailable) as unreadable:
        store.read_admission_receipt(IDENTITY)
    assert str(unreadable.value) == "admission receipt is unreadable"
    auditor = sqlite3.connect(path)
    after = tuple(tuple(auditor.execute(f"SELECT * FROM {table}").fetchall())
                  for table in tables)
    auditor.close()
    assert after == before
    store.close()


@pytest.mark.parametrize("corruption", ("missing", "digest", "mismatch"))
def test_public_receipt_read_fails_closed_on_safety_history_corruption(
        tmp_path, corruption):
    path = tmp_path / f"forged-safety-history-{corruption}.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    assert store.reserve(intent(digest=active.digest)) is not None

    attacker = sqlite3.connect(path)
    if corruption == "missing":
        attacker.execute("DROP TRIGGER safety_admission_history_no_delete")
        attacker.execute(
            "DELETE FROM safety_admission_history WHERE stage_identity = ?", (IDENTITY,))
    else:
        attacker.execute("DROP TRIGGER safety_admission_history_no_update")
        if corruption == "digest":
            attacker.execute(
                "UPDATE safety_admission_history SET history_digest = ? "
                "WHERE stage_identity = ?", ("f" * 64, IDENTITY))
        else:
            forged_state = "f" * 64
            forged_digest = _digest({
                "stage_identity": IDENTITY,
                "route_cell_digest": active.digest,
                "safety_state_id": forged_state,
            })
            attacker.execute(
                "UPDATE safety_admission_history SET safety_state_id = ?, history_digest = ? "
                "WHERE stage_identity = ?", (forged_state, forged_digest, IDENTITY))
    attacker.commit()
    before = tuple(attacker.execute(
        "SELECT * FROM safety_admission_history").fetchall())
    attacker.close()

    with pytest.raises(StoreUnavailable, match="admission receipt is unreadable"):
        store.read_admission_receipt(IDENTITY)
    auditor = sqlite3.connect(path)
    after = tuple(auditor.execute(
        "SELECT * FROM safety_admission_history").fetchall())
    auditor.close()
    assert after == before
    store.close()


@pytest.mark.parametrize("corruption", ("record-json", "record-utf8", "receipt-table"))
def test_public_receipt_read_closes_json_and_database_corruption(
        tmp_path, corruption):
    path = tmp_path / f"corrupt-receipt-read-{corruption}.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    assert store.reserve(intent(digest=active.digest)) is not None

    attacker = sqlite3.connect(path)
    if corruption == "record-json":
        attacker.execute("UPDATE records SET data = '{' WHERE identity = ?", (IDENTITY,))
    elif corruption == "record-utf8":
        attacker.execute(
            "UPDATE records SET data = CAST(X'FF' AS BLOB) WHERE identity = ?", (IDENTITY,))
    else:
        attacker.execute("DROP TABLE admission_receipts")
    attacker.commit()
    attacker.close()

    with pytest.raises(StoreUnavailable) as unreadable:
        store.read_admission_receipt(IDENTITY)
    assert str(unreadable.value) == "admission receipt is unreadable"
    store.close()


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


def test_schema_is_row_closed_and_insert_or_replace_cannot_delete_original(tmp_path):
    path = tmp_path / "closed.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    result = store.reserve(intent(digest=active.digest))
    assert result is not None
    assert store._conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    original = store._conn.execute("SELECT * FROM canary_attributions").fetchone()
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("UPDATE canary_attributions SET cohort_id = ?",
                            ("f" * 64,))
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute("DELETE FROM canary_attributions")
    replacement = asdict(result.canary_attribution)
    replacement["receipt_binding"] = "f" * 64
    replacement["attribution_digest"] = _digest({
        "domain": ROW_DIGEST_DOMAIN,
        **{name: value for name, value in replacement.items()
           if name != "attribution_digest"},
    })
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store._conn.execute(
            "INSERT OR REPLACE INTO canary_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(replacement.values()))
    assert store._conn.execute("SELECT * FROM canary_attributions").fetchone() == original
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
    assert _schema_fingerprint(store._conn) == STORE_V4_SCHEMA_FINGERPRINT
    store.close()
    reopened = composed(path, receipts)
    assert reopened._conn.execute("PRAGMA recursive_triggers").fetchone()[0] == 1
    assert reopened._conn.execute("SELECT * FROM canary_attributions").fetchone() == original
    reopened.close()


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


def test_public_read_is_row_only_when_records_data_changes(tmp_path):
    path = tmp_path / "row-only.db"
    receipts = Receipts()
    active, _ = seed(path, receipts)
    store = composed(path, receipts)
    result = store.reserve(intent(digest=active.digest))
    assert result is not None and result.canary_attribution is not None
    reads = list(receipts.reads)
    store._conn.execute(
        "UPDATE records SET data = ? WHERE identity = ?",
        ('{"mutable":"records authority removed"}', IDENTITY))
    assert store.read_canary_attribution(IDENTITY) == result.canary_attribution
    assert receipts.reads == reads
    store.close()


def test_reservation_write_set_is_only_receipt_attribution_and_successor_write(tmp_path):
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
        (sqlite3.SQLITE_INSERT, "admission_receipts"),
        (sqlite3.SQLITE_INSERT, "safety_admission_history"),
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
    assert final._conn.execute("SELECT COUNT(*) FROM admission_receipts").fetchone()[0] == 1
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

    def remember_safety_resolve(self, *args, **kwargs):
        seen["safety_resolve"] = id(self)
        return safety_resolve(self, *args, **kwargs)

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
    store.resolve_admitted_launch(IDENTITY, 1, PRIMARY_ROUTE_ID)
    assert store.reserve(intent(digest=active.digest)) is not None
    store.read_canary_attribution(IDENTITY)
    assert seen["safety_admit"] == seen["safety_resolve"]
    assert seen["canary_admit"] == seen["canary_read"]
    store.close()


def test_direct_v2_to_v4_migration_preserves_records_and_has_zero_receipts(tmp_path):
    path = tmp_path / "migration.db"
    record = Record(
        IDENTITY, "build", "codex", 1, repo="octo/app", subject="641",
        model="gpt-5", state=WAITING, revision=7)
    records, cell = make_v2(path, record=record)
    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _schema_fingerprint(store._conn) == STORE_V4_SCHEMA_FINGERPRINT
    assert tuple(store.load()[item.identity] for item in records) == records
    with pytest.raises(SafetyRefused, match="requires operator reconciliation"):
        OperationalSafety(store)
    assert store._conn.execute(
        "SELECT active_digest FROM safety_route_state").fetchone()[0] == cell.digest
    assert store._conn.execute("SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM admission_receipts").fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM safety_admission_history").fetchone()[0] == 0
    store.close()


def test_current_v3_schema_migrates_directly_to_exact_v4_without_record_rewrite(tmp_path):
    path = tmp_path / "v3-to-v4.db"
    record = Record(
        IDENTITY, "review", "codex", 1, repo="octo/app", subject="646",
        model="gpt-5", state=RUNNING, revision=9, outcome="historical")
    records, _cell = make_v2(path, record=record)
    conn = Store._open(path)
    Store._migrate_v2_to_v3(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert _schema_fingerprint(conn) == STORE_V3_SCHEMA_FINGERPRINT
    before = conn.execute("SELECT data FROM records ORDER BY identity").fetchall()
    conn.close()

    store = Store(path)
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _schema_fingerprint(store._conn) == STORE_V4_SCHEMA_FINGERPRINT
    assert store._conn.execute("SELECT data FROM records ORDER BY identity").fetchall() == before
    assert tuple(store.load()[item.identity] for item in records) == records
    assert store.read_admission_receipt(IDENTITY) is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM safety_admission_history").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("observation", V3_TO_V4_FAULT_OBSERVATIONS)
def test_every_v3_to_v4_fault_observation_is_atomic(tmp_path, monkeypatch, observation):
    path = tmp_path / (observation.replace(":", "-") + ".db")
    records, _cell = make_v2(path)
    conn = Store._open(path)
    Store._migrate_v2_to_v3(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert _schema_fingerprint(conn) == STORE_V3_SCHEMA_FINGERPRINT
    before = conn.execute("SELECT data FROM records ORDER BY identity").fetchall()
    conn.close()

    def crash(name):
        if name == observation:
            raise RuntimeError("migration fault")

    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(crash))
    with pytest.raises(RuntimeError, match="migration fault"):
        Store(path)
    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(lambda _name: None))

    check = sqlite3.connect(path)
    if observation == "v3-to-v4:after-commit":
        assert check.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _schema_fingerprint(check) == STORE_V4_SCHEMA_FINGERPRINT
        assert check.execute("SELECT data FROM records ORDER BY identity").fetchall() == before
        assert check.execute("SELECT COUNT(*) FROM admission_receipts").fetchone()[0] == 0
        assert check.execute(
            "SELECT COUNT(*) FROM safety_admission_history").fetchone()[0] == 0
    else:
        assert check.execute("PRAGMA user_version").fetchone()[0] == 3
        assert _schema_fingerprint(check) == STORE_V3_SCHEMA_FINGERPRINT
        assert check.execute("SELECT data FROM records ORDER BY identity").fetchall() == before
        assert check.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN "
            "('admission_receipts', 'safety_admission_history')").fetchone()[0] == 0
    check.close()


@pytest.mark.parametrize("observation", V2_TO_V3_FAULT_OBSERVATIONS)
def test_every_declared_migration_fault_observation_is_atomic(tmp_path, monkeypatch, observation):
    path = tmp_path / (observation.replace(":", "-") + ".db")
    records, cell = make_v2(path)

    def crash(name):
        if name == observation:
            raise RuntimeError("migration fault")

    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(crash))
    with pytest.raises(RuntimeError, match="migration fault"):
        Store(path)
    monkeypatch.setattr(Store, "_migration_checkpoint", staticmethod(lambda _name: None))
    if observation == "v2-to-v3:after-commit":
        reopened = Store(path)
        assert reopened._conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert _schema_fingerprint(reopened._conn) == STORE_V4_SCHEMA_FINGERPRINT
        assert tuple(reopened.load()[item.identity] for item in records) == records
        with pytest.raises(SafetyRefused, match="requires operator reconciliation"):
            OperationalSafety(reopened)
        assert reopened._conn.execute(
            "SELECT active_digest FROM safety_route_state").fetchone()[0] == cell.digest
        assert reopened._conn.execute(
            "SELECT COUNT(*) FROM canary_attributions").fetchone()[0] == 0
        reopened.close()
    else:
        assert_v2_snapshot(path, records, cell)


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
