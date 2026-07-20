"""The one module that owns the crash-safe human-handoff envelope (ADR 0042).

Whenever a stage hands a piece of work back to a human — parks a reviewed PR, holds an
issue at intake or mockup, settles a reply — it runs the same crash-safe recipe so a
daemon that dies mid-handoff and restarts does not act twice or ping twice:

1. read the subject's comments;
2. post a marker comment **only if it is not already present**;
3. re-read and **prove** the marker landed — if it cannot be proven, return ``None`` so
   the next cycle retries (proof is deliberately withheld to force the retry);
4. notify the operator **exactly once**, only when the marker was newly posted, keyed by
   a sequence id derived from the work's identity plus a stage tag.

That ordering — the exact thing that must never be reassembled wrong — used to be
copy-pasted across seven-plus hold/park/settle sites, with the notify key even copied at
inconsistent lengths (12 versus 24 characters). Here it lives once. The marker comment's
presence is the idempotency signal for the *action*; the derived sequence id is the
idempotency signal for the *notification*. Both are owned here.

The envelope is deliberately thin: stage-specific bookkeeping (deleting a finished review
checkout, recording ratchet state) is *not* part of this module — the stage does that
itself after the call confirms (returns non-``None``).

This is a purely additive keystone (ADR 0042): it migrates no existing caller, so it
cannot change how the pipeline behaves.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Literal, Sequence

from agentflow import github, notify as _notify

# One fixed length for every derived notify key, replacing the copy-pasted 12-versus-24
# character hashes the scattered sites used.
_SEQUENCE_ID_LENGTH = 24


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
    """What to ping the operator with, exactly once, when the marker is newly posted."""
    title: str
    message: str


# Seams, defaulting to the real GitHub-access module (ADR 0040) and the ntfy push. Tests
# state GitHub's answer and observe the ping by substituting these.
CommentReader = Callable[[Subject], "Sequence[github.Comment] | None"]
Notifier = Callable[[str, str, str, str], bool]


def _read_comments(subject: Subject) -> Sequence[github.Comment] | None:
    if subject.kind == "pr":
        return github.pr_comments(subject.repo, subject.number)
    return github.issue_comments(subject.repo, subject.number)


class DurableHandoff:
    """Runs the whole crash-safe handoff envelope in one call.

    The caller supplies the marker text, the action to take when the marker is absent, and
    the notification to send; :meth:`hand_off` does read → act-iff-absent → re-read-and-
    prove → notify-once and returns the subject URL (durable proof) or ``None``.
    """

    def __init__(self, *, read_comments: CommentReader | None = None,
                 notify: Notifier | None = None) -> None:
        self._read_comments = read_comments or _read_comments
        self._notify = notify or _notify.notify

    def hand_off(self, subject: Subject, *, identity: str, stage: str, marker: str,
                 action: Callable[[], object], notification: Notification) -> str | None:
        """Hand ``subject`` to a human, crash-safely, and return its URL — or ``None`` to retry.

        ``identity`` is the work's stable identity and ``stage`` a per-stage tag; together
        they derive the notify key, so a crash-restart repeat replaces the same client
        notification instead of pinging twice. ``action`` posts the marker comment and is
        called **only when the marker is absent**; its result is ignored because the marker
        is proven by a re-read, never trusted from the write.
        """
        before = self._read_comments(subject)
        if before is None:
            return None
        already = self._has_marker(before, marker)
        if not already:
            action()
        proved = self._read_comments(subject)
        if proved is None or not self._has_marker(proved, marker):
            # The marker could not be proven present — withhold proof so the next cycle
            # retries, and send no ping.
            return None
        if not already:
            sequence_id = self._sequence_id(identity, stage)
            self._notify(notification.title, notification.message, subject.url, sequence_id)
        return subject.url

    @staticmethod
    def _has_marker(comments: Sequence[github.Comment], marker: str) -> bool:
        return any(marker in comment.body for comment in comments)

    @staticmethod
    def _sequence_id(identity: str, stage: str) -> str:
        return sha256(f"{identity}:{stage}".encode()).hexdigest()[:_SEQUENCE_ID_LENGTH]
