"""Dashboard data (ADR 0010) — read GitHub + agentflow state into the operator view.

Read-*over*-GitHub: this gathers the fleet snapshot the dashboard renders; it never
mutates. GitHub stays the system of record. The pure shaping (`pr_stage`) is the
test surface; the gh queries are orchestration exercised live.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from datetime import datetime, timezone

from agentflow import github, live, ratchet
from agentflow.balancer import _claude_dispatch_status, _codex_dispatch_status, _query_pool
from agentflow.gate import reply_pending
from agentflow.labels import HELD_LABELS
from agentflow.loop import _CONFLICT_MARK, RepoConfig
from agentflow.prompts import UI_GAP_REASON
from agentflow.repo_facts import repo_profile


def pools() -> list[dict]:
    """Both prepaid pools' headroom (ADR 0006) — the 'idle while queued = bug' signal.

    `clear` carries the same unattended dispatch verdict launch applies — weekly pacing
    included — never the raw window reading alone: the console must not show a pool as
    dispatchable while the ratchet blocks it (#436). `spent_pct` stays the raw utilization;
    `headroom_pct` is *dispatchable* headroom, so a blocked pool reports 0 and `reason`
    names the block."""
    now = time.time()
    out = []
    for tool, dispatch_status in (("claude", _claude_dispatch_status),
                                  ("codex", _codex_dispatch_status)):
        s = dispatch_status(_query_pool(tool), now)
        out.append({"tool": s.tool, "clear": s.clear, "spent_pct": s.spent_pct,
                    "headroom_pct": round(100 - s.spent_pct, 1) if s.clear else 0.0,
                    "reason": s.reason})
    return out


def pr_stage(head_ref: str) -> str:
    """Which tool built an in-flight agentflow PR, from its branch. Pure."""
    if head_ref.startswith("agentflow/claude/"):
        return "claude"
    if head_ref.startswith("agentflow/codex/"):
        return "codex"
    return "other"


def _prs(repo: str, state: str) -> list[github.SnapshotPrRow]:
    rows = github.list_prs(repo, state)
    return [] if rows is None else [r for r in rows if r.head_ref_name.startswith("agentflow/")]


# --- workspace pipeline join (ADR 0033): coarse pipeline state + landed evidence for the small
# set of published Build-Issue proposals, keyed by issue number. The pure shaping is the test
# surface; the `gh` read stays daemon-side so the web layer never queries GitHub. -------------

# An agentflow build branch is `agentflow/<tool>/issue-<N>-slug` (coordinated_build), so the
# issue a PR was cut for is recoverable from its branch.
_ISSUE_BRANCH = re.compile(r"^agentflow/[^/]+/issue-(\d+)(?:-|$)")


def issue_of_branch(head_ref: str) -> int | None:
    """The issue number an agentflow build branch was cut for, or ``None``. Pure."""
    m = _ISSUE_BRANCH.match(head_ref or "")
    return int(m.group(1)) if m else None


def _review_verdict(decision) -> str | None:
    """Coarse agent-review verdict from a PR's ``reviewDecision``: approved / changes_requested,
    or ``None`` when there is no clean signal (then 'in review' collapses into 'PR open'). Pure."""
    d = (decision or "").upper()
    if d == "APPROVED":
        return "approved"
    if d == "CHANGES_REQUESTED":
        return "changes_requested"
    return None


def _ci_verdict(rollup) -> str | None:
    """Coarse CI verdict from a PR's status-check rollup: passing / failing / pending, or ``None``
    when there are no checks. Handles both check-run (``conclusion``) and status (``state``)
    entries. Pure."""
    checks = rollup or []
    if not checks:
        return None
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED") for s in states):
        return "failing"
    if any(s in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING") for s in states):
        return "pending"
    return "passing"


def _pr_for_issue(issue_number: int,
                  prs: list[github.PipelinePrRow]) -> github.PipelinePrRow | None:
    """The most recent agentflow PR cut for this issue, by branch, or ``None``. The highest PR
    number is the freshest attempt (a rebuild opens a new PR). Pure."""
    matches = [p for p in prs if issue_of_branch(p.head_ref_name) == issue_number]
    return max(matches, key=lambda p: p.number) if matches else None


def pipeline_state(issue_number: int,
                   open_prs: list[github.PipelinePrRow],
                   merged_prs: list[github.PipelinePrRow]) -> dict:
    """Coarse pipeline state + Acceptance Evidence for one published build issue, from the PRs that
    reference it by branch (the daemon already read these). Merged wins over open; among open PRs a
    review signal lifts 'PR open' to 'in review'; with no PR yet the handed-off issue is 'building'.
    On merge the evidence is the merge commit + review/CI verdict. Pure (the test surface)."""
    merged = _pr_for_issue(issue_number, merged_prs)
    if merged is not None:
        return {
            "state": "merged",
            "pr_number": merged.number,
            "pr_url": merged.url,
            "merged_at": merged.merged_at,
            "merge_commit": merged.merge_commit_oid,
            "review": _review_verdict(merged.review_decision),
            "ci": _ci_verdict(merged.ci_rollup),
        }
    openpr = _pr_for_issue(issue_number, open_prs)
    if openpr is not None:
        review = _review_verdict(openpr.review_decision)
        return {
            "state": "in_review" if review else "pr_open",
            "pr_number": openpr.number,
            "pr_url": openpr.url,
            "review": review,
            "ci": _ci_verdict(openpr.ci_rollup),
        }
    return {"state": "building", "pr_number": None, "pr_url": None}


def _pipeline_prs(repo: str, state: str) -> list[github.PipelinePrRow]:
    rows = github.list_pipeline_prs(repo, state)
    return [] if rows is None else [r for r in rows if r.head_ref_name.startswith("agentflow/")]


def workspace_pipeline(repo: str, issue_numbers) -> dict:
    """Resolve coarse pipeline state + landed evidence for the given published build issues, keyed
    by issue number — the daemon-side GitHub read the workspace projection joins onto (ADR 0033).
    Only the published issues are resolved, so this stays cheap next to the whole-fleet snapshot.
    Returns ``{issue_number: state_dict}``; the web layer never calls this."""
    wanted = {int(n) for n in issue_numbers if n}
    if not wanted:
        return {}
    open_prs = _pipeline_prs(repo, "open")
    merged_prs = _pipeline_prs(repo, "merged")
    return {n: pipeline_state(n, open_prs, merged_prs) for n in wanted}


def _dial_of(labels: list[dict], prefix: str) -> str | None:
    for lbl in labels:
        if lbl.get("name", "").startswith(prefix):
            return lbl["name"].split(":")[-1]
    return None


def _complexity_of(labels: list[dict]) -> str | None:
    return _dial_of(labels, "agentflow:complexity:")


def _effort_of(labels: list[dict]) -> str | None:
    return _dial_of(labels, "agentflow:effort:")


def _ready_issues(repo: str) -> list[dict]:
    """Open issues labeled ready-for-agent — the queue waiting to be built."""
    rows = github.list_issues(repo, label="ready-for-agent", limit=20)
    if rows is None:
        return []
    return [{"number": r.number, "title": r.title,
             "complexity": _complexity_of([{"name": n} for n in r.labels]),
             "effort": _effort_of([{"name": n} for n in r.labels])}
            for r in rows]


# Why a `needs-grilling` / `needs-mockup` issue is held — the meaning of the label,
# in the operator's own terms. Held issues carry this as their reason (the row + drawer
# render it), keyed on the state label so it stays honest when intake's routes change.
_HELD_REASON = {
    "agentflow:needs-grilling": "a real fork the pipeline couldn't settle — needs your call",
    "agentflow:needs-mockup": "a user-facing surface that needs a mockup before it's built",
}

# The disclaimer that opens every human-review park comment (`gate.park`), distinct from
# the conflict-survivor marker (`loop._CONFLICT_MARK`). We classify from the posted marker,
# never by re-running the pipeline (issue #71).
_PARK_MARK = "agentflow: parked for human review"
# The squash-merge-failed park reason (`loop`, the MERGE-then-failed branch).
_SQUASH_FAIL = "could not be squash-merged"


def _reviewer_of(builder: str) -> str:
    """The tool that reviews a builder's PR — always the other tool (ADR 0003)."""
    return "codex" if builder == "claude" else "claude"


def park_reason(comments: list[dict]) -> str | None:
    """Why an open agentflow PR is parked for a human, or None if it isn't. Pure (test
    surface): reads the markers the pipeline already posted, one reason per PR.

      open-question   — an unanswered maintainer question is the freshest word (issue #18)
      failed-merge    — a squash-merge failed, or the branch conflicts after main advanced
      ui-evidence     — a UI change with no before/after screenshot (ADR 0018)
      drop-to-reviewed— the normal reviewed/guarded hand-off (or any other park)
    """
    if reply_pending(comments):
        return "open-question"
    for c in reversed(comments):  # the most recent park state wins
        body = c.get("body", "")
        if _CONFLICT_MARK in body:
            return "failed-merge"
        if _PARK_MARK in body:
            if UI_GAP_REASON in body:
                return "ui-evidence"
            if _SQUASH_FAIL in body:
                return "failed-merge"
            return "drop-to-reviewed"
    return None


def _park_since(comments: list[dict]) -> str | None:
    """When the PR started waiting on you — the timestamp of its most recent comment
    (the park notice, or the maintainer's question that stopped it)."""
    for c in reversed(comments):
        if c.get("body", "").strip():
            return c.get("createdAt")
    return None


def _held_issues(repo: str) -> list[dict]:
    """Open issues the pipeline is holding for you — labeled needs-grilling / needs-mockup
    (no builder touches a held issue). `since` is the issue's last activity."""
    out = []
    for label in sorted(HELD_LABELS):
        rows = github.list_issues(repo, label=label, limit=20)
        if rows is None:
            continue
        for r in rows:
            out.append({"number": r.number, "title": r.title,
                        "state": label.split(":")[-1], "reason": _HELD_REASON[label],
                        "since": r.updated_at})
    return out


def _parked_prs(repo: str) -> list[dict]:
    """Open agentflow PRs parked for a human — found by the park markers the pipeline
    already posted, then classified into one reason (never by re-running the pipeline)."""
    out = []
    for p in _prs(repo, "open"):
        comments = github.pr_comment_rows(repo, p.number)
        if comments is None:  # a `gh` blip reads as 'unknown', not 'not parked'
            continue
        reason = park_reason(comments)
        if reason is None:
            continue
        builder = pr_stage(p.head_ref_name)
        out.append({"number": p.number, "title": p.title, "reason": reason,
                    "builder": builder, "reviewer": _reviewer_of(builder),
                    "since": _park_since(comments)})
    return out


def repo_view(cfg: RepoConfig) -> dict:
    return {
        "repo": cfg.repo,
        "profile": repo_profile(cfg.workdir),
        "ready": _ready_issues(cfg.repo),
        "held": _held_issues(cfg.repo),
        "in_flight": [{"number": p.number, "title": p.title,
                       "builder": pr_stage(p.head_ref_name)}
                      for p in _prs(cfg.repo, "open")],
        "parked": _parked_prs(cfg.repo),
        "recent_merges": [{"number": p.number, "title": p.title,
                           "merged_at": p.merged_at}
                          for p in _prs(cfg.repo, "merged")][:10],
        "ratchet": ratchet.status(cfg.repo),
    }


def snapshot(repos: list[RepoConfig], *, dispatch_enabled: bool) -> dict:
    """The whole operator view: whether the daemon may claim new work, the sessions running
    right now (from the daemon's live-session file), and a daemon status block. Per-pool
    running counts are DERIVED from `running[]` so a pool's count always equals its sessions
    in the list. A missing/corrupt live file reads as fleet idle, never an error."""
    running = live.running()
    per_pool = Counter(s.get("tool") for s in running)
    pool_list = pools()
    for p in pool_list:
        p["running"] = per_pool.get(p["tool"], 0)
    status = live.daemon_status()
    return {
        "dispatch": {"enabled": dispatch_enabled},
        "daemon": {
            "enabled": dispatch_enabled,
            "last_cycle_at": status.get("last_cycle_at"),
            "poll_seconds": status.get("poll_seconds"),
            # How fresh this snapshot's GitHub reads are — stamped as they're produced here,
            # then held by the server's ~15s cache (ADR 0023's two clocks).
            "gh_fresh_at": datetime.now(timezone.utc).isoformat(),
        },
        "pools": pool_list,
        "running": running,
        "repos": [repo_view(c) for c in repos],
    }
