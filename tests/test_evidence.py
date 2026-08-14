from __future__ import annotations

import pytest
import hashlib
import json
import sqlite3

from agentflow.evidence import (AuthorityPointer, EvidenceEnvelopeV2, EvidenceLink,
                                EvidenceStore, Event, FailureFacts, Observation, ProducerEvent, ProducerFacts,
                                SubjectRevision, ApprovedAuthority, Evaluation,
                                EvidenceError, FakeAuthorityVerifier, LessonCandidate,
                                _V2_SCHEMA, _V3_SCHEMA, _V4_SCHEMA, _schema_fingerprint,
                                _schema_fingerprint_for)
from agentflow.evidence_contract import validate_fixtures


OLD_V1_DDL = """
CREATE TABLE events (event_id TEXT PRIMARY KEY, repository TEXT NOT NULL, subject TEXT NOT NULL, revision TEXT NOT NULL, failure_class TEXT NOT NULL, signature TEXT NOT NULL, normalizer TEXT NOT NULL, UNIQUE(repository,subject,revision,failure_class,signature,normalizer));
CREATE TABLE observations (observation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_repository TEXT NOT NULL, source_locator TEXT NOT NULL, source_revision TEXT NOT NULL, source_hash_algorithm TEXT NOT NULL, source_hash TEXT NOT NULL, source_scope TEXT NOT NULL, validation_state TEXT NOT NULL, observed_at INTEGER NOT NULL, parent_revision TEXT NOT NULL, fixer_revision TEXT NOT NULL);
CREATE TABLE evaluations (evaluation_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, validation_state TEXT NOT NULL, evaluated_at INTEGER NOT NULL);
CREATE TABLE candidates (candidate_id TEXT PRIMARY KEY, proposal_digest TEXT NOT NULL, policy_version INTEGER NOT NULL, nominated_at INTEGER NOT NULL);
CREATE TABLE candidate_events (candidate_id TEXT NOT NULL, event_id TEXT NOT NULL, PRIMARY KEY(candidate_id,event_id));
CREATE TABLE receipts (candidate_id TEXT PRIMARY KEY, receipt_id TEXT NOT NULL, approval_id TEXT NOT NULL, policy_version INTEGER NOT NULL, promoted_at INTEGER NOT NULL);
"""

V2_TO_V3_CHECKPOINTS = (
    *(f"rename:{table}" for table in (
        "receipts", "candidate_events", "evaluations", "observations", "candidates", "events")),
    *(f"drop-old-index:{index}" for index in (
        "candidate_events_by_event", "evaluations_by_event", "observations_by_event")),
    *(f"create-table:{table}" for table in (
        "events", "observations", "evaluations", "candidates", "candidate_events", "receipts",
        "event_links")),
    *(f"copy:{table}" for table in (
        "events", "observations", "evaluations", "candidates", "candidate_events", "receipts")),
    "verify:copied-values",
    *(f"drop-old-table:{table}" for table in (
        "receipts", "candidate_events", "evaluations", "observations", "candidates", "events")),
    *(f"create-index:{index}" for index in (
        "events_failure_identity", "observations_by_event", "evaluations_by_event",
        "candidate_events_by_event", "event_links_by_target")),
    "verify:fingerprint", "set:user-version",
)
V3_TO_V4_CHECKPOINTS = (
    "v3-to-v4:after-add-contract", "v3-to-v4:after-demote-receipts",
    "v3-to-v4:verify:fingerprint", "v3-to-v4:set:user-version",
)


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


def _old_v2(path):
    conn = sqlite3.connect(path)
    conn.executescript(_V2_SCHEMA)
    conn.execute("INSERT INTO events VALUES ('event-bd4f1b3ab721f07da0aef1f7531c5fb8', "
                 "'octo/repo', 'pr/42', ?, 'original_defect', ?, 'v1')",
                 ("a" * 40, "c" * 64))
    conn.execute("INSERT INTO observations VALUES ('obs-1', "
                 "'event-bd4f1b3ab721f07da0aef1f7531c5fb8', 'github', "
                 "'octo/repo', 'issues/42', ?, 'sha256', ?, 'issue', 'observed', 1, '', '')",
                 ("a" * 40, "b" * 64))
    conn.execute("INSERT INTO evaluations VALUES ('evaluation-1', "
                 "'event-bd4f1b3ab721f07da0aef1f7531c5fb8', "
                 "'human_validated', 2)")
    conn.execute("INSERT INTO candidates VALUES ('candidate-1', ?, 1, 3)", ("d" * 64,))
    conn.execute("INSERT INTO candidate_events VALUES "
                 "('candidate-1', 'event-bd4f1b3ab721f07da0aef1f7531c5fb8')")
    conn.execute("INSERT INTO receipts VALUES ('candidate-1', 'receipt-candidate-1', "
                 "'approval-1', 1, 4, 'legacy_unverifiable', NULL, NULL, NULL, NULL, "
                 "NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()


def _old_v3(path):
    conn = sqlite3.connect(path)
    conn.executescript(_V3_SCHEMA)
    conn.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "event-legacy", "failure_observation", "octo/repo", "", "pr/42", "a" * 40,
        "", "", "original_defect", "", "c" * 64, "v1", ""))
    conn.execute("INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "obs-legacy", "event-legacy", "github", "octo/repo", "issues/42", "a" * 40,
        "sha256", "b" * 64, "issue", "observed", 1, "", "", "", "", ""))
    conn.execute("INSERT INTO candidates VALUES (?,?,?,?)",
                 ("candidate-legacy", "b" * 64, 1, 2))
    conn.execute("INSERT INTO candidate_events VALUES (?,?)",
                 ("candidate-legacy", "event-legacy"))
    conn.execute("INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
        "candidate-legacy", "receipt-legacy", "approval-legacy", 1, 3, "verified",
        "github", "octo/repo", "issues/42", "a" * 40, "sha256", "b" * 64, "issue",
        "fake", "v1", "verified", "a" * 40, "b" * 64, "issue"))
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    conn.close()


def _source():
    return AuthorityPointer("github", "octo/repo", "issues/42", "a" * 40,
                            "sha256", "b" * 64, "issue")


def _promotion_authority():
    return AuthorityPointer("github", "octo/repo", "pulls/42/files/docs/policy.json", "a" * 40,
                            "sha256", "b" * 64, "repository-policy/octo/repo/0-to-1")


def _observation(*, source=None, revision="a" * 40):
    return Observation(
        observation_id="obs-1" if source is None else "obs-2",
        subject=SubjectRevision("review", "pr/42", revision),
        failure_class="original_defect", validation_state="observed",
        signature_digest="c" * 64, normalizer_version="v1", source=source or _source(),
        observed_at=1,
    )


def _producer(observation_id, subject, producer_kind, fact_digest, observed_at, *links,
              validation_state="observed", review_action=None):
    return EvidenceEnvelopeV2(
        "producer_fact", observation_id, subject, _source(), observed_at,
        links=tuple(EvidenceLink(relation, target, ordinal)
                    for ordinal, relation, target in links),
        producer=ProducerFacts(producer_kind, fact_digest, "v2", validation_state,
                               review_action),
    )


def test_zero_link_producer_uses_the_canonical_v2_identity_vector(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    event = store.observe(EvidenceEnvelopeV2(
        envelope_kind="producer_fact",
        observation_id="obs-producer-zero",
        subject=SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64),
        source=_source(),
        observed_at=1,
        producer=ProducerFacts("revision", "b" * 64, "v2", "observed"),
    ))

    assert event == ProducerEvent(
        event_id="event-3cddfd1752601a42163fe748514fcba7",
        observation_ids=("obs-producer-zero",),
        producer_kind="revision",
        review_action="",
        validation_states=("observed",),
        links=(),
    )


def test_checked_in_producer_identity_preimages_have_no_trailing_nul():
    vectors = json.loads(__import__("pathlib").Path(
        "tests/fixtures/evidence-producer-id-vectors.json").read_text())
    assert {vector["name"] for vector in vectors} == {"zero-link", "one-link", "multi-link"}
    for vector in vectors:
        digest = hashlib.sha256("\0".join(vector["elements"]).encode("utf-8")).hexdigest()
        assert vector["event_id"] == "event-" + digest[:32]


def test_one_link_producer_resolves_the_target_and_uses_the_canonical_identity_vector(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    revision = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-revision", SubjectRevision(
            "issue", "issue/596", "rev-1", "issues/596", "a" * 64),
        _source(), 1, producer=ProducerFacts("revision", "b" * 64, "v2", "observed"),
    ))
    criterion = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-criterion", SubjectRevision(
            "issue", "issue/596", "rev-1", "issues/596", "a" * 64),
        _source(), 2,
        links=(EvidenceLink("derives_from", revision.event_id, 0),),
        producer=ProducerFacts("criterion", "c" * 64, "v2", "human_validated"),
    ))

    assert criterion.event_id == "event-351a6c1595d6ce6c393841901c1e0f1d"
    assert criterion.links == (EvidenceLink("derives_from", revision.event_id, 0),)


def test_multi_link_producer_uses_the_canonical_identity_vector(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    revision = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-revision", SubjectRevision(
            "issue", "issue/596", "rev-1", "issues/596", "a" * 64),
        _source(), 1, producer=ProducerFacts("revision", "b" * 64, "v2", "observed"),
    ))
    criterion = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-criterion", SubjectRevision(
            "issue", "issue/596", "rev-1", "issues/596", "a" * 64),
        _source(), 2, links=(EvidenceLink("derives_from", revision.event_id, 0),),
        producer=ProducerFacts("criterion", "c" * 64, "v2", "human_validated"),
    ))
    failure = store.observe(_observation())
    finding = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-finding", SubjectRevision(
            "issue", "issue/596", "rev-1", "issues/596", "a" * 64),
        _source(), 3,
        links=(EvidenceLink("derives_from", criterion.event_id, 0),
               EvidenceLink("derives_from", failure.event_id, 1)),
        producer=ProducerFacts("finding", "d" * 64, "v2", "observed"),
    ))

    assert failure.event_id == "event-bd4f1b3ab721f07da0aef1f7531c5fb8"
    assert finding.event_id == "event-8b43449bea4e06e0ac886ec0dc96f9ce"


def test_observe_rejects_unresolved_cross_repository_and_inapplicable_lineage(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    revision = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-revision", subject, _source(), 1,
        producer=ProducerFacts("revision", "b" * 64, "v2", "observed"),
    ))

    with pytest.raises(EvidenceError, match="target"):
        store.observe(EvidenceEnvelopeV2(
            "producer_fact", "obs-missing", subject, _source(), 2,
            links=(EvidenceLink("derives_from", "event-missing", 0),),
            producer=ProducerFacts("criterion", "c" * 64, "v2", "observed"),
        ))
    other_source = AuthorityPointer("github", "other/repo", "issues/596", "a" * 40,
                                    "sha256", "b" * 64, "issue")
    with pytest.raises(EvidenceError, match="repository"):
        store.observe(EvidenceEnvelopeV2(
            "producer_fact", "obs-cross", subject, other_source, 2,
            links=(EvidenceLink("derives_from", revision.event_id, 0),),
            producer=ProducerFacts("criterion", "c" * 64, "v2", "observed"),
        ))
    with pytest.raises(EvidenceError, match="applicable"):
        store.observe(EvidenceEnvelopeV2(
            "producer_fact", "obs-wrong-relation", subject, _source(), 2,
            links=(EvidenceLink("addresses", revision.event_id, 0),),
            producer=ProducerFacts("criterion", "c" * 64, "v2", "observed"),
        ))
    with pytest.raises(EvidenceError, match="requires"):
        store.observe(EvidenceEnvelopeV2(
            "producer_fact", "obs-linkless-fix", subject, _source(), 2,
            producer=ProducerFacts("fix", "d" * 64, "v2", "observed"),
        ))


def test_v2_envelopes_reject_invalid_arms_actions_fixer_lineage_and_link_sequences():
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    producer = ProducerFacts("revision", "b" * 64, "v2", "observed")
    failure = FailureFacts("original_defect", "observed", "c" * 64, "v2")
    for kind, failure_arm, producer_arm in (
        ("unknown", None, producer),
        ("producer_fact", failure, producer),
        ("failure_observation", None, None),
    ):
        with pytest.raises(EvidenceError, match="tagged"):
            EvidenceEnvelopeV2(kind, "obs", subject, _source(), 1,
                               failure=failure_arm, producer=producer_arm)
    with pytest.raises(EvidenceError, match="pointers"):
        EvidenceEnvelopeV2("producer_fact", "obs", "not-a-subject", _source(), 1,
                           producer=producer)
    with pytest.raises(EvidenceError, match="both fixer"):
        FailureFacts("fix_introduced_defect", "observed", "c" * 64, "v2",
                     reviewed_parent_revision="a" * 40)
    with pytest.raises(EvidenceError, match="requires a review action"):
        ProducerFacts("review_action", "b" * 64, "v2", "observed")
    with pytest.raises(EvidenceError, match="forbidden"):
        ProducerFacts("revision", "b" * 64, "v2", "observed", "ask_maintainer")
    with pytest.raises(EvidenceError, match="dense"):
        EvidenceEnvelopeV2(
            "producer_fact", "obs", subject, _source(), 1,
            links=(EvidenceLink("derives_from", "event-one", 0),
                   EvidenceLink("derives_from", "event-two", 0)), producer=producer)
    with pytest.raises(EvidenceError, match="unique"):
        EvidenceEnvelopeV2(
            "producer_fact", "obs", subject, _source(), 1,
            links=(EvidenceLink("derives_from", "event-one", 0),
                   EvidenceLink("derives_from", "event-one", 1)), producer=producer)
    with pytest.raises(EvidenceError, match="bounded"):
        EvidenceEnvelopeV2(
            "producer_fact", "obs", subject, _source(), 1,
            links=tuple(EvidenceLink("derives_from", f"event-{index}", 0)
                        for index in range(33)), producer=producer)


def test_same_producer_scalars_with_different_lineage_have_distinct_identities(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    first_target = store.observe(_producer("obs-r1", subject, "revision", "b" * 64, 1))
    second_target = store.observe(_producer("obs-r2", subject, "revision", "c" * 64, 2))
    first = store.observe(_producer(
        "obs-c1", subject, "criterion", "d" * 64, 3,
        (0, "derives_from", first_target.event_id)))
    second = store.observe(_producer(
        "obs-c2", subject, "criterion", "d" * 64, 4,
        (0, "derives_from", second_target.event_id)))
    assert first.event_id != second.event_id


def test_v2_failure_replay_rejects_complete_subject_conflict_on_legacy_identity(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    facts = FailureFacts("original_defect", "observed", "a" * 64, "v2")
    first = EvidenceEnvelopeV2(
        "failure_observation", "obs-issue",
        SubjectRevision("issue", "item/596", "rev-1", "issues/596", "b" * 64),
        _source(), 1, failure=facts)
    conflicting = EvidenceEnvelopeV2(
        "failure_observation", "obs-document",
        SubjectRevision("document", "item/596", "rev-1", "docs/596", "c" * 64),
        _source(), 2, failure=facts)

    admitted = store.observe(first)
    with pytest.raises(EvidenceError, match="immutable failure facts"):
        store.observe(conflicting)
    assert store.brief_for("item/596", repository="octo/repo", now=3) == (admitted,)


def test_producer_replay_collects_sorted_observations_and_rejects_changed_facts(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    first = store.observe(_producer("obs-z", subject, "revision", "b" * 64, 2,
                                    validation_state="refuted"))
    replay = store.observe(_producer("obs-a", subject, "revision", "b" * 64, 1,
                                     validation_state="human_validated"))
    assert replay.event_id == first.event_id
    assert replay.observation_ids == ("obs-a", "obs-z")
    assert replay.validation_states == ("human_validated", "refuted")
    with pytest.raises(EvidenceError, match="immutable"):
        store.observe(_producer("obs-a", subject, "revision", "c" * 64, 1,
                                validation_state="human_validated"))


def test_remaining_relation_vocabulary_is_enforced_through_observe(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    revision = store.observe(_producer("obs-r", subject, "revision", "a" * 64, 1))
    criterion = store.observe(_producer(
        "obs-c", subject, "criterion", "b" * 64, 2,
        (0, "derives_from", revision.event_id)))
    governed = store.observe(_producer(
        "obs-govern", subject, "decision", "c" * 64, 3,
        (0, "governs", criterion.event_id)))
    delegated = store.observe(_producer(
        "obs-delegate", subject, "delegation", "d" * 64, 4,
        (0, "delegates", criterion.event_id)))
    refuted = store.observe(_producer(
        "obs-refute", subject, "verification", "e" * 64, 5,
        (0, "refutes", criterion.event_id)))
    revised = store.observe(_producer(
        "obs-revise", subject, "revision", "f" * 64, 6,
        (0, "revises", governed.event_id)))
    assert {event.links[0].relation for event in (governed, delegated, refuted, revised)} == {
        "governs", "delegates", "refutes", "revises"}


def test_repository_brief_filters_roots_and_closes_over_contextual_lineage(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    upstream = SubjectRevision("issue", "issue/upstream", "rev-1", "issues/1", "a" * 64)
    requested = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "b" * 64)
    revision = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-revision", upstream, _source(), 1,
        producer=ProducerFacts("revision", "c" * 64, "v2", "refuted"),
    ))
    criterion = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-criterion", requested, _source(), 2,
        links=(EvidenceLink("derives_from", revision.event_id, 0),),
        producer=ProducerFacts("criterion", "d" * 64, "v2", "human_validated"),
    ))
    failure = store.observe(Observation(
        "obs-unvalidated-failure", requested, "original_defect", "unvalidated",
        "e" * 64, "v2", _source(), 3,
    ))

    brief = store.brief_for(
        "issue/596", repository="octo/repo", now=4,
        accepted_validation_states=("human_validated",),
    )
    contextual_revision = ProducerEvent(
        revision.event_id, (), "revision", "", (), (), contextual=True)
    assert brief == tuple(sorted((contextual_revision, criterion), key=lambda event: event.event_id))
    assert store.brief_for("issue/596", now=4) == (failure,)
    assert store.brief_for(
        "issue/596", repository="octo/repo", now=4, accepted_validation_states=(),
    ) == ()
    for invalid in (["observed"], ("observed", "observed"), ("unknown",)):
        with pytest.raises(EvidenceError, match="validation"):
            store.brief_for(
                "issue/596", repository="octo/repo", now=4,
                accepted_validation_states=invalid,
            )


@pytest.mark.parametrize("keyword,value", [
    ("accepted_validation_states", (["observed"],)),
    ("effective_policy_versions", ([1],)),
])
def test_briefing_tuple_elements_fail_with_evidence_error_before_set_or_arithmetic(
        tmp_path, keyword, value):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    with pytest.raises(EvidenceError, match="invalid|must be"):
        store.brief_for("issue/596", now=1, **{keyword: value})


def test_retention_marks_live_descendant_closure_then_sweeps_after_last_root(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    cutoff_age = 90 * 24 * 60 * 60
    target_subject = SubjectRevision("issue", "issue/upstream", "rev-1", "issues/1", "a" * 64)
    source_subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "b" * 64)
    target = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-target", target_subject, _source(), 1,
        producer=ProducerFacts("revision", "c" * 64, "v2", "observed"),
    ))
    source = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-source", source_subject, _source(), 101,
        links=(EvidenceLink("derives_from", target.event_id, 0),),
        producer=ProducerFacts("criterion", "d" * 64, "v2", "observed"),
    ))

    retained = store.brief_for(
        "issue/596", repository="octo/repo", now=cutoff_age + 100)
    assert retained == tuple(sorted((
        source,
        ProducerEvent(target.event_id, (), "revision", "", (), (), contextual=True),
    ), key=lambda event: event.event_id))
    assert store.brief_for(
        "issue/596", repository="octo/repo", now=cutoff_age + 102) == ()


def test_retention_marks_two_hop_branching_target_closure(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    age = 90 * 24 * 60 * 60
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    revision = store.observe(_producer("obs-r", subject, "revision", "a" * 64, 1))
    first = store.observe(_producer(
        "obs-c1", subject, "criterion", "b" * 64, 2,
        (0, "derives_from", revision.event_id)))
    second = store.observe(_producer(
        "obs-c2", subject, "criterion", "c" * 64, 3,
        (0, "derives_from", revision.event_id)))
    finding = store.observe(_producer(
        "obs-finding", subject, "finding", "d" * 64, 101,
        (0, "derives_from", first.event_id), (1, "derives_from", second.event_id)))

    brief = store.brief_for("issue/596", repository="octo/repo", now=age + 100)
    assert {event.event_id for event in brief} == {
        revision.event_id, first.event_id, second.event_id, finding.event_id}
    assert next(event for event in brief if event.event_id == finding.event_id).contextual is False
    assert all(event.contextual for event in brief if event.event_id != finding.event_id)


def test_recent_evaluation_and_candidate_reference_root_events_until_all_expire(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    age = 90 * 24 * 60 * 60
    subject = SubjectRevision("issue", "issue/upstream", "rev-1", "issues/1", "a" * 64)
    evaluated = store.observe(_producer("obs-evaluated", subject, "revision", "a" * 64, 1))
    nominated = store.observe(_producer("obs-nominated", subject, "revision", "b" * 64, 1))
    store.evaluate(Evaluation("evaluation-recent", evaluated.event_id, "human_validated", 101))
    store.nominate(LessonCandidate("candidate-recent", (nominated.event_id,), "c" * 64, 1, 101))

    assert store.brief_for("none", repository="octo/repo", now=age + 100) == ()
    requested = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "d" * 64)
    store.observe(_producer(
        "obs-from-evaluation", requested, "criterion", "e" * 64, 102,
        (0, "derives_from", evaluated.event_id)))
    store.observe(_producer(
        "obs-from-candidate", requested, "criterion", "f" * 64, 102,
        (0, "derives_from", nominated.event_id)))
    assert len(store.brief_for("issue/596", repository="octo/repo", now=age + 100)) == 4
    assert store.brief_for("issue/596", repository="octo/repo", now=age + 200) == ()


def test_exact_v2_migrates_to_exact_v4_with_legacy_subject_sentinels(tmp_path):
    path = tmp_path / "evidence.db"
    _old_v2(path)
    authority = _source()
    approved = ApprovedAuthority(authority, "approval-new", authority.revision,
                                 authority.content_hash, authority.scope, "fake", "v2", "verified")
    store = EvidenceStore(path=path, verifier=FakeAuthorityVerifier((approved,)))
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _schema_fingerprint(conn) == _schema_fingerprint_for(_V4_SCHEMA)
    assert conn.execute("SELECT * FROM events").fetchone() == (
        "event-bd4f1b3ab721f07da0aef1f7531c5fb8", "failure_observation", "octo/repo",
        "", "pr/42", "a" * 40, "", "", "original_defect", "", "c" * 64, "v1", "",
    )
    assert conn.execute("SELECT * FROM observations").fetchone() == (
        "obs-1", "event-bd4f1b3ab721f07da0aef1f7531c5fb8", "github", "octo/repo",
        "issues/42", "a" * 40, "sha256", "b" * 64, "issue", "observed", 1,
        "", "", "", "", "",
    )
    assert conn.execute("SELECT count(*) FROM event_links").fetchone()[0] == 0
    assert conn.execute("SELECT * FROM evaluations").fetchall() == [
        ("evaluation-1", "event-bd4f1b3ab721f07da0aef1f7531c5fb8", "human_validated", 2)]
    assert conn.execute("SELECT * FROM candidates").fetchall() == [
        ("candidate-1", "d" * 64, 1, 3)]
    assert conn.execute("SELECT * FROM candidate_events").fetchall() == [
        ("candidate-1", "event-bd4f1b3ab721f07da0aef1f7531c5fb8")]
    assert conn.execute("SELECT * FROM receipts").fetchall() == [
        ("candidate-1", "receipt-candidate-1", "approval-1", 1, 4,
         "legacy_unverifiable", None, None, None, None, None, None, None, None,
         None, None, None, None, None, "")]
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'v2_%'").fetchall()
    conn.close()
    migrated = Event("event-bd4f1b3ab721f07da0aef1f7531c5fb8", 1, ("obs-1",))
    assert store.brief_for("pr/42", now=4) == (migrated,)
    assert store.observe(_observation()) == migrated
    evaluation = Evaluation("evaluation-1", migrated.event_id, "human_validated", 2)
    assert store.evaluate(evaluation) == evaluation
    candidate = LessonCandidate("candidate-1", (migrated.event_id,), "d" * 64, 1, 3)
    assert store.nominate(candidate) == candidate
    with pytest.raises(EvidenceError, match="legacy receipt"):
        store.promote("candidate-1", authority, promoted_at=5)


def test_new_store_creates_exact_v4_with_required_foreign_key_actions(tmp_path):
    path = tmp_path / "evidence.db"
    EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert _schema_fingerprint(conn) == _schema_fingerprint_for(_V4_SCHEMA)
    links = {row[3]: row[6] for row in conn.execute("PRAGMA foreign_key_list(event_links)")}
    assert links == {"target_event_id": "RESTRICT", "source_event_id": "CASCADE"}
    conn.close()


@pytest.mark.parametrize("checkpoint", V2_TO_V3_CHECKPOINTS)
def test_every_v2_to_v3_fault_rolls_back_exactly_and_remains_reopenable(
        tmp_path, monkeypatch, checkpoint):
    path = tmp_path / "evidence.db"
    _old_v2(path)
    conn = sqlite3.connect(path)
    fingerprint = _schema_fingerprint(conn)
    rows = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in (
        "events", "observations", "evaluations", "candidates", "candidate_events", "receipts")}
    conn.close()

    def fail(label):
        if label == checkpoint:
            raise RuntimeError(f"injected at {label}")

    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _schema_fingerprint(conn) == fingerprint
    assert {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in rows} == rows
    conn.close()

    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(lambda label: None))
    EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    conn.close()


@pytest.mark.parametrize("checkpoint", V3_TO_V4_CHECKPOINTS)
def test_every_v3_to_v4_fault_rolls_back_before_legacy_receipts_can_activate(
        tmp_path, monkeypatch, checkpoint):
    path = tmp_path / "evidence.db"
    _old_v3(path)
    conn = sqlite3.connect(path)
    fingerprint = _schema_fingerprint(conn)
    conn.close()

    def fail(label):
        if label == checkpoint:
            raise RuntimeError(f"injected at {label}")

    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert _schema_fingerprint(conn) == fingerprint
    assert conn.execute("SELECT binding_status FROM receipts").fetchone()[0] == "verified"
    conn.close()


def test_v3_verified_receipt_migrates_to_unverifiable_and_cannot_replay(tmp_path):
    path = tmp_path / "evidence.db"
    _old_v3(path)
    store = EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert conn.execute(
        "SELECT binding_status, promotion_contract FROM receipts").fetchone() == (
            "legacy_unverifiable", "")
    conn.close()
    with pytest.raises(EvidenceError, match="legacy receipt"):
        store.promote("candidate-legacy", _source(), promoted_at=4)


def test_evidence_store_exposes_exactly_five_public_verbs():
    public = {name for name in dir(EvidenceStore)
              if not name.startswith("_") and callable(getattr(EvidenceStore, name))}
    assert public == {"observe", "evaluate", "nominate", "promote", "brief_for"}


@pytest.mark.parametrize("outcome,digest", [("merge", "1" * 64), ("park", "2" * 64)])
def test_public_journey_carries_two_findings_to_distinct_settlement_outcomes(
        tmp_path, outcome, digest):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    revision = store.observe(_producer("obs-r", subject, "revision", "a" * 64, 1))
    criterion = store.observe(_producer(
        "obs-c", subject, "criterion", "b" * 64, 2,
        (0, "derives_from", revision.event_id)))
    failure_one = store.observe(Observation(
        "obs-f1", subject, "original_defect", "observed", "c" * 64, "v2", _source(), 3))
    failure_two = store.observe(Observation(
        "obs-f2", subject, "plan_gap", "observed", "d" * 64, "v2", _source(), 4))
    finding_a = store.observe(_producer(
        "obs-a", subject, "finding", "e" * 64, 5,
        (0, "derives_from", criterion.event_id),
        (1, "derives_from", failure_one.event_id)))
    finding_b = store.observe(_producer(
        "obs-b", subject, "finding", "f" * 64, 6,
        (0, "derives_from", criterion.event_id),
        (1, "derives_from", failure_two.event_id)))
    action = store.observe(_producer(
        "obs-q", subject, "review_action", "3" * 64, 7,
        (0, "addresses", finding_a.event_id),
        (1, "addresses", finding_b.event_id),
        review_action="fix_before_completion"))
    fix = store.observe(_producer(
        "obs-x", subject, "fix", "4" * 64, 8,
        (0, "addresses", finding_a.event_id),
        (1, "addresses", finding_b.event_id),
        (2, "implements", action.event_id)))
    verification = store.observe(_producer(
        "obs-v", subject, "verification", "5" * 64, 9,
        (0, "verifies", fix.event_id)))
    verdict = store.observe(_producer(
        "obs-d", subject, "verdict", "6" * 64, 10,
        (0, "derives_from", verification.event_id)))
    settlement = store.observe(_producer(
        f"obs-{outcome}", subject, "settlement", digest, 11,
        (0, "settles", verdict.event_id)))

    assert fix.links[:2] == (
        EvidenceLink("addresses", finding_a.event_id, 0),
        EvidenceLink("addresses", finding_b.event_id, 1),
    )
    edited = SubjectRevision("issue", "issue/596", "rev-2", "issues/596", "a" * 64)
    edited_criterion = store.observe(_producer(
        "obs-c-edited", edited, "criterion", "b" * 64, 12,
        (0, "derives_from", revision.event_id)))
    assert edited_criterion.event_id != criterion.event_id
    brief = store.brief_for("issue/596", repository="octo/repo", now=13)
    assert settlement in brief
    assert edited_criterion in brief
    assert len(brief) == 12


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
    authority = _promotion_authority()
    approved = ApprovedAuthority(authority, "approval-1", authority.revision, authority.content_hash,
                                 authority.scope, "fake", "v1", "verified")
    store = EvidenceStore(path=tmp_path / "evidence.db", verifier=FakeAuthorityVerifier((approved,)))
    event = store.observe(_observation())
    assert store.evaluate(Evaluation("evaluation-1", event.event_id, "human_validated", 2)).event_id == event.event_id
    candidate = LessonCandidate("candidate-1", (event.event_id,), authority.content_hash, 1, 3)
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
    authority = _promotion_authority()
    approved = ApprovedAuthority(authority, "approval-1", authority.revision, authority.content_hash,
                                 authority.scope, "fake", "v1", "verified")
    store = EvidenceStore(path=tmp_path / "evidence.db", verifier=FakeAuthorityVerifier((approved,)))
    event = store.observe(_observation())
    store.nominate(LessonCandidate("candidate-1", (event.event_id,), authority.content_hash, 1, 1))
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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
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
    conn = sqlite3.connect(path)
    fingerprint = _schema_fingerprint(conn)
    rows = {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in (
        "events", "observations", "evaluations", "candidates", "candidate_events", "receipts")}
    conn.close()

    def fail(label):
        assert label == "v1-to-v2:after-copy-receipts"
        raise RuntimeError("injected migration failure")

    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match="injected"):
        EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert _schema_fingerprint(conn) == fingerprint
    assert {table: conn.execute(f"SELECT * FROM {table}").fetchall() for table in rows} == rows
    conn.close()
    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(lambda label: None))
    EvidenceStore(path=path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 4


def test_chained_migration_second_leg_failure_lands_on_exact_v2(tmp_path, monkeypatch):
    path = tmp_path / "evidence.db"
    _old_v1(path)

    def fail(label):
        if label == "create-table:event_links":
            raise RuntimeError("second leg")

    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(fail))
    with pytest.raises(RuntimeError, match="second leg"):
        EvidenceStore(path=path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _schema_fingerprint(conn) == _schema_fingerprint_for(_V2_SCHEMA)
    assert conn.execute("SELECT binding_status FROM receipts").fetchone()[0] == "legacy_unverifiable"
    conn.close()
    monkeypatch.setattr(EvidenceStore, "_migration_checkpoint", staticmethod(lambda label: None))
    EvidenceStore(path=path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 4


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


def test_reopen_rejects_persisted_cross_repository_lineage_and_brief_never_traverses_it(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path=path)
    subject_a = SubjectRevision("issue", "repo/a-item", "rev-1", "issues/1", "a" * 64)
    subject_b = SubjectRevision("issue", "repo/b-item", "rev-1", "issues/2", "b" * 64)
    source_b = AuthorityPointer("github", "repo/b", "issues/2", "a" * 40,
                                "sha256", "b" * 64, "issue")
    target_b = store.observe(EvidenceEnvelopeV2(
        "producer_fact", "obs-b", subject_b, source_b, 1,
        producer=ProducerFacts("revision", "c" * 64, "v2", "observed")))
    source_a = store.observe(_producer("obs-a", subject_a, "revision", "d" * 64, 2))
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO event_links VALUES (?,?,?,?)",
                 (source_a.event_id, 0, "derives_from", target_b.event_id))
    conn.commit()
    conn.close()

    brief = store.brief_for("repo/a-item", repository="octo/repo", now=3)
    assert tuple(event.event_id for event in brief) == (source_a.event_id,)
    assert target_b.event_id not in {event.event_id for event in brief}
    with pytest.raises(EvidenceError, match="persisted Evidence lineage"):
        EvidenceStore(path=path)


@pytest.mark.parametrize("tamper", ["missing-target", "invalid-relation", "sparse-ordinal"])
def test_reopen_rejects_unresolved_invalid_or_nondense_persisted_lineage(tmp_path, tamper):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path=path)
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    target = store.observe(_producer("obs-r", subject, "revision", "b" * 64, 1))
    source = store.observe(_producer(
        "obs-c", subject, "criterion", "c" * 64, 2,
        (0, "derives_from", target.event_id)))
    conn = sqlite3.connect(path)
    if tamper == "missing-target":
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("UPDATE event_links SET target_event_id='event-missing' "
                     "WHERE source_event_id=?", (source.event_id,))
    elif tamper == "invalid-relation":
        conn.execute("UPDATE event_links SET relation='invalid' WHERE source_event_id=?",
                     (source.event_id,))
    else:
        conn.execute("UPDATE event_links SET ordinal=1 WHERE source_event_id=?", (source.event_id,))
    conn.commit()
    conn.close()

    with pytest.raises(EvidenceError, match="persisted Evidence lineage"):
        EvidenceStore(path=path)


@pytest.mark.parametrize("tamper", ["inapplicable-relation", "missing-required", "failure-source"])
def test_reopen_rejects_persisted_source_kind_and_required_relation_violations(
        tmp_path, tamper):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path=path)
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    target = store.observe(_producer("obs-r", subject, "revision", "b" * 64, 1))
    source = store.observe(_producer(
        "obs-c", subject, "criterion", "c" * 64, 2,
        (0, "derives_from", target.event_id)))
    conn = sqlite3.connect(path)
    if tamper == "inapplicable-relation":
        conn.execute("UPDATE event_links SET relation='addresses' WHERE source_event_id=?",
                     (source.event_id,))
    elif tamper == "missing-required":
        conn.execute("UPDATE events SET producer_kind='fix' WHERE event_id=?", (source.event_id,))
    else:
        failure = store.observe(_observation())
        conn.execute("INSERT INTO event_links VALUES (?,?,?,?)",
                     (failure.event_id, 0, "derives_from", target.event_id))
    conn.commit()
    conn.close()

    with pytest.raises(EvidenceError, match="persisted Evidence lineage"):
        EvidenceStore(path=path)


def test_reopen_rejects_same_repository_retarget_under_the_old_producer_id(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path=path)
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    first_target = store.observe(_producer("obs-r1", subject, "revision", "b" * 64, 1))
    second_target = store.observe(_producer("obs-r2", subject, "revision", "c" * 64, 2))
    source = store.observe(_producer(
        "obs-c", subject, "criterion", "d" * 64, 3,
        (0, "derives_from", first_target.event_id)))
    conn = sqlite3.connect(path)
    conn.execute("UPDATE event_links SET target_event_id=? WHERE source_event_id=?",
                 (second_target.event_id, source.event_id))
    conn.commit()
    conn.close()

    with pytest.raises(EvidenceError, match="producer identity"):
        EvidenceStore(path=path)


def test_reopen_rejects_persisted_self_cycle(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path=path)
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    event = store.observe(_producer("obs-r", subject, "revision", "b" * 64, 1))
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO event_links VALUES (?,?,?,?)",
                 (event.event_id, 0, "derives_from", event.event_id))
    conn.commit()
    conn.close()

    with pytest.raises(EvidenceError, match="cycle"):
        EvidenceStore(path=path)


def test_reopen_rejects_persisted_two_node_cycle(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path=path)
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    first = store.observe(_producer("obs-r1", subject, "revision", "b" * 64, 1))
    second = store.observe(_producer("obs-r2", subject, "revision", "c" * 64, 2))
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO event_links VALUES (?,?,?,?)",
                 (first.event_id, 0, "derives_from", second.event_id))
    conn.execute("INSERT INTO event_links VALUES (?,?,?,?)",
                 (second.event_id, 0, "derives_from", first.event_id))
    conn.commit()
    conn.close()

    with pytest.raises(EvidenceError, match="cycle"):
        EvidenceStore(path=path)


def test_valid_branched_lineage_reopens(tmp_path):
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path=path)
    subject = SubjectRevision("issue", "issue/596", "rev-1", "issues/596", "a" * 64)
    revision = store.observe(_producer("obs-r", subject, "revision", "b" * 64, 1))
    first = store.observe(_producer(
        "obs-c1", subject, "criterion", "c" * 64, 2,
        (0, "derives_from", revision.event_id)))
    second = store.observe(_producer(
        "obs-c2", subject, "criterion", "d" * 64, 3,
        (0, "derives_from", revision.event_id)))
    finding = store.observe(_producer(
        "obs-f", subject, "finding", "e" * 64, 4,
        (0, "derives_from", first.event_id),
        (1, "derives_from", second.event_id)))

    reopened = EvidenceStore(path=path)
    assert finding in reopened.brief_for("issue/596", repository="octo/repo", now=5)
