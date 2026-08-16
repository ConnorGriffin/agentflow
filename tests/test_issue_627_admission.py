"""Public composed-admission and recovery proofs for issue 627."""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
import sqlite3

import pytest

from agentflow.canary_attribution import (
    ATTRIBUTION_CONTRACT_VERSION,
    ROW_DIGEST_DOMAIN,
    CanaryAttribution,
    _digest as _canary_digest,
)
from agentflow.capability_contracts import CapabilityPreflightResult, _ready_fact
from agentflow.coordinator import Coordinator, StageOutcome, Submission
from agentflow.coordinator.launcher import NOT_STARTED, STARTED, StartResult
from agentflow.coordinator.record import RUNNING, Record
from agentflow.coordinator.store import (
    AdmissionRefused,
    OperationalSafetyAndCanary,
    ReservationIntent,
    SafetySources,
    Store,
    StoreUnavailable,
    _digest as _store_digest,
)
from agentflow.effective_policy import (
    ApplicabilityFacts, BriefingAuthority, BriefingReceipt, EffectivePolicyResolver,
    NotApplicableBriefing, PolicyValidationError, ReadyBriefing, _finish, _hold,
)
from agentflow.evidence import ApprovedAuthority, AuthorityPointer, PromotionReceipt
from agentflow.operational_safety import CanaryActivationRequest, OperationalSafety
from agentflow.operational_safety import CheckEvidence, DETERMINISTIC_CHECKS, ObservationRequest
from agentflow.prompts import stage_prompt_spec
from agentflow.routing import routing


REVISION = "a" * 40
_ADMISSION_FACT_TABLES = (
    "admission_receipts",
    "safety_admission_history",
    "canary_attributions",
    "lesson_use_attributions",
)
_ADMISSION_FACT_DELETE_TRIGGERS = {
    table: f"{table}_no_delete" for table in _ADMISSION_FACT_TABLES
}


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


class _NeverStartsLauncher:
    def start(self, _record, _store, _admitted=None):
        return StartResult(NOT_STARTED)

    def is_alive(self, _family):
        return False


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


def _approved_briefing(repository, stage, subject_revision, receipt_id):
    authority = BriefingAuthority(
        "github", repository, "pulls/1/files/policy.json", subject_revision, "sha256", "b" * 64,
        "fleet-policy/0-to-1", "approval-1", subject_revision, "b" * 64, "fleet-policy/0-to-1",
        "github-authority", "v1", "verified")
    receipt = BriefingReceipt(receipt_id, "candidate-1", "approval-1", 1, True, authority)
    applicability = ApplicabilityFacts("fleet-policy/0-to-1", stage, subject_revision)
    value = {
        "applicability": applicability.value(), "briefing_digest": "", "briefing_id": "",
        "capabilities": [], "policy_version": 1, "receipts": [receipt.value()],
        "repository": repository, "schema": "briefing-v1", "stage": stage,
        "status": "ready", "subject_revision": subject_revision,
    }
    digest, identity, _ = _finish(value)
    return ReadyBriefing(repository, stage, subject_revision, digest, identity, 1, (receipt,), (),
                         applicability)


def _coordinator(
        tmp_path, capability, *, briefing=None, register=True, receipts=None, checks=None,
        launcher=None):
    path = tmp_path / "coordinator.db"
    receipts = receipts or _Receipts()
    store = Store(path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(check_evidence=checks), receipts))
    launcher = launcher or _StartedLauncher()
    adapter = _Prepared()
    coordinator = Coordinator(
        store=store, launcher=launcher, adapter=adapter,
        capability_preflight=capability,
        briefing_resolver=briefing or _Briefings(),
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


def test_stale_durable_briefing_is_refreshed_and_admitted(tmp_path):
    current = _approved_briefing("octo/app", "build", REVISION, "current")
    stale = _approved_briefing("octo/app", "build", REVISION, "stale")

    class _CurrentBriefings:
        def brief_for(self, repository, stage, subject_revision):
            assert (repository, stage, subject_revision) == ("octo/app", "build", REVISION)
            return current

    capability = lambda record, _materialize: _ready(record.stage, record.pool)
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, capability, briefing=_CurrentBriefings())
    waiting = store.record_of(identity)
    assert waiting is not None
    waiting.input_ptr = stage_prompt_spec("build").with_briefing(
        "Build the durable change.\n", stale)
    assert stale.briefing_id in waiting.input_ptr
    assert store.upsert(waiting)

    coordinator.cycle("codex")

    admitted = store.record_of(identity)
    assert admitted is not None and admitted.state == "running"
    assert admitted.refusal != "briefing_mismatch"
    assert admitted.input_ptr.count("<!-- agentflow-effective-briefing:") == 1
    assert current.briefing_id in admitted.input_ptr
    assert stale.briefing_id not in admitted.input_ptr
    assert launcher.identities == [identity]
    store.close()


def _insert_optional_admission_facts(store, record):
    lesson = {
        "stage_identity": record.identity,
        "repository": record.repo,
        "stage": record.stage,
        "subject_revision": record.subject_revision,
        "briefing_id": "briefing-v1:" + "b" * 64,
        "briefing_digest": "b" * 64,
        "promotion_receipt_id": "receipt-withdraw",
        "method_revision": "c" * 40,
    }
    store._conn.execute(
        "INSERT INTO lesson_use_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*lesson.values(), _store_digest({"domain": "lesson-use-attribution-v1", **lesson})),
    )
    canary = {
        "stage_identity": record.identity,
        "repository": record.repo,
        "route_cell_digest": record.route_cell_digest,
        "receipt_binding": "b" * 64,
        "method_revision": "c" * 40,
        "cohort_id": "d" * 64,
        "contract_version": ATTRIBUTION_CONTRACT_VERSION,
    }
    attribution = CanaryAttribution(
        **canary,
        attribution_digest=_canary_digest({"domain": ROW_DIGEST_DOMAIN, **canary}),
    )
    store._conn.execute(
        "INSERT INTO canary_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(asdict(attribution).values()),
    )


def _admission_fact_rows(store, identity):
    return {
        table: tuple(store._conn.execute(
            f"SELECT * FROM {table} WHERE stage_identity = ?", (identity,),
        ).fetchall())
        for table in _ADMISSION_FACT_TABLES
    }


def _assert_delete_triggers_enforced(store, identity):
    trigger_names = {
        row[0] for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger'",
        ).fetchall()
    }
    assert set(_ADMISSION_FACT_DELETE_TRIGGERS.values()) <= trigger_names
    for table in _ADMISSION_FACT_TABLES:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            store._conn.execute(
                f"DELETE FROM {table} WHERE stage_identity = ?", (identity,),
            )


def _two_never_started_admissions(tmp_path):
    coordinator, store, _launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        launcher=_NeverStartsLauncher())
    unrelated_identity = coordinator.submit_stage(Submission(
        repo="octo/app", subject="628", stage="build", pool="codex",
        complexity="deep", subject_revision="b" * 40))
    unrelated = store.record_of(unrelated_identity)
    assert unrelated is not None
    store.register_route_selection(routing.select_route(
        unrelated.repo, unrelated.stage, unrelated.pool, unrelated.model,
        complexity=unrelated.complexity, effort=unrelated.effort,
        builder_complexity=unrelated.builder_complexity))

    coordinator.cycle("codex")
    records = (store.record_of(identity), store.record_of(unrelated_identity))
    assert all(record is not None and record.start_fact == NOT_STARTED for record in records)
    for record in records:
        _insert_optional_admission_facts(store, record)
    return coordinator, store, identity, unrelated_identity


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
        "effective-policy": "783ebc4a6de2217b49130ae448f353a8c4ce62b712f0ce94cea49c53a7215c0d",
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
        "Store v5 schema":
            "7103be329c503a9f263ba6e3d4cec882913892b82e2dd0de744b0579f3351dd1",
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


def test_legacy_admission_identity_holds_and_frees_the_pool(tmp_path):
    class _LegacyBriefings:
        def brief_for(self, repository, stage, subject_revision):
            if not subject_revision:
                return None
            return _Briefings().brief_for(repository, stage, subject_revision)

    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        briefing=_LegacyBriefings())
    legacy = store.record_of(identity)
    assert legacy is not None
    legacy.subject_revision = ""
    legacy.route_id = ""
    legacy.route_cell_digest = ""
    legacy.launch_config_digest = ""
    assert store.upsert(legacy)
    sibling = coordinator.submit_stage(Submission(
        repo="octo/app", subject="628", stage="build", pool="codex",
        complexity="deep", subject_revision="b" * 40))

    outcomes = coordinator.cycle("codex")

    held = store.record_of(identity)
    started_sibling = store.record_of(sibling)
    assert held is not None and held.state == "held" and not held.claim
    assert held.hold_reason == "refused before start — admission_identity_migration_required"
    assert outcomes == [StageOutcome(identity, "build", "held", held.handoff_kind)]
    assert started_sibling is not None and started_sibling.state == "running"
    assert launcher.identities == [sibling]
    store.close()


def test_malformed_immutable_overlay_holds_and_frees_the_pool(tmp_path):
    class _MalformedOverlay:
        def __init__(self):
            self.calls = []

        def read(self, repository, subject_revision):
            self.calls.append((repository, subject_revision))
            raise PolicyValidationError("invalid immutable overlay")

    source = _MalformedOverlay()
    resolver = EffectivePolicyResolver(
        promotion_receipts=object(), overlay_source=source)

    class _MixedBriefings:
        def brief_for(self, repository, stage, subject_revision):
            if subject_revision == REVISION:
                return resolver.brief_for(repository, stage, subject_revision)
            return _Briefings().brief_for(repository, stage, subject_revision)

    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        briefing=_MixedBriefings())
    sibling = coordinator.submit_stage(Submission(
        repo="octo/app", subject="628", stage="build", pool="codex",
        complexity="deep", subject_revision="b" * 40))

    outcomes = coordinator.cycle("codex")

    held = store.record_of(identity)
    started_sibling = store.record_of(sibling)
    assert source.calls == [("octo/app", REVISION)]
    assert held is not None and held.state == "held" and not held.claim
    assert held.hold_reason == "refused before start — invalid_overlay_authority"
    assert outcomes == [StageOutcome(identity, "build", "held", held.handoff_kind)]
    assert started_sibling is not None and started_sibling.state == "running"
    assert launcher.identities == [sibling]
    store.close()


def test_transient_overlay_unavailability_retries_without_terminal_handoff(tmp_path):
    class _UnavailableOverlay:
        def read(self, _repository, _subject_revision):
            raise OSError("repository temporarily unavailable")

    resolver = EffectivePolicyResolver(
        promotion_receipts=object(), overlay_source=_UnavailableOverlay())

    class _RecoveringBriefings:
        available = False

        def brief_for(self, repository, stage, subject_revision):
            if not self.available:
                return resolver.brief_for(repository, stage, subject_revision)
            return _Briefings().brief_for(repository, stage, subject_revision)

    briefings = _RecoveringBriefings()
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        briefing=briefings)

    assert coordinator.cycle("codex") == []
    waiting = store.record_of(identity)
    assert waiting is not None and waiting.state == "waiting" and waiting.claim
    assert waiting.refusal == "invalid_overlay" and waiting.hold_reason is None

    briefings.available = True
    assert coordinator.cycle("codex") == []
    assert store.record_of(identity).state == "running"
    assert launcher.identities == [identity]
    store.close()


def test_withdraw_stage_cleans_only_its_four_composed_admission_facts(tmp_path):
    coordinator, store, identity, unrelated_identity = _two_never_started_admissions(tmp_path)
    before = _admission_fact_rows(store, unrelated_identity)
    assert all(_admission_fact_rows(store, identity).values())
    assert all(before.values())

    assert coordinator.withdraw_stage(identity) is True

    assert _admission_fact_rows(store, identity) == {
        table: () for table in _ADMISSION_FACT_TABLES
    }
    assert _admission_fact_rows(store, unrelated_identity) == before
    _assert_delete_triggers_enforced(store, unrelated_identity)
    store.close()


def test_withdraw_stage_failure_rolls_back_all_facts_and_delete_triggers(
        tmp_path, monkeypatch):
    import agentflow.canary_attribution as canary_attribution

    coordinator, store, identity, _unrelated_identity = _two_never_started_admissions(tmp_path)
    before = _admission_fact_rows(store, identity)

    def fail_mid_sequence(_conn, _stage_identity):
        raise sqlite3.OperationalError("injected discard failure")

    monkeypatch.setattr(
        canary_attribution, "rollback_never_started_canary_attribution", fail_mid_sequence)

    with pytest.raises(StoreUnavailable, match="cannot discard continuation"):
        coordinator.withdraw_stage(identity)

    assert store.record_of(identity) is not None
    assert _admission_fact_rows(store, identity) == before
    _assert_delete_triggers_enforced(store, identity)
    store.close()


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


def test_pointer_change_repins_active_route_without_spending_attempt(tmp_path, monkeypatch):
    receipts = _Receipts()
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        receipts=receipts)
    record = store.record_of(identity)
    original_identity = (
        record.repo, record.subject, record.stage, record.target, record.subject_revision)
    old_digest = record.route_cell_digest
    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "601")
    authority_store = Store(store.path)
    authority = OperationalSafety(authority_store, promotion_receipts=receipts)
    candidate_selection = routing.select_route(
        record.repo, record.stage, record.pool, record.model,
        complexity=record.complexity, effort=record.effort,
        builder_complexity=record.builder_complexity)
    candidate = authority.register_route_cell(
        candidate_selection.repository, candidate_selection.stage,
        candidate_selection.provider, candidate_selection.model,
        candidate_selection.route_id, candidate_selection.launch_config)
    request = CanaryActivationRequest("receipt-repin", candidate.digest, old_digest, 0)
    receipts.issue(request)
    authority.approve_canary(request)
    authority_store.close()

    coordinator.cycle("codex")

    repinned = store.record_of(identity)
    assert repinned.state == "waiting" and repinned.claim
    assert repinned.route_cell_digest == candidate.digest
    assert repinned.launch_config_digest == candidate.launch_config_digest
    assert (repinned.repo, repinned.subject, repinned.stage, repinned.target,
            repinned.subject_revision) == original_identity
    assert repinned.refusal == "" and repinned.refusals == 0
    assert repinned.attempts == 0 and store.permits_used("codex") == 0
    assert launcher.launches == []

    coordinator.cycle("codex")

    admitted = store.record_of(identity)
    assert admitted.state == "running" and admitted.attempts == 1
    assert launcher.identities == [identity]
    store.close()


def test_pointer_change_to_unselected_route_refuses_without_repin_loop(tmp_path):
    receipts = _Receipts()
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        receipts=receipts)
    record = store.record_of(identity)
    original_pin = (record.route_id, record.route_cell_digest, record.launch_config_digest)
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
    waiting = store.record_of(identity)
    assert (waiting.route_id, waiting.route_cell_digest,
            waiting.launch_config_digest) == original_pin
    store.close()


def test_quarantined_route_is_not_repinned_and_refuses(tmp_path):
    checks = _Checks()
    coordinator, store, launcher, _adapter, identity = _coordinator(
        tmp_path, lambda record, _materialize: _ready(record.stage, record.pool),
        checks=checks)
    record = store.record_of(identity)
    original_pin = (record.route_id, record.route_cell_digest, record.launch_config_digest)
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
    waiting = store.record_of(identity)
    assert (waiting.route_id, waiting.route_cell_digest,
            waiting.launch_config_digest) == original_pin
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
