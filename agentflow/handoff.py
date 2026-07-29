"""The one module that owns the crash-safe human-handoff envelope (ADR 0042).

Whenever a stage hands a piece of work back to a human — parks a reviewed PR, holds an
issue at intake or mockup, settles a reply — it runs the same crash-safe recipe so a
daemon that dies mid-handoff and restarts never acts twice, and never leaves the operator
un-told:

1. read the subject's comments;
2. post a marker comment **only if it is not already present**;
3. re-read and **prove** the marker landed — if it cannot be proven, return ``None`` so
   the next cycle retries (proof is deliberately withheld to force the retry);
4. notify the operator on **every** cycle that proves the marker, keyed by a sequence id
   derived from the work's identity plus a stage tag.

Step 4 is deliberately *at-least-once*, not exactly-once. Pinging only on the cycle that posts
the marker loses the ping outright whenever the daemon dies between the comment reaching GitHub
and the push going out — and that window is seconds wide, because the posting action goes on to
edit titles and shuffle labels before returning. The next cycle would see the marker, take
itself for a repeat, and never tell anyone the pipeline is waiting on them. For "a human is
needed here", a second ping costs an operator two seconds and a dropped one costs a stalled
issue, so the envelope pings whenever it can prove the handoff exists. Exactly-once would need
durable notification state, which this module deliberately does not have.

That ordering — the exact thing that must never be reassembled wrong — used to be
copy-pasted across seven-plus hold/park/settle sites, with the notify key even copied at
inconsistent lengths (12 versus 24 characters). Here it lives once. The marker comment's
presence is the idempotency signal for the *action*, and this module also derives the markers
(:func:`proof_marker`) so that two different holds on one subject can never be mistaken for
each other. The sequence id travels with the ping as its stable key.

The envelope is deliberately thin: stage-specific bookkeeping (deleting a finished review
checkout, recording ratchet state) is *not* part of this module — the stage does that
itself after the call confirms (returns non-``None``).

Every handoff that hands a GitHub issue or PR to a human now runs through here: the review
and respond parks, the intake hold, the intake grill/mockup routes, and the build and mockup
holds. What stays outside is what is not a handoff — resolving a research ticket writes no
marker for a human to answer and pings no one, and a parked conversation turn lives in the
workspace store rather than on an issue or PR.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Literal, Sequence

from agentflow import github, notify as _notify

# One fixed length for every derived notify key (see module docstring).
_SEQUENCE_ID_LENGTH = 24
# And one for every derived marker, matching the park marker this generalizes.
_MARKER_DIGEST_LENGTH = 20


def proof_marker(identity: str, reason: str, *, tag: str) -> str:
    """The hidden tag one handoff matches on, scoped to this exact record *and* its reason.

    A marker derived from the comment's own text cannot do that job: two genuinely different
    holds that happen to compose the same words read as one, and the second silently posts
    nothing and pings no one. Deriving it from the record identity plus the reason it is handing
    off keeps distinct holds distinct while staying stable across a crash and restart, which is
    what the envelope needs. Mirrors :func:`agentflow.pr_park.park_proof_marker`.
    """
    digest = sha256(f"{identity}:{reason}".encode()).hexdigest()[:_MARKER_DIGEST_LENGTH]
    return f"<!-- agentflow-{tag}:{digest} -->"


def marked_body(body: str, marker: str) -> str:
    """One handoff comment's text carrying its marker invisibly, after the leading disclaimer.

    Not appended at the end, deliberately: a comment ending in the marker still contains the
    *unmarked* text verbatim, and that unmarked text is what an older deploy matched on. A second
    record's hold would then read the first record's comment as proof of its own and go silent —
    the collision this marker exists to prevent.
    """
    head, separator, tail = body.partition("\n\n")
    return f"{head}\n\n{marker}\n\n{tail}" if separator else f"{body}\n\n{marker}"


@dataclass(frozen=True)
class Subject:
    """The issue or PR whose work is being handed to a human."""
    repo: str
    number: int
    kind: Literal["issue", "pr"]

    @property
    def url(self) -> str:
        """The durable proof the envelope returns once the marker is proven present."""
        path = "issues" if self.kind == "issue" else "pull"
        return f"https://github.com/{self.repo}/{path}/{self.number}"


@dataclass(frozen=True)
class Notification:
    """What to ping the operator with once the handoff is proven to exist."""
    title: str
    message: str


# Seams, defaulting to the real GitHub-access module (ADR 0040) and the ntfy push. Tests
# state GitHub's answer and observe the ping by substituting these.
CommentReader = Callable[[Subject], Sequence[github.Comment] | None]
Notifier = Callable[[str, str, str, str], bool]


def _read_comments(subject: Subject) -> Sequence[github.Comment] | None:
    if subject.kind == "pr":
        return github.pr_comments(subject.repo, subject.number)
    return github.issue_comments(subject.repo, subject.number)


class DurableHandoff:
    """Runs the whole crash-safe handoff envelope in one call.

    The caller supplies the marker, the action to take when the marker is absent, and the
    notification to send; :meth:`hand_off` does read → act-iff-absent → re-read-and-prove →
    notify and returns the subject URL (durable proof) or ``None``.
    """

    def __init__(self, *, read_comments: CommentReader | None = None,
                 notify: Notifier | None = None) -> None:
        self._read_comments = read_comments or _read_comments
        self._notify = notify or _notify.notify

    def hand_off(self, subject: Subject, *, identity: str, stage: str, marker: str,
                 action: Callable[[], object], notification: Notification,
                 also_proven_by: str = "") -> str | None:
        """Hand ``subject`` to a human, crash-safely, and return its URL — or ``None`` to retry.

        ``identity`` is the work's stable identity and ``stage`` a per-stage tag; together they
        derive the notify key the ping carries. ``action`` posts the marker comment and is called
        **only when the marker is absent**; its result is ignored because the marker is proven by
        a re-read, never trusted from the write. ``also_proven_by`` is a second string whose
        presence proves the same handoff — an earlier marker format still live on a thread, or a
        stage-native comment that already *is* the handoff — and, like the marker, suppresses the
        action.

        The ping is at-least-once: it goes out on every cycle that proves the marker, not only on
        the cycle that posts it (see the module docstring). The consequence the caller accepts is
        that a crash between this call returning and the record retiring — or any bookkeeping the
        caller retries afterwards — sends the operator a second copy of the same ping. That is
        the deliberate trade against dropping it silently.
        """
        before = self._read_comments(subject)
        if before is None:
            return None
        if not self._has_marker(before, marker, also_proven_by):
            action()
        proved = self._read_comments(subject)
        if proved is None or not self._has_marker(proved, marker, also_proven_by):
            # The marker could not be proven present — withhold proof so the next cycle
            # retries, and send no ping.
            return None
        sequence_id = self._sequence_id(identity, stage)
        self._notify(notification.title, notification.message, subject.url, sequence_id)
        return subject.url

    @staticmethod
    def _has_marker(comments: Sequence[github.Comment], marker: str, alternative: str) -> bool:
        return any(marker in comment.body or (alternative and alternative in comment.body)
                   for comment in comments)

    @staticmethod
    def _sequence_id(identity: str, stage: str) -> str:
        return sha256(f"{identity}:{stage}".encode()).hexdigest()[:_SEQUENCE_ID_LENGTH]
