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
import time
from enum import Enum

from agentflow.reviewer import Verdict
from agentflow.runner import _run


class MergeDecision(str, Enum):
    MERGE = "merge"
    REVISE = "revise"
    PARK = "park"    # drop-to-reviewed: a human merges


# Revise a fixable miss, but bail after this many unproductive rounds rather than
# looping forever (ADR 0020; was a single round under ADR 0004).
MAX_REVISES = 2


def decide_merge(*, verdict: Verdict, ci_green: bool, reviewer_tool: str,
                 builder_tool: str, revises_used: int,
                 ui_evidence_missing: bool = False) -> MergeDecision:
    """Pure. Merge only on independent review + green CI + clean verdict — and never
    when a change to a declared UI surface carries no screenshot.

    `ui_evidence_missing` is the mechanical UI-evidence gate (ADR 0018): it is decided
    from the diff and the PR's attachments, NOT from the review verdict, so a reviewer
    who waves a screenshot-less UI change through as "not blocking" cannot clear it.
    A missing screenshot parks for a human rather than churning revises — the builder
    was already told to attach one."""
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
    evidence = "\n".join([data.get("body") or ""]
                         + [c.get("body", "") for c in data.get("comments", [])])
    return not has_image_evidence(evidence)


# --- gh actions (exercised live, not unit-tested) ------------------------------
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
