"""The Respond stage behind the durable session coordinator (issue #107).

One unanswered maintainer comment on an existing agentflow PR becomes one durable ``respond``
submission, bound to that comment as its immutable target and holding the ``building`` change
claim while it answers. This module is the daemon-side glue, mirroring its stage siblings:

- **submission mapping** — one maintainer comment → one ``respond`` :class:`Submission` on the
  change's original tool lineage and its retained PR branch/worktree.
- **stage collaborators** — the marked-reply-plus-verified-push outcome, the settlement that
  releases the change claim with no successor, and the target-scoped park. These are the
  production wiring :mod:`agentflow.pipeline` injects into :class:`RespondStageAdapter`; the
  branch/worktree preparation Respond shares with Build, Revise and Mockup is
  :mod:`agentflow.stage_worktree`, and the PR park scaffolding is :mod:`agentflow.pr_park`.

The mapping is pure and exercised directly; the live GitHub/git reads are exercised through the
adapter seam (ADR 0020).
"""

from __future__ import annotations

import re

from agentflow import github
from agentflow.labels import BUILDING
from agentflow.pr_park import park_pr_number
from agentflow.prompts import RESPOND_PROMPT
from agentflow.runner import _run
from agentflow.worktree_ref import BUILD_BRANCH_RE, WorktreeRef, source_facts


def respond_submission(cfg, pr_number, branch, comment, target, baseline):
    """Translate one unanswered maintainer comment on an existing agentflow PR into a single
    Respond stage submission — the minimal facts the coordinator needs (ADR 0030). Respond adopts
    the change's *original tool lineage* and its retained PR branch/worktree — both recovered from
    the branch name (``agentflow/<tool>/issue-<n>-<slug>``), so capacity on the other pool can never
    silently switch this code-writing continuation — is bound to the maintainer comment it answers
    (its immutable ``target``, so a later comment opens a genuinely new Respond with a fresh budget),
    and holds the ``building`` change claim while it waits. Pure: the mapping is the test surface
    (ADR 0020). The durable prompt carries the PR head observed before Respond so completion can
    verify a requested push actually advanced that baseline. Returns ``None`` when the branch is
    not an agentflow PR branch or either immutable target is missing."""
    from agentflow.coordinator import Submission
    from agentflow.gate import respond_reply_disclaimer
    m = BUILD_BRANCH_RE.match(branch or "")
    if m is None or not target or not baseline:
        return None
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    brief = RESPOND_PROMPT.format(
        n=pr_number, comment=comment, baseline=baseline,
        disclaimer=respond_reply_disclaimer(str(target)))
    return Submission(
        repo=cfg.repo, subject=str(n), stage="respond", target=str(target),
        pool=tool, complexity="deep", source=WorktreeRef.for_build(cfg.workdir, tool, n, sl).path,
        claim=True, input_ptr=brief, builder_lineage=tool)


# --- Respond stage: posted-reply outcome on the retained PR branch (live; ADR 0020) ------
def _untracked_respond_scratch(path: str) -> bool:
    """Classify conventional temporary artifacts that are outside PR state.

    This is path semantics, not a one-filename exception: nested screenshot runners, test reports,
    cache/temp directories, editor swap files, logs, and trace artifacts are disposable local
    proof machinery. Unknown untracked files remain relevant because they may be requested source.
    """
    normalized = path.strip().strip('"')
    parts = tuple(part for part in normalized.split("/") if part)
    if not parts:
        return False
    basename = parts[-1].lower()
    temporary_dirs = {
        "tmp", "temp", ".tmp", ".cache", "test-results", "playwright-report",
        "coverage", ".coverage", "__pycache__",
    }
    return (
        any(part.lower() in temporary_dirs for part in parts[:-1])
        or re.fullmatch(r"run-shot(?:-[^/]*)?\.(?:sh|js|mjs|json)", basename) is not None
        or basename in {".ds_store", "thumbs.db"}
        or basename.endswith((".tmp", ".temp", ".log", ".trace", ".swp", ".swo"))
    )


def _reply_ready(record, obs) -> bool:
    """The Respond outcome is the marked agentflow reply to the maintainer comment this record
    answers, plus any branch change verified pushed (ADR 0028, issue #107) — read from GitHub
    independently of how the responder exited:

    - the reply: a marked agentflow comment names this record's immutable maintainer-comment
      target, so another reply or generic agentflow comment cannot satisfy it; and
    - verified pushed: when the reply marks a change, its SHA must be the current pushed head (not the
      baseline — a no-op push cannot count), and the retained PR-branch worktree holds no commit
      absent from that head *and* no uncommitted change at all. A responder that committed a small fix
      but never pushed it left the remote branch unchanged; one that edited a file but never committed
      it (a modified tracked file, a staged change, or an untracked new file) never turned that change
      into a pushed commit either. Both leave the stage incomplete so it continues on that same
      retained worktree. History rewritten by a rebase (the fix a conflict demands) is accepted: the
      marked head plus the clean owned worktree sitting on it prove the reviser's work either way, so
      the baseline need not be an ancestor of the pushed head.

    A record without that exact targeted reply stays incomplete. Live orchestration; exercised
    through the Coordinator/Respond adapter seam in ``tests/test_respond_tracer.py``."""
    from agentflow.gate import respond_reply_change, respond_reply_posted
    parsed = source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, wt = parsed
    pr = github.open_pr_for_branch(record.repo, branch)
    if pr is None:
        return False
    comments = github.pr_comment_rows(record.repo, pr.number)
    if comments is None or not respond_reply_posted(comments, record.target or ""):
        return False   # no durable reply bound to this record's maintainer-comment target
    change = respond_reply_change(comments, record.target or "")
    baseline_match = re.search(r"agentflow-respond-baseline:([^\s>]+)", record.input_ptr or "")
    baseline = baseline_match.group(1) if baseline_match is not None else ""
    if not change or not baseline:
        return False
    # A reply exists. The owned worktree is mandatory evidence: without it there is no way to
    # prove that a requested branch change was either pushed or never left locally. Fail closed and
    # let preparation recover the PR branch before another attempt.
    if not wt.exists():
        return False
    head = pr.head_ref_oid or ""
    if change != "none" and (change == baseline or change != head):
        return False
    fetched = _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", branch])
    if fetched.returncode != 0:
        return False
    ahead = _run(["git", "-C", str(wt), "rev-list", "--count", f"{head}..HEAD"])
    if not head or ahead.returncode != 0 or ahead.stdout.strip() not in ("", "0"):
        return False
    # Tracked/staged changes and arbitrary untracked files are relevant retained branch work and
    # still block. Ignore only the known root-level screenshot runner regression: broader treatment
    # of untracked files as scratch could retire a requested new source file before it is pushed.
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if status.returncode != 0:
        return False
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        if not line.startswith("?? ") or not _untracked_respond_scratch(line[3:]):
            return False
    return True


def _settle_respond(record) -> str | None:
    """Release Respond's change claim once the reply is durable, retiring the record with no
    successor and no human handoff (issue #107). Drops the ``building`` claim label so the answered
    PR returns to the normal merge pipeline, proves the label is gone, then returns the PR (or issue)
    URL as the durable proof. Idempotent and crash-safe: removing an already-removed label is a
    no-op, so a repeat re-proves the same release. Returns ``None`` when the issue is unreadable or
    the label is still present, so settlement retries next cycle rather than retiring over a claim it
    never released. Live orchestration, not unit-tested (ADR 0020)."""
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    github.remove_label(record.repo, number, BUILDING)
    # An unreadable proof read means retry rather than retire over a live claim.
    settlement = github.issue_settlement(record.repo, number)
    if settlement is None:
        return None
    if BUILDING in settlement.labels:
        return None   # the claim label is still present — retry rather than retire over it
    pr = park_pr_number(record)
    if pr is not None:
        return f"https://github.com/{record.repo}/pull/{pr}"
    return settlement.url or f"https://github.com/{record.repo}/issues/{number}"


def _park_respond(record) -> str | None:
    """Create Respond's record-specific park proof and idempotent phone notification.

    A generic Review park may already be present on the PR, so this uses its own target-scoped
    marker to prove that this exact maintainer-comment target exhausted its Respond budget. The
    crash-safe post-once → prove → notify-once recipe is the shared :class:`DurableHandoff` envelope
    (ADR 0042): it derives the stable ntfy sequence id, so a replay across the window between posting
    the durable comment and recording completion locally replaces the same notification rather than
    multiplying it.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject

    pr = park_pr_number(record)
    if pr is None or not record.target:
        return None
    proof = f"<!-- agentflow-respond-park-target:{record.target} -->"
    body = ("> *agentflow: parked for human review (Respond).*\n"
            f"{proof}\n\n"
            "## Maintainer decision needed\n\n"
            f"Affected behavior: the requested application change in maintainer comment "
            f"`{record.target}` was not fully answered and proved.\n\n"
            "Options:\n"
            "- Restate the requested behavior and resume this retained Respond stage.\n"
            "- Withdraw the request and keep the PR's current behavior.\n\n"
            "Consequences: resuming may change the PR; withdrawing leaves its current pushed "
            "behavior unchanged.\n\n"
            "Recommendation: restate the exact desired behavior, then resume this same target.\n\n"
            "## Agent handoff\n\n"
            f"Code locations: PR #{pr} at `{record.target}`; inspect the retained branch diff.\n\n"
            "Conflicting changes or unresolved facts: the targeted reply/pushed outcome was not "
            "proved before the continuation budget ended.\n\n"
            "Checks: no complete targeted reply + exact pushed-head proof was recorded.\n\n"
            f"Retained work: `{record.source}` at `{record.target}`.\n\n"
            "Exact next action: record the chosen behavior, then resume this Respond target on "
            "the same PR.")
    return DurableHandoff().hand_off(
        Subject(repo=record.repo, number=pr, kind="pr"),
        identity=record.identity, stage="respond",
        marker=proof,
        action=lambda: github.pr_comment(record.repo, pr, body),
        notification=Notification(
            "agentflow needs you",
            f"{record.repo} PR #{pr}: Respond parked for maintainer comment {record.target}"))
