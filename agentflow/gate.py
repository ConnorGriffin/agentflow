"""The auto-merge gate — decide to merge, revise, or park (ADR 0003, 0004, 0020).

`decide_merge` is pure: auto-merge requires ALL of an independent (cross-tool)
review, green CI, and a clean verdict. Anything else revises — up to `MAX_REVISES`
rounds — then parks for a human. So `autonomous` is never less safe than `reviewed`.
The gh actions it dispatches (CI check, squash-merge, park) are thin wrappers around
the pure decision.
"""

from __future__ import annotations

import os
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
                 builder_tool: str, revises_used: int) -> MergeDecision:
    """Pure. Merge only on independent review + green CI + clean verdict."""
    independent = bool(reviewer_tool) and reviewer_tool != builder_tool
    if not independent:
        # ADR 0003: a same-tool / missing review never auto-merges.
        return MergeDecision.PARK
    if not verdict.parsed:
        # The review itself failed to produce a usable verdict — a builder revise
        # can't fix that. Park for a human (or a review retry), don't churn the build.
        return MergeDecision.PARK
    if ci_green and verdict.clean:
        return MergeDecision.MERGE
    if revises_used < MAX_REVISES:
        # ADR 0020: revise a fixable miss, bailing after MAX_REVISES rounds.
        return MergeDecision.REVISE
    return MergeDecision.PARK


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
