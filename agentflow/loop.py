"""The M0 loop — one ready-for-agent issue through the whole pipeline, serially.

Ephemeral hands, single issue (ADR 0011); the persistent daemon and real two-pool
balancing are M1. For M0 the pair is fixed — Claude builds, Codex reviews — so
cross-tool independence holds and swapping in the headroom balancer is the M1 change.
Every issue must carry a `tier:*` label (ADR 0014's hard gate); intake will stamp it
later, but M0 reads it directly and skips an issue that has none.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from agentflow.balancer import pick_pair
from agentflow.gate import MergeDecision, ci_is_green, decide_merge, park, squash_merge
from agentflow.notify import notify
from agentflow.reviewer import Finding, Reviewer, Verdict
from agentflow.runner import BuildStatus, BuildTask, Tier, _run


def _pr_url(repo: str, pr: int) -> str:
    return f"https://github.com/{repo}/pull/{pr}"

_TIER_LABEL = re.compile(r"^tier:(light|standard|deep)$")


@dataclass(frozen=True)
class RepoConfig:
    repo: str        # "owner/name" on GitHub
    workdir: str     # local main checkout


def tier_from_labels(labels: list[str]) -> Tier | None:
    """The issue's cost tier from a `tier:<light|standard|deep>` label. Hard gate."""
    for name in labels:
        m = _TIER_LABEL.match(name)
        if m:
            return Tier(m.group(1))
    return None


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40].strip("-") or "issue"


def pr_number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


BUILD_PROMPT = """Implement {repo} issue #{n}: {title}

{body}

You are in a fresh worktree on a new branch off origin/main. Implement the change,
add or update tests, and make the suite green. Commit your work.

Before opening the PR, `git fetch origin` and rebase once onto `origin/main`, then
rerun the tests. If the rebase conflicts (or tests fail post-rebase for a reason not
your own), stop and post a comment prefixed `INTEGRATION-COLLISION:` instead of
forcing it. Otherwise push the branch and open a PR with `Closes #{n}` in the body.

Keep the change minimal and match the surrounding code. If you hit a blocker you
cannot safely resolve, post a comment prefixed `MISSING-CONTEXT:` and stop instead
of guessing."""

REVISE_PROMPT = """Address the blocking review findings on PR #{n} in this worktree,
push to the same branch, and keep the test suite green. Do NOT open a new PR.

Blocking findings:
{findings}"""


def _next_ready_issue(cfg: RepoConfig) -> dict | None:
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", "ready-for-agent", "--json", "number,title,body,labels",
              "--limit", "50"])
    if r.returncode != 0:
        return None
    issues = sorted(json.loads(r.stdout or "[]"), key=lambda i: i["number"])
    return issues[0] if issues else None


def _builder_worktree(cfg: RepoConfig, tool: str, n: int, sl: str) -> str:
    return str(Path(cfg.workdir) / ".agentflow" / "worktrees" / tool / f"issue-{n}-{sl}")


def run_once(cfg: RepoConfig) -> str:
    """Pull one ready issue and run it end to end. Returns a one-line result."""
    issue = _next_ready_issue(cfg)
    if not issue:
        return "no ready-for-agent issues"
    n = issue["number"]
    tier = tier_from_labels([lbl["name"] for lbl in issue["labels"]])
    if tier is None:
        return f"#{n}: skipped — no tier:* label (ADR 0014 hard gate)"

    builder, reviewer_runner = pick_pair()   # ADR 0006: more headroom builds; other reviews
    if builder is None:
        return f"#{n}: no pool has headroom right now — deferring"
    sl = slug(issue["title"])
    task = BuildTask(cfg.repo, cfg.workdir, n, sl, tier,
                     prompt=BUILD_PROMPT.format(repo=cfg.repo, n=n, title=issue["title"],
                                                body=issue.get("body") or ""))
    outcome = builder.build(task)
    if outcome.status is not BuildStatus.PR_OPENED:
        notify("agentflow", f"{cfg.repo} #{n}: build {outcome.status.value}",
               f"https://github.com/{cfg.repo}/issues/{n}")
        return f"#{n}: build {outcome.status.value} — {outcome.detail}"

    pr = pr_number(outcome.pr_url)
    head_branch = f"agentflow/{builder.tool}/issue-{n}-{sl}"
    if reviewer_runner is None:
        # Single-tool fallback (ADR 0003): no independent reviewer — never auto-merge.
        park(cfg.repo, pr, Verdict(clean=False, parsed=False,
             findings=(Finding("blocking", "no independent cross-tool reviewer available"),)))
        notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} parked — single-tool",
               _pr_url(cfg.repo, pr))
        return f"#{n}: built PR #{pr}; parked — only one pool had headroom"
    reviewer = Reviewer(reviewer_runner)

    acceptance = issue.get("body") or ""
    revises_used = 0
    while True:
        verdict = reviewer.review(cfg.repo, cfg.workdir, pr, head_branch, sl, tier, acceptance=acceptance)
        decision = decide_merge(verdict=verdict, ci_green=ci_is_green(cfg.repo, pr),
                                reviewer_tool=reviewer_runner.tool, builder_tool=builder.tool,
                                revises_used=revises_used)
        if decision is MergeDecision.MERGE:
            ok = squash_merge(cfg.repo, pr)
            return f"#{n}: MERGED PR #{pr}" if ok else f"#{n}: merge failed on PR #{pr}"
        if decision is MergeDecision.PARK:
            park(cfg.repo, pr, verdict)
            notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} parked after review",
                   _pr_url(cfg.repo, pr))
            return f"#{n}: parked PR #{pr} for human review"
        # REVISE — one builder pass on the PR branch, then re-review (ADR 0004).
        findings = "\n".join(f"- {f.summary}" for f in verdict.blocking) or "- (see review)"
        builder.launch(REVISE_PROMPT.format(n=pr, findings=findings),
                       cwd=_builder_worktree(cfg, builder.tool, n, sl),
                       model=builder.model_for(tier))
        revises_used += 1


if __name__ == "__main__":  # M0 entrypoint — the sandbox dogfood target
    cfg = RepoConfig(repo="ConnorGriffin/agentflow-sandbox",
                     workdir=str(Path.home() / "Code" / "ConnorGriffin" / "agentflow-sandbox"))
    print(run_once(cfg))
