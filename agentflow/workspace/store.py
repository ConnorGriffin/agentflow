"""The daemon-owned per-Project workspace store (ADR 0033/0034).

One SQLite database per Project under ``AGENTFLOW_STATE/workspace``, holding that Project's
Conversations and their append-only immutable turns. This is deliberately a *different* store
from the coordinator's ``records.db``: a Conversation has different identity, lifetime, failure,
and retention rules than fail-closed provider admission, and neither store mutates the other
(ADR 0033).

The durable model:

- A **Conversation** is the aggregate. It carries a stable ID, an optimistic ``revision``, and a
  lifecycle (``active`` / ``parked``). The revision is the turn *claim*: submitting a turn expects
  a revision, and a stale expectation is rejected — there is no GitHub label to claim, so the
  aggregate revision is the concurrency guard (ADR 0034).
- A **Turn** is one immutable round keyed by ``(repository, Conversation ID, turn ordinal)``. Its
  operator prompt is recorded when the turn starts and never rewritten; the daemon-side finalizer
  writes the reply exactly once. A second completion for the same ordinal is a no-op that returns
  the existing reply — the anti-duplication guarantee (ADR 0034: one turn, one reply).

Every mutation is a domain **command** carrying a stable idempotency key. A key's terminal
outcome is durable, so a repeat of the same command replays the same outcome instead of doing the
work twice (ADR 0033: idempotent commands). Reads never happen here — the daemon projects a
bounded JSON read model separately (:mod:`agentflow.workspace.projection`).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

SCHEMA_VERSION = 1
_BUSY_TIMEOUT_MS = int(os.environ.get("AGENTFLOW_WORKSPACE_BUSY_MS", "2000"))

# Turn states are honest and bounded (ADR 0034): a turn is only ever one of these — never a
# faked live-typing stream.
WORKING = "working"   # the operator's message is submitted; its bounded session is in flight
REPLIED = "replied"   # the session produced a reply; the turn is complete
PAUSED = "paused"     # the turn exhausted its budget and parked "needs you", message preserved

# Command outcome statuses.
ACCEPTED = "accepted"
REJECTED = "rejected"


class WorkspaceUnavailable(RuntimeError):
    """The workspace store could not be read or is a schema this build does not understand.
    The daemon projects the workspace as unavailable and fails workspace commands closed rather
    than rebuilding working history from external artifacts (ADR 0033)."""


def workspace_dir() -> Path:
    """Where per-Project workspace databases live, honoring ``AGENTFLOW_STATE`` like the rest of
    the daemon. Separate from the coordinator's ``coordinator/`` subtree (ADR 0033)."""
    root = Path(os.environ.get("AGENTFLOW_STATE", os.path.expanduser("~/.agentflow")))
    return root / "workspace"


def project_slug(repo: str) -> str:
    """A filesystem-safe stable slug for a Project's repository, used as its database name.
    ``owner/name`` → ``owner__name``; any other character collapses to ``-``."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", repo.replace("/", "__"))


@dataclass(frozen=True)
class CommandOutcome:
    """The terminal fact a workspace command returns. The same idempotency key always replays the
    same outcome, so a retried command is safe (ADR 0033)."""

    status: str                       # accepted | rejected
    conversation_id: str | None = None
    ordinal: int | None = None
    revision: int | None = None       # the aggregate revision after the command
    error: str | None = None          # why a command was rejected (stale revision, unknown convo)

    @property
    def accepted(self) -> bool:
        return self.status == ACCEPTED


@dataclass
class Turn:
    """One immutable conversation round keyed by ``(conversation_id, ordinal)``."""

    conversation_id: str
    ordinal: int
    prompt: str                       # the operator's message — recorded at start, never rewritten
    state: str = WORKING
    reply: str | None = None          # the session's reply — written once by the finalizer
    skill: str = "grilling"
    priority: str = "interactive"     # interactive Ask turns outrank background work (ADR 0034)
    started_at: int = 0
    replied_at: int = 0
    park_reason: str | None = None


@dataclass
class Conversation:
    """The Conversation aggregate. ``revision`` is the optimistic turn claim (ADR 0034)."""

    id: str
    repo: str
    title: str = ""
    verb: str = "ask"
    scope: str | None = None
    state: str = "active"             # active | parked
    revision: int = 0
    created_at: int = 0
    updated_at: int = 0
    turns: list[Turn] = field(default_factory=list)


class WorkspaceStore:
    """One durable per-Project workspace database. Constructed with a repository; a fresh instance
    over the same ``AGENTFLOW_STATE`` recovers every Conversation and turn, which is how a parked
    conversation survives a daemon restart (ADR 0034). Nothing auto-expires or is garbage
    collected.

    Every write goes through one ``BEGIN IMMEDIATE`` so a second daemon over the same file can
    never append two turns at one ordinal or advance the aggregate twice for one command.
    """

    def __init__(self, repo: str, *, path: Path | str | None = None) -> None:
        self.repo = repo
        self.path = Path(path) if path is not None else workspace_dir() / f"{project_slug(repo)}.db"
        self._lock = threading.RLock()
        self._conn = self._connect()

    # --- lifecycle ----------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        try:
            conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000,
                                   isolation_level=None, check_same_thread=False)
            conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            if fresh:
                self._initialize(conn)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                conn.close()
                raise WorkspaceUnavailable(
                    f"workspace schema {version} is not readable (want {SCHEMA_VERSION})")
        except sqlite3.DatabaseError as e:
            raise WorkspaceUnavailable(f"cannot open workspace store: {e}") from e
        return conn

    @staticmethod
    def _initialize(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "CREATE TABLE conversations ("
                " id TEXT PRIMARY KEY,"
                " repo TEXT NOT NULL,"
                " title TEXT NOT NULL DEFAULT '',"
                " verb TEXT NOT NULL DEFAULT 'ask',"
                " scope TEXT,"
                " state TEXT NOT NULL DEFAULT 'active',"
                " revision INTEGER NOT NULL DEFAULT 0,"
                " created_at INTEGER NOT NULL DEFAULT 0,"
                " updated_at INTEGER NOT NULL DEFAULT 0)"
            )
            conn.execute(
                "CREATE TABLE turns ("
                " conversation_id TEXT NOT NULL REFERENCES conversations(id),"
                " ordinal INTEGER NOT NULL,"
                " prompt TEXT NOT NULL,"
                " state TEXT NOT NULL DEFAULT 'working',"
                " reply TEXT,"
                " skill TEXT NOT NULL DEFAULT 'grilling',"
                " priority TEXT NOT NULL DEFAULT 'interactive',"
                " started_at INTEGER NOT NULL DEFAULT 0,"
                " replied_at INTEGER NOT NULL DEFAULT 0,"
                " park_reason TEXT,"
                " PRIMARY KEY (conversation_id, ordinal))"
            )
            # The idempotency ledger: one row per command key, carrying the terminal outcome so a
            # retried command replays it (ADR 0033).
            conn.execute(
                "CREATE TABLE commands ("
                " key TEXT PRIMARY KEY,"
                " outcome TEXT NOT NULL)"
            )
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            conn.execute("COMMIT")
        except sqlite3.DatabaseError:
            conn.execute("ROLLBACK")
            raise

    def close(self) -> None:
        self._conn.close()

    # --- commands -----------------------------------------------------------------------

    def open_conversation(self, *, title: str, idempotency_key: str, scope: str | None = None,
                          verb: str = "ask", conversation_id: str | None = None,
                          now: int = 0) -> CommandOutcome:
        """Open a new Conversation (an Ask). Idempotent on ``idempotency_key``: a repeat returns
        the same outcome and never creates a second Conversation."""
        with self._lock:
            return self._commit(lambda: self._open(
                title, scope, verb, conversation_id, now), idempotency_key)

    def start_turn(self, conversation_id: str, prompt: str, *, expected_revision: int,
                   idempotency_key: str, skill: str = "grilling",
                   priority: str = "interactive", now: int = 0) -> CommandOutcome:
        """Record the operator's message as the next immutable turn and claim the aggregate.

        The turn identity is ``(repo, conversation_id, ordinal)`` and submission is idempotent on
        the command key — a repeat never appends a duplicate turn. A stale ``expected_revision`` is
        rejected (the aggregate moved under the caller), and that rejection is itself terminal for
        the key, so a retry of the same stale command replays the same rejection (ADR 0034)."""
        with self._lock:
            return self._commit(lambda: self._start_turn(
                conversation_id, prompt, expected_revision, skill, priority, now), idempotency_key)

    def complete_turn(self, conversation_id: str, ordinal: int, reply: str, *,
                      now: int = 0) -> CommandOutcome:
        """Append the session's reply to a working turn — the single place the workspace adopts an
        accepted turn (ADR 0034). Idempotent per ``(conversation_id, ordinal)``: once a turn is
        replied, a second completion is a no-op that returns the existing reply, so one
        ``(repository, Conversation ID, turn ordinal)`` can never carry two replies."""
        with self._lock:
            return self._commit(lambda: self._complete_turn(
                conversation_id, ordinal, reply, now), None)

    def park_turn(self, conversation_id: str, ordinal: int, *, reason: str,
                  now: int = 0) -> CommandOutcome:
        """Park a working turn "needs you" on budget exhaustion, preserving the operator's message
        (it is already the immutable prompt). Idempotent and durable; nothing auto-expires."""
        with self._lock:
            return self._commit(lambda: self._park_turn(
                conversation_id, ordinal, reason, now), None)

    # --- reads (for the daemon projection builder) --------------------------------------

    def conversation(self, conversation_id: str) -> Conversation | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, repo, title, verb, scope, state, revision, created_at, updated_at"
                " FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
            if row is None:
                return None
            convo = self._conversation_from_row(row)
            convo.turns = self._turns_of(conversation_id)
            return convo

    def conversations(self) -> list[Conversation]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, repo, title, verb, scope, state, revision, created_at, updated_at"
                " FROM conversations ORDER BY updated_at DESC, id").fetchall()
            convos = [self._conversation_from_row(r) for r in rows]
            for convo in convos:
                convo.turns = self._turns_of(convo.id)
            return convos

    # --- internal: transaction wrapper --------------------------------------------------

    def _commit(self, operation, idempotency_key: str | None) -> CommandOutcome:
        """Run one command under ``BEGIN IMMEDIATE`` with idempotency replay. A key already in the
        ledger replays its terminal outcome without re-running ``operation``; otherwise the
        operation's outcome is recorded under the key before commit."""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            if idempotency_key is not None:
                row = self._conn.execute(
                    "SELECT outcome FROM commands WHERE key = ?", (idempotency_key,)).fetchone()
                if row is not None:
                    self._conn.execute("ROLLBACK")
                    return _decode_outcome(row[0])
            outcome = operation()
            if idempotency_key is not None:
                self._conn.execute(
                    "INSERT INTO commands (key, outcome) VALUES (?, ?)",
                    (idempotency_key, _encode_outcome(outcome)))
            self._conn.execute("COMMIT")
            return outcome
        except sqlite3.DatabaseError as e:
            self._rollback_quietly()
            raise WorkspaceUnavailable(f"cannot apply workspace command: {e}") from e
        except BaseException:
            self._rollback_quietly()
            raise

    # --- internal: operations (run inside a transaction) --------------------------------

    def _open(self, title, scope, verb, conversation_id, now) -> CommandOutcome:
        cid = conversation_id or uuid4().hex
        exists = self._conn.execute(
            "SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone()
        if exists is not None:
            # A caller-supplied id that already exists is not an error under a fresh key: return
            # the current aggregate so open stays idempotent even without the command ledger.
            rev = self._conn.execute(
                "SELECT revision FROM conversations WHERE id = ?", (cid,)).fetchone()[0]
            return CommandOutcome(ACCEPTED, cid, None, rev)
        self._conn.execute(
            "INSERT INTO conversations (id, repo, title, verb, scope, state, revision,"
            " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'active', 0, ?, ?)",
            (cid, self.repo, title, verb, scope, now, now))
        return CommandOutcome(ACCEPTED, cid, None, 0)

    def _start_turn(self, conversation_id, prompt, expected_revision, skill, priority,
                    now) -> CommandOutcome:
        row = self._conn.execute(
            "SELECT revision FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if row is None:
            return CommandOutcome(REJECTED, conversation_id, error="unknown conversation")
        revision = row[0]
        if revision != expected_revision:
            return CommandOutcome(REJECTED, conversation_id, revision=revision,
                                  error="stale revision")
        ordinal_row = self._conn.execute(
            "SELECT COALESCE(MAX(ordinal) + 1, 0) FROM turns WHERE conversation_id = ?",
            (conversation_id,)).fetchone()
        ordinal = ordinal_row[0]
        self._conn.execute(
            "INSERT INTO turns (conversation_id, ordinal, prompt, state, skill, priority,"
            " started_at) VALUES (?, ?, ?, 'working', ?, ?, ?)",
            (conversation_id, ordinal, prompt, skill, priority, now))
        new_revision = revision + 1
        self._conn.execute(
            "UPDATE conversations SET revision = ?, state = 'active', updated_at = ? WHERE id = ?",
            (new_revision, now, conversation_id))
        return CommandOutcome(ACCEPTED, conversation_id, ordinal, new_revision)

    def _complete_turn(self, conversation_id, ordinal, reply, now) -> CommandOutcome:
        row = self._conn.execute(
            "SELECT t.state FROM turns t WHERE t.conversation_id = ? AND t.ordinal = ?",
            (conversation_id, ordinal)).fetchone()
        if row is None:
            return CommandOutcome(REJECTED, conversation_id, ordinal, error="unknown turn")
        state = row[0]
        if state == REPLIED:
            # Already adopted — replay the existing outcome; never overwrite the recorded reply.
            rev = self._conn.execute(
                "SELECT revision FROM conversations WHERE id = ?", (conversation_id,)).fetchone()[0]
            return CommandOutcome(ACCEPTED, conversation_id, ordinal, rev)
        self._conn.execute(
            "UPDATE turns SET state = 'replied', reply = ?, replied_at = ?, park_reason = NULL"
            " WHERE conversation_id = ? AND ordinal = ?", (reply, now, conversation_id, ordinal))
        # Restore the aggregate: adopting the reply ends any park, so a resumed conversation is no
        # longer reported as parked (the projection reads conversations.state). No-op if active.
        self._conn.execute(
            "UPDATE conversations SET state = 'active' WHERE id = ?", (conversation_id,))
        new_revision = self._bump(conversation_id, now)
        return CommandOutcome(ACCEPTED, conversation_id, ordinal, new_revision)

    def _park_turn(self, conversation_id, ordinal, reason, now) -> CommandOutcome:
        row = self._conn.execute(
            "SELECT state FROM turns WHERE conversation_id = ? AND ordinal = ?",
            (conversation_id, ordinal)).fetchone()
        if row is None:
            return CommandOutcome(REJECTED, conversation_id, ordinal, error="unknown turn")
        if row[0] == REPLIED:
            rev = self._conn.execute(
                "SELECT revision FROM conversations WHERE id = ?", (conversation_id,)).fetchone()[0]
            return CommandOutcome(ACCEPTED, conversation_id, ordinal, rev)
        self._conn.execute(
            "UPDATE turns SET state = 'paused', park_reason = ? WHERE conversation_id = ?"
            " AND ordinal = ?", (reason, conversation_id, ordinal))
        self._conn.execute(
            "UPDATE conversations SET state = 'parked', updated_at = ? WHERE id = ?",
            (now, conversation_id))
        new_revision = self._bump(conversation_id, now)
        return CommandOutcome(ACCEPTED, conversation_id, ordinal, new_revision)

    def _bump(self, conversation_id, now) -> int:
        self._conn.execute(
            "UPDATE conversations SET revision = revision + 1, updated_at = ? WHERE id = ?",
            (now, conversation_id))
        return self._conn.execute(
            "SELECT revision FROM conversations WHERE id = ?", (conversation_id,)).fetchone()[0]

    # --- internal: row mapping ----------------------------------------------------------

    @staticmethod
    def _conversation_from_row(row) -> Conversation:
        return Conversation(id=row[0], repo=row[1], title=row[2], verb=row[3], scope=row[4],
                            state=row[5], revision=row[6], created_at=row[7], updated_at=row[8])

    def _turns_of(self, conversation_id: str) -> list[Turn]:
        rows = self._conn.execute(
            "SELECT conversation_id, ordinal, prompt, state, reply, skill, priority,"
            " started_at, replied_at, park_reason FROM turns WHERE conversation_id = ?"
            " ORDER BY ordinal", (conversation_id,)).fetchall()
        return [Turn(conversation_id=r[0], ordinal=r[1], prompt=r[2], state=r[3], reply=r[4],
                     skill=r[5], priority=r[6], started_at=r[7], replied_at=r[8], park_reason=r[9])
                for r in rows]

    def _rollback_quietly(self) -> None:
        try:
            self._conn.execute("ROLLBACK")
        except sqlite3.DatabaseError:
            pass


def _encode_outcome(outcome: CommandOutcome) -> str:
    return json.dumps({
        "status": outcome.status, "conversation_id": outcome.conversation_id,
        "ordinal": outcome.ordinal, "revision": outcome.revision, "error": outcome.error})


def _decode_outcome(payload: str) -> CommandOutcome:
    data = json.loads(payload)
    return CommandOutcome(status=data["status"], conversation_id=data.get("conversation_id"),
                          ordinal=data.get("ordinal"), revision=data.get("revision"),
                          error=data.get("error"))
