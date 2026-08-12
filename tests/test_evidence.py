from __future__ import annotations

import pytest
import sqlite3

from agentflow.evidence import (AuthorityPointer, EvidenceStore, Observation,
                                SubjectRevision, ApprovedAuthority, Evaluation,
                                EvidenceError, FakeAuthorityVerifier, LessonCandidate)
from agentflow.evidence_contract import validate_fixtures


OLD_V1_DDL = """
CREATE TABLE events (event_id TEXT PRIMARY KEY, repository TEXT NOT NULL, subject TEXT NOT NULL, revision TEXT NOT NULL, failure_class TEXT NOT NULL, signature TEXT NOT NULL, normalizer TEXT NOT NULL, UNIQUE(repository,subject,revision,failure_class,signature,normalizer));
CREATE TABLE observations (observation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_repository TEXT NOT NULL, source_locator TEXT NOT NULL, source_revision TEXT NOT NULL, source_hash_algorithm TEXT NOT NULL, source_hash TEXT NOT NULL, source_scope TEXT NOT NULL, validation_state TEXT NOT NULL, observed_at INTEGER NOT NULL, parent_revision TEXT NOT NULL, fixer_revision TEXT NOT NULL);
CREATE TABLE evaluations (evaluation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, validation_state TEXT NOT NULL, evaluated_at INTEGER NOT NULL);
CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY, proposal_digest TEXT NOT NULL, policy_version INTEGER NOT NULL, nominated_at INTEGER NOT NULL);
CREATE TABLE candidate_events (candidate_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(candidate_id,event_id));
CREATE TABLE receipts (candidate_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, approval_id TEXT NOT NULL, policy_version INTEGER NOT NULL, promoted_at INTEGER NOT NULL);
"""


def _old_v1(path):
    conn = sqlite3.connect(path)
    conn.executescript(OLD_V1_DDL)
    conn.execute("INSERT INTO events VALUES ('event-1', 'octo/repo', 'pr/42', ?, 'original_defect', ?, 'v1')", ("a" * 40, "b" * 64))
    conn.execute("INSERT INTO observations VALUES ('obs-1', 'event-1', 'github', 'octo/repo', 'issues/42', ?, 'sha256', ?, 'issue', 'observed', 1, '', '')", ("a" * 40, "c" * 64))
    conn.execute("INSERT INTO evaluations VALUES ('evaluation-1', 'event-1', 'human_validated', 2)")
    conn.execute("INSERT INTO candidates VALUES ('candidate-1', ?, 1, 3)", ("d" * 64,))
    conn.execute("INSERT INTO candidate_events VALUES ('candidate-1', 'event-1')")
    conn.execute("INSERT INTO receipts VALUES ('candidate-1', 'receipt-candidate-1', 'approval-1', 1, 4)")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()


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
    assert first.authority == approved
    assert EvidenceStore(path=tmp_path / "evidence.db", verifier=FakeAuthorityVerifier((approved,))).promote(
        candidate.candidate_id, authority, promoted_at=6) == first


def test_evaluation_and_nomination_replays_require_exact_immutable_facts(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    first = store.observe(_observation())
    second = store.observe(_observation(revision="f" * 40, source=AuthorityPointer(
        "github", "octo/repo", "issues/43", "d" * 40, "sha256", "e" * 64, "issue")))
    evaluation = Evaluation("evaluation-1", first.event_id, "human_validated", 2)
    assert store.evaluate(evaluation) == evaluation
    assert store.evaluate(evaluation) == evaluation
    with pytest.raises(EvidenceError, match="immutable"):
        store.evaluate(Evaluation("evaluation-1", first.event_id, "refuted", 2))
    candidate = LessonCandidate("candidate-1", (first.event_id,), "d" * 64, 1, 3)
    assert store.nominate(candidate) == candidate
    assert store.nominate(candidate) == candidate
    with pytest.raises(EvidenceError, match="immutable"):
        store.nominate(LessonCandidate("candidate-1", (second.event_id,), "d" * 64, 1, 3))
    with pytest.raises(EvidenceError, match="immutable"):
        store.nominate(LessonCandidate("candidate-1", (first.event_id,), "e" * 64, 1, 3))


def test_promotion_requires_verified_exact_authority(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    event = store.observe(_observation())
    store.nominate(LessonCandidate("candidate-1", (event.event_id,), "d" * 64, 1, 3))
    with pytest.raises(EvidenceError, match="not verified"):
        store.promote("candidate-1", _source(), promoted_at=4)
    with pytest.raises(EvidenceError, match="exact authority"):
        ApprovedAuthority(_source(), "approval-1", "b" * 40, "c" * 64, "issue", "fake", "v1", "verified")


def test_promotion_rejects_a_verifier_response_for_another_authority(tmp_path):
    requested = _source()
    other = AuthorityPointer("github", "octo/repo", "issues/43", "d" * 40,
                             "sha256", "e" * 64, "issue")
    approved = ApprovedAuthority(other, "approval-1", other.revision, other.content_hash,
                                 other.scope, "fake", "v1", "verified")

    class WrongAuthorityVerifier:
        def verify(self, authority):
            return approved

    store = EvidenceStore(path=tmp_path / "evidence.db", verifier=WrongAuthorityVerifier())
    event = store.observe(_observation())
    store.nominate(LessonCandidate("candidate-1", (event.event_id,), "d" * 64, 1, 3))
    with pytest.raises(EvidenceError, match="requested authority"):
        store.promote("candidate-1", requested, promoted_at=4)


def test_authority_pointer_requires_kind_specific_immutable_revision():
    with pytest.raises(EvidenceError, match="immutable revision"):
        AuthorityPointer("github", "octo/repo", "issues/42", "main", "sha256", "a" * 64, "issue")
    with pytest.raises(EvidenceError, match="immutable revision"):
        AuthorityPointer("repository", "octo/repo", "docs/guide", "main", "sha256", "a" * 64, "document")
    pointer = AuthorityPointer("repository", "octo/repo", "docs/guide", "sha256:" + "a" * 64,
                               "sha256", "a" * 64, "document")
    assert pointer.revision.startswith("sha256:")


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


def test_exact_e79a_v1_migrates_atomically_preserving_data_and_marking_receipt_legacy(tmp_path):
    path = tmp_path / "evidence.db"
    _old_v1(path)
    authority = _source()
    approved = ApprovedAuthority(authority, "approval-1", authority.revision, authority.content_hash,
                                 authority.scope, "fake", "v1", "verified")
    store = EvidenceStore(path=path, verifier=FakeAuthorityVerifier((approved,)))
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM evaluations").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM candidate_events").fetchone()[0] == 1
    assert conn.execute("SELECT binding_status FROM receipts").fetchone()[0] == "legacy_unverifiable"
    conn.close()
    with pytest.raises(EvidenceError, match="legacy receipt"):
        store.promote("candidate-1", authority, promoted_at=5)


def test_v1_migration_rolls_back_version_and_data_on_injected_failure(tmp_path, monkeypatch):
    path = tmp_path / "evidence.db"
    _old_v1(path)

    def fail():
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    conn.close()


def test_tampered_exact_v1_schema_is_not_a_migration_source(tmp_path):
    path = tmp_path / "evidence.db"
    _old_v1(path)
    conn = sqlite3.connect(path)
    conn.execute("ALTER TABLE events ADD COLUMN tampered TEXT")
    conn.commit()
    conn.close()
    with pytest.raises(EvidenceError, match="v1 schema"):
        EvidenceStore(path=path)


@pytest.mark.parametrize("statement", [
    "CREATE TABLE events (event_id TEXT PRIMARY KEY)",
    "CREATE TABLE events (event_id TEXT PRIMARY KEY, repository TEXT NOT NULL, subject TEXT NOT NULL, revision TEXT NOT NULL, failure_class TEXT NOT NULL, signature TEXT NOT NULL, normalizer TEXT NOT NULL)",
])
def test_versioned_malformed_evidence_schema_fails_closed_before_use(tmp_path, statement):
    path = tmp_path / "evidence.db"
    conn = sqlite3.connect(path)
    conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()
    with pytest.raises(EvidenceError, match="schema"):
        EvidenceStore(path=path)


@pytest.mark.parametrize("tamper", ["ALTER TABLE events ADD COLUMN untrusted TEXT", "DROP INDEX observations_by_event"])
def test_complete_v2_schema_fingerprint_rejects_column_and_index_tampering(tmp_path, tamper):
    path = tmp_path / "evidence.db"
    EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    conn.execute(tamper)
    conn.commit()
    conn.close()
    with pytest.raises(EvidenceError, match="schema"):
        EvidenceStore(path=path)
