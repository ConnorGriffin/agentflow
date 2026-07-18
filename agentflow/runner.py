"""Provider command and worktree adapters for the session coordinator.

The coordinator is the only launch owner. Runners provide model mapping, structured command
construction, provisioning, and git plumbing; they do not orchestrate a Build lifecycle.

Ported and generalized from the dotfiles `codex-go` wrapper (Codex-only) into a
two-tool abstraction — the "unified runner" the reuse map flagged as net-new.

The interface is command construction, model resolution, and fail-closed worktree plumbing,
tested without spawning a provider (see tests/test_runner.py).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_ACTIVE_WORKTREES: dict[str, int] = {}

_CLAUDE_AUTONOMOUS_SETTINGS = json.dumps({
    "sandbox": {
        "enabled": True,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True,
        "enableWeakerNetworkIsolation": True,
        "network": {
            "allowedDomains": [
                "github.com",
                "api.github.com",
                "ssh.github.com",
                "*.githubusercontent.com",
                "registry.npmjs.org",
                "pypi.org",
                "files.pythonhosted.org",
            ],
        },
    },
}, separators=(",", ":"))

_CODEX_HEADLESS_RECOVERY = """
Codex-only macOS browser recovery: when the shared drive-local-webapp driver prints
HEADLESS-SANDBOX-BLOCKED, rerun that same driver command with
sandbox_permissions=require_escalated and continue the required screenshot capture. The launcher
can review this narrow request unattended. Keep the app server and every other command inside the
workspace sandbox. Do not replace required UI evidence with a written explanation.
"""

_CODEX_AUTO_REVIEW_POLICY = (
    "Approve sandbox escalation only for a Node command launching the shared "
    "drive-local-webapp/driver.mjs browser driver to capture required UI evidence. "
    "Reject every other sandbox escalation and every request to weaken the session sandbox."
)


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


@dataclass(frozen=True, slots=True)
class WorktreeRecovery:
    """What a recovery pass changed and which owned sessions it left for recovery."""

    removed: tuple[str, ...]
    retained: tuple[str, ...]


def _run(cmd: list[str], cwd: str | None = None, timeout: int | None = None) -> subprocess.CompletedProcess:
    if cmd and Path(str(cmd[0])).name in {"claude", "codex"}:
        raise RuntimeError("provider commands may only be executed by the coordinator launcher")
    t = timeout if timeout is not None else int(os.environ.get("AGENTFLOW_GH_TIMEOUT", "120"))
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=t)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=f"timed out after {t}s")


def _bounded_prompt(prompt: str, cwd: str) -> str:
    """Tell the provider which checkout it owns; the CLI sandbox enforces the same boundary."""
    worktree = os.path.realpath(cwd)
    current = _run(["git", "-C", worktree, "branch", "--show-current"])
    branch = current.stdout.strip() if current.returncode == 0 else ""
    branch = branch or "detached HEAD"
    return f"""Session boundary (enforced by the launcher):
- Your assigned worktree is `{worktree}`.
- Your assigned branch is `{branch}`.
- Work only in that worktree and do not switch or create branches.
- Never use another checkout, even if an index, hook, or search result names one.

{prompt}"""


def _active_marker(wt: Path) -> Path | None:
    r = _run(["git", "-C", str(wt), "rev-parse", "--git-path", "agentflow-active"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    marker = Path(r.stdout.strip())
    return marker if marker.is_absolute() else wt / marker


def _pid_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


def _worktree_is_active(wt: Path) -> bool:
    path = os.path.realpath(wt)
    if _ACTIVE_WORKTREES.get(path, 0):
        return True
    marker = _active_marker(wt)
    if marker is None:
        return False
    try:
        return _pid_is_alive(int(marker.read_text().strip()))
    except (OSError, ValueError):
        return False


def _worktree_is_disposable(workdir: str, wt: Path) -> bool:
    target = os.path.realpath(wt)
    main = os.path.realpath(workdir)
    if target == main:
        return False
    if _worktree_is_active(wt):
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
    return remote_refs.returncode == 0 and bool(remote_refs.stdout.strip())


def discard_orphaned_worktree(workdir: str, wt: Path) -> None:
    """Discard a stale directory that occupies a worktree path but is not a
    registered worktree, then prune dangling metadata so a fresh checkout can be
    added there.

    A registered worktree — which may hold committed or in-progress work — is
    never discarded this way; callers must have already established that git
    tracks no worktree at ``wt`` (its metadata is gone/orphaned), so nothing
    durable is lost. Pruning also frees any same-basename metadata whose own
    directory is already gone, which otherwise blocks the ``worktree add``.
    """
    shutil.rmtree(wt, ignore_errors=True)
    _run(["git", "-C", workdir, "worktree", "prune"])


def remove_worktree_if_safe(workdir: str, wt: Path) -> bool:
    """Remove a finished session only when Git proves all progress is durable.

    The target must be a registered worktree owned by ``workdir``, clean, and at
    a commit reachable from ``origin``. Unknown state fails closed. The force flag
    removes ignored provisioning files only after those checks have passed.
    """
    if not _worktree_is_disposable(workdir, wt):
        return False
    removed = _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    return removed.returncode == 0


@contextmanager
def worktree_session(wt: Path):
    """Mark local git work active so an overlapping worktree cleanup retains it.

    This marker is local recovery evidence only. The live-session console file is generated
    separately from durable coordinator running records.
    """
    path = os.path.realpath(wt)
    marker = _active_marker(wt)
    _ACTIVE_WORKTREES[path] = _ACTIVE_WORKTREES.get(path, 0) + 1
    if marker is not None:
        marker.write_text(str(os.getpid()))
    try:
        yield
    finally:
        remaining = _ACTIVE_WORKTREES.get(path, 1) - 1
        if remaining:
            _ACTIVE_WORKTREES[path] = remaining
        else:
            _ACTIVE_WORKTREES.pop(path, None)
            if marker is not None:
                try:
                    marker.unlink()
                except OSError:
                    pass


class _WorktreeRunner:
    """Shared provider/worktree plumbing; subclasses supply tool commands and model maps."""

    tool: str = "?"
    MODELS: dict[Complexity, str] = {}

    def model_for(self, complexity: Complexity) -> str:
        """Resolve a tool-agnostic complexity to this tool's concrete model."""
        return self.MODELS[complexity]

    # --- shared git/gh plumbing used by coordinated stage preparation ------------------------
    def prepare_worktree(self, workdir: str, branch: str, wt: Path,
                         repo: str | None = None) -> None:
        _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).check_returncode()
        if wt.exists():
            if not _worktree_is_registered(workdir, wt):
                raise subprocess.CalledProcessError(1, ["git", "worktree", "list"])
            verified, pr_url = self._open_pr_for_branch(repo, branch) if repo else (False, None)
            if not verified:
                raise subprocess.CalledProcessError(1, ["gh", "pr", "list"])
            if pr_url:
                return
            if not remove_worktree_if_safe(workdir, wt):
                raise subprocess.CalledProcessError(1, ["git", "worktree", "remove"])
            _run(["git", "-C", workdir, "branch", "-f", branch, "origin/main"]).check_returncode()
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

        A directory that exists on disk but is *not* a registered worktree is
        orphaned — its git metadata was lost (e.g. a daemon killed mid-prepare).
        Git holds no state for it and ``worktree add`` would fail on the existing
        dir every cycle, so it is discarded and rebuilt. A *registered* worktree
        is never force-discarded here: a non-disposable one still fails closed.
        """
        _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).check_returncode()
        if wt.exists():
            if _worktree_is_registered(workdir, wt):
                if not _worktree_is_disposable(workdir, wt):
                    raise subprocess.CalledProcessError(1, ["git", "status", "--porcelain"])
                # Freshen a reused worktree to the (possibly moved) ref — otherwise a
                # re-review after a revise push would inspect a stale checkout.
                _run(["git", "-C", str(wt), "reset", "--hard", ref]).check_returncode()
                _run(["git", "-C", str(wt), "clean", "-fdx"])
                return
            discard_orphaned_worktree(workdir, wt)
        wt.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "-C", workdir, "worktree", "add", "--detach", str(wt), ref]).check_returncode()

    def _open_pr_for_branch(self, repo: str, branch: str) -> tuple[bool, str | None]:
        r = _run(["gh", "pr", "list", "--repo", repo, "--head", branch,
                  "--state", "open", "--json", "url", "-q", ".[0].url // \"\""])
        return r.returncode == 0, r.stdout.strip() or None

class ClaudeRunner(_WorktreeRunner):
    tool = "claude"
    MODELS = {Complexity.STANDARD: "sonnet", Complexity.DEEP: "opus"}

    def structured_argv(self, prompt: str, model: str, cwd: str) -> list[str]:
        """Build the structured Claude command run only by the coordinator launcher."""
        return ["claude", "-p", _bounded_prompt(prompt, cwd), "--model", model,
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits", "--setting-sources", "project",
                "--settings", _CLAUDE_AUTONOMOUS_SETTINGS]


class CodexRunner(_WorktreeRunner):
    tool = "codex"
    # TODO(verify): gpt-5.6-sol confirmed working; confirm the terra ID.
    MODELS = {Complexity.STANDARD: "gpt-5.6-terra", Complexity.DEEP: "gpt-5.6-sol"}

    _CLI_MODEL = {"sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra"}

    def structured_argv(self, prompt: str, model: str, cwd: str) -> list[str]:
        """Build the structured Codex command run only by the coordinator launcher."""
        codex_bin = os.environ.get("AGENTFLOW_CODEX_BIN", "codex")
        worktree = os.path.realpath(cwd)
        common = _run(["git", "-C", worktree, "rev-parse", "--path-format=absolute",
                       "--git-common-dir"])
        writable_roots = json.dumps([common.stdout.strip()]) if common.returncode == 0 else "[]"
        approval_policy = (
            "approval_policy={granular={sandbox_approval=true,rules=false,"
            "mcp_elicitations=false,request_permissions=false,skill_approval=false}}"
        )
        return [codex_bin, "exec", "-m", self._CLI_MODEL.get(model, model), "--json",
                "--sandbox", "workspace-write", "--cd", worktree,
                "--ignore-user-config", "--ephemeral", "-c", approval_policy,
                "-c", 'approvals_reviewer="auto_review"',
                "-c", f"auto_review.policy={json.dumps(_CODEX_AUTO_REVIEW_POLICY)}",
                "-c", "sandbox_workspace_write.network_access=true",
                "-c", f"sandbox_workspace_write.writable_roots={writable_roots}",
                "--skip-git-repo-check", _bounded_prompt(_CODEX_HEADLESS_RECOVERY + prompt, cwd)]

    def account_fact(self) -> dict | None:
        """Read the existing typed Codex limit companion. It establishes capacity only when a
        reported window is exhausted; unavailable or unsupported account diagnoses remain
        ``None`` so provider prose can never be promoted into a cause (ADR 0030)."""
        gate = os.environ.get(
            "AGENTFLOW_TRIAGE_GATE",
            os.path.expanduser("~/Code/ConnorGriffin/dotfiles/scripts/triage-gate.sh"))
        try:
            result = subprocess.run(
                [gate, "limits"], env={**os.environ, "TRIAGE_AGENT": "codex"},
                text=True, capture_output=True, timeout=30)
            if result.returncode != 0:
                return None
            payload = json.loads(result.stdout)
            windows = payload.get("windows")
            if not isinstance(windows, list):
                return None
            exhausted = [window for window in windows
                         if isinstance(window, dict)
                         and isinstance(window.get("used_percent"), (int, float))
                         and not isinstance(window.get("used_percent"), bool)
                         and window["used_percent"] >= 100
                         and isinstance(window.get("resets_at"), (int, float))
                         and not isinstance(window.get("resets_at"), bool)]
            if exhausted:
                return {"kind": "rate_limited",
                        "reset_at": min(window["resets_at"] for window in exhausted)}
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError):
            return None
        return None

def _pr_state_for_branch(repo: str, branch: str) -> str | None:
    """The current state of the most recent PR for this branch across all states
    (OPEN, MERGED, or CLOSED), or None when no PR has ever been opened for it."""
    r = _run(["gh", "pr", "list", "--repo", repo, "--head", branch,
              "--state", "all", "--json", "state", "-q", ".[0].state // \"\""])
    return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else None


def _pr_info(repo: str, pr: int) -> dict | None:
    r = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "state,comments"])
    if r.returncode != 0:
        return None
    try:
        value = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


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
        info = _pr_info(repo, int(pr_match.group(1)))
        if info is None:
            return False
        return info.get("state") in ("MERGED", "CLOSED") or any(
            "agentflow: parked for human review" in comment.get("body", "")
            for comment in info.get("comments", []) if isinstance(comment, dict))

    if lane not in ("claude", "codex") or branch is None:
        return False
    mockup = re.fullmatch(rf"agentflow/{lane}/mockup-(\d+)-.+", branch)
    if mockup and name == branch.rsplit("/", 1)[-1]:
        state = _issue_state(repo, int(mockup.group(1)))
        if state is None:
            return False
        labels = {label.get("name") for label in state.get("labels", [])
                  if isinstance(label, dict)}
        if "agentflow:drawing-mockup" in labels:
            return False
        return any("mockup variants" in comment.get("body", "")
                   for comment in state.get("comments", []) if isinstance(comment, dict))
    current = re.fullmatch(rf"agentflow/{lane}/issue-(\d+)-.+", branch)
    legacy = re.fullmatch(rf"{lane}/[^/]+", branch)
    if current and name == branch.rsplit("/", 1)[-1]:
        state = _issue_state(repo, int(current.group(1)))
        if state is None:
            return False
        labels = {label.get("name") for label in state.get("labels", [])
                  if isinstance(label, dict)}
        if "agentflow:building" in labels:
            return False
        return _pr_state_for_branch(repo, branch) in ("OPEN", "MERGED", "CLOSED")
    if legacy and name == branch.rsplit("/", 1)[-1]:
        return _pr_state_for_branch(repo, branch) in ("OPEN", "MERGED", "CLOSED")
    return False


def recover_stale_worktrees(repo: str, workdir: str,
                            protected: set[str] = frozenset()) -> WorktreeRecovery:
    """Prune stale registrations and remove completed agentflow-owned sessions.

    Git's registry establishes repository ownership; the path is used only after
    ownership is known to recognize agentflow's current and legacy session names.
    Completion lookups and the final clean/pushed checks all fail closed. ``protected`` contains
    durable coordinator-owned sources; recovery retains them even when they are clean and no
    provider is currently alive, because a waiting continuation still owns that worktree.
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
        if owned_path in protected:
            retained.append(path)
            continue
        if _worktree_is_active(Path(path)):
            retained.append(path)
            continue
        if not _completed_agentflow_session(repo, lane, name, branch):
            if lane.startswith(("claude", "codex")):
                retained.append(path)
            continue
        if remove_worktree_if_safe(workdir, Path(path)):
            removed.append(path)
        else:
            retained.append(path)
    return WorktreeRecovery(tuple(removed), tuple(retained))
