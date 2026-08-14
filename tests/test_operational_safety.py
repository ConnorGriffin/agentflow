"""Security contract for bounded operational self-healing (ADR 585)."""

from __future__ import annotations

from dataclasses import replace
import builtins
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from agentflow import config, github, routing
from agentflow.coordinator.record import Record
from agentflow.coordinator.store import SCHEMA_VERSION, Store, StoreUnavailable
from agentflow.evidence import (
    ApprovedAuthority,
    AuthorityPointer,
    EvidenceError,
    EvidenceStore,
    PromotionReceipt,
    PromotionReceiptReader,
    _V4_SCHEMA,
)
from agentflow.operational_safety import (
    ACTION_STATE_MAP,
    CanaryActivationRequest,
    CheckEvidence,
    CheckEvidenceUnavailable,
    DEPENDENCY_RECEIPTS,
    DETERMINISTIC_CHECKS,
    DETERMINISTIC_CHECK_ALLOWLIST_DIGEST,
    EffectEvidence,
    ObservationRequest,
    OperationalSafety,
    OPERATIONAL_SAFETY_CONTRACT_DIGEST,
    PROMOTION_VERIFIER,
    ROUTE_CELL_CONTRACT_DIGEST,
    SafetyRefused,
    _state_id,
)


class Checks:
    def __init__(self) -> None:
        self.results: dict[str, CheckEvidence] = {}
        self.unreadable: set[str] = set()

    def issue(self, request: ObservationRequest, outcome: str, *,
              safety_state_id: str = "") -> str:
        declaration = next(item for item in DETERMINISTIC_CHECKS
                           if (item.identifier, item.version)
                           == (request.check_id, request.check_version))
        self.results[request.evidence_ref] = CheckEvidence(
            "observation-" + request.evidence_ref.replace("/", "-"),
            request.repository, request.subject, request.subject_revision,
            request.check_id, request.check_version, request.route_cell_digest,
            declaration.digest, outcome, request.evidence_ref,
            "authority-verified:" + request.evidence_ref, safety_state_id,
        )
        return request.evidence_ref

    def read(self, evidence_ref: str) -> CheckEvidence:
        if evidence_ref in self.unreadable:
            raise CheckEvidenceUnavailable(evidence_ref)
        return self.results[evidence_ref]


class Receipts:
    def __init__(self) -> None:
        self.receipts: dict[str, PromotionReceipt] = {}
        self.unavailable = False

    def issue(self, request: CanaryActivationRequest, *,
              scope: str = "fleet-policy/0-to-1",
              authoritative: bool = True,
              verifier_id: str = "github-authority",
              verifier_version: str = "v1") -> PromotionReceipt:
        pointer = AuthorityPointer(
            "github", "octo/governance", "pulls/584/files/canary.json",
            "a" * 40, "sha256", request.digest, scope,
        )
        approved = ApprovedAuthority(
            pointer, "approval-585", pointer.revision, pointer.content_hash,
            pointer.scope, verifier_id, verifier_version, "verified",
        )
        receipt = PromotionReceipt(
            request.promotion_receipt_id, "candidate-585", approved.approval_id,
            1, approved if authoritative else None, authoritative,
        )
        self.receipts[receipt.receipt_id] = receipt
        return receipt

    def read(self, receipt_id: str) -> PromotionReceipt:
        if self.unavailable:
            raise EvidenceError("receipt storage unavailable")
        return self.receipts[receipt_id]


class Reruns:
    def __init__(self) -> None:
        self.effects: dict[str, EffectEvidence] = {}
        self.applied: list[str] = []
        self.crash = ""
        self.entered = threading.Event()
        self.release = threading.Event()
        self.block = False
        self.on_apply = None
        self.transaction_states: list[bool] = []
        self.conn = None

    def evidence_for(self, action_id: str) -> EffectEvidence | None:
        if self.conn is not None:
            self.transaction_states.append(self.conn.in_transaction)
        return self.effects.get(action_id)

    def apply(self, intent) -> EffectEvidence:
        if self.conn is not None:
            self.transaction_states.append(self.conn.in_transaction)
        if self.crash == "before_effect":
            raise RuntimeError("crash before effect")
        existing = self.effects.get(intent.action_id)
        if existing is not None:
            return existing
        self.applied.append(intent.action_id)
        evidence = EffectEvidence(
            f"transport/reruns/{intent.action_id}",
            f"provider accepted action_id={intent.action_id}",
        )
        self.effects[intent.action_id] = evidence
        if self.on_apply is not None:
            self.on_apply()
        self.entered.set()
        if self.block:
            assert self.release.wait(2)
        if self.crash == "after_effect":
            raise RuntimeError("crash after effect")
        return evidence


@pytest.fixture
def safety(tmp_path):
    store = Store(tmp_path / "coordinator.db")
    checks, receipts, reruns = Checks(), Receipts(), Reruns()
    owner = OperationalSafety(
        store, check_evidence=checks, promotion_receipts=receipts,
        rerun_effect=reruns,
    )
    yield owner, store, checks, receipts, reruns
    store.close()


def route(owner, *, repository="octo/app", route_id="primary", config_value=None):
    return owner.register_route_cell(
        repository, "build", "codex", "gpt-5", route_id,
        config_value or {"model": "gpt-5", "effort": "high", "timeout": 900},
    )


def request(cell, evidence_ref, *, check="route-health", subject="issue-585",
            revision="abc123"):
    return ObservationRequest(
        cell.repository, subject, revision, check, "1", cell.digest, evidence_ref,
    )


def observe(owner, checks, cell, outcome, evidence_ref, **kwargs):
    item = request(cell, evidence_ref, **kwargs)
    checks.issue(item, outcome)
    return owner.observe(item)


def quarantine(owner, checks, cell, *, revision="abc123"):
    observe(owner, checks, cell, "fail", f"evidence/{revision}/first", revision=revision)
    observe(owner, checks, cell, "fail", f"evidence/{revision}/second", revision=revision)
    return owner.route_state(cell.digest)


def approval(receipts, bad, predecessor, generation, receipt_id):
    item = CanaryActivationRequest(receipt_id, bad.digest, predecessor.digest, generation)
    receipts.issue(item)
    return item


def passing_evidence(checks, cell, state):
    refs = []
    for check in ("capability-parity", "route-health"):
        item = request(cell, f"reopen/{check}/{state.generation}", check=check)
        refs.append(checks.issue(item, "pass", safety_state_id=state.safety_state_id))
    return tuple(refs)


def write_promotion_receipt_db(path, *, verifier_id="github-authority",
                               verifier_version="v1", content_hash="b" * 64,
                               receipt_id="receipt-585"):
    conn = sqlite3.connect(path)
    conn.executescript(_V4_SCHEMA)
    conn.execute("INSERT INTO candidates VALUES (?, ?, ?, ?)", (
        "candidate-585", content_hash, 1, 1))
    conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "candidate-585", receipt_id, "approval-585", 1, 2, "verified",
        "github", "octo/governance", "pulls/584/files/canary.json", "a" * 40,
        "sha256", content_hash, "fleet-policy/0-to-1", verifier_id, verifier_version,
        "verified", "a" * 40, content_hash, "fleet-policy/0-to-1",
        "github-merged-pr-v1",
    ))
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()


def test_dependency_and_registry_receipts_are_exact():
    assert DEPENDENCY_RECEIPTS == {
        "issue_582_merge": "a58dc0c84a7459774631048a67b3e71f8328d144",
        "capability_manifest_sha256":
            "cba84e63be53884e6ed566a534883912f7d22156aad7e4a5590515140d18fcad",
        "issue_584_merge": "ef08dd3d2f691aa154ddaa193e6161b559099396",
        "promotion_scope_registry_sha256":
            "83e02ca43be08e0505d7075c5bdbe8ae032bf28ca50e4074a0632b4fd14a6006",
    }
    assert DETERMINISTIC_CHECK_ALLOWLIST_DIGEST == (
        "66af2cb2c82a3cba92170e0d920f7a4ea9cae8509f482969f90883a42ca47458")
    assert ROUTE_CELL_CONTRACT_DIGEST == (
        "c762ed469c4c2a311391898196713b26a2dbe2985896c262ea05a425368f63a5")
    assert OPERATIONAL_SAFETY_CONTRACT_DIGEST == (
        "4a78101a35168ae33fab177d2eda21a33928caa5b9e124bdb8b478691dbffae6")
    assert PROMOTION_VERIFIER == ("github-authority", "v1")
    assert ACTION_STATE_MAP == {
        "rerun": "claimed -> effect_lease_claimed -> idempotent_effect -> result_committed",
        "quarantine": "claimed -> exact_cell_quarantined + result_committed",
        "rollback": "claimed -> predecessor_pointer_restored + result_committed",
    }


def test_launch_config_is_immutable_and_resolves_only_through_active_pointer(safety):
    owner, _store, _checks, receipts, _reruns = safety
    first = route(owner)
    assert route(owner) == first
    changed = route(owner, config_value={
        "model": "gpt-5", "effort": "low", "timeout": 900})
    assert changed.digest != first.digest
    assert changed.launch_config_digest != first.launch_config_digest
    assert owner.resolve(
        "octo/app", "build", "codex", "gpt-5", "primary").route_cell == first
    item = approval(receipts, changed, first, 0, "receipt-resolution")
    owner.approve_canary(item)
    resolved = owner.resolve("octo/app", "build", "codex", "gpt-5", "primary")
    assert resolved.route_cell == changed
    assert json.loads(resolved.config_bytes)["effort"] == "low"


@pytest.mark.parametrize("reader", ["resolve", "route_state", "canary_state", "admission"])
@pytest.mark.parametrize("target", ["config", "cell"])
def test_every_route_read_recomputes_content_digests(safety, target, reader):
    owner, store, _checks, _receipts, _reruns = safety
    cell = route(owner)
    if target == "config":
        store._conn.execute(
            "UPDATE safety_launch_configs SET content = ? WHERE digest = ?",
            (b'{"effort":"low"}', cell.launch_config_digest),
        )
        message = "launch configuration digest"
    else:
        body = json.loads(store._conn.execute(
            "SELECT data FROM safety_route_cells WHERE digest = ?", (cell.digest,),
        ).fetchone()[0])
        body["repository"] = "attacker/repo"
        store._conn.execute(
            "UPDATE safety_route_cells SET data = ? WHERE digest = ?",
            (json.dumps(body, sort_keys=True, separators=(",", ":")), cell.digest),
        )
        message = "RouteCell digest"
    with pytest.raises(SafetyRefused, match=message):
        if reader == "resolve":
            owner.resolve("octo/app", "build", "codex", "gpt-5", "primary")
        elif reader == "route_state":
            owner.route_state(cell.digest)
        elif reader == "canary_state":
            owner.canary_state(cell.digest)
        else:
            owner.participate_in_admission(store._conn, cell.digest)


@pytest.mark.parametrize("reader", ["resolve", "admission"])
@pytest.mark.parametrize("column", [
    "cell_key", "active_digest", "quarantined_digest",
    "quarantine_action_id", "safety_state_id", "generation",
])
def test_every_route_state_column_is_revalidated_on_read_and_admission(
        safety, column, reader):
    owner, store, _checks, _receipts, _reruns = safety
    cell = route(owner)
    row = store._conn.execute(
        "SELECT cell_key, active_digest, quarantined_digest, quarantine_action_id,"
        " safety_state_id, generation FROM safety_route_state WHERE cell_key = ?",
        (cell.key,)).fetchone()
    if column == "cell_key":
        store._conn.execute(
            "UPDATE safety_route_state SET cell_key = ? WHERE cell_key = ?",
            ("f" * 64, cell.key))
    elif column == "active_digest":
        other = route(owner, route_id="other-cell")
        store._conn.execute(
            "UPDATE safety_route_state SET active_digest = ?, safety_state_id = ?"
            " WHERE cell_key = ?",
            (other.digest, _state_id(cell.key, other.digest, None, row[5]), cell.key))
    elif column == "quarantined_digest":
        store._conn.execute(
            "UPDATE safety_route_state SET quarantined_digest = ?, safety_state_id = ?"
            " WHERE cell_key = ?",
            (cell.digest, _state_id(cell.key, cell.digest, cell.digest, row[5]), cell.key))
    elif column == "quarantine_action_id":
        store._conn.execute(
            "UPDATE safety_route_state SET quarantine_action_id = ? WHERE cell_key = ?",
            ("forged-action", cell.key))
    elif column == "safety_state_id":
        store._conn.execute(
            "UPDATE safety_route_state SET safety_state_id = ? WHERE cell_key = ?",
            ("forged-state", cell.key))
    else:
        store._conn.execute(
            "UPDATE safety_route_state SET generation = ?, safety_state_id = ?"
            " WHERE cell_key = ?",
            (-1, _state_id(cell.key, cell.digest, None, -1), cell.key))

    with pytest.raises(SafetyRefused):
        if reader == "resolve":
            owner.resolve("octo/app", "build", "codex", "gpt-5", "primary")
        else:
            owner.participate_in_admission(store._conn, cell.digest)


@pytest.mark.parametrize("reader", ["resolve", "admission"])
def test_route_and_canary_active_pointers_must_agree(safety, reader):
    owner, store, _checks, _receipts, _reruns = safety
    cell = route(owner)
    other = route(owner, route_id="other-cell")
    store._conn.execute(
        "UPDATE safety_canary_state SET active_digest = ? WHERE cell_key = ?",
        (other.digest, cell.key))

    with pytest.raises(SafetyRefused, match="active pointers disagree"):
        if reader == "resolve":
            owner.resolve("octo/app", "build", "codex", "gpt-5", "primary")
        else:
            owner.participate_in_admission(store._conn, cell.digest)


def test_authority_controls_semantic_failure_and_unreadable_is_transport_only(safety):
    owner, _store, checks, _receipts, _reruns = safety
    cell = route(owner)
    forged = request(cell, "forged/result")
    checks.issue(forged, "fail")
    checks.results[forged.evidence_ref] = replace(
        checks.results[forged.evidence_ref], declaration_digest="f" * 64)
    with pytest.raises(SafetyRefused, match="authority binding"):
        owner.observe(forged)

    unreadable = request(cell, "transport/timeout")
    checks.unreadable.add(unreadable.evidence_ref)
    actions = owner.observe(unreadable)
    assert [item.kind for item in actions] == ["rerun"]
    assert not owner.route_state(cell.digest).quarantined
    assert [(item.kind, item.evidence_ref) for item in owner.alerts(cell.digest)] == [
        ("transport", "transport/timeout")]

    observe(owner, checks, cell, "fail", "semantic/one")
    actions = observe(owner, checks, cell, "fail", "semantic/two")
    assert {item.kind for item in actions} == {"rerun", "quarantine"}
    assert owner.route_state(cell.digest).quarantined


def test_rerun_intent_crash_recovery_and_concurrent_reconcilers_are_single_flight(tmp_path):
    path = tmp_path / "coordinator.db"
    seed = Store(path)
    checks, reruns = Checks(), Reruns()
    seed_owner = OperationalSafety(seed, check_evidence=checks, rerun_effect=reruns)
    reruns.conn = seed._conn
    cell = route(seed_owner)
    intent = observe(seed_owner, checks, cell, "fail", "semantic/first")[0]

    reruns.crash = "before_effect"
    with pytest.raises(RuntimeError, match="before effect"):
        seed_owner.reconcile(intent.action_id)
    assert reruns.applied == [] and seed_owner.action_result(intent.action_id) is None
    assert seed._conn.execute(
        "SELECT COUNT(*) FROM safety_rerun_claims").fetchone()[0] == 0
    reruns.crash = "after_effect"
    with pytest.raises(RuntimeError, match="after effect"):
        seed_owner.reconcile(intent.action_id)
    assert reruns.applied == [intent.action_id]
    assert seed._conn.execute(
        "SELECT COUNT(*) FROM safety_rerun_claims").fetchone()[0] == 0
    reruns.crash = ""
    recovered = seed_owner.reconcile(intent.action_id)
    assert reruns.applied == [intent.action_id]
    assert reruns.transaction_states and not any(reruns.transaction_states)

    second = observe(seed_owner, checks, cell, "fail", "semantic/second")[0]
    assert second.action_id == intent.action_id  # same exact scope has one rerun
    assert seed_owner.reconcile(second.action_id) == recovered

    other_request = request(cell, "semantic/other", subject="issue-586")
    checks.issue(other_request, "fail")
    other_intent = seed_owner.observe(other_request)[0]
    reruns.conn = None
    seed.close()
    reruns.block = True
    reruns.entered.clear()
    reruns.release.clear()
    results, errors = [], []

    def reconcile():
        store = Store(path)
        owner = OperationalSafety(store, check_evidence=checks, rerun_effect=reruns)
        try:
            results.append(owner.reconcile(other_intent.action_id))
        except BaseException as error:
            errors.append(error)
        finally:
            store.close()

    first = threading.Thread(target=reconcile)
    second_thread = threading.Thread(target=reconcile)
    first.start()
    assert reruns.entered.wait(2)
    writer_finished = threading.Event()
    writer_errors = []

    def write_unrelated_record():
        store = Store(path)
        try:
            store.upsert(Record("unrelated", "review", "claude", 1, state="waiting"))
        except BaseException as error:
            writer_errors.append(error)
        finally:
            store.close()
            writer_finished.set()

    writer = threading.Thread(target=write_unrelated_record)
    writer.start()
    assert writer_finished.wait(1.5), "external effect retained the global Store write lock"
    assert writer_errors == []
    second_thread.start()
    reruns.release.set()
    first.join()
    second_thread.join()
    writer.join()
    assert errors == [] and len(results) == 2 and results[0] == results[1]
    assert reruns.applied.count(other_intent.action_id) == 1


def test_rerun_effect_may_reenter_the_same_store_without_nested_transaction(tmp_path):
    store = Store(tmp_path / "coordinator.db")
    checks, reruns = Checks(), Reruns()
    owner = OperationalSafety(store, check_evidence=checks, rerun_effect=reruns)
    cell = route(owner)
    intent = observe(owner, checks, cell, "fail", "reentrant/failure")[0]
    reruns.conn = store._conn
    reruns.on_apply = lambda: store.upsert(
        Record("reentrant", "review", "claude", 1, state="waiting"))

    result = owner.reconcile(intent.action_id)

    assert result.action_id == intent.action_id
    assert store.record_of("reentrant").state == "waiting"
    assert reruns.transaction_states and not any(reruns.transaction_states)
    store.close()


def test_expired_durable_rerun_lease_recovers_after_process_crash(tmp_path):
    store = Store(tmp_path / "coordinator.db")
    checks, reruns = Checks(), Reruns()
    owner = OperationalSafety(store, check_evidence=checks, rerun_effect=reruns)
    cell = route(owner)
    intent = observe(owner, checks, cell, "fail", "abandoned/failure")[0]
    store._conn.execute(
        "INSERT INTO safety_rerun_claims VALUES (?, ?, ?, ?)",
        (intent.action_id, "dead-process", 4, 0))

    result = owner.reconcile(intent.action_id)

    assert result.action_id == intent.action_id
    assert reruns.applied == [intent.action_id]
    assert store._conn.execute(
        "SELECT COUNT(*) FROM safety_rerun_claims").fetchone()[0] == 0
    store.close()


def test_concurrent_duplicate_observation_claims_return_one_durable_intent(tmp_path):
    path = tmp_path / "coordinator.db"
    seed = Store(path)
    checks = Checks()
    cell = route(OperationalSafety(seed, check_evidence=checks))
    item = request(cell, "semantic/same")
    checks.issue(item, "fail")
    seed.close()
    barrier = threading.Barrier(2)
    action_ids, errors = [], []

    def claim():
        store = Store(path)
        owner = OperationalSafety(store, check_evidence=checks)
        try:
            barrier.wait()
            action_ids.append(owner.observe(item)[0].action_id)
        except BaseException as error:
            errors.append(error)
        finally:
            store.close()

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and len(action_ids) == 2 and len(set(action_ids)) == 1


def test_reopen_requires_authority_bound_passes_and_exact_state_cas(safety):
    owner, _store, checks, _receipts, _reruns = safety
    cell = route(owner)
    state = quarantine(owner, checks, cell)
    refs = passing_evidence(checks, cell, state)
    checks.results[refs[0]] = replace(checks.results[refs[0]], safety_state_id="forged")
    with pytest.raises(SafetyRefused, match="authority binding"):
        owner.reopen(cell.digest, state.safety_state_id, refs)
    checks.issue(request(cell, refs[0], check="capability-parity"), "pass",
                 safety_state_id=state.safety_state_id)
    reopened = owner.reopen(cell.digest, state.safety_state_id, refs)
    assert not reopened.quarantined
    with pytest.raises(SafetyRefused, match="compare-and-swap"):
        owner.reopen(cell.digest, state.safety_state_id, refs)


def test_canary_receipt_binding_duplicate_rollback_and_generation_cas(safety):
    owner, _store, checks, receipts, _reruns = safety
    good = route(owner)
    bad = route(owner, config_value={"model": "gpt-5", "effort": "medium"})
    later = route(owner, config_value={"model": "gpt-5", "effort": "low"})
    other = route(owner, repository="octo/other")
    first = approval(receipts, bad, good, 0, "receipt-canary-1")
    forged = replace(first, approved_disabled_generation=1)
    with pytest.raises(SafetyRefused, match="does not bind"):
        owner.approve_canary(forged)

    owner.approve_canary(first)
    with pytest.raises(SafetyRefused, match="committed quarantine"):
        owner.rollback_canary(first)
    quarantine(owner, checks, bad)
    result = owner.rollback_canary(first)
    assert owner.canary_state(good.digest).disabled_generation == 1

    fresh = approval(receipts, later, good, 1, "receipt-canary-2")
    owner.approve_canary(fresh)
    receipts.unavailable = True
    assert owner.rollback_canary(first) == result
    receipts.unavailable = False
    assert owner.canary_state(later.digest).active_route_cell_digest == later.digest
    assert owner.canary_state(other.digest).active_route_cell_digest == other.digest
    quarantine(owner, checks, later, revision="later")
    owner.rollback_canary(fresh)
    assert owner.canary_state(good.digest).disabled_generation == 2


def test_repository_promotion_receipt_cannot_cross_route_repository(safety):
    owner, _store, _checks, receipts, _reruns = safety
    good = route(owner)
    bad = route(owner, config_value={"model": "gpt-5", "effort": "medium"})
    item = CanaryActivationRequest("receipt-cross", bad.digest, good.digest, 0)
    receipts.issue(item, scope="repository-policy/other/repo/0-to-1")
    with pytest.raises(SafetyRefused, match="does not bind"):
        owner.approve_canary(item)


@pytest.mark.parametrize("verifier_id,verifier_version", [
    ("fake-authority", "v1"), ("github-authority", "v2"),
])
def test_canary_requires_exact_584_production_verifier(
        safety, tmp_path, verifier_id, verifier_version):
    _owner, store, checks, _receipts, reruns = safety
    owner = OperationalSafety(store, check_evidence=checks, rerun_effect=reruns)
    good = route(owner)
    bad = route(owner, config_value={"model": "gpt-5", "effort": "medium"})
    item = CanaryActivationRequest("receipt-verifier", bad.digest, good.digest, 0)
    path = tmp_path / "wrong-verifier.db"
    write_promotion_receipt_db(
        path, verifier_id=verifier_id, verifier_version=verifier_version,
        content_hash=item.digest, receipt_id=item.promotion_receipt_id)
    owner = OperationalSafety(
        store, check_evidence=checks,
        promotion_receipts=PromotionReceiptReader(path=path), rerun_effect=reruns)
    with pytest.raises(SafetyRefused, match="does not bind"):
        owner.approve_canary(item)


def test_promotion_receipt_authority_is_exact_schema_and_read_only(tmp_path):
    path = tmp_path / "evidence.db"
    write_promotion_receipt_db(path)

    reader = PromotionReceiptReader(path=path)
    assert reader.read("receipt-585").authoritative
    with reader._connect() as read_only:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            read_only.execute("UPDATE receipts SET approval_id='forged'")


@pytest.mark.parametrize("change", ["migration", "schema", "replacement"])
def test_promotion_receipt_reader_revalidates_every_new_connection(tmp_path, change):
    path = tmp_path / "evidence.db"
    write_promotion_receipt_db(path)
    reader = PromotionReceiptReader(path=path)
    assert reader.read("receipt-585").authoritative

    if change == "replacement":
        path.rename(tmp_path / "original.db")
        replacement = sqlite3.connect(path)
        replacement.execute("CREATE TABLE receipts (receipt_id TEXT)")
        replacement.execute("PRAGMA user_version = 4")
        replacement.commit()
        replacement.close()
    else:
        conn = sqlite3.connect(path)
        if change == "migration":
            conn.execute("PRAGMA user_version = 5")
        else:
            conn.execute("ALTER TABLE receipts ADD COLUMN forged TEXT")
        conn.commit()
        conn.close()

    with pytest.raises(EvidenceError, match="not accepted"):
        reader.read("receipt-585")


def test_admission_refusal_consumes_no_permit_and_never_touches_running_work(safety):
    owner, store, checks, _receipts, _reruns = safety
    cell = route(owner)
    quarantine(owner, checks, cell)
    store.upsert(Record("waiting", "build", "codex", 1, state="waiting"))
    with pytest.raises(SafetyRefused, match="not admissible"):
        store.reserve(Record("waiting", "build", "codex", 1, state="running"), 5,
                      operational_safety=owner, route_cell_digest=cell.digest)
    assert store.record_of("waiting").state == "waiting"
    assert store.permits_used("codex") == 0

    healthy = route(owner, route_id="fallback",
                    config_value={"model": "gpt-5", "effort": "low"})
    store.upsert(Record("healthy", "build", "codex", 1, state="waiting"))
    assert store.reserve(Record("healthy", "build", "codex", 1, state="running"), 5,
                         operational_safety=owner, route_cell_digest=healthy.digest)
    before = store.record_of("healthy")
    quarantine(owner, checks, healthy, revision="healthy-later")
    assert store.record_of("healthy") == before


def test_quarantine_and_admission_race_serialize_in_one_store_transaction(tmp_path):
    path = tmp_path / "coordinator.db"
    seed = Store(path)
    checks = Checks()
    owner = OperationalSafety(seed, check_evidence=checks)
    cell = route(owner)
    seed.upsert(Record("race", "build", "codex", 1, state="waiting"))
    observe(owner, checks, cell, "fail", "race/first")
    second = request(cell, "race/second")
    checks.issue(second, "fail")
    seed.close()
    barrier = threading.Barrier(2)
    outcome, errors = {}, []

    def admit():
        store = Store(path)
        safety_owner = OperationalSafety(store, check_evidence=checks)
        try:
            barrier.wait()
            outcome["admitted"] = store.reserve(
                Record("race", "build", "codex", 1, state="running"), 5,
                operational_safety=safety_owner, route_cell_digest=cell.digest)
        except SafetyRefused:
            outcome["admitted"] = False
        except BaseException as error:
            errors.append(error)
        finally:
            store.close()

    def contain():
        store = Store(path)
        try:
            barrier.wait()
            OperationalSafety(store, check_evidence=checks).observe(second)
        except BaseException as error:
            errors.append(error)
        finally:
            store.close()

    threads = [threading.Thread(target=admit), threading.Thread(target=contain)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    final = Store(path)
    assert errors == []
    assert OperationalSafety(final).route_state(cell.digest).quarantined
    durable = final.record_of("race")
    if outcome["admitted"]:
        assert durable.state == "running" and final.permits_used("codex") == 1
    else:
        assert durable.state == "waiting" and final.permits_used("codex") == 0
    final.close()


def test_store_rejects_inexact_v1_before_migration_and_inexact_v2(tmp_path):
    v1 = tmp_path / "v1.db"
    conn = sqlite3.connect(v1)
    conn.execute(
        "CREATE TABLE records (identity TEXT PRIMARY KEY, pool TEXT NOT NULL,"
        " state TEXT NOT NULL, demand INTEGER NOT NULL, data TEXT NOT NULL)")
    conn.execute("CREATE TABLE attacker (value TEXT)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(StoreUnavailable, match="migration source"):
        Store(v1)
    check = sqlite3.connect(v1)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    assert check.execute(
        "SELECT name FROM sqlite_master WHERE name='safety_actions'").fetchone() is None
    check.close()

    v2 = tmp_path / "v2.db"
    accepted = Store(v2)
    accepted.close()
    conn = sqlite3.connect(v2)
    conn.execute("ALTER TABLE safety_actions ADD COLUMN attacker TEXT")
    conn.commit()
    conn.close()
    with pytest.raises(StoreUnavailable, match="accepted schema"):
        Store(v2)


def test_store_advances_only_the_exact_v1_schema_without_rewriting_records(tmp_path):
    path = tmp_path / "exact-v1.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE records (identity TEXT PRIMARY KEY, pool TEXT NOT NULL,"
        " state TEXT NOT NULL, demand INTEGER NOT NULL, data TEXT NOT NULL)")
    record = Record("legacy", "review", "claude", 2, state="running", revision=7)
    conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (
        record.identity, record.pool, record.state, record.demand, Store._encode(record)))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    store = Store(path)
    assert store.record_of("legacy") == record
    assert store.permits_used("claude") == 2
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    store.close()


def test_real_adapter_and_sql_write_set_boundaries_deny_on_touch(safety, monkeypatch):
    owner, store, checks, receipts, _reruns = safety
    touched = set()
    allowed = {name for name in (
        "safety_launch_configs", "safety_route_cells", "safety_route_state",
        "safety_observations", "safety_actions", "safety_action_results",
        "safety_alerts", "safety_canary_state", "safety_rerun_claims",
    )}

    def authorizer(action, arg1, _arg2, _db, _trigger):
        if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE}:
            touched.add(arg1)
            return sqlite3.SQLITE_OK if arg1 in allowed else sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def denied(*_args, **_kwargs):
        pytest.fail("out-of-scope adapter was touched")

    store._conn.set_authorizer(authorizer)
    monkeypatch.setattr(builtins, "open", denied)
    monkeypatch.setattr(Path, "read_text", denied)
    monkeypatch.setattr(Path, "write_text", denied)
    monkeypatch.setattr(github, "promotion_authority_read", denied)
    monkeypatch.setattr(config, "load_config", denied)
    monkeypatch.setattr(routing.CapabilityRouting, "from_path", denied)
    monkeypatch.setattr(EvidenceStore, "promote", denied)

    good = route(owner)
    bad = route(owner, config_value={"model": "gpt-5", "effort": "medium"})
    item = approval(receipts, bad, good, 0, "receipt-boundary")
    owner.approve_canary(item)
    quarantine(owner, checks, bad)
    owner.rollback_canary(item)
    assert "records" not in touched
    assert {"safety_launch_configs", "safety_route_cells", "safety_route_state",
            "safety_actions", "safety_action_results"} <= touched
