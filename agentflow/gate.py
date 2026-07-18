"""The auto-merge gate — decide to merge, revise, or park (ADR 0003, 0004, 0020).

`decide_merge` is pure: auto-merge requires ALL of an independent (cross-tool)
review, green CI, and a clean verdict. Anything else revises — up to `MAX_REVISES`
rounds — then parks for a human. So `autonomous` is never less safe than `reviewed`.
The gh actions it dispatches (CI check, squash-merge, park) are thin wrappers around
the pure decision.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from enum import Enum

from agentflow.reviewer import Verdict
from agentflow.runner import _run

# Merges stay serialized even as builds run concurrently (ADR 0009 collision floor): two
# PRs never squash-merge at the same instant. Concurrent dispatch (ADR 0023) multiplies
# builds, never overlapping merges — this process-wide lock is where that floor is held.
_MERGE_LOCK = threading.Lock()


class MergeDecision(str, Enum):
    MERGE = "merge"
    REVISE = "revise"
    PARK = "park"    # drop-to-reviewed: a human merges


# Revise a fixable miss, but bail after this many unproductive rounds rather than
# looping forever (ADR 0020; was a single round under ADR 0004).
MAX_REVISES = 2

# Every agentflow comment on a PR (the park notice, a build-agent reply) carries this
# marker in its disclaimer, so we can tell our own comments from the maintainer's — the
# same discipline intake uses on issues (INTAKE_MARK). The bot posts as the maintainer,
# so we key on the marker, not authorship.
PR_MARK = "agentflow:"
_RESPOND_TARGET_PREFIX = "agentflow-respond-target:"
_RESPOND_TARGET_RE = re.compile(r"<!--\s*agentflow-respond-target:([^>]+?)\s*-->")
_RESPOND_PARK_TARGET_RE = re.compile(r"<!--\s*agentflow-respond-park-target:([^>]+?)\s*-->")
_RESPOND_CHANGE_RE = re.compile(r"<!--\s*agentflow-respond-change:([^>]+?)\s*-->")


def respond_reply_disclaimer(target: str) -> str:
    """The human-readable Respond marker plus its immutable maintainer-comment target.

    GitHub posts agentflow comments as the maintainer account, so the visible disclaimer keeps
    authorship distinguishable while the hidden target binds completion to the exact comment one
    durable Respond record owns.
    """
    return ("> *agentflow: reply from the build agent.*\n"
            f"<!-- {_RESPOND_TARGET_PREFIX}{target} -->")


def respond_change_marker(result: str) -> str:
    """Durable Respond outcome claim: ``none`` or the pushed PR head SHA."""
    return f"<!-- agentflow-respond-change:{result} -->"


def _respond_reply_target(body: str) -> str:
    match = _RESPOND_TARGET_RE.search(body)
    return match.group(1).strip() if match is not None else ""


def respond_reply_posted(comments: list[dict], target: str) -> bool:
    """Whether agentflow durably posted the reply for this exact Respond target."""
    return bool(target) and any(
        PR_MARK in comment.get("body", "")
        and _respond_reply_target(comment.get("body", "")) == str(target)
        for comment in comments
    )


def respond_reply_change(comments: list[dict], target: str) -> str:
    """The unique targeted reply's declared branch outcome, or empty when unproved.

    Requiring exactly one targeted reply makes duplicate posting visible and fail-closed instead
    of accepting whichever duplicate happens to appear last.
    """
    matches = [comment.get("body", "") for comment in comments
               if _respond_reply_target(comment.get("body", "")) == str(target)]
    if len(matches) != 1:
        return ""
    changes = _RESPOND_CHANGE_RE.findall(matches[0])
    return changes[0].strip() if len(changes) == 1 else ""


def _unanswered_maintainer_comments(comments: list[dict]) -> list[tuple[str, str]]:
    """Return unanswered maintainer comments in arrival order, one durable target each.

    A target-aware Respond reply removes only the comment it answered, so a second maintainer
    comment that arrived before that reply remains pending with its own budget. Legacy generic
    agentflow replies retain their old run-level meaning and answer everything before them.
    """
    pending: dict[str, str] = {}
    for index, comment in enumerate(comments):
        body = comment.get("body", "").strip()
        if not body:
            continue
        answered = _respond_reply_target(body)
        if answered:
            pending.pop(answered, None)
            continue
        parked = _RESPOND_PARK_TARGET_RE.search(body)
        if parked is not None:
            pending.pop(parked.group(1).strip(), None)
            continue
        if PR_MARK in body:
            pending.clear()
            continue
        target = str(comment.get("id") or comment.get("url") or "")
        pending.setdefault(target or f"__agentflow_missing_target_{index}", body)
    return list(pending.items())


def reply_pending(comments: list[dict]) -> bool:
    """True when at least one maintainer comment has no matching agentflow reply. Pure
    (test surface).

    On an `autonomous` repo this BLOCKS auto-merge: nothing merges while a maintainer
    question hangs (issue #18). Mirrors intake's `awaiting_recheck`, keyed on our marker."""
    return bool(_unanswered_maintainer_comments(comments))


def maintainer_comment_id(comments: list[dict]) -> str:
    """The oldest unanswered comment id — one immutable coordinated Respond target."""
    pending = _unanswered_maintainer_comments(comments)
    if not pending or pending[0][0].startswith("__agentflow_missing_target_"):
        return ""
    return pending[0][0]


def maintainer_comment(comments: list[dict]) -> str:
    """The oldest unanswered maintainer comment text — exactly what one Respond answers."""
    pending = _unanswered_maintainer_comments(comments)
    return pending[0][1] if pending else ""


def decide_merge(*, verdict: Verdict, ci_green: bool, reviewer_tool: str,
                 builder_tool: str, revises_used: int,
                 ui_evidence_missing: bool = False,
                 reply_pending: bool = False) -> MergeDecision:
    """Pure. Merge only on independent review + green CI + clean verdict — and never
    when a change to a declared UI surface carries no screenshot, nor over an
    unanswered maintainer question on the PR (issue #18).

    `ui_evidence_missing` is the mechanical UI-evidence gate (ADR 0018): it is decided
    from the diff and the PR's attachments, NOT from the review verdict, so a reviewer
    who waves a screenshot-less UI change through as "not blocking" cannot clear it.
    A missing screenshot parks for a human rather than churning revises — the builder
    was already told to attach one."""
    if reply_pending:
        # An open question from the human who merges blocks auto-merge until the
        # responder addresses it — a reply, not a merge, is the next move.
        return MergeDecision.PARK
    independent = bool(reviewer_tool) and reviewer_tool != builder_tool
    if not independent:
        # ADR 0003: a same-tool / missing review never auto-merges.
        return MergeDecision.PARK
    if not verdict.parsed:
        # The review itself failed to produce a usable verdict — a builder revise
        # can't fix that. Park for a human (or a review retry), don't churn the build.
        return MergeDecision.PARK
    if ui_evidence_missing:
        # Mechanical, unwaivable: a declared UI surface changed with no screenshot.
        return MergeDecision.PARK
    if ci_green and verdict.clean:
        return MergeDecision.MERGE
    if revises_used < MAX_REVISES:
        # ADR 0020: revise a fixable miss, bailing after MAX_REVISES rounds.
        return MergeDecision.REVISE
    return MergeDecision.PARK


# --- the mechanical UI-evidence gate (ADR 0018) --------------------------------
# A change under a declared UI surface must ship a before/after screenshot. Both
# predicates are pure (the test surface); `ui_evidence_gap` wires them to live `gh`.

_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")           # ![alt](url)
_HTML_IMG_RE = re.compile(r"<img[\s>]", re.IGNORECASE)       # <img ...>
# GitHub drag-drop uploads render as a bare link, not a markdown image:
# user-images.githubusercontent.com/... or github.com/<owner>/<repo|user-attachments>/assets/...
_ASSET_URL_RE = re.compile(
    r"https?://(?:[\w.-]*githubusercontent\.com/|github\.com/[^\s)]+/assets/)", re.IGNORECASE)
# The browserless attachment path builders are instructed to use: screenshots committed
# on the branch under docs/screenshots/, viewable in the PR's Files-changed tab.
_EVIDENCE_FILE_RE = re.compile(
    r"(?:^|/)docs/screenshots/.+\.(?:png|jpe?g|gif|webp)$", re.IGNORECASE)


def touches_ui_surface(changed_files: list[str], surfaces: list[str]) -> bool:
    """Pure. True if any changed file lies under a declared UI-surface prefix. With no
    declared surfaces the intersection is empty — the gate is inert for a non-UI repo."""
    prefixes = [s.strip().lstrip("./") for s in surfaces if s.strip()]
    return any(f.startswith(p) for f in changed_files for p in prefixes)


def has_image_evidence(text: str) -> bool:
    """Pure. True if the text carries an image: a markdown image, an `<img>` tag, or a
    GitHub user-asset URL (drag-dropped uploads are bare links, not markdown images)."""
    return bool(_MD_IMAGE_RE.search(text) or _HTML_IMG_RE.search(text)
                or _ASSET_URL_RE.search(text))


def has_committed_evidence(changed_files: list[str]) -> bool:
    """Pure. True if the PR itself commits screenshots under the evidence convention
    (docs/screenshots/**). Agents cannot use GitHub's drag-drop upload (it needs a
    signed-in browser), so committed files are the first-class evidence channel."""
    return any(_EVIDENCE_FILE_RE.search(f) for f in changed_files)


def ui_evidence_gap(repo: str, pr_number: int, surfaces: list[str]) -> bool:
    """Live: does this PR change a declared UI surface but carry no screenshot in its
    body or comments? Fail-safe — a `gh` error with surfaces declared returns True (we
    can't prove a UI change is evidenced, so don't auto-merge it unseen)."""
    if not surfaces:
        return False   # non-UI repo: gate inert
    r = _run(["gh", "pr", "view", str(pr_number), "--repo", repo,
              "--json", "files,body,comments"])
    if r.returncode != 0:
        return True
    data = json.loads(r.stdout or "{}")
    files = [f.get("path", "") for f in data.get("files", [])]
    if not touches_ui_surface(files, surfaces):
        return False
    if has_committed_evidence(files):
        return False
    evidence = "\n".join([data.get("body") or ""]
                         + [c.get("body", "") for c in data.get("comments", [])])
    return not has_image_evidence(evidence)


# --- gh actions ----------------------------------------------------------------
def ci_is_green(repo: str, pr_number: int, *,
                timeout: int | None = None,
                interval: int | None = None) -> bool:
    """True only if all required checks completed successfully.

    Polls `gh pr checks` every `interval` seconds until all checks pass or
    `timeout` is reached. `timeout` defaults to AGENTFLOW_CI_TIMEOUT (30 min);
    `interval` defaults to AGENTFLOW_CI_INTERVAL (30 s). Returns False at the
    deadline — never hangs indefinitely. Non-zero on fail or on a repo with no
    checks at all is treated as not-green: fail safe, never auto-merges.
    """
    t = timeout if timeout is not None else int(os.environ.get("AGENTFLOW_CI_TIMEOUT", str(30 * 60)))
    iv = interval if interval is not None else int(os.environ.get("AGENTFLOW_CI_INTERVAL", "30"))
    deadline = time.monotonic() + t
    while True:
        r = _run(["gh", "pr", "checks", str(pr_number), "--repo", repo])
        if r.returncode == 0:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(iv, remaining))


def squash_merge(repo: str, pr_number: int) -> bool:
    # The merge lock serializes the actual land across all concurrent build chains and the
    # survivor re-rebase pass, so merges never overlap (ADR 0009). Held only around the
    # merge itself — never during CI polling — so it can't stall other builds.
    with _MERGE_LOCK:
        state = _run(["gh", "pr", "view", str(pr_number), "--repo", repo,
                      "--json", "isDraft"])
        if state.returncode != 0:
            return False
        try:
            is_draft = json.loads(state.stdout or "{}").get("isDraft")
        except (json.JSONDecodeError, AttributeError):
            return False
        if not isinstance(is_draft, bool):
            return False
        if is_draft:
            ready = _run(["gh", "pr", "ready", str(pr_number), "--repo", repo])
            if ready.returncode != 0:
                return False
        r = _run(["gh", "pr", "merge", str(pr_number), "--repo", repo,
                  "--squash", "--delete-branch"])
        return r.returncode == 0


def park(repo: str, pr_number: int, verdict: Verdict,
         reason: str = "could not be auto-merged after review") -> None:
    """Post the review findings so a human can pick the PR up (drop-to-reviewed, or
    the normal hand-off for a `reviewed`/`guarded` repo)."""
    lines = [f"- **{f.severity}** {f.file}:{f.line} — {f.summary}".rstrip(" —:0")
             for f in verdict.findings] or ["- (no blocking findings)"]
    body = ("> *agentflow: parked for human review.*\n\n"
            f"This PR {reason}. Review findings:\n" + "\n".join(lines))
    _run(["gh", "pr", "comment", str(pr_number), "--repo", repo, "--body", body])
