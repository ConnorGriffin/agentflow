from __future__ import annotations

import pytest
import sqlite3

from agentflow.evidence import (AuthorityPointer, EvidenceStore, Observation,
                                SubjectRevision, ApprovedAuthority, Evaluation,
                                EvidenceError, FakeAuthorityVerifier, LessonCandidate)
from agentflow.evidence_contract import validate_fixtures


def _source():
    return AuthorityPointer("github", "octo/repo", "issues/42", "a" * 40,
                            "sha256", "b" * 64, "issue")


def _observation(*, source=None, revision="a" * 40):
    return Observation(
        observation_id="obs-1" if source is None else "obs-2",
        subject=SubjectRevision("review", "pr/42", revision),
        failure_class="original_defect", validation_state="observed",
        signature_digest="c" * 64, normalizer_version="v1", source=source or _source(),
        observed_at=1,
    )


def test_observe_returns_one_canonical_event_but_keeps_source_observations(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    first = store.observe(_observation())
    second = store.observe(_observation(source=AuthorityPointer(
        "github", "octo/repo", "issues/43", "d" * 40, "sha256", "e" * 64, "issue")))
    assert first.event_id == second.event_id
    assert first.recurrence_count == 1
    assert second.observation_ids == ("obs-1", "obs-2")


def test_same_signature_on_a_different_reviewed_sha_is_a_distinct_event(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    first = store.observe(_observation())
    second = store.observe(_observation(revision="f" * 40, source=AuthorityPointer(
        "github", "octo/repo", "issues/43", "d" * 40, "sha256", "e" * 64, "issue")))
    assert first.event_id != second.event_id


def test_reusing_an_observation_id_with_different_source_facts_is_rejected(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    store.observe(_observation())
    with pytest.raises(EvidenceError, match="immutable"):
        store.observe(Observation("obs-1", SubjectRevision("review", "pr/42", "a" * 40),
            "original_defect", "observed", "c" * 64, "v1", AuthorityPointer(
                "github", "octo/repo", "issues/43", "d" * 40, "sha256", "e" * 64, "issue"), 1))


def test_immutable_subject_revisions_and_closed_facts_reject_unredacted_input():
    with pytest.raises(EvidenceError, match="exact reviewed SHA"):
        SubjectRevision("review", "pr/42", "main")
    with pytest.raises(EvidenceError, match="locator"):
        SubjectRevision("issue", "issue/42", "v1", "issues/42?body=secret", "a" * 64)
    with pytest.raises(EvidenceError, match="failure_class"):
        Observation("obs", SubjectRevision("review", "pr/42", "a" * 40), "raw finding",
                    "observed", "b" * 64, "v1", _source(), 1)
    with pytest.raises(EvidenceError, match="content_hash"):
        AuthorityPointer("github", "octo/repo", "issues/42", "a" * 40, "sha256",
                         "a prompt or source body", "issue")


def test_fix_introduced_defect_keeps_parent_and_fixer_lineage_separate(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    event = store.observe(Observation("obs-fix", SubjectRevision("review", "pr/42", "a" * 40),
        "fix_introduced_defect", "reproduced", "c" * 64, "v1", _source(), 1,
        reviewed_parent_revision="b" * 40, fixer_revision="d" * 40))
    original = store.observe(_observation())
    assert event.event_id != original.event_id


def test_evaluation_nomination_and_verified_promotion_are_idempotent(tmp_path):
    authority = _source()
    approved = ApprovedAuthority(authority, "approval-1", authority.revision, authority.content_hash,
                                 authority.scope, "fake", "v1", "verified")
    store = EvidenceStore(path=tmp_path / "evidence.db", verifier=FakeAuthorityVerifier((approved,)))
    event = store.observe(_observation())
    assert store.evaluate(Evaluation("evaluation-1", event.event_id, "human_validated", 2)).event_id == event.event_id
    candidate = LessonCandidate("candidate-1", (event.event_id,), "d" * 64, 1, 3)
    assert store.nominate(candidate) == candidate
    first = store.promote(candidate.candidate_id, authority, promoted_at=4)
    second = store.promote(candidate.candidate_id, authority, promoted_at=5)
    assert first == second and first.approval_id == "approval-1"


def test_promotion_requires_verified_exact_authority(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    event = store.observe(_observation())
    store.nominate(LessonCandidate("candidate-1", (event.event_id,), "d" * 64, 1, 3))
    with pytest.raises(EvidenceError, match="not verified"):
        store.promote("candidate-1", _source(), promoted_at=4)
    with pytest.raises(EvidenceError, match="exact authority"):
        ApprovedAuthority(_source(), "approval-1", "b" * 40, "c" * 64, "issue", "fake", "v1", "verified")


def test_retention_expires_unreferenced_and_abandoned_candidates_but_keeps_effective_versions(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    old = store.observe(_observation())
    assert store.brief_for("pr/42", now=90 * 24 * 60 * 60 + 2) == ()

    # A nomination alone does not pin evidence after the 90-day window.
    event = store.observe(_observation())
    store.nominate(LessonCandidate("candidate-old", (event.event_id,), "d" * 64, 1, 1))
    assert store.brief_for("pr/42", now=90 * 24 * 60 * 60 + 2) == ()


def test_effective_policy_and_one_successor_retain_a_promoted_candidate(tmp_path):
    authority = _source()
    approved = ApprovedAuthority(authority, "approval-1", authority.revision, authority.content_hash,
                                 authority.scope, "fake", "v1", "verified")
    store = EvidenceStore(path=tmp_path / "evidence.db", verifier=FakeAuthorityVerifier((approved,)))
    event = store.observe(_observation())
    store.nominate(LessonCandidate("candidate-1", (event.event_id,), "d" * 64, 1, 1))
    store.promote("candidate-1", authority, promoted_at=2)
    late = 90 * 24 * 60 * 60 + 2
    assert store.brief_for("pr/42", now=late, effective_policy_versions=(1,))
    assert store.brief_for("pr/42", now=late, effective_policy_versions=(2,))  # superseding version
    assert store.brief_for("pr/42", now=late, effective_policy_versions=(3,)) == ()


def test_versioned_contract_fixtures_admit_only_redacted_envelopes():
    validate_fixtures(__import__("pathlib").Path("docs/evidence"))


def test_separate_evidence_schema_fails_closed_and_never_changes_records_db(tmp_path):
    records = tmp_path / "records.db"
    conn = sqlite3.connect(records); conn.execute("CREATE TABLE records (id TEXT)"); conn.commit(); conn.close()
    with pytest.raises(EvidenceError, match="unversioned"):
        EvidenceStore(path=records)
    conn = sqlite3.connect(records)
    assert conn.execute("SELECT name FROM sqlite_master WHERE name='records'").fetchone()
    conn.close()
