"""Public-interface integration for the governed learning closure (#571)."""
from __future__ import annotations

import inspect
import json
from datetime import date
from types import SimpleNamespace

import pytest

from agentflow import coordinated_review, coordinated_revise, github
from agentflow.capability_contracts import CapabilityPreflightResult, _ready_fact
from agentflow.coordinator import Coordinator, ReviewStageAdapter
from agentflow.coordinator.launcher import STARTED, StartResult
from agentflow.coordinator.providers import ProviderCause, ProviderObservation
from agentflow.coordinator.record import COMPLETED, RUNNING, Record
from agentflow.coordinator.revise_stage import ReviseStageAdapter
from agentflow.coordinator.store import (
    AdmissionRefused, OperationalSafetyAndCanary, SafetySources, Store)
from agentflow.coordinator.telemetry import AttemptTelemetry, AttemptUsage, record_attempt
from agentflow.effective_policy import EffectivePolicyResolver, FleetPolicyV1, ReadyBriefing
from agentflow.evidence import (
    ApprovedAuthority, AuthorityPointer, EvidenceError, EvidenceReceiptReader, EvidenceStore,
    FakeAuthorityVerifier, PromotionReceiptReader)
from agentflow.evidence_pipeline import EvidenceMiner, EvidenceProducer, LessonInput
from agentflow.learning import report
from agentflow.prompts import stage_prompt_spec
from agentflow.review_policy import ReviewAction, ReviewFinding, ReviewState
from agentflow.routing import routing
from agentflow.worktree_ref import WorktreeRef


REPOSITORY = "ConnorGriffin/agentflow"
PROPOSAL_DIGEST = "9" * 64
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
    state = ReviewState(findings=(ReviewFinding(
        ReviewAction.FIX, "private finding body", "charter: public-interface coverage",
        "agentflow/example.py", 7, "original_defect"),))
    return Record(
        identity=identity, stage="review", pool="codex", demand=1,
        repo=REPOSITORY, subject="571", target=reviewed_sha,
        subject_revision=reviewed_sha, source=WorktreeRef.for_review(
            "/private/tmp/issue-571", "codex", 571, "learning-loop").path,
        created_at=started_at - 1, started_at=started_at, state=RUNNING,
        **state.record_fields())


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


def test_public_learning_loop_closes_without_network_provider_or_daemon_side_effects(
        tmp_path, monkeypatch):
    evidence_path = tmp_path / "evidence.db"
    evidence = EvidenceStore(
        path=evidence_path, verifier=FakeAuthorityVerifier((METHOD_APPROVAL,)))
    producer = EvidenceProducer(evidence, repository=REPOSITORY)
    flow_store = Store(tmp_path / "review-flow.db")
    review_events = []
    revise_events = []

    review_adapter = ReviewStageAdapter(
        verdict_ready=lambda _record, _observation: True,
        worktree_reset=lambda _record: True,
        evidence=lambda record, observation: review_events.extend(
            coordinated_review.record_evidence(producer, record, observation)))
    revise_adapter = ReviseStageAdapter(
        revision_ready=lambda _record, _observation: True,
        worktree_ready=lambda _record: True,
        evidence=lambda record, observation: revise_events.append(
            coordinated_revise.record_evidence(producer, record, observation)))
    heads = {}
    monkeypatch.setattr(github, "open_pr_for_branch", lambda _repo, branch: heads[branch])

    for ordinal, (reviewed, revised) in enumerate((("a" * 40, "b" * 40),
                                                    ("c" * 40, "d" * 40)), 1):
        review = _review_record(
            f"{REPOSITORY}|571-{ordinal}|review|{reviewed}", reviewed,
            started_at=100 + ordinal * 10)
        outcome = review_adapter.capture(
            review, ProviderObservation(final_message='{"verdict":"BLOCK"}'))
        assert outcome
        review.outcome = outcome
        assert flow_store.upsert(review)
        review_adapter.project_outcome(review, ProviderObservation(final_message=outcome))

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
    baseline_prompt = approved_prompt.replace(method_instruction, "Use the prior classification")
    consumed_prompt = stage_prompt_spec("review").with_briefing(approved_prompt, briefing)
    assert approved_prompt != baseline_prompt
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
    adapter = ReviewStageAdapter(
        verdict_ready=lambda _record, _observation: True,
        worktree_reset=lambda _record: True, observer=_Observer())
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
    record = store.record_of(identity)
    assert record is not None
    store._conn.execute("BEGIN IMMEDIATE")
    assert store._insert_or_validate_lesson_use(record, briefing) == original_attribution
    store._conn.execute("ROLLBACK")
    conflicting = resolver.brief_for(REPOSITORY, "review", "a" * 40)
    store._conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(AdmissionRefused, match="briefing_mismatch"):
        store._insert_or_validate_lesson_use(record, conflicting)
    store._conn.execute("ROLLBACK")

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
