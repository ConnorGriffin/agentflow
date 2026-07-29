"""The Mockup stage behind the durable session coordinator (issue #108).

One held ``needs-mockup`` issue becomes one durable ``mockup`` submission: a single bounded round
of visual variants pushed to its own branch and posted as one marked issue comment, ending at the
human-pick boundary. This module is the daemon-side glue, mirroring its stage siblings:

- **submission mapping** — one eligible held issue → one ``mockup`` :class:`Submission` whose
  pinned pool, owned branch/worktree and durable prompt reconstruct the same visual-design job
  across continuations.
- **stage collaborators** — the pushed-round outcome, the MISSING-CONTEXT human boundary, the
  visible drawing-claim check, the settlement that hands the issue back to the maintainer's
  choice, and the exhaustion handoff. These are the production wiring :mod:`agentflow.pipeline`
  injects into :class:`MockupStageAdapter`; the branch/worktree preparation Mockup shares with
  Build, Revise and Respond is :mod:`agentflow.stage_worktree`.

The mapping is pure and exercised directly; the live GitHub/git reads are exercised through the
adapter seam (ADR 0020).
"""

from __future__ import annotations

import hashlib

from agentflow import github, worktree_ref
from agentflow.labels import DRAWING, MOCKUP_MARK, mockup_scope_from_labels
from agentflow.prompts import MOCKUP_DISCLAIMER, PRODUCE_PROMPT, SCOPE_GUIDANCE
from agentflow.repo_facts import surface_declaration, surfaces_phrase
from agentflow.runner import _run, remove_worktree_if_safe
from agentflow.worktree_ref import WorktreeRef, source_facts


def mockup_submission(cfg, issue: dict, tool: str):
    """Translate one eligible held issue into its single durable Mockup variant round.

    The stable identity is ``(repo, issue, mockup)``: repeated discovery returns the same record,
    while the pinned pool and owned branch/worktree preserve tool lineage and local progress across
    fresh-session continuations. The durable prompt reconstructs the exact same visual-design job.
    """
    from agentflow.coordinator import Submission

    n = int(issue["number"])
    sl = worktree_ref.slug(issue.get("title", ""))
    ref = WorktreeRef.for_mockup(cfg.workdir, tool, n, sl)
    branch = ref.branch
    source = ref.path
    scope = mockup_scope_from_labels([lbl["name"] for lbl in issue.get("labels", [])])
    prompt = PRODUCE_PROMPT.format(
        repo=cfg.repo, n=n, title=issue.get("title", ""), body=issue.get("body") or "",
        branch=branch, surfaces=surfaces_phrase(surface_declaration(cfg.workdir)),
        scope_guidance=SCOPE_GUIDANCE[scope], disclaimer=MOCKUP_DISCLAIMER)
    return Submission(
        repo=cfg.repo, subject=str(n), stage="mockup", pool=tool, complexity="deep",
        source=source, claim=True, input_ptr=prompt, builder_lineage=tool)


def _mockup_outcome_ready(record, obs) -> bool:
    """Prove one pushed variant round: committed artifacts/screenshots and one marked comment.

    The worktree is continuation state, never outcome authority. Completion requires its clean
    head to equal the remote branch, at least three branch-only HTML variants and screenshots,
    and exactly one durable issue comment that embeds every committed screenshot. A
    MISSING-CONTEXT comment is a human hold, not a completed visual round.
    """

    parsed = source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, wt = parsed
    if not wt.exists():
        return False
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    marked = [comment for comment in (github.issue_comment_rows(record.repo, number) or [])
              if MOCKUP_MARK in comment.get("body", "")]
    if len(marked) != 1 or "MISSING-CONTEXT:" in marked[0].get("body", ""):
        return False
    fetched = _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", "main", branch])
    if fetched.returncode != 0:
        return False
    local = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    remote = _run(["git", "-C", str(wt), "rev-parse", f"origin/{branch}"])
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if (local.returncode != 0 or remote.returncode != 0 or status.returncode != 0
            or not local.stdout.strip() or local.stdout.strip() != remote.stdout.strip()
            or status.stdout.strip()):
        return False
    changed = _run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=ACMRT",
                    "origin/main...HEAD"])
    if changed.returncode != 0:
        return False
    paths = [path for path in changed.stdout.splitlines() if path.startswith("mockups/")]
    variants = [path for path in paths if path.lower().endswith((".html", ".htm"))]
    screenshots = [path for path in paths
                   if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    body = marked[0].get("body", "")
    return (len(variants) >= 3 and len(screenshots) >= 3
            and all(path in body for path in screenshots))


def _mockup_missing_context(record) -> bool:
    """Whether this issue carries Mockup's deliberate durable MISSING-CONTEXT boundary."""

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    return any(MOCKUP_MARK in comment.get("body", "")
               and "MISSING-CONTEXT:" in comment.get("body", "")
               for comment in (github.issue_comment_rows(record.repo, number) or []))


def _mockup_claim_ready(record) -> bool:
    """Prove Mockup's visible drawing claim immediately before admission."""

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    labels = github.issue_labels(record.repo, number)
    if labels is None:
        return False
    return DRAWING in labels and "agentflow:needs-mockup" in labels


def _settle_mockup(record) -> str | None:
    """Retire one completed visual round at the human-pick boundary.

    The durable comment and pushed artifacts were already verified by the adapter. Settlement
    removes and proves the drawing claim, keeps ``needs-mockup`` in place for the maintainer's
    choice, and disposes the clean pushed worktree before coordinator ownership disappears.
    Every step is idempotent; an unreadable label or stubborn worktree retries next cycle.
    """

    parsed = source_facts(record)
    if parsed is None:
        return None
    workdir, _branch, wt = parsed
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    github.remove_label(record.repo, number, DRAWING)
    # An unreadable proof read (None) retries next cycle.
    settlement = github.issue_settlement(record.repo, number)
    if settlement is None:
        return None
    if DRAWING in settlement.labels or "agentflow:needs-mockup" not in settlement.labels:
        return None
    if wt.exists() and not remove_worktree_if_safe(workdir, wt):
        return None
    if wt.exists():
        return None
    return settlement.url or f"https://github.com/{record.repo}/issues/{number}"


def _hold_mockup(record) -> str | None:
    """Create Mockup's one issue-native handoff while preserving unfinished local work.

    The per-record proof marker is what makes the hold durable, so the crash-safe post-once →
    prove → notify-once recipe is the shared :class:`~agentflow.handoff.DurableHandoff` envelope
    (ADR 0042) keyed on that marker: a repeat after a daemon crash observes it and neither
    re-posts nor pings again. The round only ever gets *one* marked comment — MISSING-CONTEXT
    already is the durable stage-native handoff and is never restated, so it gains the marker in
    place; an unfinished round gains the marker and the exhaustion explanation. Handing the issue
    back to the maintainer's choice (``needs-mockup`` kept, the drawing claim released) is stage
    bookkeeping that runs once the handoff confirms; the worktree is retained either way.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    comments = github.issue_comments(record.repo, number)
    if comments is None:
        return None
    marked = next((comment for comment in comments if MOCKUP_MARK in comment.body), None)
    missing = next((comment for comment in comments if MOCKUP_MARK in comment.body
                    and "MISSING-CONTEXT:" in comment.body), None)
    proof = "<!-- agentflow-mockup-hold:" + hashlib.sha256(
        record.identity.encode()).hexdigest()[:24] + " -->"
    explanation = ("Mockup exhausted its continuation budget before completing the visual round. "
                   "The branch and local worktree are retained for a human to continue.")

    def post() -> None:
        if marked is None:
            github.comment(record.repo, number,
                           f"{MOCKUP_DISCLAIMER}\n{proof}\n\n{explanation}")
            return
        # The round already has its one marked comment: a MISSING-CONTEXT handoff says why on its
        # own and gains only the marker; an unfinished one also gains the exhaustion explanation.
        tail = proof if missing is not None else f"{proof}\n\n{explanation}"
        github.edit_comment(marked.id, f"{marked.body.rstrip()}\n\n{tail}")

    reason = "missing context" if missing is not None else "continuation budget exhausted"
    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="mockup-hold",
        marker=proof,
        action=post,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: Mockup held — {reason}"))
    if url is None:
        return None
    # A single edit that both adds and removes a label is not on the typed surface, so the write
    # goes through the escape hatch; the label read below is authoritative either way.
    github.api(["issue", "edit", str(number), "--repo", record.repo,
                "--add-label", "agentflow:needs-mockup", "--remove-label", DRAWING])
    labels = github.issue_labels(record.repo, number)
    if labels is None or DRAWING in labels or "agentflow:needs-mockup" not in labels:
        return None
    return url
