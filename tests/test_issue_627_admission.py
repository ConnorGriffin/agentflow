"""Public composed-admission and recovery proofs for issue 627."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from agentflow.capability_contracts import CapabilityPreflightResult, _ready_fact
from agentflow.coordinator import Coordinator, Submission
from agentflow.coordinator.launcher import STARTED, StartResult
from agentflow.coordinator.record import RUNNING, Record
from agentflow.coordinator.store import (
    AdmissionRefused,
    OperationalSafetyAndCanary,
    ReservationIntent,
    SafetySources,
    Store,
    StoreUnavailable,
)
from agentflow.effective_policy import NotApplicableBriefing, _finish, _hold
from agentflow.evidence import ApprovedAuthority, AuthorityPointer, PromotionReceipt
from agentflow.operational_safety import CanaryActivationRequest, OperationalSafety
from agentflow.operational_safety import CheckEvidence, DETERMINISTIC_CHECKS, ObservationRequest
from agentflow.routing import routing


REVISION = "a" * 40


class _Receipts:
    def __init__(self) -> None:
        self.values = {}

    def issue(self, request):
        pointer = AuthorityPointer(
            "github", "octo/governance", "pulls/627/files/canary.json",
            REVISION, "sha256", request.digest, "fleet-policy/0-to-1")
        authority = ApprovedAuthority(
            pointer, "approval-627", REVISION, request.digest,
            "fleet-policy/0-to-1", "github-authority", "v1", "verified")
        receipt = PromotionReceipt(
            request.promotion_receipt_id, "candidate-627", authority.approval_id,
            1, authority, True)
        self.values[receipt.receipt_id] = receipt
        return receipt

    def read(self, receipt_id):
        return self.values[receipt_id]


class _Checks:
    def __init__(self) -> None:
        self.values = {}

    def issue(self, request, outcome):
        declaration = next(item for item in DETERMINISTIC_CHECKS
                           if item.identifier == request.check_id)
        self.values[request.evidence_ref] = CheckEvidence(
            "observation-" + request.evidence_ref.replace("/", "-"),
            request.repository, request.subject, request.subject_revision,
            request.check_id, request.check_version, request.route_cell_digest,
            declaration.digest, outcome, request.evidence_ref,
            "authority-verified:" + request.evidence_ref)

    def read(self, evidence_ref):
        return self.values[evidence_ref]


class _Prepared:
    def __init__(self) -> None:
        self.calls = 0

    def prepare(self, _record):
        self.calls += 1
        return True

    def observe(self, _record):
        raise AssertionError("a live test family must not be observed")

    def verify(self, _record, _observation):
        return False


class _StartedLauncher:
    def __init__(self) -> None:
        self.launches = []
        self.identities = []
        self.alive = set()

    def start(self, record, store, admitted=None):
        assert admitted is not None
        self.launches.append(admitted)
        self.identities.append(record.identity)
        family = str(970000 + len(self.launches))
        record.start_fact = STARTED
        record.family = family
        record.process_alive = True
        assert store.upsert(record)
        self.alive.add(family)
        return StartResult(STARTED, family)

    def is_alive(self, family):
        return family in self.alive


class _Briefings:
    def __init__(self, *, available=True) -> None:
        self.available = available

    def brief_for(self, repository, stage, subject_revision):
        if not self.available:
            return _hold(repository, stage, subject_revision, "missing_policy")
        value = {
            "briefing_digest": "", "briefing_id": "", "reason": "stage_not_applicable",
            "repository": repository, "schema": "briefing-v1", "stage": stage,
            "status": "not_applicable", "subject_revision": subject_revision,
        }
        digest, identity, _ = _finish(value)
        return NotApplicableBriefing(
            repository, stage, subject_revision, digest, identity)


def _ready(stage="build", provider="codex"):
    fact = _ready_fact(stage, provider, b"manifest", ())
    return CapabilityPreflightResult(
        stage, provider, (), "ready", ("private evidence",), "private repair", fact)


def _failure(state, stage="build", provider="codex"):
    return CapabilityPreflightResult(
        stage, provider, (), state, ("secret evidence",), "secret repair")


def _coordinator(
        tmp_path, capability, *, briefing=None, register=True, receipts=None, checks=None):
    path = tmp_path / "coordinator.db"
    receipts = receipts or _Receipts()
    store = Store(path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(check_evidence=checks), receipts))
    launcher = _StartedLauncher()
    adapter = _Prepared()
    coordinator = Coordinator(
        store=store, launcher=launcher, adapter=adapter,
        capability_preflight=capability, briefing_resolver=briefing or _Briefings(),
        route_selector=routing.select_route, daemon_generation="daemon-627")
    identity = coordinator.submit_stage(Submission(
        repo="octo/app", subject="627", stage="build", pool="codex",
        complexity="deep", subject_revision=REVISION))
    record = store.record_of(identity)
    assert record is not None
    if register:
        store.register_route_selection(routing.select_route(
            record.repo, record.stage, record.pool, record.model,
            complexity=record.complexity, effort=record.effort,
            builder_complexity=record.builder_complexity))
    return coordinator, store, launcher, adapter, identity


def _assert_zero_outputs(store, launcher, identity, code):
    waiting = store.record_of(identity)
    assert waiting.state == "waiting" and waiting.claim and waiting.refusal == code
    assert store.permits_used("codex") == 0 and launcher.launches == []
    assert store.read_admission_receipt(identity) is None
    assert store.read_canary_attribution(identity) is None


def _corrupt_committed_authority(path, identity, authority):
    target = {
        "receipt": ("admission_receipts", "admission_receipts_no_update", "receipt_digest"),
        "history": (
            "safety_admission_history", "safety_admission_history_no_update",
            "history_digest"),
    }[authority]
    table, trigger, column = target
    attacker = sqlite3.connect(path)
    trigger_sql = attacker.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'trigger' AND name = ?", (trigger,),
    ).fetchone()[0]
    attacker.execute(f"DROP TRIGGER {trigger}")
    attacker.execute(
        f"UPDATE {table} SET {column} = ? WHERE stage_identity = ?",
        ("f" * 64, identity),
    )
    attacker.execute(trigger_sql)
    attacker.commit()
    rows = (
        tuple(attacker.execute(
            "SELECT * FROM admission_receipts WHERE stage_identity = ?", (identity,)
        ).fetchall()),
        tuple(attacker.execute(
            "SELECT * FROM safety_admission_history WHERE stage_identity = ?", (identity,)
        ).fetchall()),
    )
    attacker.close()
    return rows


def _full_capacity_store(tmp_path, *, register):
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        register=register)
    waiting = store.record_of(identity)
    blocker = Record(
        "octo/app|capacity-blocker|review|-", "review", "codex", 5,
        repo="octo/app", subject="capacity-blocker", model="gpt-5", state=RUNNING)
    assert store.upsert(blocker)
    briefing = _Briefings().brief_for(
        waiting.repo, waiting.stage, waiting.subject_revision)
    capability = _ready(waiting.stage, waiting.pool).ready_fact
    reservation = ReservationIntent(
        waiting.identity, waiting.launch_token, waiting.revision, 1_000,
        "daemon-capacity-order", 5, None, briefing, capability,
        waiting.route_cell_digest)
    return coordinator, store, launcher, waiting, reservation


def test_missing_route_authority_precedes_full_capacity_without_outputs(tmp_path):
    _coordinator_owner, store, launcher, waiting, reservation = _full_capacity_store(
        tmp_path, register=False)

    with pytest.raises(AdmissionRefused) as refused:
        store.reserve(reservation)

    assert refused.value.code == "route_cell:missing"
    assert store.record_of(waiting.identity) == waiting
    assert store.permits_used("codex") == 5 and launcher.launches == []
    assert store.read_admission_receipt(waiting.identity) is None
    assert store.read_canary_attribution(waiting.identity) is None
    store.close()


def test_valid_route_with_full_capacity_is_retryable_without_outputs(tmp_path):
    _coordinator_owner, store, launcher, waiting, reservation = _full_capacity_store(
        tmp_path, register=True)

    assert store.reserve(reservation) is None

    assert store.record_of(waiting.identity) == waiting
    assert store.permits_used("codex") == 5 and launcher.launches == []
    assert store.read_admission_receipt(waiting.identity) is None
    assert store.read_canary_attribution(waiting.identity) is None
    store.close()


def test_adr_627_pins_every_prerequisite_and_public_contract_digest():
    adr = (Path(__file__).parents[1]
           / "docs/adr/adr-627-composed-operational-admission.md").read_text()
    pins = {
        "#582": "a58dc0c84a7459774631048a67b3e71f8328d144",
        "#585": "bd818fa1d65c92def671192464207e6bc3904a34",
        "#628": "ab9c1ffa6f86de149db46f0dca96e89499159172",
        "effective-policy": "ea12ea2c28622dcbf2aeed7fa060f54250de3903d3942bfc8f6b8a04ffd53cef",
        "#641": "80f5a144621a990953d8ccacc08dd93a76090eaa",
        "#645": "46e0109a10e08a9ea6a8dc0621dcafde5a1d3d2f",
        "#646": "4ffde0671ff496feb6cad697e7536bb8e4dc0454",
        "#648": "b1ae64543761b808f7c0d357eded8551d684db3a",
        "Evaluation artifact":
            "a0e90b5b41c87ff67f257315cc6578b0b181249037f1ced2bac827cd3670d1ec",
        "Evaluation receipt":
            "f39ec2e8a6eeff7718ad3db5a58a1bc762aec46f7e59c9cddd6f4b0121707562",
        "Store v4 schema":
            "a2dd624722d0d4cbe93ffcf381f4de5cf6f52db1ebaa307453f51ede90986f7b",
    }
    for label, digest in pins.items():
        assert label in adr and digest in adr


def test_source_failure_is_advisory_when_final_prepared_root_is_ready(tmp_path):
    calls = []

    def capability(record, materialize):
        calls.append(materialize)
        return _ready(record.stage, record.pool) if materialize else _failure(
            "missing", record.stage, record.pool)

    coordinator, store, launcher, adapter, identity = _coordinator(tmp_path, capability)
    coordinator.cycle("codex")

    assert calls == [False, True]
    assert adapter.calls == 1 and len(launcher.launches) == 1
    receipt = store.read_admission_receipt(identity)
    assert receipt is not None and receipt.capability_id.startswith("capability-ready-v1:")
    assert store.record_of(identity).state == "running"
    store.close()


def test_unreadable_source_probe_is_advisory_when_final_prepared_root_is_ready(tmp_path):
    calls = []

    def capability(record, materialize):
        calls.append(materialize)
        if not materialize:
            raise RuntimeError("private source probe detail")
        return _ready(record.stage, record.pool)

    coordinator, store, launcher, adapter, identity = _coordinator(tmp_path, capability)
    coordinator.cycle("codex")

    assert calls == [False, True]
    assert adapter.calls == 1 and len(launcher.launches) == 1
    assert store.record_of(identity).state == "running"
    store.close()


@pytest.mark.parametrize("state", ("missing", "drifted", "incompatible"))
def test_only_final_prepared_root_failure_is_authoritative_and_retryable(
        tmp_path, state):
    deployed = False
    lines = []

    def capability(record, materialize):
        if materialize and not deployed:
            return _failure(state, record.stage, record.pool)
        return _ready(record.stage, record.pool)

    coordinator, store, launcher, adapter, identity = _coordinator(tmp_path, capability)
    coordinator._log = lines.append
    coordinator.cycle("codex")

    refusal = f"capability_environment_failure:{state}"
    waiting = store.record_of(identity)
    assert waiting.state == "waiting" and waiting.claim and waiting.refusal == refusal
    assert waiting.capability_preflight == "" and waiting.hold_reason is None
    assert store.permits_used("codex") == 0 and launcher.launches == []
    assert store.read_admission_receipt(identity) is None
    assert store.read_canary_attribution(identity) is None
    assert any(refusal in line for line in lines)
    assert all("secret evidence" not in line and "secret repair" not in line for line in lines)

    deployed = True
    coordinator.cycle("codex")
    assert len(launcher.launches) == 1 and store.record_of(identity).state == "running"
    store.close()


def test_missing_policy_authority_retries_after_deployment_without_outputs(tmp_path):
    briefings = _Briefings(available=False)
    receipts = _Receipts()
    capability = lambda record, _materialize: _ready(record.stage, record.pool)
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, capability, briefing=briefings, receipts=receipts)

    coordinator.cycle("codex")
    waiting = store.record_of(identity)
    assert waiting.state == "waiting" and waiting.claim and waiting.refusal == "missing_policy"
    assert store.permits_used("codex") == 0 and store.read_admission_receipt(identity) is None
    assert launcher.launches == []

    path = store.path
    store.close()
    briefings.available = True
    reopened = Store(path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(), receipts))
    recovered_launcher = _StartedLauncher()
    recovered = Coordinator(
        store=reopened, launcher=recovered_launcher, adapter=_Prepared(),
        capability_preflight=capability, briefing_resolver=briefings,
        route_selector=routing.select_route, daemon_generation="daemon-after-deploy")
    recovered.cycle("codex")
    assert len(recovered_launcher.launches) == 1
    assert reopened.record_of(identity).state == "running"
    reopened.close()


def test_missing_route_authority_retries_after_public_registration(tmp_path):
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        register=False)

    coordinator.cycle("codex")
    waiting = store.record_of(identity)
    assert waiting.state == "waiting" and waiting.claim
    assert waiting.refusal == "route_cell:missing"
    assert store.permits_used("codex") == 0 and store.read_admission_receipt(identity) is None
    assert launcher.launches == []

    store.register_route_selection(routing.select_route(
        waiting.repo, waiting.stage, waiting.pool, waiting.model,
        complexity=waiting.complexity, effort=waiting.effort,
        builder_complexity=waiting.builder_complexity))
    coordinator.cycle("codex")
    assert len(launcher.launches) == 1 and store.record_of(identity).state == "running"
    store.close()


def test_pointer_change_returns_route_cell_stale_without_consumption(tmp_path):
    receipts = _Receipts()
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        receipts=receipts)
    record = store.record_of(identity)
    old_digest = record.route_cell_digest
    authority_store = Store(store.path)
    authority = OperationalSafety(authority_store, promotion_receipts=receipts)
    candidate_selection = routing.select_route(
        record.repo, record.stage, record.pool, "gpt-5.6-sol",
        complexity=record.complexity, effort=record.effort,
        builder_complexity=record.builder_complexity)
    candidate = authority.register_route_cell(
        candidate_selection.repository, candidate_selection.stage,
        candidate_selection.provider, candidate_selection.model,
        candidate_selection.route_id, candidate_selection.launch_config)
    request = CanaryActivationRequest("receipt-stale", candidate.digest, old_digest, 0)
    receipts.issue(request)
    authority.approve_canary(request)
    authority_store.close()

    coordinator.cycle("codex")
    _assert_zero_outputs(store, launcher, identity, "route_cell:stale")
    store.close()


def test_quarantine_returns_named_route_code_without_consumption(tmp_path):
    checks = _Checks()
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        checks=checks)
    record = store.record_of(identity)
    authority_store = Store(store.path)
    authority = OperationalSafety(authority_store, check_evidence=checks)
    for suffix in ("first", "second"):
        request = ObservationRequest(
            record.repo, record.subject, record.subject_revision, "route-health", "1",
            record.route_cell_digest, f"evidence/{suffix}")
        checks.issue(request, "fail")
        authority.observe(request)
    authority_store.close()

    coordinator.cycle("codex")
    _assert_zero_outputs(store, launcher, identity, "route_cell:quarantined")
    store.close()


def test_corrupt_active_launch_config_returns_route_cell_unreadable_without_consumption(
        tmp_path):
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool))
    record = store.record_of(identity)
    attacker = sqlite3.connect(store.path)
    attacker.execute(
        "UPDATE safety_launch_configs SET content = ? WHERE digest = ?",
        (b"{}", record.launch_config_digest))
    attacker.commit()
    attacker.close()

    coordinator.cycle("codex")
    _assert_zero_outputs(store, launcher, identity, "route_cell:unreadable")
    store.close()


def test_durable_launch_config_mismatch_returns_named_code_without_consumption(tmp_path):
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool))
    record = store.record_of(identity)
    record.launch_config_digest = "f" * 64
    assert store.upsert(record)

    coordinator.cycle("codex")
    _assert_zero_outputs(store, launcher, identity, "route_cell:mismatched")
    store.close()


@pytest.mark.parametrize("activation_count", (0, 1, 3))
def test_lost_admission_ack_reopens_exact_launch_after_later_approved_activations(
        tmp_path, monkeypatch, activation_count):
    receipts = _Receipts()
    capability_calls = []

    def capability(record, materialize):
        capability_calls.append(materialize)
        return _ready(record.stage, record.pool)

    coordinator, store, first_launcher, _adapter, identity = _coordinator(
        tmp_path, capability, receipts=receipts)

    def lose_ack(name):
        if name == "after-commit":
            raise RuntimeError("lost admission acknowledgement")

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lose_ack))
    with pytest.raises(RuntimeError, match="lost admission acknowledgement"):
        coordinator.cycle("codex")
    committed = store.record_of(identity)
    receipt = store.read_admission_receipt(identity)
    assert committed.state == "running" and committed.start_fact is None
    assert receipt is not None and first_launcher.launches == []
    historical_digest = receipt.route_cell_digest
    path = store.path
    store.close()

    authority_store = Store(path)
    authority = OperationalSafety(authority_store, promotion_receipts=receipts)
    selection = routing.select_route(
        committed.repo, committed.stage, committed.pool, "gpt-5.6-sol",
        complexity=committed.complexity, effort=committed.effort,
        builder_complexity=committed.builder_complexity)
    active_digest = historical_digest
    for ordinal in range(activation_count):
        model = f"gpt-5.6-sol-{ordinal}"
        config = replace(
            selection.launch_config, internal_model=model, cli_model=model)
        candidate = authority.register_route_cell(
            selection.repository, selection.stage, selection.provider, model,
            selection.route_id, config)
        request = CanaryActivationRequest(
            f"receipt-pointer-change-{ordinal}", candidate.digest, active_digest, 0)
        receipts.issue(request)
        state = authority.approve_canary(request)
        assert state.active_route_cell_digest == candidate.digest
        active_digest = candidate.digest
    authority_store.close()

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lambda _name: None))
    reopened = Store(path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(), receipts))
    recovered_launcher = _StartedLauncher()

    def must_not_read_current_authority(*_args):
        raise AssertionError("recovery must not re-run current admission authority")

    recovered = Coordinator(
        store=reopened, launcher=recovered_launcher, adapter=_Prepared(),
        capability_preflight=must_not_read_current_authority,
        briefing_resolver=must_not_read_current_authority,
        route_selector=must_not_read_current_authority,
        daemon_generation="daemon-after-crash")
    recovered.cycle("codex")

    assert capability_calls == [False, True]
    assert len(recovered_launcher.launches) == 1
    launched = recovered_launcher.launches[0]
    assert launched.route_cell.digest == historical_digest
    if activation_count:
        assert launched.route_cell.digest != active_digest
    assert reopened.read_admission_receipt(identity) == receipt
    assert reopened.record_of(identity).start_fact == STARTED
    reopened.close()


@pytest.mark.parametrize("authority", ("receipt", "history"))
def test_unreadable_committed_authority_holds_without_start_or_attempt_and_continues_sibling(
        tmp_path, monkeypatch, authority):
    receipts = _Receipts()
    coordinator, store, first_launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        receipts=receipts)

    def lose_ack(name):
        if name == "after-commit":
            raise RuntimeError("lost admission acknowledgement")

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lose_ack))
    with pytest.raises(RuntimeError, match="lost admission acknowledgement"):
        coordinator.cycle("codex")
    assert first_launcher.launches == []
    sibling = coordinator.submit_stage(Submission(
        repo="octo/app", subject="sibling", stage="build", pool="codex",
        complexity="deep", subject_revision=REVISION))
    path = store.path
    store.close()

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lambda _name: None))
    reopened = Store(path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(), receipts))
    forensic_rows = _corrupt_committed_authority(path, identity, authority)
    recovered_launcher = _StartedLauncher()
    recovered = Coordinator(
        store=reopened, launcher=recovered_launcher, adapter=_Prepared(),
        capability_preflight=lambda record, _materialize: _ready(record.stage, record.pool),
        briefing_resolver=_Briefings(), route_selector=routing.select_route,
        daemon_generation="daemon-after-crash")

    outcomes = recovered.cycle("codex")

    held = reopened.record_of(identity)
    started_sibling = reopened.record_of(sibling)
    assert held.state == "held" and not held.claim and not held.hold_pending
    assert held.attempts == 0 and not held.attempt_committed and held.start_fact is None
    assert held.handoff_proof and outcomes[0].identity == identity
    assert started_sibling.state == "running" and started_sibling.start_fact == STARTED
    assert recovered_launcher.identities == [sibling]
    assert reopened.permits_used("codex") == started_sibling.demand
    with pytest.raises(StoreUnavailable, match="admission receipt is unreadable"):
        reopened.read_admission_receipt(identity)
    assert forensic_rows == (
        tuple(reopened._conn.execute(
            "SELECT * FROM admission_receipts WHERE stage_identity = ?", (identity,)
        ).fetchall()),
        tuple(reopened._conn.execute(
            "SELECT * FROM safety_admission_history WHERE stage_identity = ?", (identity,)
        ).fetchall()),
    )
    reopened.close()


def test_unreadable_committed_authority_pending_hold_survives_handoff_crash(
        tmp_path, monkeypatch):
    receipts = _Receipts()
    coordinator, store, first_launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        receipts=receipts)

    def lose_ack(name):
        if name == "after-commit":
            raise RuntimeError("lost admission acknowledgement")

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lose_ack))
    with pytest.raises(RuntimeError, match="lost admission acknowledgement"):
        coordinator.cycle("codex")
    assert first_launcher.launches == []
    path = store.path
    store.close()

    monkeypatch.setattr(Store, "_admission_checkpoint", staticmethod(lambda _name: None))
    reopened = Store(path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(), receipts))
    forensic_rows = _corrupt_committed_authority(path, identity, "history")

    class CrashHandoff(_Prepared):
        def finalize_hold(self, _record):
            raise RuntimeError("handoff crash")

    crash_launcher = _StartedLauncher()
    crashing = Coordinator(
        store=reopened, launcher=crash_launcher, adapter=CrashHandoff(),
        capability_preflight=lambda record, _materialize: _ready(record.stage, record.pool),
        briefing_resolver=_Briefings(), route_selector=routing.select_route,
        daemon_generation="daemon-after-crash")
    with pytest.raises(RuntimeError, match="handoff crash"):
        crashing.cycle("codex")
    pending = reopened.record_of(identity)
    assert pending.state == "waiting" and pending.claim and pending.hold_pending
    assert pending.attempts == 0 and not pending.attempt_committed
    assert reopened.permits_used("codex") == 0 and crash_launcher.launches == []
    reopened.close()

    final_store = Store(path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(), receipts))
    final_launcher = _StartedLauncher()
    final = Coordinator(
        store=final_store, launcher=final_launcher, adapter=_Prepared(),
        capability_preflight=lambda record, _materialize: _ready(record.stage, record.pool),
        briefing_resolver=_Briefings(), route_selector=routing.select_route,
        daemon_generation="daemon-after-second-crash")
    outcomes = final.cycle("codex")

    held = final_store.record_of(identity)
    assert held.state == "held" and not held.claim and not held.hold_pending
    assert held.attempts == 0 and held.handoff_proof and outcomes[0].identity == identity
    assert final_launcher.launches == [] and final_store.permits_used("codex") == 0
    assert forensic_rows == (
        tuple(final_store._conn.execute(
            "SELECT * FROM admission_receipts WHERE stage_identity = ?", (identity,)
        ).fetchall()),
        tuple(final_store._conn.execute(
            "SELECT * FROM safety_admission_history WHERE stage_identity = ?", (identity,)
        ).fetchall()),
    )
    final_store.close()
