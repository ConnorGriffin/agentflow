"""The Runner seam — spawn one isolated agent session and classify its outcome.

A deep module. Callers say `runner.build(task)` and get back a classified
`BuildOutcome`. Hidden behind that one call: creating a git worktree off fresh
`origin/main`, provisioning its environment, invoking the tool-specific CLI at the
issue's cost tier (`claude -p --model …` vs `codex exec -m …`), and classifying
what happened by inspecting the resulting PR and marker comments. The two tools
differ *only* inside their adapters (`_launch` + the tier→model map); the worktree
choreography and the classification are shared.

Ported and generalized from the dotfiles `codex-go` wrapper (Codex-only) into a
two-tool abstraction — the "unified runner" the reuse map flagged as net-new.

The interface is the test surface: `classify_build` and each adapter's tier→model
resolution are pure, tested without spawning anything (see tests/test_runner.py).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Stable bail markers a session posts as a comment when it hits a gap (ADR 0005).
MARKERS = ("MISSING-CONTEXT", "SCOPE-EXPANSION", "INTEGRATION-COLLISION")
_MARKER_RE = re.compile(rf"^({'|'.join(MARKERS)}):")


class Tier(str, Enum):
    """Cost-appropriate model tier, assigned per issue by intake (ADR 0014).

    Tool-agnostic; each adapter maps it to a concrete model. A hard requirement —
    the deep tier burns rate-limit headroom fastest, so mis-sizing wastes the very
    resource ADR 0006 optimizes.
    """

    LIGHT = "light"        # routine/mechanical: CSS, config, docs, default flips
    STANDARD = "standard"  # ordinary features, moderate logic
    DEEP = "deep"          # correctness-sensitive, design-heavy, multi-surface


class BuildStatus(str, Enum):
    PR_OPENED = "pr_opened"      # a PR exists for the branch — success
    BAIL = "bail"               # session posted a marker comment and stopped
    INCOMPLETE = "incomplete"   # ran, but neither a PR nor a bail — needs a look
    ERROR = "error"             # the launch itself failed


@dataclass(frozen=True, slots=True)
class BuildTask:
    repo: str        # "owner/name" on GitHub
    workdir: str     # local main checkout (worktrees are cut from here)
    issue: int
    slug: str        # short kebab title, for branch/worktree naming
    tier: Tier       # cost tier assigned by intake — no build without one
    prompt: str      # the work order / self-scoped brief handed to the agent


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    status: BuildStatus
    pr_url: str | None = None
    marker: str | None = None
    detail: str = ""


def classify_build(pr_url: str | None, new_marker_comments: list[str]) -> BuildOutcome:
    """Classify a finished build session from what it left behind. Pure.

    Precedence mirrors `codex-go`: a PR is success even if a marker was also
    posted; otherwise the first marker comment is a bail; otherwise incomplete.
    """
    if pr_url:
        return BuildOutcome(BuildStatus.PR_OPENED, pr_url=pr_url)
    for body in new_marker_comments:
        m = _MARKER_RE.match(body.strip())
        if m:
            first_line = body.strip().splitlines()[0]
            return BuildOutcome(BuildStatus.BAIL, marker=m.group(1), detail=first_line)
    return BuildOutcome(BuildStatus.INCOMPLETE, detail="no PR and no bail marker")


def _run(cmd: list[str], cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True)


class _WorktreeRunner:
    """Shared build orchestration; subclasses supply `tool`, `MODELS`, `_launch`."""

    tool: str = "?"
    MODELS: dict[Tier, str] = {}

    def model_for(self, tier: Tier) -> str:
        """Resolve a tool-agnostic tier to this tool's concrete model."""
        return self.MODELS[tier]

    def build(self, task: BuildTask) -> BuildOutcome:
        branch = f"agentflow/{self.tool}/issue-{task.issue}-{task.slug}"
        wt = Path(task.workdir) / ".agentflow" / "worktrees" / self.tool / f"issue-{task.issue}-{task.slug}"
        try:
            self._prepare_worktree(task.workdir, branch, wt)
            self._provision(wt)
        except subprocess.CalledProcessError as e:
            return BuildOutcome(BuildStatus.ERROR, detail=f"worktree/provision failed: {e}")

        started = time.time()
        launched = self._launch(task.prompt, cwd=str(wt), model=self.model_for(task.tier))

        pr_url = self._pr_for_branch(task.repo, branch)
        markers = self._new_marker_comments(task.repo, task.issue, since=started)
        outcome = classify_build(pr_url, markers)
        if outcome.status is BuildStatus.INCOMPLETE and not launched:
            return BuildOutcome(BuildStatus.ERROR, detail="launch exited non-zero, no PR, no marker")
        return outcome

    # --- shared git/gh plumbing -------------------------------------------------
    def _prepare_worktree(self, workdir: str, branch: str, wt: Path) -> None:
        _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).check_returncode()
        if wt.exists():
            return
        wt.parent.mkdir(parents=True, exist_ok=True)
        have_branch = _run(["git", "-C", workdir, "show-ref", "--quiet", f"refs/heads/{branch}"]).returncode == 0
        add = ["git", "-C", workdir, "worktree", "add"]
        add += [str(wt), branch] if have_branch else ["-b", branch, str(wt), "origin/main"]
        _run(add).check_returncode()

    def _provision(self, wt: Path) -> None:
        if (wt / "uv.lock").exists() and not (wt / ".venv" / "bin" / "python").exists():
            _run(["uv", "sync", "--all-extras"], cwd=str(wt)).check_returncode()

    def _pr_for_branch(self, repo: str, branch: str) -> str | None:
        r = _run(["gh", "pr", "list", "--repo", repo, "--head", branch,
                  "--state", "all", "--json", "url", "-q", ".[0].url // \"\""])
        return r.stdout.strip() or None

    def _new_marker_comments(self, repo: str, issue: int, since: float) -> list[str]:
        r = _run(["gh", "issue", "view", str(issue), "--repo", repo, "--json", "comments"])
        if r.returncode != 0:
            return []
        try:
            comments = json.loads(r.stdout).get("comments", [])
        except json.JSONDecodeError:
            return []
        out = []
        for c in comments:
            ts = _iso_to_epoch(c.get("createdAt", ""))
            if ts is not None and ts >= since:
                out.append(c.get("body", ""))
        return out

    def _launch(self, prompt: str, cwd: str, model: str) -> bool:  # returns launched_ok
        raise NotImplementedError


class ClaudeRunner(_WorktreeRunner):
    tool = "claude"
    MODELS = {Tier.LIGHT: "haiku", Tier.STANDARD: "sonnet", Tier.DEEP: "opus"}

    def _launch(self, prompt: str, cwd: str, model: str) -> bool:
        # Hazard-free autonomous build: skip permission prompts. A hazardous repo
        # would pass a tight --allowedTools instead (profile-driven, later).
        r = _run(["claude", "-p", prompt, "--model", model,
                  "--dangerously-skip-permissions"], cwd=cwd)
        return r.returncode == 0


class CodexRunner(_WorktreeRunner):
    tool = "codex"
    # TODO(verify): gpt-5.6-sol confirmed working; confirm the terra/luna IDs.
    MODELS = {Tier.LIGHT: "gpt-5.6-luna", Tier.STANDARD: "gpt-5.6-terra", Tier.DEEP: "gpt-5.6-sol"}

    def _launch(self, prompt: str, cwd: str, model: str) -> bool:
        r = _run(["codex", "exec", "-m", model, "--dangerously-bypass-approvals-and-sandbox",
                  "--skip-git-repo-check", prompt], cwd=cwd)
        return r.returncode == 0


def _iso_to_epoch(s: str) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
