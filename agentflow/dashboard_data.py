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
from agentflow.loop import RepoConfig, repo_profile
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


def repo_view(cfg: RepoConfig) -> dict:
    return {
        "repo": cfg.repo,
        "profile": repo_profile(cfg.workdir),
        "ready": _ready_issues(cfg.repo),
        "in_flight": [{"number": p["number"], "title": p["title"],
                       "builder": pr_stage(p.get("headRefName", ""))}
                      for p in _prs(cfg.repo, "open")],
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
