"""Dashboard data (ADR 0010) — read GitHub + agentflow state into the operator view.

Read-*over*-GitHub: this gathers the fleet snapshot the dashboard renders; it never
mutates. GitHub stays the system of record. The pure shaping (`pr_stage`) is the
test surface; the gh queries are orchestration exercised live.
"""

from __future__ import annotations

import json

from agentflow import ratchet
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


def _tier_of(labels: list[dict]) -> str | None:
    for lbl in labels:
        n = lbl.get("name", "")
        if n.startswith("tier:"):
            return n.split(":", 1)[1]
    return None


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
    return [{"number": i["number"], "title": i["title"], "tier": _tier_of(i["labels"])}
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


def snapshot(repos: list[RepoConfig]) -> dict:
    """The whole operator view: two-pool headroom + per-repo fleet state."""
    return {"pools": pools(), "repos": [repo_view(c) for c in repos]}
