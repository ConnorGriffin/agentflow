"""Public composed-admission and recovery proofs for issue 627."""

from __future__ import annotations

import sqlite3

import pytest

from agentflow.capability_contracts import CapabilityPreflightResult, _ready_fact
from agentflow.coordinator import Coordinator, Submission
from agentflow.coordinator.launcher import STARTED, StartResult
from agentflow.coordinator.store import OperationalSafetyAndCanary, SafetySources, Store
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
        self.alive = set()

    def start(self, record, store, admitted=None):
        assert admitted is not None
        self.launches.append(admitted)
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


def test_lost_admission_ack_reopens_and_launches_historical_receipt_after_pointer_change(
        tmp_path, monkeypatch):
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
    candidate_selection = routing.select_route(
        committed.repo, committed.stage, committed.pool, "gpt-5.6-sol",
        complexity=committed.complexity, effort=committed.effort,
        builder_complexity=committed.builder_complexity)
    candidate = authority.register_route_cell(
        candidate_selection.repository, candidate_selection.stage,
        candidate_selection.provider, candidate_selection.model,
        candidate_selection.route_id, candidate_selection.launch_config)
    request = CanaryActivationRequest(
        "receipt-pointer-change", candidate.digest, historical_digest, 0)
    receipts.issue(request)
    state = authority.approve_canary(request)
    assert state.active_route_cell_digest == candidate.digest
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
    assert launched.route_cell.digest != candidate.digest
    assert reopened.read_admission_receipt(identity) == receipt
    assert reopened.record_of(identity).start_fact == STARTED
    reopened.close()
