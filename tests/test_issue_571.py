"""Public-interface integration for the governed learning closure (#571)."""
from __future__ import annotations

import inspect
import json
import sqlite3
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import (coordinated_research, coordinated_review, coordinated_revise, github,
                       pipeline, reviewer)
from agentflow.capability_contracts import CapabilityPreflightResult, _ready_fact
from agentflow.coordinator import Coordinator, ReviewStageAdapter, StageRouter
from agentflow.coordinator.launcher import STARTED, StartResult
from agentflow.coordinator.providers import ProviderCause, ProviderObservation
from agentflow.coordinator.record import COMPLETED, RUNNING, Record
from agentflow.coordinator.revise_stage import ReviseStageAdapter
from agentflow.coordinator.store import OperationalSafetyAndCanary, SafetySources, Store
from agentflow.coordinator.telemetry import AttemptTelemetry, AttemptUsage, record_attempt
from agentflow.effective_policy import (
    CapabilityRequirement, EffectivePolicyResolver, FleetPolicyV1, ReadyBriefing)
from agentflow.evidence import (
    ApprovedAuthority, AuthorityPointer, EvidenceError, EvidenceReceiptReader, EvidenceStore,
    FakeAuthorityVerifier, PromotionReceiptReader)
from agentflow.evidence_pipeline import EvidenceMiner, EvidenceProducer, LessonInput
from agentflow.learning import report
from agentflow.prompts import stage_prompt_spec
from agentflow.review_policy import ReviewState
from agentflow.routing import routing
from agentflow.worktree_ref import WorktreeRef


REPOSITORY = "ConnorGriffin/agentflow"
METHOD_ARTIFACT = Path(inspect.getsourcefile(reviewer) or "")
PROPOSAL_DIGEST = sha256(METHOD_ARTIFACT.read_bytes()).hexdigest()
METHOD_REVISION = "e" * 40
METHOD_POINTER = AuthorityPointer(
    "github", REPOSITORY, "pulls/571/files/agentflow/reviewer.py",
    METHOD_REVISION, "sha256", PROPOSAL_DIGEST, "fleet-policy/0-to-1")
METHOD_APPROVAL = ApprovedAuthority(
    METHOD_POINTER, "approval-issue-571", METHOD_REVISION, PROPOSAL_DIGEST,
    "fleet-policy/0-to-1", "github-authority", "v1", "verified")


class _NoOverlay:
    def read(self, _repository, _revision):
        return None


class _Observer:
    def observe(self, _record):
        return ProviderObservation(
            cause=ProviderCause.NONE, exit_status=0, has_end_fact=True,
            final_message='{"verdict":"PASS","approved_method":"applied"}',
            usage=AttemptUsage(input_tokens=7, output_tokens=3, cost_usd=0.25))


class _Launcher:
    def __init__(self):
        self.alive = set()
        self.started = []

    def start(self, record, store, admitted=None):
        attribution = store.read_lesson_use_attribution(record.identity)
        assert attribution is not None
        assert attribution.promotion_receipt_id in (record.input_ptr or "")
        self.started.append((record.identity, attribution.attribution_digest, record.input_ptr))
        family = "issue-571-fixture-family"
        record.start_fact = STARTED
        record.family = family
        record.process_alive = True
        assert store.upsert(record)
        self.alive.add(family)
        return StartResult(STARTED, family)

    def is_alive(self, family):
        return family in self.alive


def _review_record(identity: str, reviewed_sha: str, *, started_at: int) -> Record:
    state = ReviewState(change_author_tool="claude")
    return Record(
        identity=identity, stage="review", pool="codex", demand=1,
        repo=REPOSITORY, subject="571", target=reviewed_sha,
        subject_revision=reviewed_sha, source=WorktreeRef.for_review(
            "/private/tmp/issue-571", "codex", 571, "learning-loop").path,
        created_at=started_at - 1, started_at=started_at, state=RUNNING,
        **state.record_fields())


def _review_payload(reviewed_sha: str) -> str:
    return json.dumps({
        "verdict": "BLOCK", "depth": "targeted",
        "depth_reason": "one contained change", "axis": "combined",
        "change_author_tool": "claude", "reviewed_sha": reviewed_sha,
        "final_sha": reviewed_sha, "pushed_sha": "", "fixes": [],
        "follow_ups": [], "checks": ["public-interface fixture"],
        "findings": [{
            "action": "fix_before_completion", "summary": "private finding body",
            "grounding": "charter: public-interface coverage",
            "file": "agentflow/example.py", "line": 7,
            "failure_class": "original_defect",
        }],
        "uncertainty": None, "decision": "",
    }, sort_keys=True)


def _revise_record(review: Record) -> Record:
    state = ReviewState.from_record(review)
    assert state is not None
    return Record(
        identity=review.identity.replace("|review|", "|revise|"),
        stage="revise", pool="codex", demand=1, repo=review.repo,
        subject=review.subject, target=review.target, subject_revision=review.target or "",
        source=WorktreeRef.for_build(
            "/private/tmp/issue-571", "codex", 571, "learning-loop").path,
        created_at=review.created_at + 1, started_at=review.started_at + 1,
        state=RUNNING, lineage="codex", branch_lineage="codex", **state.record_fields())


def _ready(record, _materialize):
    fact = _ready_fact(record.stage, record.pool, b"issue-571-manifest", ())
    return CapabilityPreflightResult(record.stage, record.pool, (), "ready", (), "", fact)


def test_production_router_wires_terminal_intake_attack_and_research_evidence(tmp_path):
    evidence_path = tmp_path / "terminal-evidence.db"
    evidence = EvidenceStore(path=evidence_path)
    coordinator = pipeline.build_coordinator(
        store=Store(tmp_path / "terminal-coordinator.db"), evidence_store=evidence)
    records = [
        Record("terminal-intake", "intake", "codex", 1, repo=REPOSITORY, subject="1",
               subject_revision="1" * 40, input_ptr="private intake prompt",
               outcome='{"route":"close"}', started_at=10),
        Record("terminal-attack", "attack", "codex", 1, repo=REPOSITORY, subject="2",
               subject_revision="2" * 40, input_ptr="private attack prompt",
               outcome='{"objections":"private objection"}', started_at=20),
        Record("terminal-research", "research", "codex", 1, repo=REPOSITORY, subject="3",
               subject_revision="3" * 40, input_ptr="private research prompt",
               source=str(tmp_path / "research"), started_at=30),
    ]
    findings = Path(coordinated_research.findings_path(records[2]))
    findings.parent.mkdir(parents=True)
    findings.write_text("private research findings")

    for record in records:
        coordinator._adapter.project_outcome(record, ProviderObservation())

    with sqlite3.connect(evidence_path) as connection:
        kinds = [row[0] for row in connection.execute(
            "SELECT producer_kind FROM events WHERE producer_kind IN ('verification','objection')")]
    assert sorted(kinds) == ["objection", "objection", "verification", "verification"]
    retained = evidence_path.read_bytes()
    assert all(value not in retained for value in (
        b"private intake prompt", b"private attack prompt", b"private objection",
        b"private research prompt", b"private research findings"))


def test_public_learning_loop_closes_without_network_provider_or_daemon_side_effects(
        tmp_path, monkeypatch):
    evidence_path = tmp_path / "evidence.db"
    evidence = EvidenceStore(
        path=evidence_path, verifier=FakeAuthorityVerifier((METHOD_APPROVAL,)))
    producer = EvidenceProducer(evidence, repository=REPOSITORY)
    flow_store = Store(tmp_path / "review-flow.db")
    review_events = []
    revise_events = []

    review_adapter = StageRouter({"review": ReviewStageAdapter(
        verdict_ready=coordinated_review._verdict_ready,
        worktree_reset=lambda _record: True,
        capture_state=coordinated_review.capture_verdict_state,
        evidence=lambda record, observation: review_events.extend(
            coordinated_review.record_evidence(producer, record, observation)))})
    revise_adapter = StageRouter({"revise": ReviseStageAdapter(
        revision_ready=lambda _record, _observation: True,
        worktree_ready=lambda _record: True,
        evidence=lambda record, observation: revise_events.append(
            coordinated_revise.record_evidence(producer, record, observation)))})
    heads = {}
    monkeypatch.setattr(github, "open_pr_for_branch", lambda _repo, branch: heads[branch])

    for ordinal, (reviewed, revised) in enumerate((("a" * 40, "b" * 40),
                                                    ("c" * 40, "d" * 40)), 1):
        review = _review_record(
            f"{REPOSITORY}|571-{ordinal}|review|{reviewed}", reviewed,
            started_at=100 + ordinal * 10)
        observation = ProviderObservation(final_message=_review_payload(reviewed))
        outcome = review_adapter.capture(review, observation)
        assert outcome
        review.outcome = outcome
        assert flow_store.upsert(review)
        captured = ReviewState.from_record(flow_store.record_of(review.identity))
        assert captured is not None
        assert [item.failure_class for item in captured.findings] == ["original_defect"]
        review_adapter.project_outcome(review, observation)

        revise = _revise_record(review)
        parsed = WorktreeRef.parse(revise.source)
        assert parsed is not None
        heads[parsed.branch] = SimpleNamespace(number=571, head_ref_oid=revised)
        assert revise_adapter.verify(revise, ProviderObservation())
        revise_adapter.project_outcome(revise, ProviderObservation())
        revise.state = COMPLETED
        assert flow_store.upsert(revise)

    assert len(review_events) == len(revise_events) == 2
    assert [item.actionable_finding_ids for item in revise_events] == [
        (item.finding_id,) for item in review_events]
    receipt_reader = EvidenceReceiptReader(path=evidence_path)
    failure_receipts = [receipt_reader.read(event.failure_event_id) for event in review_events]
    assert len({item.normalized_signature for item in failure_receipts}) == 1
    assert {item.failure_class for item in failure_receipts} == {"original_defect"}
    assert b"private finding body" not in evidence_path.read_bytes()
    assert b"charter: public-interface coverage" not in evidence_path.read_bytes()

    candidates = EvidenceMiner(receipt_reader).candidates(tuple(
        LessonInput(event.event.event_id, PROPOSAL_DIGEST) for event in review_events),
        policy_version=1, nominated_at=150)
    assert len(candidates) == 1
    candidate = evidence.nominate(candidates[0])
    with pytest.raises(EvidenceError, match="unknown promotion receipt"):
        PromotionReceiptReader(path=evidence_path).read(f"receipt-{candidate.candidate_id}")

    coordinator_path = tmp_path / "coordinator.db"
    pre_store = Store(coordinator_path)
    assert pre_store.load_lesson_use_attributions_read_only(coordinator_path) == {}
    pre_store.close()
    promoted = evidence.promote(candidate.candidate_id, METHOD_POINTER, promoted_at=160)
    assert promoted.authoritative and promoted.authority == METHOD_APPROVAL
    assert promoted.authority.approved_hash == sha256(METHOD_ARTIFACT.read_bytes()).hexdigest()

    promotion_reader = PromotionReceiptReader(path=evidence_path)
    policy = FleetPolicyV1(1, (EffectivePolicyResolver._receipt_value(promoted),), ())
    implementation = inspect.getclosurevars(
        EffectivePolicyResolver.brief_for).nonlocals["implementation"]

    def fixture_policy(self, repo, stage, subject_revision):
        return implementation(self, repo, stage, subject_revision, policy)

    monkeypatch.setattr(EffectivePolicyResolver, "brief_for", fixture_policy)
    resolver = EffectivePolicyResolver(
        promotion_receipts=promotion_reader, overlay_source=_NoOverlay())
    briefing = resolver.brief_for(REPOSITORY, "review", "f" * 40)
    assert isinstance(briefing, ReadyBriefing)
    assert briefing.receipts[0].receipt_id == promoted.receipt_id

    build = Record(
        "approved-method-build", "build", "claude", 1, repo=REPOSITORY,
        subject="571", target="f" * 40, subject_revision="f" * 40,
        source=WorktreeRef.for_build(
            "/private/tmp/issue-571", "codex", 571, "learning-loop").path,
        state=COMPLETED, claim=True)
    approved_submission = coordinated_review.review_submission(
        build, "f" * 40, "codex", 571,
        acceptance="Use the exact promoted Review methodology.")
    assert approved_submission is not None
    approved_prompt = approved_submission.input_ptr
    method_instruction = "Classify failure independently from action as exactly one of"
    consumed_prompt = stage_prompt_spec("review").with_briefing(approved_prompt, briefing)
    assert consumed_prompt != approved_prompt
    assert method_instruction in consumed_prompt
    assert promoted.receipt_id in consumed_prompt

    store = Store(coordinator_path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(), promotion_reader))
    assert store.upsert(build)
    before = Record(
        "pre-adoption-review", "review", "codex", 1, repo=REPOSITORY,
        subject="before", subject_revision="1" * 40, state=COMPLETED)
    assert store.upsert(before)
    record_attempt(coordinator_path, AttemptTelemetry(
        "pre-attempt", before.identity, REPOSITORY, before.subject, "review", "codex",
        "gpt-5", "deep", None, None, 1, False, 0, 0, 0, True, "PASS", "none",
        "incomplete", 1_723_686_400, 1_723_686_410, usage=AttemptUsage()))

    launcher = _Launcher()
    adapter = StageRouter({"review": ReviewStageAdapter(
        verdict_ready=lambda _record, _observation: True,
        worktree_reset=lambda _record: True, observer=_Observer())})
    coordinator = Coordinator(
        store=store, launcher=launcher, adapter=adapter,
        capability_preflight=_ready, briefing_resolver=resolver,
        route_selector=routing.select_route, daemon_generation="issue-571-fixture")
    monkeypatch.setattr("agentflow.coordinator.coordinator.time.time",
                        lambda: 1_723_686_500)
    identity = coordinator.submit_stage(approved_submission)
    waiting = store.record_of(identity)
    assert waiting is not None
    store.register_route_selection(routing.select_route(
        waiting.repo, waiting.stage, waiting.pool, waiting.model,
        complexity=waiting.complexity, effort=waiting.effort,
        builder_complexity=waiting.builder_complexity))
    assert store.read_lesson_use_attribution(identity) is None
    assert coordinator.cycle("codex", now=1_723_686_500) == []
    assert launcher.started and store.read_lesson_use_attribution(identity) is not None
    launcher.alive.clear()
    outcomes = coordinator.cycle("codex", now=1_723_686_510)
    assert [item.status for item in outcomes] == ["completed"]

    original_attribution = store.read_lesson_use_attribution(identity)
    store.close()
    store = Store(coordinator_path, admission_mode=OperationalSafetyAndCanary(
        SafetySources(), promotion_reader))
    assert store.read_lesson_use_attribution(identity) == original_attribution

    conflict_policy = FleetPolicyV1(
        1, policy.receipts,
        (CapabilityRequirement("fixture-method", "v1", "0" * 64),))
    conflicting_briefing = implementation(
        resolver, REPOSITORY, "review", "f" * 40, conflict_policy)
    assert isinstance(conflicting_briefing, ReadyBriefing)
    conflict_store = Store(
        tmp_path / "conflicting-attribution.db",
        admission_mode=OperationalSafetyAndCanary(SafetySources(), promotion_reader))
    conflict_launcher = _Launcher()
    conflict_coordinator = Coordinator(
        store=conflict_store, launcher=conflict_launcher, adapter=adapter,
        capability_preflight=_ready, briefing_resolver=resolver,
        route_selector=routing.select_route, daemon_generation="issue-571-conflict-fixture")
    conflict_identity = conflict_coordinator.submit_stage(
        replace(approved_submission, transfer_from=None))
    conflict_waiting = conflict_store.record_of(conflict_identity)
    assert conflict_waiting is not None
    conflict_store.register_route_selection(routing.select_route(
        conflict_waiting.repo, conflict_waiting.stage, conflict_waiting.pool,
        conflict_waiting.model, complexity=conflict_waiting.complexity,
        effort=conflict_waiting.effort,
        builder_complexity=conflict_waiting.builder_complexity))
    values = {
        "stage_identity": conflict_identity, "repository": conflict_waiting.repo,
        "stage": conflict_waiting.stage,
        "subject_revision": conflict_waiting.subject_revision,
        "briefing_id": conflicting_briefing.briefing_id,
        "briefing_digest": conflicting_briefing.briefing_digest,
        "promotion_receipt_id": promoted.receipt_id,
        "method_revision": promoted.authority.approved_revision,
    }
    digest = sha256(json.dumps(
        {"domain": "lesson-use-attribution-v1", **values}, sort_keys=True,
        separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode()).hexdigest()
    conflict_store._conn.execute(
        "INSERT INTO lesson_use_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (*values.values(), digest))

    assert conflict_coordinator.cycle("codex", now=1_723_686_600) == []
    refused = conflict_store.record_of(conflict_identity)
    assert refused is not None and refused.refusal == "briefing_mismatch"
    assert conflict_launcher.started == []
    assert conflict_store.read_admission_receipt(conflict_identity) is None
    conflict_store.close()

    learning = report(
        REPOSITORY, date(2024, 8, 15), date(2024, 8, 16), coordinator_path)
    pre = learning["attribution"]["cohorts"]["pre_adoption"]
    post = learning["attribution"]["cohorts"]["post_adoption"]
    assert learning["attribution"]["kind"] == "observational_non_causal"
    assert pre["revision_required_rate"]["denominator"] == 1
    assert post["revision_required_rate"]["denominator"] == 1
    assert post["attributed_stage_records"] == 1
    assert post["tokens"] == {"total": 10, "attempts_known": 1, "attempts_unknown": 0}

    attribution = store.read_lesson_use_attribution(identity)
    trace = {
        "candidate": candidate.candidate_id,
        "decision_events": [item.finding_id for item in review_events],
        "fix_events": [item.event.event_id for item in revise_events],
        "promotion_receipt": promoted.receipt_id,
        "briefing": briefing.briefing_id,
        "lesson_use": attribution.attribution_digest,
        "post_revision_rate": post["revision_required_rate"],
    }
    print("ISSUE571_TRACE=" + json.dumps(trace, sort_keys=True, separators=(",", ":")))
    flow_store.close()
    store.close()
