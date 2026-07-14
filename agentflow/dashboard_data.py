"""Dashboard data (ADR 0010) — read GitHub + agentflow state into the operator view.

Read-*over*-GitHub: this gathers the fleet snapshot the dashboard renders; it never
mutates. GitHub stays the system of record. The pure shaping (`pr_stage`) is the
test surface; the gh queries are orchestration exercised live.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone

from agentflow import live, ratchet
from agentflow.balancer import _query_pool
from agentflow.gate import reply_pending
from agentflow.loop import (
    _CONFLICT_MARK,
    _UI_GAP_REASON,
    HELD_LABELS,
    RepoConfig,
    _pr_comments,
    repo_profile,
)
from agentflow.runner import _run


def pools() -> list[dict]:
    """Both prepaid pools' headroom (ADR 0006) — the 'idle while queued = bug' signal."""
    out = []
    for tool in ("claude", "codex"):
        s = _query_pool(tool)
        out.append({"tool": s.tool, "clear": s.clear,
                    "spent_pct": s.spent_pct, "headroom_pct": round(100 - s.spent_pct, 1)})
    return out


def pr_stage(head_ref: str) -> str:
    """Which tool built an in-flight agentflow PR, from its branch. Pure."""
    if head_ref.startswith("agentflow/claude/"):
        return "claude"
    if head_ref.startswith("agentflow/codex/"):
        return "codex"
    return "other"


def _prs(repo: str, state: str) -> list[dict]:
    r = _run(["gh", "pr", "list", "--repo", repo, "--state", state, "--json",
              "number,title,headRefName,mergedAt", "--limit", "30"])
    if r.returncode != 0:
        return []
    try:
        return [p for p in json.loads(r.stdout)
                if p.get("headRefName", "").startswith("agentflow/")]
    except json.JSONDecodeError:
        return []


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
    r = _run(["gh", "issue", "list", "--repo", repo, "--state", "open",
              "--label", "ready-for-agent", "--json", "number,title,labels", "--limit", "20"])
    if r.returncode != 0:
        return []
    try:
        issues = json.loads(r.stdout)
    except json.JSONDecodeError:
        return []
    return [{"number": i["number"], "title": i["title"],
             "complexity": _complexity_of(i["labels"]), "effort": _effort_of(i["labels"])}
            for i in issues]


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
            if _UI_GAP_REASON in body:
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
        r = _run(["gh", "issue", "list", "--repo", repo, "--state", "open",
                  "--label", label, "--json", "number,title,updatedAt", "--limit", "20"])
        if r.returncode != 0:
            continue
        try:
            issues = json.loads(r.stdout)
        except json.JSONDecodeError:
            continue
        for i in issues:
            out.append({"number": i["number"], "title": i["title"],
                        "state": label.split(":")[-1], "reason": _HELD_REASON[label],
                        "since": i.get("updatedAt")})
    return out


def _parked_prs(repo: str) -> list[dict]:
    """Open agentflow PRs parked for a human — found by the park markers the pipeline
    already posted, then classified into one reason (never by re-running the pipeline)."""
    out = []
    for p in _prs(repo, "open"):
        comments = _pr_comments(repo, p["number"])
        if comments is None:  # a `gh` blip reads as 'unknown', not 'not parked'
            continue
        reason = park_reason(comments)
        if reason is None:
            continue
        builder = pr_stage(p.get("headRefName", ""))
        out.append({"number": p["number"], "title": p["title"], "reason": reason,
                    "builder": builder, "reviewer": _reviewer_of(builder),
                    "since": _park_since(comments)})
    return out


def repo_view(cfg: RepoConfig) -> dict:
    return {
        "repo": cfg.repo,
        "profile": repo_profile(cfg.workdir),
        "ready": _ready_issues(cfg.repo),
        "held": _held_issues(cfg.repo),
        "in_flight": [{"number": p["number"], "title": p["title"],
                       "builder": pr_stage(p.get("headRefName", ""))}
                      for p in _prs(cfg.repo, "open")],
        "parked": _parked_prs(cfg.repo),
        "recent_merges": [{"number": p["number"], "title": p["title"],
                           "merged_at": p.get("mergedAt")}
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
