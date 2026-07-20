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
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

_ACTIVE_WORKTREES: dict[str, int] = {}

_CLAUDE_SANDBOX_SETTINGS = {
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
}


def _claude_settings(deny_tools: tuple[str, ...] = ()) -> str:
    """Serialize the autonomous session settings, adding a ``permissions.deny`` block for the
    tools a read-only profile withholds. The allowlist (``--tools``) already removes those tools
    from the loaded surface; the deny block is the fail-closed backstop ADR 0044 pt 1 requires,
    not the mechanism."""
    settings = dict(_CLAUDE_SANDBOX_SETTINGS)
    if deny_tools:
        settings["permissions"] = {"deny": list(deny_tools)}
    return json.dumps(settings, separators=(",", ":"))


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


def _canonical_graph_project(cwd: str) -> str | None:
    """The maintained code-graph project identity for the repository this worktree belongs to.

    codebase-memory names a project by the real path of its checkout (``/a/b/c`` → ``a-b-c``) and
    resolves the active project from the caller's directory. A per-stage worktree therefore
    resolves to its *own* path — a separate, empty-or-stale project — not the maintained graph the
    operator keeps at the repository's main checkout. Resolve that shared main checkout from the
    worktree (the git common dir's parent) and derive its project name, so a session can target the
    maintained graph from whichever worktree it runs in. Repo- and provider-neutral: the same
    derivation names every fleet repository's graph, with no owner, path, or project id hardcoded.
    ``None`` when ``cwd`` is not inside a git checkout (there is no graph to name)."""
    common = _run(["git", "-C", cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"])
    if common.returncode != 0 or not common.stdout.strip():
        return None
    main_checkout = os.path.realpath(os.path.dirname(common.stdout.strip()))
    return main_checkout.strip("/").replace("/", "-") or None


def _bounded_prompt(prompt: str, cwd: str) -> str:
    """Tell the provider which checkout it owns and how to ground structural questions.

    The CLI sandbox enforces the worktree boundary; the appended discovery protocol points
    structural discovery at the repository's maintained main-checkout code graph instead of shell
    orientation, and is identical for both providers. When the worktree resolves to no graph
    identity the protocol is omitted and the session simply keeps searching files."""
    worktree = os.path.realpath(cwd)
    current = _run(["git", "-C", worktree, "branch", "--show-current"])
    branch = current.stdout.strip() if current.returncode == 0 else ""
    branch = branch or "detached HEAD"
    project = _canonical_graph_project(worktree)
    discovery = "" if project is None else f"""
Discovery protocol (ground structural questions in the code graph first):
- The maintained code graph for this repository is the codebase-memory project `{project}`. Pass
  `project={project}` to the codebase-memory tools — do not let them resolve the project from this
  worktree, which is a separate, empty copy.
- For any structural question — where a function/class/route is defined, who calls it, what it
  calls, the impact of a change, the shape of a module — query that graph first (search_graph,
  trace_path, get_code_snippet, get_architecture) before falling back to grep/find/ls orientation.
- Fall back to searching the worktree files when the graph is unavailable or has no result for a
  symbol, or when the question is about prose/config rather than code structure.
- Graph results name paths in the repository's main checkout. Use them only to orient; every file
  you read and every edit you make stays inside your assigned worktree above.
"""
    return f"""Session boundary (enforced by the launcher):
- Your assigned worktree is `{worktree}`.
- Your assigned branch is `{branch}`.
- Work only in that worktree and do not switch or create branches.
- Never use another checkout, even if an index, hook, or search result names one.
{discovery}
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

def _clamp_reasoning(level: str, ladder: tuple[str, ...]) -> str:
    """The reasoning rung this provider actually accepts for ``level``.

    A rung the provider's ladder does not carry clamps to the ladder's top rung rather than
    failing the launch (ADR 0046). Inert for today's models — both providers accept every rung
    the daemon maps — but defensive for a future model whose ladder stops short.
    """
    return level if level in ladder else ladder[-1]


def _write_output_schema(schema: dict) -> str:
    """Persist a provider-neutral result schema to a temp file for Codex's ``--output-schema``.

    Codex reads the schema from a file path (unlike Claude's inline flag), so the neutral
    schema is serialized outside the checkout — the launcher reads it, never the sandboxed
    session — and its path handed to the CLI. Small and short-lived, like the reviewer's
    verdict temp file.
    """
    fd, path = tempfile.mkstemp(prefix="agentflow-output-schema-", suffix=".json")
    with os.fdopen(fd, "w") as handle:
        json.dump(schema, handle)
    return path


def _operator_local_mcp_servers() -> dict:
    """The operator's user-scoped local MCP servers, read from ``~/.claude.json``.

    These are the deliberate local tools the operator configured — notably the codebase
    code-graph server that Build and Review sessions lean on. The personal claude.ai
    connectors (Gmail, Google Drive, Google Calendar) that #240 removes are *account*
    connectors: they are attached by the signed-in claude.ai account, never stored here.
    Re-supplying just this map under ``--strict-mcp-config`` therefore keeps the code-graph
    tool while the personal connectors stay dropped (#244).
    """
    try:
        data = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _codex_local_mcp_config(servers: dict) -> list[str]:
    """Render the operator's local MCP servers as Codex ``-c`` config overrides.

    Codex launches with ``--ignore-user-config`` (no personal MCP leaks in), which also drops the
    code-graph server Claude re-supplies via ``--mcp-config``. Codex takes MCP servers under the
    ``mcp_servers.<name>`` config table, so re-supply the *same* map here as dotted ``-c``
    overrides — applied on top of the ignored user config — giving both providers the code-graph
    tool from one source while the account connectors stay excluded. Each ``-c`` *value* is parsed
    as TOML: ``json.dumps`` renders strings and lists as valid TOML scalars/arrays, and env vars go
    in one key at a time so no inline table has to be hand-built. The server name is used verbatim
    as a dotted-key segment, which is correct for the fleet's bare-word (dash-separated) server
    names; a name containing a ``.`` would nest as sub-tables — not a shape any operator config
    uses, so it is left unescaped rather than guarded speculatively. A server without a ``command``
    is skipped; an empty map yields no overrides (nothing to attach)."""
    argv: list[str] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        command = spec.get("command")
        if not command:
            continue
        argv += ["-c", f"mcp_servers.{name}.command={json.dumps(command)}"]
        args = spec.get("args")
        if isinstance(args, list) and args:
            argv += ["-c", f"mcp_servers.{name}.args={json.dumps(args)}"]
        env = spec.get("env")
        if isinstance(env, dict):
            for key, value in env.items():
                argv += ["-c", f"mcp_servers.{name}.env.{key}={json.dumps(str(value))}"]
    return argv


class ClaudeRunner(_WorktreeRunner):
    tool = "claude"
    MODELS = {Complexity.STANDARD: "sonnet", Complexity.DEEP: "opus"}
    # Claude's reasoning ladder for ``--effort`` (ascending). ``max`` is manual-only (ADR 0046).
    _REASONING_LADDER = ("low", "medium", "high", "xhigh", "max")

    def structured_argv(self, prompt: str, model: str, cwd: str,
                        schema: dict | None = None, profile=None) -> list[str]:
        """Build the structured Claude command run only by the coordinator launcher.

        A ``schema`` (Intake's or Review's provider-neutral result contract) is wired to
        Claude's native ``--json-schema`` so the final response is validated structured
        output, not free text the parser must scavenge.

        ``--strict-mcp-config`` pins the session to only the MCP servers we hand it, so the
        operator's personal claude.ai connectors can never attach (#240). We then re-supply
        the operator's local dev servers — the codebase code-graph tool — so daemon sessions
        keep it (#244). With no local servers configured the MCP set is simply empty.

        A ``profile`` (ADR 0044) narrows the session to its stage: a read-only stage's
        allowlist is handed to ``--tools`` so the withheld edit tools are absent from the
        loaded surface (with a ``permissions.deny`` backstop in the settings), and the turn
        ceiling is handed to ``--max-turns``. The read-only allowlist is the research §3a
        read/search set plus the operator's re-supplied local servers (the code-graph tool):
        an exploration stage keeps the same code-graph access Build has — it is the withheld
        *edit* tools, not the local read-only MCP tools, that a read-only stage loses.

        A build/revise profile also carries a reasoning-effort rung (ADR 0046), handed to Claude's
        first-class ``--effort`` flag; every other stage leaves it ``None`` (provider default). A
        rung above Claude's ladder clamps to its top rather than failing the launch.
        """
        from agentflow.coordinator.profiles import WITHHELD_EDIT_TOOLS

        deny: tuple[str, ...] = ()
        argv = ["claude", "-p", _bounded_prompt(prompt, cwd), "--model", model,
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits", "--setting-sources", "project",
                "--strict-mcp-config"]
        servers = _operator_local_mcp_servers()
        if servers:
            argv += ["--mcp-config",
                     json.dumps({"mcpServers": servers}, separators=(",", ":"))]
        if profile is not None:
            if profile.allowed_tools is not None:
                tools = list(profile.allowed_tools)
                tools += [f"mcp__{name}" for name in servers]
                argv += ["--tools", ",".join(tools)]
                deny = WITHHELD_EDIT_TOOLS
            argv += ["--max-turns", str(profile.turn_ceiling)]
            if profile.reasoning_effort is not None:
                argv += ["--effort",
                         _clamp_reasoning(profile.reasoning_effort, self._REASONING_LADDER)]
        argv += ["--settings", _claude_settings(deny)]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
        return argv


class CodexRunner(_WorktreeRunner):
    tool = "codex"
    # TODO(verify): gpt-5.6-sol confirmed working; confirm the terra ID.
    MODELS = {Complexity.STANDARD: "gpt-5.6-terra", Complexity.DEEP: "gpt-5.6-sol"}

    _CLI_MODEL = {"sol": "gpt-5.6-sol", "terra": "gpt-5.6-terra"}
    # Codex's reasoning ladder for ``model_reasoning_effort`` (ascending). ``max`` is manual-only
    # (ADR 0046); the daemon never maps below ``low``, so the sub-``low`` rungs are inert here.
    _REASONING_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    def structured_argv(self, prompt: str, model: str, cwd: str,
                        schema: dict | None = None, profile=None) -> list[str]:
        """Build the structured Codex command run only by the coordinator launcher.

        A ``schema`` (Intake's or Review's provider-neutral result contract) is wired to
        Codex's native ``--output-schema`` file so the final response is validated structured
        output, not free text the parser must scavenge.

        A read-only ``profile`` (ADR 0044) launches the session in the ``read-only`` Codex
        sandbox so it cannot edit the checkout — Codex's own shape of the read-only stage
        surface (there is no Claude-style per-tool allowlist flag). Codex runs
        ``--ignore-user-config`` so no personal MCP leaks in, which also drops the code-graph
        server; we re-supply *only* that operator-local map as ``mcp_servers`` ``-c`` overrides,
        so both providers get the code graph from one source while the account connectors stay
        excluded, on every stage including read-only ones. The wall ceiling is applied per-record
        by the launcher, the same as for Claude.

        A build/revise profile also carries a reasoning-effort rung (ADR 0046). Codex has no
        ``--effort`` flag — reasoning effort is a config override — so it is appended as another
        ``-c model_reasoning_effort=<level>`` alongside the existing ``-c`` overrides, before the
        positional prompt. Every other stage leaves it ``None`` (provider default); a rung above
        Codex's ladder clamps to its top rather than failing the launch.
        """
        codex_bin = os.environ.get("AGENTFLOW_CODEX_BIN", "codex")
        worktree = os.path.realpath(cwd)
        common = _run(["git", "-C", worktree, "rev-parse", "--path-format=absolute",
                       "--git-common-dir"])
        writable_roots = json.dumps([common.stdout.strip()]) if common.returncode == 0 else "[]"
        approval_policy = (
            "approval_policy={granular={sandbox_approval=true,rules=false,"
            "mcp_elicitations=false,request_permissions=false,skill_approval=false}}"
        )
        read_only = profile is not None and profile.allowed_tools is not None
        sandbox = "read-only" if read_only else "workspace-write"
        argv = [codex_bin, "exec", "-m", self._CLI_MODEL.get(model, model), "--json",
                "--sandbox", sandbox, "--cd", worktree,
                "--ignore-user-config", "--ephemeral", "-c", approval_policy,
                "-c", 'approvals_reviewer="auto_review"',
                "-c", f"auto_review.policy={json.dumps(_CODEX_AUTO_REVIEW_POLICY)}",
                "-c", "sandbox_workspace_write.network_access=true",
                "-c", f"sandbox_workspace_write.writable_roots={writable_roots}",
                "--skip-git-repo-check"]
        argv += _codex_local_mcp_config(_operator_local_mcp_servers())
        if profile is not None and profile.reasoning_effort is not None:
            level = _clamp_reasoning(profile.reasoning_effort, self._REASONING_LADDER)
            argv += ["-c", f"model_reasoning_effort={level}"]
        if schema is not None:
            argv += ["--output-schema", _write_output_schema(schema)]
        argv.append(_bounded_prompt(_CODEX_HEADLESS_RECOVERY + prompt, cwd))
        return argv

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
