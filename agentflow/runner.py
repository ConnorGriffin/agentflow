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
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Stable bail markers a session posts as a comment when it hits a gap (ADR 0005).
MARKERS = ("MISSING-CONTEXT", "SCOPE-EXPANSION", "INTEGRATION-COLLISION")
_MARKER_RE = re.compile(rf"^({'|'.join(MARKERS)}):")


class Complexity(str, Enum):
    """The model-size dial intake stamps per issue (ADR 0018). Tool-agnostic; each
    adapter maps it to a concrete model. A hard gate — the deep tier burns rate-limit
    headroom fastest, so mis-sizing wastes the very resource ADR 0006 optimizes.
    """

    STANDARD = "standard"  # ordinary features, moderate logic → sonnet/Terra
    DEEP = "deep"          # correctness-sensitive, design-heavy → opus/Sol


class Effort(str, Enum):
    """The second dial intake stamps (ADR 0018): how much work the issue warrants,
    independent of model size. Carried into the build brief as guidance."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    EXTRA = "extra"


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
    complexity: Complexity  # model-size dial from intake — no build without one
    effort: Effort   # effort dial from intake — how much work the issue warrants
    prompt: str      # the work order / self-scoped brief handed to the agent


@dataclass(frozen=True, slots=True)
class BuildOutcome:
    status: BuildStatus
    pr_url: str | None = None
    marker: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class WorktreeRecovery:
    """What a recovery pass changed and which owned sessions it left for recovery."""

    removed: tuple[str, ...]
    retained: tuple[str, ...]


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


def _run(cmd: list[str], cwd: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    t = timeout if timeout is not None else int(os.environ.get("AGENTFLOW_GH_TIMEOUT", "120"))
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=f"timed out after {t}s")


def remove_worktree_if_safe(workdir: str, wt: Path) -> bool:
    """Remove a finished session only when Git proves all progress is durable.

    The target must be a registered worktree owned by ``workdir``, clean, and at
    a commit reachable from ``origin``. Unknown state fails closed. The force flag
    removes ignored provisioning files only after those checks have passed.
    """
    target = os.path.realpath(wt)
    main = os.path.realpath(workdir)
    if target == main:
        return False
    if not _worktree_is_registered(workdir, wt):
        return False
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if status.returncode != 0 or status.stdout.strip():
        return False
    head = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    if head.returncode != 0 or not head.stdout.strip():
        return False
    remote_refs = _run(["git", "-C", workdir, "for-each-ref", "--contains",
                        head.stdout.strip(), "--format=%(refname)", "refs/remotes/origin/"])
    if remote_refs.returncode != 0 or not remote_refs.stdout.strip():
        return False
    removed = _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    return removed.returncode == 0


class _WorktreeRunner:
    """Shared build orchestration; subclasses supply `tool`, `MODELS`, `_launch`."""

    tool: str = "?"
    MODELS: dict[Complexity, str] = {}

    def model_for(self, complexity: Complexity) -> str:
        """Resolve a tool-agnostic complexity to this tool's concrete model."""
        return self.MODELS[complexity]

    def build(self, task: BuildTask) -> BuildOutcome:
        branch = f"agentflow/{self.tool}/issue-{task.issue}-{task.slug}"
        wt = Path(task.workdir) / ".agentflow" / "worktrees" / self.tool / f"issue-{task.issue}-{task.slug}"
        try:
            self.prepare_worktree(task.workdir, branch, wt, task.repo)
            self.provision(wt)
        except subprocess.CalledProcessError as e:
            return BuildOutcome(BuildStatus.ERROR, detail=f"worktree/provision failed: {e}")

        started = time.time()
        launched, _ = self.launch(task.prompt, cwd=str(wt), model=self.model_for(task.complexity))

        pr_url = self._pr_for_branch(task.repo, branch)
        markers = self._new_marker_comments(task.repo, task.issue, since=started)
        outcome = classify_build(pr_url, markers)
        if outcome.status is BuildStatus.INCOMPLETE and not launched:
            return BuildOutcome(BuildStatus.ERROR, detail="launch exited non-zero, no PR, no marker")
        if outcome.status is BuildStatus.PR_OPENED:
            self.remove_worktree_if_safe(task.workdir, wt)
        return outcome

    # --- shared git/gh plumbing (reused by the reviewer) ------------------------
    def prepare_worktree(self, workdir: str, branch: str, wt: Path,
                         repo: str | None = None) -> None:
        _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).check_returncode()
        if wt.exists():
            if not _worktree_is_registered(workdir, wt):
                raise subprocess.CalledProcessError(1, ["git", "worktree", "list"])
            if repo and not self._pr_for_branch(repo, branch):
                # Reused worktree with no open PR — reset onto origin/main so stale
                # branch state doesn't pollute the new build.
                _run(["git", "-C", str(wt), "reset", "--hard", "origin/main"]).check_returncode()
                _run(["git", "-C", str(wt), "clean", "-fdx"])
            return
        wt.parent.mkdir(parents=True, exist_ok=True)
        have_branch = _run(["git", "-C", workdir, "show-ref", "--quiet", f"refs/heads/{branch}"]).returncode == 0
        add = ["git", "-C", workdir, "worktree", "add"]
        add += [str(wt), branch] if have_branch else ["-b", branch, str(wt), "origin/main"]
        _run(add).check_returncode()

    def provision(self, wt: Path) -> None:
        if (wt / "uv.lock").exists() and not (wt / ".venv" / "bin" / "python").exists():
            _run(["uv", "sync", "--all-extras"], cwd=str(wt)).check_returncode()

    def prepare_worktree_detached(self, workdir: str, ref: str, wt: Path) -> None:
        """A detached worktree at `ref` (e.g. `origin/<pr-branch>`) — for review.

        Detached avoids the "branch already checked out" collision with the
        builder's worktree, which still holds the PR branch.
        """
        _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).check_returncode()
        if wt.exists():
            if not _worktree_is_registered(workdir, wt):
                raise subprocess.CalledProcessError(1, ["git", "worktree", "list"])
            # Freshen a reused worktree to the (possibly moved) ref — otherwise a
            # re-review after a revise push would inspect a stale checkout.
            _run(["git", "-C", str(wt), "reset", "--hard", ref]).check_returncode()
            _run(["git", "-C", str(wt), "clean", "-fdx"])
            return
        wt.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "-C", workdir, "worktree", "add", "--detach", str(wt), ref]).check_returncode()

    def remove_worktree_if_safe(self, workdir: str, wt: Path) -> bool:
        return remove_worktree_if_safe(workdir, wt)

    def _pr_for_branch(self, repo: str, branch: str) -> str | None:
        r = _run(["gh", "pr", "list", "--repo", repo, "--head", branch,
                  "--state", "open", "--json", "url", "-q", ".[0].url // \"\""])
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

    def launch(self, prompt: str, cwd: str, model: str) -> tuple[bool, str]:
        """Run a session; return (ok, final_message). The message is captured by
        us — used by the reviewer to read the verdict without trusting a
        model-written file in the (untrusted) PR tree."""
        raise NotImplementedError


class ClaudeRunner(_WorktreeRunner):
    tool = "claude"
    MODELS = {Complexity.STANDARD: "sonnet", Complexity.DEEP: "opus"}

    def launch(self, prompt: str, cwd: str, model: str) -> tuple[bool, str]:
        # Hazard-free autonomous build: skip permission prompts. A hazardous repo
        # would pass a tight --allowedTools instead (profile-driven, later).
        # `claude -p` prints the final assistant message to stdout — that's the message.
        session_timeout = int(os.environ.get("AGENTFLOW_SESSION_TIMEOUT", str(2 * 3600)))
        r = _run(["claude", "-p", prompt, "--model", model,
                  "--dangerously-skip-permissions"], cwd=cwd, timeout=session_timeout)
        return r.returncode == 0, r.stdout


class CodexRunner(_WorktreeRunner):
    tool = "codex"
    # TODO(verify): gpt-5.6-sol confirmed working; confirm the terra ID.
    MODELS = {Complexity.STANDARD: "gpt-5.6-terra", Complexity.DEEP: "gpt-5.6-sol"}

    def launch(self, prompt: str, cwd: str, model: str) -> tuple[bool, str]:
        # `-o <file>` writes Codex's final message to a file we control.
        # AGENTFLOW_CODEX_BIN overrides the binary — needed when the PATH `codex`
        # is missing its `codex-code-mode-host` companion (e.g. an incomplete
        # Homebrew cask) and can't run shell commands.
        codex_bin = os.environ.get("AGENTFLOW_CODEX_BIN", "codex")
        session_timeout = int(os.environ.get("AGENTFLOW_SESSION_TIMEOUT", str(2 * 3600)))
        fd, outfile = tempfile.mkstemp(prefix="agentflow-codex-")
        os.close(fd)
        try:
            r = _run([codex_bin, "exec", "-m", model, "--dangerously-bypass-approvals-and-sandbox",
                      "--skip-git-repo-check", "-o", outfile, prompt], cwd=cwd, timeout=session_timeout)
            try:
                msg = Path(outfile).read_text()
            except OSError:
                msg = r.stdout
            return r.returncode == 0, msg
        finally:
            Path(outfile).unlink(missing_ok=True)


def _iso_to_epoch(s: str) -> float | None:
    from datetime import datetime
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _pr_state_for_branch(repo: str, branch: str) -> str | None:
    """The current state of the most recent PR for this branch across all states
    (OPEN, MERGED, or CLOSED), or None when no PR has ever been opened for it."""
    r = _run(["gh", "pr", "list", "--repo", repo, "--head", branch,
              "--state", "all", "--json", "state", "-q", ".[0].state // \"\""])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _pr_state(repo: str, pr: int) -> str | None:
    r = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "state", "-q", ".state"])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _issue_state(repo: str, issue: int) -> dict | None:
    r = _run(["gh", "issue", "view", str(issue), "--repo", repo,
              "--json", "state,labels,comments"])
    if r.returncode != 0:
        return None
    try:
        value = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _registered_worktrees(workdir: str) -> list[tuple[str, str | None]] | None:
    listed = _run(["git", "-C", workdir, "worktree", "list", "--porcelain", "-z"])
    if listed.returncode != 0:
        return None
    records: list[tuple[str, str | None]] = []
    for raw in listed.stdout.split("\0\0"):
        fields = raw.strip("\0").split("\0")
        path = next((f.removeprefix("worktree ") for f in fields if f.startswith("worktree ")), "")
        branch = next((f.removeprefix("branch refs/heads/") for f in fields
                       if f.startswith("branch refs/heads/")), None)
        if path:
            records.append((path, branch))
    return records


def _worktree_is_registered(workdir: str, wt: Path) -> bool:
    registered = _registered_worktrees(workdir)
    if registered is None:
        return False
    target = os.path.realpath(wt)
    return any(os.path.realpath(path) == target for path, _ in registered)


def _completed_agentflow_session(repo: str, lane: str, name: str,
                                 branch: str | None) -> bool:
    intake = re.fullmatch(r"(?:claude|codex)-intake", lane)
    issue_match = re.fullmatch(r"issue-(\d+)", name)
    if intake and issue_match and branch is None:
        state = _issue_state(repo, int(issue_match.group(1)))
        if state is None:
            return False
        labels = {label.get("name") for label in state.get("labels", [])
                  if isinstance(label, dict)}
        return (state.get("state") == "CLOSED" or bool(labels & {
            "ready-for-agent", "agentflow:needs-grilling", "agentflow:needs-mockup"
        })) and "agentflow:triaging" not in labels

    review = re.fullmatch(r"(?:claude|codex)-review", lane)
    pr_match = re.fullmatch(r"pr-(\d+)-.+", name)
    if review and pr_match and branch is None:
        return _pr_state(repo, int(pr_match.group(1))) in ("MERGED", "CLOSED")

    if lane not in ("claude", "codex") or branch is None:
        return False
    mockup = re.fullmatch(rf"agentflow/{lane}/mockup-(\d+)-.+", branch)
    if mockup and name == branch.rsplit("/", 1)[-1]:
        state = _issue_state(repo, int(mockup.group(1)))
        if state is None:
            return False
        return any("mockup variants" in comment.get("body", "")
                   for comment in state.get("comments", []) if isinstance(comment, dict))
    current = re.fullmatch(rf"agentflow/{lane}/issue-\d+-.+", branch)
    legacy = re.fullmatch(rf"{lane}/[^/]+", branch)
    if ((current and name == branch.rsplit("/", 1)[-1]) or
            (legacy and name == branch.rsplit("/", 1)[-1])):
        return _pr_state_for_branch(repo, branch) in ("OPEN", "MERGED", "CLOSED")
    return False


def recover_stale_worktrees(repo: str, workdir: str) -> WorktreeRecovery:
    """Prune stale registrations and remove completed agentflow-owned sessions.

    Git's registry establishes repository ownership; the path is used only after
    ownership is known to recognize agentflow's current and legacy session names.
    Completion lookups and the final clean/pushed checks all fail closed.
    """
    _run(["git", "-C", workdir, "worktree", "prune"])
    registered = _registered_worktrees(workdir)
    if registered is None:
        return WorktreeRecovery((), ())
    root = os.path.realpath(Path(workdir) / ".agentflow" / "worktrees")
    removed: list[str] = []
    retained: list[str] = []
    for path, branch in registered:
        owned_path = os.path.realpath(path)
        try:
            relative = Path(owned_path).relative_to(root)
        except ValueError:
            continue
        if len(relative.parts) != 2:
            continue
        lane, name = relative.parts
        if not _completed_agentflow_session(repo, lane, name, branch):
            if lane.startswith(("claude", "codex")):
                retained.append(path)
            continue
        if remove_worktree_if_safe(workdir, Path(path)):
            removed.append(path)
        else:
            retained.append(path)
    return WorktreeRecovery(tuple(removed), tuple(retained))
