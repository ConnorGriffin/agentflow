"""Public-interface contract for bounded operational self-healing (ADR 585)."""

from __future__ import annotations

from dataclasses import replace
import inspect
import json
import sqlite3
import threading

import pytest

from agentflow.coordinator.record import Record
from agentflow.coordinator.store import SCHEMA_VERSION, Store
from agentflow.operational_safety import (
    ACTION_STATE_MAP,
    CanaryApproval,
    DEPENDENCY_RECEIPTS,
    DETERMINISTIC_CHECKS,
    DETERMINISTIC_CHECK_ALLOWLIST_DIGEST,
    EffectEvidence,
    OperationalSafety,
    OPERATIONAL_SAFETY_CONTRACT_DIGEST,
    ReopenProof,
    SafetyObservation,
    SafetyRefused,
    ROUTE_CELL_CONTRACT_DIGEST,
)


class Reruns:
    def __init__(self) -> None:
        self.effects: dict[str, EffectEvidence] = {}
        self.applied: list[str] = []
        self.crash = ""

    def evidence_for(self, action_id):
        return self.effects.get(action_id)

    def apply(self, intent):
        if self.crash == "before_effect":
            raise RuntimeError("crash before effect")
        self.applied.append(intent.action_id)
        evidence = EffectEvidence(
            f"transport/reruns/{intent.action_id}",
            f"provider accepted action_id={intent.action_id}",
        )
        self.effects[intent.action_id] = evidence
        if self.crash == "after_effect":
            raise RuntimeError("crash after effect")
        return evidence


@pytest.fixture
def safety(tmp_path):
    store = Store(tmp_path / "coordinator.db")
    reruns = Reruns()
    owner = OperationalSafety(store, reruns)
    yield owner, store, reruns
    store.close()


def route(owner, *, repository="octo/app", route_id="primary", config=None):
    return owner.register_route_cell(
        repository, "build", "codex", "gpt-5", route_id,
        config or {"model": "gpt-5", "effort": "high", "timeout": 900},
    )


def observation(cell, outcome, evidence, *, check="route-health", verified=False,
                subject="issue-585", revision="abc123"):
    return SafetyObservation(
        cell.repository, subject, revision, check, "1", cell.digest,
        outcome, evidence, verified,
    )


def passing_proofs(cell, state):
    declarations = {(item.identifier, item.version): item
                    for item in DETERMINISTIC_CHECKS}
    return tuple(
        ReopenProof(check, "1", cell.digest, state.safety_state_id,
                    declarations[(check, "1")].digest,
                    f"proof/{check}/{state.generation}", True)
        for check in ("capability-parity", "route-health")
    )


def quarantine(owner, cell, *, revision="abc123"):
    owner.observe(observation(
        cell, "fail", f"evidence/{revision}/first", verified=True,
        revision=revision))
    owner.observe(observation(
        cell, "fail", f"evidence/{revision}/second", verified=True,
        revision=revision))
    return owner.route_state(cell.digest)


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
        "984242b3ea395814490c75d88c293cc20ae7747c0fd0c53f54e77e4ae31d317a")
    assert ACTION_STATE_MAP == {
        "rerun": "claimed -> externally_effected -> result_committed",
        "quarantine": "claimed -> exact_cell_quarantined + result_committed",
        "rollback": "claimed -> predecessor_pointer_restored + result_committed",
    }


def test_launch_config_is_content_addressed_immutable_and_resolved_only_by_active_pointer(safety):
    owner, _store, _reruns = safety
    first = route(owner)
    duplicate = route(owner)
    changed = route(owner, config={"model": "gpt-5", "effort": "low", "timeout": 900})

    assert duplicate == first
    assert changed.digest != first.digest
    assert changed.launch_config_digest != first.launch_config_digest
    resolved = owner.resolve("octo/app", "build", "codex", "gpt-5", "primary")
    assert resolved.route_cell == first
    assert json.loads(resolved.config_bytes) == {
        "effort": "high", "model": "gpt-5", "timeout": 900}

    owner.approve_canary(CanaryApproval("human/receipt-1", changed.digest, first.digest, 0))
    resolved = owner.resolve("octo/app", "build", "codex", "gpt-5", "primary")
    assert resolved.route_cell == changed
    assert json.loads(resolved.config_bytes)["effort"] == "low"


def test_allowlist_transport_and_duplicate_rules_bound_rerun_alert_and_quarantine(safety):
    owner, _store, _reruns = safety
    cell = route(owner)
    unknown = replace(observation(cell, "fail", "e/unknown"), check_id="mutable-check")
    with pytest.raises(SafetyRefused, match="allowlist"):
        owner.observe(unknown)

    unreadable = observation(cell, "unreadable", "transport/timeout", verified=True)
    first = owner.observe(unreadable)
    duplicate = owner.observe(unreadable)
    assert [item.action_id for item in duplicate] == [item.action_id for item in first]
    assert [item.kind for item in first] == ["rerun"]
    assert owner.route_state(cell.digest).quarantined is False
    assert [(alert.kind, alert.evidence_ref) for alert in owner.alerts(cell.digest)] == [
        ("transport", "transport/timeout")]

    owner.observe(observation(cell, "fail", "semantic/one", verified=True))
    actions = owner.observe(observation(cell, "fail", "semantic/two", verified=True))
    assert {item.kind for item in actions} == {"rerun", "quarantine"}
    assert owner.route_state(cell.digest).quarantined is True
    assert sorted(alert.kind for alert in owner.alerts(cell.digest)) == ["route", "transport"]


def test_rerun_intent_precedes_effect_and_reconciliation_never_replays_known_effect(safety):
    owner, _store, reruns = safety
    cell = route(owner)
    intent = owner.observe(observation(cell, "fail", "semantic/first", verified=True))[0]
    assert owner.action_result(intent.action_id) is None

    reruns.crash = "before_effect"
    with pytest.raises(RuntimeError, match="before effect"):
        owner.reconcile(intent.action_id)
    assert reruns.applied == [] and owner.action_result(intent.action_id) is None

    reruns.crash = "after_effect"
    with pytest.raises(RuntimeError, match="after effect"):
        owner.reconcile(intent.action_id)
    assert reruns.applied == [intent.action_id]
    assert owner.action_result(intent.action_id) is None

    reruns.crash = ""
    result = owner.reconcile(intent.action_id)
    assert result.action_id == intent.action_id
    assert intent.action_id in result.proof
    assert reruns.applied == [intent.action_id]
    assert owner.reconcile(intent.action_id) == result
    assert reruns.applied == [intent.action_id]


def test_concurrent_duplicate_claims_return_one_durable_intent(tmp_path):
    path = tmp_path / "coordinator.db"
    seed = Store(path)
    cell = route(OperationalSafety(seed))
    seed.close()
    barrier = threading.Barrier(2)
    action_ids = []
    errors = []

    def claim():
        try:
            store = Store(path)
            owner = OperationalSafety(store)
            barrier.wait()
            actions = owner.observe(observation(
                cell, "fail", "semantic/same", verified=True))
            action_ids.append(actions[0].action_id)
            store.close()
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=claim) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(action_ids)) == 1


def test_quarantine_participates_in_reservation_transaction_and_is_cell_exact(tmp_path):
    path = tmp_path / "coordinator.db"
    store = Store(path)
    owner = OperationalSafety(store)
    broken = route(owner)
    healthy = route(owner, route_id="fallback", config={"model": "gpt-5", "effort": "low"})
    quarantine(owner, broken)
    store.upsert(Record("broken", "build", "codex", 1, state="waiting"))
    store.upsert(Record("healthy", "build", "codex", 1, state="waiting"))

    rejected = Record("broken", "build", "codex", 1, state="running")
    with pytest.raises(SafetyRefused, match="not admissible"):
        store.reserve(rejected, 5, operational_safety=owner,
                      route_cell_digest=broken.digest)
    assert store.record_of("broken").state == "waiting"
    assert store.permits_used("codex") == 0

    admitted = Record("healthy", "build", "codex", 1, state="running")
    assert store.reserve(admitted, 5, operational_safety=owner,
                         route_cell_digest=healthy.digest)
    assert store.permits_used("codex") == 1

    # Quarantine never rewrites a row that was already running.
    before = store.record_of("healthy")
    quarantine(owner, healthy, revision="def456")
    after = store.record_of("healthy")
    assert after == before and after.state == "running"
    store.close()


def test_quarantine_racing_admission_has_one_serialized_outcome(tmp_path):
    path = tmp_path / "coordinator.db"
    seed = Store(path)
    owner = OperationalSafety(seed)
    cell = route(owner)
    seed.upsert(Record("race", "build", "codex", 1, state="waiting"))
    owner.observe(observation(cell, "fail", "race/first", verified=True))
    seed.close()
    barrier = threading.Barrier(2)
    outcome = {}
    errors = []

    def admit():
        store = Store(path)
        safety_owner = OperationalSafety(store)
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
        safety_owner = OperationalSafety(store)
        try:
            barrier.wait()
            safety_owner.observe(observation(
                cell, "fail", "race/second", verified=True))
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
    assert OperationalSafety(final).route_state(cell.digest).quarantined is True
    durable = final.record_of("race")
    if outcome["admitted"]:
        assert durable.state == "running" and final.permits_used("codex") == 1
    else:
        assert durable.state == "waiting" and final.permits_used("codex") == 0
    final.close()


def test_quarantine_reopens_only_by_exact_digest_and_fresh_state_cas(safety):
    owner, _store, _reruns = safety
    old = route(owner)
    changed = route(owner, config={"model": "gpt-5", "effort": "medium"})
    state = quarantine(owner, old)
    bad_proofs = passing_proofs(old, state)[:-1]
    with pytest.raises(SafetyRefused, match="proofs required"):
        owner.reopen(old.digest, state.safety_state_id, bad_proofs)

    reopened = owner.reopen(old.digest, state.safety_state_id, passing_proofs(old, state))
    assert reopened.quarantined is False
    with pytest.raises(SafetyRefused, match="compare-and-swap"):
        owner.reopen(old.digest, state.safety_state_id, passing_proofs(old, state))

    state = quarantine(owner, old, revision="new-failure")
    stale_proofs = passing_proofs(old, state)
    owner.approve_canary(CanaryApproval("human/changed", changed.digest, old.digest, 0))
    with pytest.raises(SafetyRefused, match="compare-and-swap"):
        owner.reopen(old.digest, state.safety_state_id, stale_proofs)


def test_canary_rollback_is_exact_generation_bound_and_cannot_touch_another_cell(safety):
    owner, _store, _reruns = safety
    good = route(owner)
    bad = route(owner, config={"model": "gpt-5", "effort": "medium"})
    later = route(owner, config={"model": "gpt-5", "effort": "low"})
    other = route(owner, repository="octo/other", config={"model": "gpt-5"})
    first = CanaryApproval("human/canary-1", bad.digest, good.digest, 0)
    owner.approve_canary(first)

    with pytest.raises(SafetyRefused, match="committed quarantine"):
        owner.rollback_canary(first, "alert/too-early", "must not apply")
    quarantine(owner, bad)
    result = owner.rollback_canary(first, "alert/canary-1", "predecessor restored")
    state = owner.canary_state(good.digest)
    assert result == owner.action_result(result.action_id)
    assert state.active_route_cell_digest == good.digest
    assert state.active_receipt_id is None
    assert state.disabled_generation == 1
    assert owner.resolve("octo/app", "build", "codex", "gpt-5", "primary").route_cell == good

    with pytest.raises(SafetyRefused, match="stale"):
        owner.approve_canary(first)
    fresh = CanaryApproval("human/canary-2", later.digest, good.digest, 1)
    owner.approve_canary(fresh)
    with pytest.raises(SafetyRefused, match="compare-and-swap"):
        owner.rollback_canary(first, "alert/stale", "must not apply")
    assert owner.canary_state(later.digest).active_route_cell_digest == later.digest
    assert owner.canary_state(other.digest).active_route_cell_digest == other.digest
    quarantine(owner, later, revision="later-failure")
    owner.rollback_canary(fresh, "alert/canary-2", "current predecessor restored")
    assert owner.canary_state(good.digest).disabled_generation == 2


def test_schema_migrates_v1_without_rewriting_records(tmp_path):
    path = tmp_path / "v1.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE records (identity TEXT PRIMARY KEY, pool TEXT NOT NULL,"
        " state TEXT NOT NULL, demand INTEGER NOT NULL, data TEXT NOT NULL)")
    record = Record("legacy", "review", "claude", 2, state="running", revision=7)
    conn.execute("INSERT INTO records VALUES (?, ?, ?, ?, ?)", (
        record.identity, record.pool, record.state, record.demand,
        Store._encode(record)))
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    store = Store(path)
    assert store.record_of("legacy") == record
    assert store.permits_used("claude") == 2
    check = sqlite3.connect(path)
    assert check.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 2
    assert check.execute(
        "SELECT name FROM sqlite_master WHERE name = 'safety_actions'").fetchone()
    check.close()
    store.close()


def test_interface_and_write_set_deny_policy_github_filesystem_and_configuration(safety):
    owner, store, reruns = safety
    assert tuple(inspect.signature(OperationalSafety).parameters) == ("store", "rerun_effect")
    assert not ({"filesystem", "github", "prompt", "policy", "routing", "autonomy", "merge"}
                & set(vars(owner)))
    before_records = store.load()
    cell = route(owner)
    intent = owner.observe(observation(cell, "fail", "semantic/one", verified=True))[0]
    owner.reconcile(intent.action_id)
    assert store.load() == before_records
    assert reruns.applied == [intent.action_id]

    conn = sqlite3.connect(store.path)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()
    assert tables == {
        "records", "safety_action_results", "safety_actions", "safety_alerts",
        "safety_canary_state", "safety_launch_configs", "safety_observations",
        "safety_route_cells", "safety_route_state",
    }
