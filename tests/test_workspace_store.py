"""The daemon-owned per-Project workspace store (ADR 0033/0034), exercised through its public
command surface.

The store keeps one Project's Conversations as append-only immutable turns keyed by
``(repository, Conversation ID, turn ordinal)``. The proofs that matter for the conversation
spine: submitting a turn is idempotent and revision-guarded (the aggregate revision is the turn
claim); a reply is adopted exactly once (one identity, one reply); a parked turn preserves the
operator's message and survives a fresh store over the same directory; nothing auto-expires.
"""

from __future__ import annotations

import pytest

from agentflow.workspace.store import (ACCEPTED, PAUSED, REJECTED, REPLIED, WORKING,
                                       WorkspaceStore)


@pytest.fixture
def store(tmp_path):
    s = WorkspaceStore("ConnorGriffin/agentflow", path=tmp_path / "agentflow.db")
    yield s
    s.close()


def _open(store, key="k-open", cid="conv-1"):
    out = store.open_conversation(title="What should we build?", conversation_id=cid,
                                  idempotency_key=key, now=100)
    assert out.accepted and out.conversation_id == cid and out.revision == 0
    return cid


# --- opening + the first turn ----------------------------------------------------------

def test_open_is_idempotent_on_its_key(store):
    a = store.open_conversation(title="t", conversation_id="c1", idempotency_key="k1", now=1)
    b = store.open_conversation(title="different", conversation_id="c1", idempotency_key="k1", now=2)
    assert a == b                                   # same terminal outcome replayed
    assert len(store.conversations()) == 1          # never a second Conversation


def test_first_turn_records_the_operator_message_as_working(store):
    cid = _open(store)
    out = store.start_turn(cid, "tell me about the repo", expected_revision=0,
                           idempotency_key="k-t0", now=101)
    assert out.accepted and out.ordinal == 0 and out.revision == 1
    convo = store.conversation(cid)
    assert convo.state == "active" and len(convo.turns) == 1
    turn = convo.turns[0]
    assert turn.prompt == "tell me about the repo" and turn.state == WORKING
    assert turn.reply is None and turn.priority == "interactive"


# --- idempotent, revision-guarded turn submission --------------------------------------

def test_resubmitting_the_same_turn_key_never_duplicates(store):
    cid = _open(store)
    a = store.start_turn(cid, "first", expected_revision=0, idempotency_key="k", now=1)
    b = store.start_turn(cid, "first", expected_revision=0, idempotency_key="k", now=1)
    assert a == b                                   # replayed outcome
    assert len(store.conversation(cid).turns) == 1  # one turn for the identity


def test_a_stale_expected_revision_is_rejected_and_stays_terminal(store):
    cid = _open(store)
    store.start_turn(cid, "turn zero", expected_revision=0, idempotency_key="t0", now=1)
    # The aggregate is now at revision 1; a command that still expects 0 is stale.
    stale = store.start_turn(cid, "racing turn", expected_revision=0, idempotency_key="race", now=2)
    assert stale.status == REJECTED and stale.error == "stale revision"
    assert len(store.conversation(cid).turns) == 1
    # The same key replays the same rejection — a retry of a stale command never suddenly wins.
    again = store.start_turn(cid, "racing turn", expected_revision=0, idempotency_key="race", now=3)
    assert again == stale


def test_multi_turn_conversation_advances_the_aggregate(store):
    cid = _open(store)
    r1 = store.start_turn(cid, "one", expected_revision=0, idempotency_key="a", now=1)
    store.complete_turn(cid, 0, "answer one", now=2)
    convo = store.conversation(cid)
    r2 = store.start_turn(cid, "two", expected_revision=convo.revision, idempotency_key="b", now=3)
    assert r1.ordinal == 0 and r2.ordinal == 1
    ordinals = [t.ordinal for t in store.conversation(cid).turns]
    assert ordinals == [0, 1]


# --- one identity, one reply (the anti-duplication guarantee) ---------------------------

def test_a_reply_is_adopted_exactly_once(store):
    cid = _open(store)
    store.start_turn(cid, "q", expected_revision=0, idempotency_key="t0", now=1)
    first = store.complete_turn(cid, 0, "the real reply", now=2)
    assert first.accepted
    # A second completion for the same (conversation, ordinal) — e.g. a recovered finalizer — must
    # not append or overwrite: one identity carries exactly one reply.
    second = store.complete_turn(cid, 0, "a DUPLICATE reply", now=3)
    assert second.accepted
    turns = [t for t in store.conversation(cid).turns if t.ordinal == 0]
    assert len(turns) == 1
    assert turns[0].state == REPLIED and turns[0].reply == "the real reply"


# --- parking preserves the message and survives a restart; nothing expires --------------

def test_parking_preserves_the_operator_message_and_survives_a_restart(tmp_path):
    path = tmp_path / "agentflow.db"
    store = WorkspaceStore("ConnorGriffin/agentflow", path=path)
    cid = _open(store)
    store.start_turn(cid, "an unanswered question", expected_revision=0, idempotency_key="t0", now=1)
    store.park_turn(cid, 0, reason="turn exhausted its budget — needs you", now=2)
    store.close()

    # A fresh store over the same directory recovers the parked conversation — nothing was
    # garbage collected, and the operator's message is intact so the turn can resume.
    reopened = WorkspaceStore("ConnorGriffin/agentflow", path=path)
    try:
        convo = reopened.conversation(cid)
        assert convo.state == "parked"
        turn = convo.turns[0]
        assert turn.state == PAUSED and turn.prompt == "an unanswered question"
        assert turn.park_reason == "turn exhausted its budget — needs you"
    finally:
        reopened.close()


def test_completing_a_parked_turn_lets_the_conversation_resume(store):
    cid = _open(store)
    store.start_turn(cid, "q", expected_revision=0, idempotency_key="t0", now=1)
    store.park_turn(cid, 0, reason="needs you", now=2)
    # A continuation that finally produces the reply completes the same turn (a park is not final).
    out = store.complete_turn(cid, 0, "resolved reply", now=3)
    assert out.accepted
    convo = store.conversation(cid)
    # The aggregate resumes too: adopting the reply clears the park, so the projection no longer
    # reports the conversation as parked (not just the turn).
    assert convo.state == "active"
    turn = convo.turns[0]
    assert turn.state == REPLIED and turn.reply == "resolved reply"


def test_unknown_conversation_is_rejected_not_created(store):
    out = store.start_turn("nope", "hi", expected_revision=0, idempotency_key="x", now=1)
    assert out.status == REJECTED and out.error == "unknown conversation"
    assert store.conversation("nope") is None
