"""The daemon-side command application (ADR 0033) — the only writer of workspace state.

A transported command opens an Ask or sends a turn: it records the operator's message in the
workspace store and submits the coordinated ``converse`` turn. Everything is idempotent, so a
re-drained command (the web layer retried, or the daemon crashed mid-drain) never appends a
duplicate turn or launches a second session.
"""

from __future__ import annotations

import json

import pytest

from agentflow import coordinated_converse
from agentflow.workspace import channel
from agentflow.workspace.store import WorkspaceStore

REPO = "ConnorGriffin/agentflow"


class FakeCoordinator:
    def __init__(self):
        self.submitted = []

    def submit_stage(self, submission):
        self.submitted.append(submission)
        return f"{submission.repo}|{submission.subject}|{submission.stage}|{submission.target}"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    return tmp_path


def _open_cmd(key="c1", cid="conv-1", prompt="what should we build?"):
    return {"key": key, "kind": "open_ask", "repo": REPO, "conversation_id": cid,
            "prompt": prompt, "title": "Ask"}


def test_open_ask_records_the_turn_and_submits_the_interactive_stage(state):
    coord = FakeCoordinator()
    out = coordinated_converse.apply_command(_open_cmd(), coord, workdir=str(state), now=1)
    assert out["status"] == "accepted" and out["ordinal"] == 0
    assert len(coord.submitted) == 1
    sub = coord.submitted[0]
    assert sub.stage == "converse" and sub.subject == "conv-1" and sub.target == "0"
    assert sub.interactive is True                         # operator-present → priority
    store = WorkspaceStore(REPO)
    try:
        turn = store.conversation("conv-1").turns[0]
        assert turn.prompt == "what should we build?"
    finally:
        store.close()


def test_reapplying_the_same_command_never_duplicates_the_turn_or_the_submission(state):
    coord = FakeCoordinator()
    coordinated_converse.apply_command(_open_cmd(), coord, workdir=str(state), now=1)
    coordinated_converse.apply_command(_open_cmd(), coord, workdir=str(state), now=1)  # re-drained
    store = WorkspaceStore(REPO)
    try:
        assert len(store.conversation("conv-1").turns) == 1   # no duplicate turn
    finally:
        store.close()
    # Any re-submission targets the identical turn identity, so the coordinator dedups it — a
    # re-drained command can never produce a second turn (ADR 0034 idempotent submission).
    identities = {(s.subject, s.stage, s.target) for s in coord.submitted}
    assert identities == {("conv-1", "converse", "0")}


def test_send_turn_advances_the_conversation(state):
    coord = FakeCoordinator()
    coordinated_converse.apply_command(_open_cmd(), coord, workdir=str(state), now=1)
    store = WorkspaceStore(REPO)
    try:
        rev = store.conversation("conv-1").revision
    finally:
        store.close()
    out = coordinated_converse.apply_command(
        {"key": "c2", "kind": "send_turn", "repo": REPO, "conversation_id": "conv-1",
         "prompt": "go deeper", "expected_revision": rev}, coord, workdir=str(state), now=2)
    assert out["status"] == "accepted" and out["ordinal"] == 1
    assert coord.submitted[-1].target == "1"


def test_a_stale_send_turn_is_rejected_and_submits_nothing(state):
    coord = FakeCoordinator()
    coordinated_converse.apply_command(_open_cmd(), coord, workdir=str(state), now=1)
    out = coordinated_converse.apply_command(
        {"key": "c2", "kind": "send_turn", "repo": REPO, "conversation_id": "conv-1",
         "prompt": "racing", "expected_revision": 0}, coord, workdir=str(state), now=2)
    assert out["status"] == "rejected" and out["error"] == "stale revision"
    assert len(coord.submitted) == 1                      # only the open_ask turn was submitted


# --- approve / discard a Build-Issue Proposal ------------------------------------------

def _stage_a_proposal(cid="conv-1", hash_="sha256:v1"):
    store = WorkspaceStore(REPO)
    try:
        store.open_conversation(title="Ask", conversation_id=cid, idempotency_key=f"{cid}:o", now=1)
        store.stage_proposal(cid, title="Add a button", summary="s", acceptance=["works"],
                             body="", content_hash=hash_, idempotency_key="s1", now=2)
    finally:
        store.close()


def test_approve_command_binds_to_the_exact_hash_and_submits_no_turn(state):
    _stage_a_proposal()
    coord = FakeCoordinator()
    out = coordinated_converse.apply_command(
        {"key": "ap1", "kind": "approve_proposal", "repo": REPO, "conversation_id": "conv-1",
         "content_hash": "sha256:v1"}, coord, workdir=str(state), now=3)
    assert out["status"] == "accepted" and out["content_hash"] == "sha256:v1"
    assert coord.submitted == []                          # approval is not a coordinated turn
    store = WorkspaceStore(REPO)
    try:
        assert store.proposal("conv-1").approved_hash == "sha256:v1"
    finally:
        store.close()


def test_approve_command_is_idempotent_on_its_key(state):
    _stage_a_proposal()
    coord = FakeCoordinator()
    cmd = {"key": "ap1", "kind": "approve_proposal", "repo": REPO, "conversation_id": "conv-1",
           "content_hash": "sha256:v1"}
    a = coordinated_converse.apply_command(cmd, coord, workdir=str(state), now=3)
    b = coordinated_converse.apply_command(cmd, coord, workdir=str(state), now=4)  # re-drained
    assert a == b


def test_discard_command_drops_the_proposal_but_keeps_the_conversation(state):
    _stage_a_proposal()
    coord = FakeCoordinator()
    out = coordinated_converse.apply_command(
        {"key": "d1", "kind": "discard_proposal", "repo": REPO, "conversation_id": "conv-1"},
        coord, workdir=str(state), now=3)
    assert out["status"] == "accepted"
    store = WorkspaceStore(REPO)
    try:
        assert store.proposal("conv-1").state == "discarded"
        assert store.conversation("conv-1") is not None   # the conversation survives
    finally:
        store.close()


# --- draining the spool ----------------------------------------------------------------

def test_drain_applies_pending_commands_and_acknowledges_them(state):
    coord = FakeCoordinator()
    channel.enqueue(_open_cmd(key="c1", cid="conv-a"))
    channel.enqueue(_open_cmd(key="c2", cid="conv-b"))
    coordinated_converse.drain_commands(coord, {REPO: str(state)})
    assert {s.subject for s in coord.submitted} == {"conv-a", "conv-b"}
    assert channel.pending() == []                        # each drained command is acknowledged


def test_drain_drops_a_command_for_an_unenrolled_repo(state):
    coord = FakeCoordinator()
    channel.enqueue({"key": "x", "kind": "open_ask", "repo": "someone/else",
                     "conversation_id": "c", "prompt": "hi"})
    coordinated_converse.drain_commands(coord, {REPO: str(state)})
    assert coord.submitted == [] and channel.pending() == []


def test_ack_refuses_a_key_that_would_escape_the_spool(state):
    victim = state / "victim.json"
    victim.write_text("keep")
    channel.ack("../../victim")
    assert victim.read_text() == "keep"


def test_pending_command_from_the_old_filename_format_remains_drainable(state):
    command = _open_cmd(key="legacy-key")
    spool = channel.commands_dir()
    spool.mkdir(parents=True)
    legacy = spool / "legacy-key.json"
    legacy.write_text(json.dumps(command))

    assert channel.pending() == [command]
    assert not legacy.exists()
    channel.ack("legacy-key")
    assert channel.pending() == []


def test_new_retry_wins_over_the_old_filename_format(state):
    old = _open_cmd(key="legacy-key", prompt="old")
    new = _open_cmd(key="legacy-key", prompt="new")
    spool = channel.commands_dir()
    spool.mkdir(parents=True)
    legacy = spool / "legacy-key.json"
    legacy.write_text(json.dumps(old))
    channel.enqueue(new)

    assert channel.pending() == [new]
    assert not legacy.exists()
