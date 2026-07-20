"""Tests for the one crash-safe human-handoff envelope (ADR 0042).

These exercise ``DurableHandoff`` purely through its public call. They state what GitHub's
comment thread holds at each read and observe two things: whether the caller's marker-posting
action ran, and whether the operator was pinged. The crash windows the module exists to close
are covered directly: marker already present, the action posted but the prove-read fails, a
fresh handoff, and a repeat pass after a successful one.
"""

from __future__ import annotations

from agentflow import github
from agentflow.handoff import DurableHandoff, Notification, Subject

SUBJECT = Subject(repo="owner/repo", number=7, kind="pr")
IDENTITY = "owner/repo#7:review"
MARKER = "agentflow: parked for human review"
NOTE = Notification("agentflow needs you", "owner/repo PR #7: review parked")


class _Thread:
    """A controllable GitHub comment thread standing in for one subject.

    ``reads`` is the queued sequence of answers the module will get, in order, from each
    ``read_comments`` call — a list of ``github.Comment`` for a real thread, or ``None`` for
    an unreadable one (``gh`` errored). ``post_marker`` is the caller's action: it appends a
    marked comment, standing in for the real park/hold/settle write.
    """

    def __init__(self, reads):
        self._reads = list(reads)
        self.posts = 0
        self.pings: list[tuple[str, str, str, str]] = []

    def read(self, subject):
        return self._reads.pop(0)

    def post_marker(self):
        self.posts += 1

    def notify(self, title, message, url, sequence_id):
        self.pings.append((title, message, url, sequence_id))
        return True

    def handoff(self):
        return DurableHandoff(read_comments=self.read, notify=self.notify)


def _comment(body):
    return github.Comment(body=body, created_at="2026-07-19T00:00:00Z")


def _hand_off(thread):
    return thread.handoff().hand_off(
        SUBJECT, identity=IDENTITY, stage="review", marker=MARKER,
        action=thread.post_marker, notification=NOTE)


# --- the four crash windows ----------------------------------------------------

def test_fresh_handoff_acts_proves_and_pings_once():
    # Empty thread, then the marker present after the action posts it.
    thread = _Thread([[], [_comment(f"...\n{MARKER}\n...")]])
    result = _hand_off(thread)
    assert result == SUBJECT.url
    assert thread.posts == 1
    assert len(thread.pings) == 1


def test_marker_already_present_does_not_act_or_ping_again():
    # A repeat pass over an already-handed-off subject: the marker is there on the first read.
    thread = _Thread([[_comment(MARKER)], [_comment(MARKER)]])
    result = _hand_off(thread)
    assert result == SUBJECT.url
    assert thread.posts == 0
    assert thread.pings == []


def test_action_posted_but_prove_read_cannot_confirm_returns_nothing():
    # The action ran, but the re-read comes back without the marker (the write is not yet
    # observable): proof is withheld and no ping is sent, so the next cycle retries.
    thread = _Thread([[], [_comment("unrelated chatter")]])
    result = _hand_off(thread)
    assert result is None
    assert thread.posts == 1
    assert thread.pings == []


def test_prove_read_failure_returns_nothing_and_does_not_ping():
    # The re-read itself fails (gh unreachable). Unknown is not "marker absent": still no
    # proof, still no ping.
    thread = _Thread([[], None])
    result = _hand_off(thread)
    assert result is None
    assert thread.pings == []


def test_repeat_pass_after_a_successful_handoff_does_nothing():
    # First pass hands off; a second pass (as after a crash-restart) sees the marker and is inert.
    first = _Thread([[], [_comment(MARKER)]])
    assert _hand_off(first) == SUBJECT.url

    second = _Thread([[_comment(MARKER)], [_comment(MARKER)]])
    assert _hand_off(second) == SUBJECT.url
    assert second.posts == 0
    assert second.pings == []


# --- the read-failure guard on the first read ----------------------------------

def test_unreadable_first_read_acts_on_nothing():
    # Couldn't even read the thread: never act, never prove, never ping.
    thread = _Thread([None])
    assert _hand_off(thread) is None
    assert thread.posts == 0
    assert thread.pings == []


# --- the notify key the module owns --------------------------------------------

def test_notify_key_is_stable_and_one_fixed_length():
    # The same work handed off twice derives the same key, so a crash-restart replaces the
    # same client notification rather than pinging twice — and the length is uniform, not the
    # copy-pasted 12-versus-24 of the scattered sites.
    a = _Thread([[], [_comment(MARKER)]])
    _hand_off(a)
    b = _Thread([[], [_comment(MARKER)]])
    _hand_off(b)
    key_a = a.pings[0][3]
    key_b = b.pings[0][3]
    assert key_a == key_b
    assert len(key_a) == 24


def test_notify_key_differs_by_stage_and_identity():
    # Two stages of the same work, or the same stage of two works, must not collapse to one key.
    handoff = DurableHandoff(read_comments=lambda s: [], notify=lambda *a: True)
    review = handoff._sequence_id(IDENTITY, "review")
    respond = handoff._sequence_id(IDENTITY, "respond")
    other_work = handoff._sequence_id("owner/repo#9:review", "review")
    assert review != respond
    assert review != other_work


def test_ping_carries_the_notification_and_the_proof_url():
    thread = _Thread([[], [_comment(MARKER)]])
    _hand_off(thread)
    title, message, url, _key = thread.pings[0]
    assert title == NOTE.title
    assert message == NOTE.message
    assert url == SUBJECT.url


# --- the subject URL shape (the durable proof) ---------------------------------

def test_issue_and_pr_subjects_prove_with_their_own_url():
    assert Subject("owner/repo", 3, "issue").url == "https://github.com/owner/repo/issues/3"
    assert Subject("owner/repo", 3, "pr").url == "https://github.com/owner/repo/pull/3"
