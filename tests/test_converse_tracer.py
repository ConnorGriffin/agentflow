"""A Conversation turn as the coordinated ``converse`` stage (ADR 0034), driven through the
public ``submit_stage`` / ``cycle`` seam.

Each operator message is one bounded coordinated turn with identity ``(repository, Conversation
ID, turn ordinal)``. The proofs that matter for tracer #1: submission is idempotent; the turn's
required outcome is a durable reply (a clean exit that recorded nothing continues, never a second
reply); the daemon-side finalizer is the *only* writer that appends the immutable turn, exactly
once; exhaustion parks "needs you" preserving the operator's message and survives a restart; and
an interactive Ask turn outranks background pipeline work at admission without bypassing permits.

The coordinator crash boundaries run through the same public seam and the real stage router; the
reply artifact and workspace adoption use the production collaborators against a real worktree and
a real workspace store, with only the launcher/liveness/provider faked (ADR 0020).
"""

from __future__ import annotations

from pathlib import Path

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_converse
from agentflow.coordinator import ConverseStageAdapter, StageRouter, Submission, tracer
from agentflow.coordinator.providers import ProviderCause
from agentflow.workspace.store import PAUSED, REPLIED, WorkspaceStore

REPO = "ConnorGriffin/agentflow"


def _seed_conversation(cid="conv-1", prompt="tell me about the repo", now=100):
    """Open an Ask with its first operator turn in a real workspace store (as the daemon would
    before submitting the coordinated turn)."""
    store = WorkspaceStore(REPO)
    try:
        store.open_conversation(title="Ask", conversation_id=cid, idempotency_key=f"{cid}:open",
                                now=now)
        store.start_turn(cid, prompt, expected_revision=0, idempotency_key=f"{cid}:t0", now=now)
    finally:
        store.close()


def _submission(workdir, cid="conv-1", ordinal=0, prompt="tell me about the repo"):
    return coordinated_converse.converse_submission(REPO, str(workdir), cid, ordinal, prompt)


def _adapter(fake):
    """The production converse collaborators (real reply artifact + real workspace adoption),
    with the fake playing the provider observer."""
    return ConverseStageAdapter(
        reply_ready=coordinated_converse._reply_ready,
        adopt=coordinated_converse._adopt_turn,
        park=coordinated_converse._park_ask,
        observer=fake)


def _coord(make_coord, fake):
    router = StageRouter({"converse": _adapter(fake)})
    return make_coord(fake, adapter=router, gate=tracer.build_review_revise_gate)


def _write_reply(record, text):
    path = Path(coordinated_converse.reply_path(record))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- identity: one message is one turn; re-submission never duplicates ------------------

def test_one_message_is_one_turn_identity_and_a_later_message_is_a_new_turn(make_coord, coord_state):
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    _seed_conversation()
    first = coord.submit_stage(_submission(coord_state, ordinal=0))
    again = coord.submit_stage(_submission(coord_state, ordinal=0))   # a re-drained command
    assert first == again == f"{REPO}|conv-1|converse|0"             # one stable identity
    later = coord.submit_stage(_submission(coord_state, ordinal=1))   # the operator's next message
    assert later != first
    assert record_of(coord, later).attempts == 0                     # its own fresh budget


# --- outcome-first: a clean exit with no reply continues; one reply is appended once -----

def test_clean_exit_without_a_reply_stays_incomplete_then_yields_exactly_one_reply(
        make_coord, coord_state):
    """The regression that must fail if a duplicate reply is ever appended for one
    (repository, Conversation ID, turn ordinal)."""
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    _seed_conversation()
    ident = coord.submit_stage(_submission(coord_state, ordinal=0))
    coord.cycle("claude")                                            # admit the turn
    record = record_of(coord, ident)

    fake.end(ident, success=True, cause=ProviderCause.PROCESS)       # a clean exit — but no reply
    coord.cycle("claude")
    rec = record_of(coord, ident)
    assert rec.continuation is True and rec.attempts == 2            # incomplete → it continues
    assert not rec.retired and rec.state != "held"
    store = WorkspaceStore(REPO)
    try:
        assert store.conversation("conv-1").turns[0].reply is None  # nothing adopted yet
    finally:
        store.close()

    _write_reply(record, "the repo is an autonomous issue→PR→review pipeline.")
    fake.end(ident, cause=ProviderCause.PROCESS)                     # a later attempt lands the reply
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    coord.cycle("claude")                                           # settle → finalize_completed adopts
    assert record_of(coord, ident).retired is True

    store = WorkspaceStore(REPO)
    try:
        turns = store.conversation("conv-1").turns
        assert len(turns) == 1 and turns[0].state == REPLIED
        assert turns[0].reply == "the repo is an autonomous issue→PR→review pipeline."
    finally:
        store.close()

    # Even a later DIFFERENT artifact cannot overwrite the adopted turn: adoption is once-only.
    _write_reply(record, "a DUPLICATE reply that must never be appended")
    coord.cycle("claude")

    # Idempotent across a restart: a fresh coordinator re-observes the retired record and never
    # appends a second reply for the same identity.
    _coord(make_coord, fake).cycle("claude")
    store = WorkspaceStore(REPO)
    try:
        assert len(store.conversation("conv-1").turns) == 1
    finally:
        store.close()


def test_a_bad_exit_still_completes_once_the_reply_is_durable(make_coord, coord_state):
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    _seed_conversation()
    ident = coord.submit_stage(_submission(coord_state, ordinal=0))
    coord.cycle("claude")
    _write_reply(record_of(coord, ident), "here is the answer")
    fake.end(ident, success=False, cause=ProviderCause.PROCESS)      # the provider exited badly...
    assert [o.status for o in coord.cycle("claude")] == ["completed"]  # ...but the reply is durable


# --- exhaustion parks "needs you", message preserved, survives restart ------------------

def test_exhaustion_parks_needs_you_and_preserves_the_operator_message(make_coord, coord_state):
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    _seed_conversation(prompt="an unanswered question")
    ident = coord.submit_stage(_submission(coord_state, ordinal=0, prompt="an unanswered question"))
    outcome = None
    for _ in range(8):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)                 # never a reply
    assert outcome is not None and outcome.status == "held"
    assert outcome.handoff == "ask:needs-you"

    # The park is durable and preserves the operator's message; nothing auto-expires. A fresh
    # workspace store over the same directory recovers the paused turn.
    store = WorkspaceStore(REPO)
    try:
        turn = store.conversation("conv-1").turns[0]
        assert turn.state == PAUSED and turn.prompt == "an unanswered question"
    finally:
        store.close()
    # A restart never repeats the external park.
    assert _coord(make_coord, fake).cycle("claude") == []


# --- admission: an interactive Ask turn outranks background pipeline work ----------------

def test_interactive_ask_turn_outranks_background_build_at_admission(make_coord, coord_state):
    """With pool headroom for only one of them, the operator's interactive turn admits first —
    priority reorders admission, but the permit ledger still gates the start (ADR 0034)."""
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    _seed_conversation()
    # Background build (claude, deep, medium effort) reserves four permits; the interactive
    # converse turn reserves two — together seven, past the five-permit budget, so only one fits.
    build = coord.submit_stage(Submission(repo=REPO, subject="1", stage="build", pool="claude",
                                          complexity="deep", effort="medium", builder_lineage="claude"))
    ask = coord.submit_stage(_submission(coord_state, ordinal=0))
    coord.cycle("claude")
    assert record_of(coord, ask).state == "running"                  # the operator's turn wins
    assert record_of(coord, build).state == "waiting"               # background build queued behind
    assert permits(coord, "claude") == 2                             # only the converse demand reserved


def test_priority_never_bypasses_the_permit_ledger(make_coord, coord_state):
    """An interactive turn that cannot fit the pool does not force its way in — it waits, exactly
    like any stage (ADR 0034: priority reorders, it never bypasses budgets)."""
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    # Saturate the pool with a five-permit background build first.
    coord.submit_stage(Submission(repo=REPO, subject="9", stage="build", pool="claude",
                                  complexity="deep", effort="extra", builder_lineage="claude"))
    coord.cycle("claude")
    assert permits(coord, "claude") == 5
    _seed_conversation()
    ask = coord.submit_stage(_submission(coord_state, ordinal=0))
    coord.cycle("claude")
    assert record_of(coord, ask).state == "waiting"                 # no headroom — it waits its turn
