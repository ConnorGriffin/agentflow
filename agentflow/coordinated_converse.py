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
import re
import time
from pathlib import Path

from agentflow import live
from agentflow.coordinator import Submission
from agentflow.workspace import channel, publish
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

# When the operator asks for a build-issue write-up, the turn produces a *complete* draft itself
# (decide-then-review, ADR 0034) — no interrogation, one input surface. The session writes the
# structured proposal draft alongside its human reply; the daemon-side finalizer adopts that draft
# into an immutable Proposal version. The session has no durable write path beyond its worktree, so
# staging is daemon-only.
BUILD_ISSUE_PROMPT = """\
You are answering one turn of an Ask conversation about the repository {repo}.

This is turn {ordinal} of conversation {conversation_id}. The operator asked:

{prompt}

They want you to write this up as a *complete* build issue for {repo}. Produce the whole draft
yourself from the repository and the conversation — do not ask the operator questions. Write TWO
files in your worktree, creating parent directories as needed:

1. Your human-readable reply (a short note that you drafted the build issue and what it covers):

    {reply_path}

2. The structured proposal draft as JSON with exactly these keys — "title" (string), "summary"
   (string, the problem/outcome in prose), "acceptance" (array of strings, each a checkable
   criterion), and "body" (string, any extra detail or empty):

    {proposal_path}

Do not open a pull request, push a branch, edit GitHub, apply any label, or change any durable
project state — this is a conversation, not a build. Writing those two files is the sole durable
outcome of this turn; the daemon adopts the draft into an immutable, content-hashed proposal you
can then review and approve. If you exit without writing them, the turn is incomplete and will run
again — never write them twice.
"""

# Build-issue intent is detected from the operator's own words (one input surface, no draft
# button). "write this up as a build issue", "draft a build-issue", etc. all trip it.
_BUILD_ISSUE_INTENT = re.compile(r"build[\s-]?issue", re.IGNORECASE)


def wants_build_issue(prompt: str) -> bool:
    """Whether an operator message asks to stage a Build-Issue Proposal from the conversation."""
    return bool(_BUILD_ISSUE_INTENT.search(prompt or ""))


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


def proposal_path(record) -> str:
    """The per-turn structured proposal-draft artifact inside the turn's worktree. Present only for
    a build-issue turn; per-*ordinal* so an earlier draft cannot be adopted for a later turn."""
    return os.path.join(record.source or "", ".agentflow", f"ask-proposal-{record.target}.json")


def read_proposal_draft(record) -> dict | None:
    """The structured build-issue draft the session wrote for this turn, normalized, or ``None`` if
    absent/invalid. The daemon — not the session — computes the content hash from this, so the
    provenance key is trustworthy (ADR 0034)."""
    try:
        raw = Path(proposal_path(record)).read_text()
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip()
    if not title:
        return None
    acceptance = data.get("acceptance") or []
    if not isinstance(acceptance, list):
        acceptance = [str(acceptance)]
    return {
        "title": title,
        "summary": str(data.get("summary") or "").strip(),
        "acceptance": [str(a).strip() for a in acceptance if str(a).strip()],
        "body": str(data.get("body") or "").strip(),
    }


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
    single writer of the reply turn (ADR 0034). When the turn produced a structured build-issue
    draft, also adopt it as a **new immutable, content-hashed Proposal version** in the same
    finalize; this is the sole promotion of a session draft into staged truth (ADR 0034). Returns a
    durable proof, or ``None`` to retry next cycle if the reply or a present draft could not be
    durably appended (never retire over a turn that was not adopted)."""
    reply = read_reply(record)
    if reply is None:
        return None
    now = int(time.time())
    store = WorkspaceStore(record.repo)
    try:
        outcome = store.complete_turn(record.subject, int(record.target), reply, now=now)
        if not outcome.accepted:
            return None
        draft = read_proposal_draft(record)
        if draft is not None:
            digest = publish.content_hash(draft["title"], draft["summary"],
                                          draft["acceptance"], draft["body"])
            staged = store.stage_proposal(
                record.subject, title=draft["title"], summary=draft["summary"],
                acceptance=draft["acceptance"], body=draft["body"], content_hash=digest,
                idempotency_key=f"stage:{record.subject}:{record.target}", now=now)
            if not staged.accepted:
                return None
            return f"workspace:{record.subject}#{record.target}:staged:{digest[:15]}"
    finally:
        store.close()
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
    reply_at = os.path.join(worktree, ".agentflow", f"ask-reply-{ordinal}.md")
    if wants_build_issue(prompt):
        input_ptr = BUILD_ISSUE_PROMPT.format(
            repo=repo, ordinal=ordinal, conversation_id=conversation_id, prompt=prompt,
            reply_path=reply_at,
            proposal_path=os.path.join(worktree, ".agentflow", f"ask-proposal-{ordinal}.json"))
    else:
        input_ptr = ASK_PROMPT.format(
            repo=repo, ordinal=ordinal, conversation_id=conversation_id, prompt=prompt,
            reply_path=reply_at)
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
        elif kind == "approve_proposal":
            # Hash-bound approval: the operator approves the exact version they saw, not "latest"
            # (ADR 0034). No coordinated turn — publication reconciles separately (ADR 0033).
            outcome = store.approve_proposal(command["conversation_id"], command["content_hash"],
                                             idempotency_key=key, now=now)
            return {"status": outcome.status, "conversation_id": outcome.conversation_id,
                    "content_hash": outcome.content_hash, "version": outcome.version,
                    "error": outcome.error}
        elif kind == "discard_proposal":
            outcome = store.discard_proposal(command["conversation_id"], idempotency_key=key,
                                             now=now)
            return {"status": outcome.status, "conversation_id": outcome.conversation_id,
                    "error": outcome.error}
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
            proposals = store.proposals()
        finally:
            store.close()
        projects.append({
            "id": project_slug(cfg.repo), "repo": cfg.repo,
            "profile": getattr(cfg, "profile", ""), "conversations": convos,
            "proposals": proposals})
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
