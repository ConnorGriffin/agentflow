"""Public request-to-settlement Evidence v2 journeys for issue #581."""
from __future__ import annotations

from agentflow.evidence import EvidenceStore
import pytest

from agentflow.evidence_pipeline import (AttemptFact, EvidenceMiner, EvidenceProducer, FixFact,
    GitHubComment, GitHubRequest, LessonInput, ReviewFinding, SettlementFact, StageFact)


def _request(producer, *, digest="a" * 64, reply="IC-one", locator="issues/comments/1",
             captured_at="2026-08-12T00:02:00Z"):
    return producer.request(GitHubRequest(581, "I-one", "issues/581", "2026-08-12T00:00:00Z",
        captured_at, digest, (GitHubComment(reply, locator, "2026-08-12T00:01:00Z",
        captured_at, "b" * 64),)), criterion_count=2)


def _finding(producer, request, *, review="review-1", sha="c" * 40, sequence=1,
             validation="reproduced", failure="original_defect", action="fix",
             upstream="code-review", axis="standards", pass_name="full"):
    return producer.review(ReviewFinding(review, sha, axis, pass_name, sequence, action,
        upstream, request, "d" * 64, failure, validation, 1))


def test_request_to_finding_fix_and_merge_are_typed_and_joined(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    producer = EvidenceProducer(store, repository="octo/repo")
    request = _request(producer)
    finding = _finding(producer, request)
    fix = producer.fix(FixFact("review-1", "c" * 40, "e" * 40, (finding.finding_id,), request,
        "f" * 64, "reproduced", 2))
    merged = producer.settlement(SettlementFact("merge", "review-1", "e" * 40, request,
        "a" * 64, 3, fix.event.event_id, (finding.event.event_id,), "human_validated"))

    assert request.claim.producer_kind == "claim"
    assert store.read(finding.failure_event_id).failure_class == "original_defect"
    assert store.read(finding.review_action_event_id).review_action == "fix_before_completion"
    assert store.read(merged.event.links[1].target_event_id).subject == "disposition/merge/review-1"
    assert b"I-one" not in store.path.read_bytes()


def test_source_sets_require_durable_locator_capture_and_exact_selected_replies(tmp_path):
    producer = EvidenceProducer(EvidenceStore(path=tmp_path / "evidence.db"), repository="octo/repo")
    first, edited, other_reply = _request(producer), _request(producer, digest="c" * 64), _request(producer, reply="IC-two")
    moved_reply = _request(producer, locator="issues/comments/2")
    recaptured = _request(producer, captured_at="2026-08-12T00:03:00Z")
    assert len({first.subject.revision, edited.subject.revision, other_reply.subject.revision,
                moved_reply.subject.revision, recaptured.subject.revision}) == 5
    assert producer.provenance(first, None).status == "unavailable"
    with pytest.raises(ValueError, match="durable identity"):
        producer.request(GitHubRequest(581, "I-one", "issues/581", "now", "now", "a" * 64,
            (GitHubComment("", "issues/comments/1", "now", "now", "b" * 64),)))


def test_review_action_is_closed_and_stably_names_axis_pass_and_sequence(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    producer = EvidenceProducer(store, repository="octo/repo")
    request = _request(producer)
    first = _finding(producer, request)
    replay = _finding(producer, request)
    changed = (_finding(producer, request, sequence=2),
               _finding(producer, request, axis="spec"),
               _finding(producer, request, pass_name="targeted"))
    assert first.review_action_event_id == replay.review_action_event_id
    assert all(store.read(first.review_action_event_id).subject
               != store.read(item.review_action_event_id).subject for item in changed)
    with pytest.raises(ValueError, match="review action"):
        _finding(producer, request, action="invent-action")


def test_attack_objection_publicly_retains_a_stable_opaque_reference(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    producer = EvidenceProducer(store, repository="octo/repo")
    request = _request(producer)
    first = producer.stage(StageFact("attack-1", "attack", request, "f" * 64, "reproduced", 2,
        objection_ref="objection-1"))
    replay = producer.stage(StageFact("attack-1", "attack", request, "f" * 64, "reproduced", 3,
        objection_ref="objection-1"))
    edited = producer.stage(StageFact("attack-1", "attack", request, "a" * 64, "reproduced", 4,
        objection_ref="objection-1"))
    assert first.objection_id == replay.objection_id == edited.objection_id
    assert first.event.event_id == replay.event.event_id != edited.event.event_id
    assert store.read(first.event.event_id).subject == first.objection_id
    redraft = producer.stage(StageFact("redraft-1", "redraft", request, "b" * 64, "reproduced", 5,
        objection_ref="objection-1"))
    assert redraft.objection_id == first.objection_id
    assert any(store.read(link.target_event_id).producer_kind == "objection"
               for link in redraft.event.links)


def test_fix_lineage_is_public_for_ordinary_and_fix_introduced_paths(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    producer = EvidenceProducer(store, repository="octo/repo")
    request = _request(producer)
    first, second = _finding(producer, request), _finding(producer, request, sequence=2)
    ordinary = producer.fix(FixFact("review-1", "c" * 40, "e" * 40,
        (first.finding_id, second.finding_id), request, "f" * 64, "reproduced", 2))
    parent = next(link for link in ordinary.event.links if link.relation == "revises")
    assert store.read(ordinary.event.event_id).revision == "e" * 40
    assert store.read(parent.target_event_id).revision == "c" * 40
    defect = producer.fix(FixFact("review-1", "c" * 40, "f" * 40, (first.finding_id,), request,
        "a" * 64, "reproduced", 3, "fix_introduced_defect"))
    failure = store.read(defect.failure_event_id)
    assert (failure.reviewed_parent_revision, failure.fixer_revision) == ("c" * 40, "f" * 40)
    assert any(link.relation == "derives_from" and link.target_event_id == defect.failure_event_id
               for link in defect.event.links)


def test_settlement_distinguishes_park_and_retains_attempt_join(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    producer = EvidenceProducer(store, repository="octo/repo")
    request = _request(producer)
    finding = _finding(producer, request)
    fix = producer.fix(FixFact("review-1", "c" * 40, "e" * 40, (finding.finding_id,), request,
        "f" * 64, "reproduced", 2))
    attempt = producer.attempt(AttemptFact("attempt-1", "review", "fixed", 4, request, 12, 2))
    parked = producer.settlement(SettlementFact("park", "review-1", "e" * 40, request,
        "a" * 64, 5, fix.event.event_id, (finding.event.event_id,), "human_validated", attempt))
    disposition = store.read(parked.event.links[1].target_event_id)
    assert disposition.subject == "disposition/park/review-1"
    assert parked.attempt_join == attempt
    assert attempt.governing_event_ids == (request.revision.event_id,)
    assert b"attempt-1" not in store.path.read_bytes()


@pytest.mark.parametrize("failure_class", [
    "original_defect", "plan_gap", "slice_scope_error", "reviewer_false_claim",
    "speculative_preference", "fix_introduced_defect",
])
@pytest.mark.parametrize("validation,contributes", [
    ("reproduced", True), ("model_judged", True), ("human_validated", True),
    ("observed", False), ("refuted", False), ("unvalidated", False),
])
def test_miner_reads_every_failure_class_and_validation_from_evidence(
        tmp_path, failure_class, validation, contributes):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    producer = EvidenceProducer(store, repository="octo/repo")
    request = _request(producer)
    if failure_class == "fix_introduced_defect":
        finding = _finding(producer, request, upstream="plan-review")
        first = producer.fix(FixFact("review-1", "c" * 40, "e" * 40, (finding.finding_id,),
            request, "f" * 64, validation, 2, failure_class))
        second = producer.fix(FixFact("review-2", "d" * 40, "f" * 40, (finding.finding_id,),
            request, "f" * 64, validation, 3, failure_class))
    else:
        first = _finding(producer, request, validation=validation, failure=failure_class,
                         upstream="plan-review")
        second = _finding(producer, request, review="review-2", sha="e" * 40,
            validation=validation, failure=failure_class, upstream="plan-review")
    candidates = EvidenceMiner(store).candidates((LessonInput(first.event.event_id, "f" * 64),
        LessonInput(second.event.event_id, "f" * 64)), policy_version=1, nominated_at=3)
    assert bool(candidates) is contributes
    if contributes:
        assert candidates[0].candidate_id.startswith("lesson-")


def test_miner_rejects_forged_classification_and_method_inputs(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    producer = EvidenceProducer(store, repository="octo/repo")
    finding = _finding(producer, _request(producer))
    with pytest.raises(TypeError):
        LessonInput(finding.event.event_id, "f" * 64, "plan_gap")
    assert EvidenceMiner(store).candidates((LessonInput(finding.failure_event_id, "f" * 64),),
                                           policy_version=1, nominated_at=2) == ()
