"""The Build-Issue Proposal aggregate in the workspace store (ADR 0033/0034), exercised through
its public command surface.

A Proposal is a series of immutable, content-hashed versions staged from an Ask. The proofs that
matter: refining stages a *new* version and never mutates a prior version's hash; approval is bound
to the *exact* version hash the operator saw (never "latest"); a verified publication is recorded
once and is terminal; and discarding drops the draft while leaving the Conversation intact.
"""

from __future__ import annotations

import sqlite3

import pytest

from agentflow.workspace.store import (APPROVED, DISCARDED, FAILED, PUBLISHED, REJECTED, STAGED,
                                       WorkspaceStore)


@pytest.fixture
def store(tmp_path):
    s = WorkspaceStore("ConnorGriffin/agentflow", path=tmp_path / "agentflow.db")
    yield s
    s.close()


def _convo(store, cid="conv-1"):
    store.open_conversation(title="Ask", conversation_id=cid, idempotency_key=f"{cid}:open", now=1)
    return cid


def _stage(store, cid, *, hash_, key, title="Publish button", now=10):
    return store.stage_proposal(cid, title=title, summary="do the thing",
                                acceptance=["it works"], body="", content_hash=hash_,
                                idempotency_key=key, now=now)


# --- staging: opens the Proposal, then appends new immutable versions -------------------

def test_first_build_issue_turn_stages_a_copper_proposal(store):
    cid = _convo(store)
    out = _stage(store, cid, hash_="sha256:aaa", key="s1")
    assert out.accepted and out.version == 1 and out.content_hash == "sha256:aaa"
    prop = store.proposal(cid)
    assert prop.state == STAGED and prop.title == "Publish button"
    assert [v.version for v in prop.versions] == [1]


def test_refining_stages_a_new_version_and_never_mutates_a_prior_hash(store):
    """The immutability guarantee: a second build-issue turn appends v2 with its own hash; v1's
    content hash is untouched, and the latest awaiting decision is v2. Would fail if staging
    mutated the existing version in place instead of appending a new immutable one."""
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1", title="First draft", now=10)
    _stage(store, cid, hash_="sha256:v2", key="s2", title="Sharper draft", now=20)
    prop = store.proposal(cid)
    assert [(v.version, v.content_hash) for v in prop.versions] == [
        (1, "sha256:v1"), (2, "sha256:v2")]
    assert prop.latest.version == 2 and prop.title == "Sharper draft"  # copper shows latest
    assert prop.state == STAGED


def test_restaging_the_same_turn_key_never_appends_a_duplicate_version(store):
    cid = _convo(store)
    a = _stage(store, cid, hash_="sha256:v1", key="s1")
    b = _stage(store, cid, hash_="sha256:v1", key="s1")  # a re-adopted finalize
    assert a == b
    assert len(store.proposal(cid).versions) == 1


def test_staging_against_an_unknown_conversation_is_rejected(store):
    out = _stage(store, "nope", hash_="sha256:x", key="s1")
    assert out.status == REJECTED and out.error == "unknown conversation"
    assert store.proposal("nope") is None


# --- approval: hash-bound to the exact version seen, never "latest" ---------------------

def test_approving_a_stale_but_seen_version_binds_to_that_exact_hash(store):
    """Approval is per-exact-version-hash (ADR 0034): approving v1 after v2 was staged approves the
    hash the operator actually saw, not the latest."""
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1", now=10)
    _stage(store, cid, hash_="sha256:v2", key="s2", now=20)
    out = store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=30)
    assert out.accepted and out.content_hash == "sha256:v1" and out.version == 1
    prop = store.proposal(cid)
    assert prop.state == APPROVED and prop.approved_hash == "sha256:v1"


def test_approving_a_hash_that_no_version_carries_is_rejected(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    out = store.approve_proposal(cid, "sha256:ghost", idempotency_key="a1", now=2)
    assert out.status == REJECTED and out.error == "unknown version hash"
    assert store.proposal(cid).state == STAGED


def test_a_new_version_supersedes_an_earlier_approval(store):
    """Continuing to talk after approving resets the copper card to the latest awaiting decision —
    a superseding version clears the stale approval (ADR 0034)."""
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1", now=10)
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=20)
    _stage(store, cid, hash_="sha256:v2", key="s2", now=30)
    prop = store.proposal(cid)
    assert prop.state == STAGED and prop.approved_hash is None


# --- publication receipt: recorded once, terminal --------------------------------------

def test_recording_the_publication_is_idempotent_and_terminal(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=2)
    first = store.record_publication(cid, "sha256:v1", issue_number=42,
                                     issue_url="https://x/issues/42", now=3)
    second = store.record_publication(cid, "sha256:v1", issue_number=42,
                                      issue_url="https://x/issues/42", now=4)
    assert first.accepted and second.accepted
    prop = store.proposal(cid)
    assert prop.state == PUBLISHED and prop.published_issue == 42


def test_only_the_approved_hash_can_be_recorded_as_published(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1", now=10)
    _stage(store, cid, hash_="sha256:v2", key="s2", now=20)
    store.approve_proposal(cid, "sha256:v2", idempotency_key="a1", now=30)
    out = store.record_publication(cid, "sha256:v1", issue_number=1, issue_url="u", now=40)
    assert out.status == REJECTED and out.error == "hash is not the approved one"
    assert store.proposal(cid).state == APPROVED


# --- failed disposition: a parked publish failure, retried by re-approving --------------

def test_a_publish_failure_parks_failed_with_a_reason_and_keeps_the_hash_bound(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=2)
    out = store.fail_publication(cid, "sha256:v1", reason="GitHub rejected the request.", now=9)
    assert out.accepted
    prop = store.proposal(cid)
    assert prop.state == FAILED and prop.fail_reason == "GitHub rejected the request."
    assert prop.failed_at == 9 and prop.approved_hash == "sha256:v1"   # still bound for Retry


def test_failing_a_hash_that_is_not_the_approved_one_is_rejected(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1", now=10)
    _stage(store, cid, hash_="sha256:v2", key="s2", now=20)
    store.approve_proposal(cid, "sha256:v2", idempotency_key="a1", now=30)
    out = store.fail_publication(cid, "sha256:v1", reason="nope", now=40)
    assert out.status == REJECTED and out.error == "hash is not the approved one"
    assert store.proposal(cid).state == APPROVED


def test_a_published_proposal_ignores_a_stale_failure(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=2)
    store.record_publication(cid, "sha256:v1", issue_number=7, issue_url="u", now=3)
    out = store.fail_publication(cid, "sha256:v1", reason="late failure", now=4)
    assert out.accepted                                        # a no-op replay, never unwinds
    assert store.proposal(cid).state == PUBLISHED


def test_re_approving_after_a_failure_clears_the_failure(store):
    """Retry is the operator re-issuing approve for the same hash: it clears the parked failure and
    re-authorizes the exact version, so publication can re-run."""
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=2)
    store.fail_publication(cid, "sha256:v1", reason="GitHub rejected the request.", now=9)
    assert store.proposal(cid).state == FAILED
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a2", now=11)
    prop = store.proposal(cid)
    assert prop.state == APPROVED and prop.fail_reason is None and prop.failed_at == 0
    assert prop.approved_hash == "sha256:v1"


def test_a_failed_proposal_can_be_discarded(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=2)
    store.fail_publication(cid, "sha256:v1", reason="boom", now=9)
    out = store.discard_proposal(cid, idempotency_key="d1", now=10)
    assert out.accepted and store.proposal(cid).state == DISCARDED


# --- discard: drops the draft, leaves the Conversation intact --------------------------

def test_discard_drops_the_proposal_but_keeps_the_conversation(store):
    cid = _convo(store)
    store.start_turn(cid, "chat about it", expected_revision=0, idempotency_key="t0", now=5)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    out = store.discard_proposal(cid, idempotency_key="d1", now=6)
    assert out.accepted
    assert store.proposal(cid).state == DISCARDED
    # The Conversation and its turn survive untouched.
    convo = store.conversation(cid)
    assert convo is not None and convo.turns[0].prompt == "chat about it"


def test_a_published_proposal_cannot_be_discarded(store):
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1")
    store.approve_proposal(cid, "sha256:v1", idempotency_key="a1", now=2)
    store.record_publication(cid, "sha256:v1", issue_number=7, issue_url="u", now=3)
    out = store.discard_proposal(cid, idempotency_key="d1", now=4)
    assert out.status == REJECTED
    assert store.proposal(cid).state == PUBLISHED


# --- durability: versions survive a restart --------------------------------------------

def test_a_tracer1_v1_store_migrates_in_place_keeping_its_conversations(tmp_path):
    """A workspace database from the conversation-spine tracer (schema 1, no Proposal tables) must
    migrate in place on open — adding the Proposal tables without losing its Conversations or turns
    (ADR 0033). Would fail if the schema bump fenced the old store off as unreadable."""
    path = tmp_path / "agentflow.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, repo TEXT NOT NULL, title TEXT NOT NULL"
        " DEFAULT '', verb TEXT NOT NULL DEFAULT 'ask', scope TEXT, state TEXT NOT NULL DEFAULT"
        " 'active', revision INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL DEFAULT 0,"
        " updated_at INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE turns (conversation_id TEXT NOT NULL, ordinal INTEGER NOT NULL, prompt TEXT"
        " NOT NULL, state TEXT NOT NULL DEFAULT 'working', reply TEXT, skill TEXT NOT NULL DEFAULT"
        " 'grilling', priority TEXT NOT NULL DEFAULT 'interactive', started_at INTEGER NOT NULL"
        " DEFAULT 0, replied_at INTEGER NOT NULL DEFAULT 0, park_reason TEXT,"
        " PRIMARY KEY (conversation_id, ordinal));"
        "CREATE TABLE commands (key TEXT PRIMARY KEY, outcome TEXT NOT NULL);"
        "INSERT INTO conversations (id, repo, title) VALUES ('c-old', 'ConnorGriffin/agentflow', 'Old Ask');"
        "PRAGMA user_version = 1;")
    conn.commit()
    conn.close()

    store = WorkspaceStore("ConnorGriffin/agentflow", path=path)
    try:
        assert store.conversation("c-old").title == "Old Ask"     # the old conversation survived
        # And the new Proposal tables now work.
        out = _stage(store, "c-old", hash_="sha256:v1", key="s1")
        assert out.accepted and store.proposal("c-old").state == STAGED
    finally:
        store.close()


def test_proposal_versions_survive_a_fresh_store(tmp_path):
    path = tmp_path / "agentflow.db"
    store = WorkspaceStore("ConnorGriffin/agentflow", path=path)
    cid = _convo(store)
    _stage(store, cid, hash_="sha256:v1", key="s1", now=10)
    _stage(store, cid, hash_="sha256:v2", key="s2", now=20)
    store.approve_proposal(cid, "sha256:v2", idempotency_key="a1", now=30)
    store.close()

    reopened = WorkspaceStore("ConnorGriffin/agentflow", path=path)
    try:
        prop = reopened.proposal(cid)
        assert prop.state == APPROVED and prop.approved_hash == "sha256:v2"
        assert [v.content_hash for v in prop.versions] == ["sha256:v1", "sha256:v2"]
    finally:
        reopened.close()


def test_a_v2_store_migrates_in_place_gaining_the_failed_disposition_columns(tmp_path):
    """A schema-2 store has Proposal tables but no durable failed disposition. Opening it must add
    the failure columns in place, keeping its approved Proposal — then a publish failure can park it.
    Would fail if the v3 bump fenced the old store off or dropped its rows."""
    path = tmp_path / "agentflow.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, repo TEXT NOT NULL, title TEXT NOT NULL"
        " DEFAULT '', verb TEXT NOT NULL DEFAULT 'ask', scope TEXT, state TEXT NOT NULL DEFAULT"
        " 'active', revision INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL DEFAULT 0,"
        " updated_at INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE commands (key TEXT PRIMARY KEY, outcome TEXT NOT NULL);"
        # schema-2 proposals: no fail_reason / failed_at columns
        "CREATE TABLE proposals (conversation_id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT"
        " 'staged', title TEXT NOT NULL DEFAULT '', approved_hash TEXT, published_issue INTEGER,"
        " published_url TEXT, published_at INTEGER NOT NULL DEFAULT 0, staged_at INTEGER NOT NULL"
        " DEFAULT 0, updated_at INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE proposal_versions (conversation_id TEXT NOT NULL, version INTEGER NOT NULL,"
        " content_hash TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT"
        " '', acceptance TEXT NOT NULL DEFAULT '[]', body TEXT NOT NULL DEFAULT '', staged_at"
        " INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (conversation_id, version));"
        "INSERT INTO conversations (id, repo, title) VALUES ('c2', 'ConnorGriffin/agentflow', 'A');"
        "INSERT INTO proposals (conversation_id, state, title, approved_hash) VALUES"
        " ('c2', 'approved', 'Old draft', 'sha256:v1');"
        "INSERT INTO proposal_versions (conversation_id, version, content_hash, title) VALUES"
        " ('c2', 1, 'sha256:v1', 'Old draft');"
        "PRAGMA user_version = 2;")
    conn.commit()
    conn.close()

    store = WorkspaceStore("ConnorGriffin/agentflow", path=path)
    try:
        prop = store.proposal("c2")
        assert prop.state == APPROVED and prop.approved_hash == "sha256:v1"  # survived the migration
        assert prop.fail_reason is None and prop.failed_at == 0             # new columns default clean
        out = store.fail_publication("c2", "sha256:v1", reason="GitHub rejected the request.", now=5)
        assert out.accepted and store.proposal("c2").state == FAILED
    finally:
        store.close()
