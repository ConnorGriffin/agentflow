"""The Conversation-turn stage wired into the daemon (ADR 0033/0034).

An Ask holds a multi-turn conversation, and each operator message runs as one bounded coordinated
turn through the *existing* session coordinator — a ``converse`` logical stage, sibling of the six
pipeline stages (ADR 0034). This module is the daemon-side glue, mirroring
:mod:`agentflow.coordinated_build`:

- **submission mapping** — one operator message → one ``converse`` :class:`Submission` with identity
  ``(repository, Conversation ID, turn ordinal)``, marked *interactive* so it outranks background
  pipeline work at admission (ADR 0034).
- **stage collaborators** — the reply-artifact ``verify``, the workspace ``adopt`` (the single
  place an accepted turn becomes a durable immutable turn), and the "needs you" ``park``. These are
  the production wiring the daemon injects into :class:`ConverseStageAdapter`.
- **command application** — draining the local command channel, applying each command to the
  workspace store, and submitting the coordinated turn. The web layer only transports commands;
  the daemon is the only writer (ADR 0033).
- **projection** — building and publishing the bounded workspace read model the console serves.

The methodology session writes only into its isolated worktree — a per-turn reply artifact. It
never writes the workspace, GitHub, coordinator records, or projections; only the daemon-side
finalizer adopts the reply into the durable Conversation (ADR 0033/0034).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from agentflow import live
from agentflow.coordinator import Submission
from agentflow.workspace import channel
from agentflow.workspace.projection import workspace_projection
from agentflow.workspace.store import ACCEPTED, WorkspaceStore, project_slug

# The prompt a Conversation turn runs. The turn's isolated worktree is the session's only durable
# write path; it must land its answer at exactly this per-turn artifact, which is the outcome the
# stage adapter verifies and the finalizer adopts (ADR 0034 outcome-first).
ASK_PROMPT = """\
You are answering one turn of an Ask conversation about the repository {repo}.

This is turn {ordinal} of conversation {conversation_id}. The operator asked:

{prompt}

Answer the question about the repository. Do not open a pull request, push a branch, edit
GitHub, or change any durable project state — this is a conversation, not a build. When you are
done, write your complete reply (and only your reply) to this file, creating parent directories
as needed:

    {reply_path}

Writing that file is the sole durable outcome of this turn. If you exit without writing it, the
turn is incomplete and will run again — never write it twice.
"""

WORKSPACE_PROJECTION_FILE = "workspace.json"


# --- paths / artifacts ------------------------------------------------------------------

def _short(cid: str) -> str:
    return cid.replace("-", "")[:8]


def ask_worktree(workdir: str, pool: str, conversation_id: str) -> str:
    """The isolated worktree one conversation's turns reuse across attempts (resume context)."""
    return os.path.join(workdir, ".agentflow", "worktrees", pool,
                        f"ask-{_short(conversation_id)}")


def reply_path(record) -> str:
    """The per-turn reply artifact path inside the turn's worktree. Per-*ordinal* so a reply left
    by an earlier turn on the reused worktree can never falsely complete a later turn."""
    return os.path.join(record.source or "", ".agentflow", f"ask-reply-{record.target}.md")


def read_reply(record) -> str | None:
    """The durable reply the session wrote for this turn, or ``None`` if it is absent/empty."""
    try:
        text = Path(reply_path(record)).read_text().strip()
    except OSError:
        return None
    return text or None


# --- stage collaborators (injected into ConverseStageAdapter) ---------------------------

def _reply_ready(record, obs) -> bool:
    """The Converse outcome is a durable reply for this exact turn (ADR 0034 outcome-first).
    Independent of provider exit: a bad exit that still wrote the reply completes; a clean exit
    that wrote nothing does not, and the turn continues within budget."""
    return read_reply(record) is not None


def _ask_worktree_ready(record) -> bool:
    """Provision the turn's isolated worktree before admission (ADR 0030/0034): a detached checkout
    of ``origin/main`` the bounded session reads to answer, and into which it writes its reply.

    An existing worktree is reused *exactly as it is* — a resumed turn keeps the partial reply it
    already wrote in the worktree, so it is never reset or cleaned (that is why this cannot reuse
    the review stage's freshening ``prepare_worktree_detached``). An Ask owns no branch and pushes
    nothing, so the checkout is detached. Any git failure returns False, so admission is skipped
    with no permit and no attempt consumed — the turn simply retries next cycle."""
    from agentflow.loop import _run
    from agentflow.runner import _worktree_is_registered
    src = record.source or ""
    if "/.agentflow/worktrees/" not in src:
        return False
    workdir, tail = src.split("/.agentflow/worktrees/", 1)
    if not tail.startswith(f"{record.pool}/ask-"):
        return False
    wt = Path(src)
    if wt.exists():
        return _worktree_is_registered(workdir, wt)  # reuse as-is; never rebuild a resumed turn
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    return _run(["git", "-C", workdir, "worktree", "add", "--detach", str(wt),
                 "origin/main"]).returncode == 0


def _adopt_turn(record) -> str | None:
    """Adopt the accepted turn: append its immutable reply to the daemon-owned workspace — the
    single writer of the reply turn (ADR 0034). Returns a durable proof, or ``None`` to retry next
    cycle if the reply cannot be read (never retire over a turn that was not durably appended)."""
    reply = read_reply(record)
    if reply is None:
        return None
    store = WorkspaceStore(record.repo)
    try:
        outcome = store.complete_turn(record.subject, int(record.target), reply,
                                      now=int(time.time()))
    finally:
        store.close()
    if not outcome.accepted:
        return None
    return f"workspace:{record.subject}#{record.target}:adopted"


def _park_ask(record) -> str | None:
    """Park the conversation "needs you" on budget exhaustion, preserving the operator's message
    (already the turn's immutable prompt). Durable and idempotent; nothing auto-expires."""
    store = WorkspaceStore(record.repo)
    try:
        outcome = store.park_turn(record.subject, int(record.target),
                                  reason="turn exhausted its budget — needs you",
                                  now=int(time.time()))
    finally:
        store.close()
    if not outcome.accepted:
        return None
    return f"workspace:{record.subject}#{record.target}:parked"


# --- submission mapping (pure) ----------------------------------------------------------

def converse_submission(repo: str, workdir: str, conversation_id: str, ordinal: int,
                        prompt: str, *, pool: str = "claude"):
    """One operator message → one ``converse`` stage submission. The stable identity is
    ``(repo, conversation_id, converse, ordinal)`` so re-submitting the same turn is idempotent.
    Marked interactive: an operator is present and waiting, so this turn outranks background
    pipeline work at admission (ADR 0034)."""
    worktree = ask_worktree(workdir, pool, conversation_id)
    input_ptr = ASK_PROMPT.format(
        repo=repo, ordinal=ordinal, conversation_id=conversation_id, prompt=prompt,
        reply_path=os.path.join(worktree, ".agentflow", f"ask-reply-{ordinal}.md"))
    return Submission(
        repo=repo, subject=conversation_id, stage="converse", pool=pool, complexity="deep",
        target=str(ordinal), source=worktree, input_ptr=input_ptr, claim=True, interactive=True)


# --- command application (daemon-side; the only writer, ADR 0033) -----------------------

def apply_command(command: dict, coordinator, *, workdir: str,
                  store_factory=WorkspaceStore, now=None) -> dict:
    """Apply one transported command to the workspace store and submit its coordinated turn.

    Returns a small result dict (status + conversation/ordinal/revision). Idempotent throughout:
    the store command ledger replays a repeated key, and ``submit_stage`` is idempotent on the
    turn identity, so a re-drained command never appends a duplicate turn or launches a second
    session (ADR 0034 anti-duplication).
    """
    now = int(time.time()) if now is None else now
    repo = command["repo"]
    kind = command["kind"]
    key = command["key"]
    store = store_factory(repo)
    try:
        if kind == "open_ask":
            cid = command["conversation_id"]
            store.open_conversation(title=command.get("title", ""), conversation_id=cid,
                                    scope=command.get("scope"), idempotency_key=f"{key}:open",
                                    now=now)
            outcome = store.start_turn(cid, command["prompt"], expected_revision=0,
                                       idempotency_key=f"{key}:t0", now=now)
        elif kind == "send_turn":
            cid = command["conversation_id"]
            outcome = store.start_turn(cid, command["prompt"],
                                       expected_revision=command["expected_revision"],
                                       idempotency_key=key, now=now)
        else:
            return {"status": "rejected", "error": f"unknown command kind {kind!r}"}
    finally:
        store.close()

    if outcome.status != ACCEPTED or outcome.ordinal is None:
        return {"status": outcome.status, "conversation_id": outcome.conversation_id,
                "revision": outcome.revision, "error": outcome.error}
    coordinator.submit_stage(converse_submission(
        repo, workdir, outcome.conversation_id, outcome.ordinal, command["prompt"]))
    return {"status": ACCEPTED, "conversation_id": outcome.conversation_id,
            "ordinal": outcome.ordinal, "revision": outcome.revision}


def drain_commands(coordinator, workdir_for, *, _log=None) -> list[dict]:
    """Drain every spooled command through :func:`apply_command`, acknowledging each. The daemon
    is the only interpreter of commands (ADR 0033). ``workdir_for`` maps a command's repository to
    its checkout; a command for an unknown repository is dropped."""
    resolve = workdir_for.get if isinstance(workdir_for, dict) else workdir_for
    results = []
    for command in channel.pending():
        workdir = resolve(command.get("repo"))
        if workdir is None:
            channel.ack(command["key"])  # not an enrolled Project — nothing to apply
            continue
        try:
            results.append(apply_command(command, coordinator, workdir=workdir))
        except Exception as e:  # noqa: BLE001 — one bad command must not stall the drain
            if _log:
                _log(f"workspace command error: {type(e).__name__}: {e}")
            continue
        channel.ack(command["key"])
    return results


# --- projection (daemon-published read model, ADR 0033) ---------------------------------

def build_projection(repos: list, *, store_factory=WorkspaceStore, now=None,
                     daemon_available: bool = True) -> dict:
    """Assemble the bounded workspace projection over the enrolled repos. Each Project is one
    enrolled repository; only agentflow's own repo is enrolled for this tracer (the fleet-home
    switcher is a stub)."""
    now = int(time.time()) if now is None else now
    projects = []
    for cfg in repos:
        store = store_factory(cfg.repo)
        try:
            convos = store.conversations()
        finally:
            store.close()
        projects.append({
            "id": project_slug(cfg.repo), "repo": cfg.repo,
            "profile": getattr(cfg, "profile", ""), "conversations": convos})
    return workspace_projection(projects, read_model_at=now, revision=now,
                                daemon_available=daemon_available)


def publish_projection(repos: list, *, store_factory=WorkspaceStore, now=None) -> None:
    """Publish the workspace projection atomically for the console (ADR 0033), alongside the fleet
    snapshot. A generation is current only when the whole read is durable."""
    projection = build_projection(repos, store_factory=store_factory, now=now)
    live._write_atomic(live.STATE_DIR / WORKSPACE_PROJECTION_FILE, projection)


def read_projection():
    """The last daemon-published workspace projection, or ``None`` when none exists. The web
    layer's file-only reader (ADR 0026/0033)."""
    data = live._read(live.STATE_DIR / WORKSPACE_PROJECTION_FILE, None)
    return data if isinstance(data, dict) else None
