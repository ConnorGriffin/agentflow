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
from datetime import datetime

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


def _this_round(comment, opened_at: int) -> bool:
    """Whether one marked comment belongs to *this* round rather than an earlier one on the same
    issue. A mockup issue can be drawn more than once, and every round's comment carries the same
    mark, so a round that reads the whole thread would answer for its predecessor's work. Anchored
    to when the record was opened; a record from before creation times were stamped keeps the old
    whole-thread behavior."""
    if not opened_at:
        return True
    try:
        created = datetime.fromisoformat(
            (comment.created_at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return created >= opened_at


def _hold_mockup(record) -> str | None:
    """Create Mockup's one issue-native handoff while preserving unfinished local work.

    The round only ever gets *one* marked comment, and which one it is decides the whole handoff,
    so it is selected from this round alone (:func:`_this_round`) — an earlier round's comment
    must not stand in for this one, or an exhausted round would silently report itself as missing
    context and never post its explanation.

    From there the two boundaries differ:

    - **Missing context** is already the durable stage-native handoff. It is never restated and
      never edited: its own MISSING-CONTEXT text is what proves the hold, and the envelope
      (:class:`~agentflow.handoff.DurableHandoff`, ADR 0042) writes nothing at all. An edit that
      could keep failing — an unwritable comment, a body over GitHub's size limit — would
      otherwise wedge the round with no ping and no release, forever.
    - **An unfinished round** gains the exhaustion explanation plus a per-record marker derived
      from this record and its reason. If the comment it belongs on cannot be edited, the
      explanation is posted on its own rather than leaving the stage stuck.

    Handing the issue back to the maintainer's choice (``needs-mockup`` kept, the drawing claim
    released) is stage bookkeeping that runs once the handoff confirms; the worktree is retained
    either way.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject, proof_marker

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    comments = github.issue_comments(record.repo, number)
    if comments is None:
        return None
    marked = next((comment for comment in reversed(comments)
                   if MOCKUP_MARK in comment.body and _this_round(comment, record.created_at)),
                  None)
    missing = marked is not None and "MISSING-CONTEXT:" in marked.body
    explanation = ("Mockup exhausted its continuation budget before completing the visual round. "
                   "The branch and local worktree are retained for a human to continue.")
    reason = "missing context" if missing else "continuation budget exhausted"
    proof = proof_marker(record.identity, reason, tag="mockup-hold")
    # The marker a hold posted before it was scoped to the reason, still proof of itself on an
    # issue held when the daemon deploys.
    legacy = "<!-- agentflow-mockup-hold:" + hashlib.sha256(
        record.identity.encode()).hexdigest()[:24] + " -->"

    def post() -> None:
        # The explanation belongs on the round's own comment; a comment GitHub will not rewrite
        # gets it as a comment of its own rather than wedging the round with nothing said.
        if marked is None or not github.edit_comment(
                marked.id, f"{marked.body.rstrip()}\n\n{proof}\n\n{explanation}"):
            github.comment(record.repo, number,
                           f"{MOCKUP_DISCLAIMER}\n{proof}\n\n{explanation}")

    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="mockup-hold",
        # A missing-context round is already handed off: its own boundary is the proof, and the
        # action below is never reached.
        marker="MISSING-CONTEXT:" if missing else proof,
        action=post,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: Mockup held — {reason}"),
        also_proven_by="" if missing else legacy)
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
