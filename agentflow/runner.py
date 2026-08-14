"""Provider command and worktree adapters for the session coordinator.

The coordinator is the only launch owner. Runners provide model mapping, structured command
construction, provisioning, and git plumbing; they do not orchestrate a Build lifecycle.

Ported and generalized from a private Codex-only wrapper into a
two-tool abstraction — the "unified runner" the reuse map flagged as net-new.

The interface is command construction, model resolution, and fail-closed worktree plumbing,
tested without spawning a provider (see tests/test_runner.py).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from agentflow.worktree_ref import WorktreeKind, WorktreeRef
from agentflow.work_classification import Complexity, Effort, MockupScope

_ACTIVE_WORKTREES: dict[str, int] = {}
_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_CHARTER_SOURCE_PATH = _SOURCE_ROOT / "standards" / "CHARTER.md"
_SOURCE_CHECKOUT_MARKER = _SOURCE_ROOT / ".git"

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
                "chatgpt.com",
                "*.chatgpt.com",
                "auth.openai.com",
                "api.openai.com",
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
    "Approve sandbox escalation only for either a Node command launching the shared "
    "drive-local-webapp/driver.mjs browser driver to capture required UI evidence, or a bounded "
    "worker request in either of exactly two forms: the bare worker command or its standard "
    "launcher-owned `/bin/zsh -lc '<bounded-worker-command>'` envelope. For the worker, "
    "the shell program must contain exactly one `agentflow-codex-worker` command in this order: "
    "`agentflow-codex-worker --worker <routed-allowlisted-name> --effort <allowlisted-effort> "
    "--timeout <1-through-900> < \"<absolute-private-prompt-file>\"`. The worker name must be "
    "routed and allowlisted, effort must be one of `low`, `medium`, `high`, or `extra`, and "
    "timeout must be an integer from 1 through 900. The only shell syntax allowed inside the "
    "launcher-owned envelope is one stdin `<` redirection from a quoted literal absolute path "
    "whose existing target is owned by the session user and is a private regular file with mode "
    "exactly 0600. Reject authored shell wrappers, chains, command separators, pipelines, "
    "subshells, substitutions, expansions, environment assignments, setup or cleanup commands, "
    "extra commands, extra arguments, reordered arguments, and any other shell segments. Reject "
    "bare `codex` invocations, unallowlisted workers, invalid effort, invalid timeout, unsafe stdin, "
    "worker requests without that private-file redirection, and sandbox-weakening flags. Reject "
    "worker shapes such as `/bin/bash -lc '<bounded-worker-command>'`, "
    "`<bounded-worker-command> && <extra-command>`, "
    "`agentflow-codex-worker --worker <routed-allowlisted-name> --effort medium --timeout 900 "
    "--extra < \"<mode-0600-file>\"`, `--worker <unallowlisted-worker>`, `--effort max`, "
    "`--timeout 901`, and stdin from `<mode-0644-file>`. Reject every other sandbox escalation "
    "and every request to weaken the session sandbox, including "
    "`--dangerously-bypass-approvals-and-sandbox`."
)


@dataclass(frozen=True, slots=True)
class WorktreeRecovery:
    """What a recovery pass changed and which owned sessions it left for recovery.

    ``archived`` carries ``(path, stranded-ref)`` for each session whose uncommitted state was
    snapshotted to a durable ref before its checkout was reclaimed. The ref travels with the path
    because the path is exactly what stops existing: it is the only thing that lets an operator —
    or the hold comment that named that directory — find the work again.
    """

    removed: tuple[str, ...]
    retained: tuple[str, ...]
    archived: tuple[tuple[str, str], ...] = ()


# How long a stranded session must sit untouched before reclamation may archive it. This floor is
# the *whole* protection for a checkout with no session marker — a `/agentflow pickup` session or a
# hand-cut worktree under `.agentflow/worktrees` — because none of the clocks below move for an
# editor writing nested files without running git. It is not a belt on top of a real activity
# signal; do not shorten it on the assumption that it is.
STRANDED_IDLE_SECONDS = 24 * 3600

# How many idle, unprotected, agentflow-owned sessions a repository keeps registered. The newest
# survive; everything older is archived to a stranded ref and reclaimed, because every surviving
# registration costs a deny path in the provider sandbox profile and that profile has a hard byte
# ceiling (ADR 0050).
RETAINED_WORKTREE_CAP = 12

# The most archives one sweep performs. A sweep runs ahead of dispatch in the same pass, so an
# unbounded backlog would delay admission and snapshot publication for as long as it took to drain;
# successive sweeps converge instead.
SWEEP_ARCHIVE_BUDGET = 20

# Above this many *registered* worktrees, a repository stops receiving new cold work (ADR 0050,
# recalibrated by ADR 442). It is a count proxy for a byte cliff: the Claude CLI embeds a sandbox
# profile in every shell spawn's argv — three filesystem deny paths per linked worktree — and the
# whole command line must stay under the OS exec-argument limit (kern.argmax = 1,048,576 bytes
# here). Measured against the 2026-07-31 dead-shell incident (#442): the two builds that died at
# launch hit the cliff at 52/51 linked worktrees (210/207 deny paths, ~1.1 MB spawn argv), i.e.
# 53/52 listed registrations as this gate counts them. Synthetic repos on the same machine and
# CLI (2.1.212) put the slope at ~24 KB of profile per registration over a ~0.4 MB base
# (60 linked → 1.8 MB, 120 → 3.2 MB). The retired 175 came from an older ~246-registration /
# ~1.6 MB measurement the current CLI no longer matches — per-registration cost moves with CLI
# version and path length, so re-measure before trusting this number on another machine.
#
# 40 refuses ~12 registrations below the observed death point: the margin covers what a burst of
# concurrent sessions adds between preflight and spawn (the incident hour grew the registry from
# ~41 listed at 15:01 to 53 at 16:13 with no refusal at 175).
WORKTREE_DISPATCH_CEILING = 40


def _run(cmd: list[str], cwd: str | None = None, timeout: int | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    if cmd and Path(str(cmd[0])).name in {"claude", "codex"}:
        raise RuntimeError("provider commands may only be executed by the coordinator launcher")
    t = timeout if timeout is not None else int(os.environ.get("AGENTFLOW_GH_TIMEOUT", "120"))
    try:
        return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=t, env=env)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr=f"timed out after {t}s")


def run_provider_discovery_probe(argv: list[str], cwd: str):
    """Run the explicit operator-requested native discovery proof outside orchestration."""
    try:
        return subprocess.run(
            argv, cwd=cwd, text=True, capture_output=True, timeout=300
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(argv, 1, "", str(exc))


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


def _canonical_charter() -> str:
    """The standards every unattended stage receives from AgentFlow's canonical bytes.

    A source clone reads its sole canonical ``standards/CHARTER.md``. Built distributions carry
    those bytes as an ``agentflow`` package resource, so an installed runner never reaches
    sideways into ``site-packages/standards``. Missing or empty bytes block launch rather than
    silently running a stage without its review standard.
    """
    try:
        if _SOURCE_CHECKOUT_MARKER.exists():
            charter = _CHARTER_SOURCE_PATH.read_text()
            source = str(_CHARTER_SOURCE_PATH)
        else:
            resource = resources.files("agentflow").joinpath("_data", "CHARTER.md")
            charter = resource.read_text()
            source = "agentflow/_data/CHARTER.md"
    except OSError as exc:
        raise RuntimeError("canonical engineering charter unavailable") from exc
    if not charter.strip():
        raise RuntimeError(f"canonical engineering charter is empty: {source}")
    return charter


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
    charter = _canonical_charter()
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
Canonical engineering charter (applies to this entire stage):
{charter}

{prompt}"""


def _active_marker(wt: Path) -> Path | None:
    r = _run(["git", "-C", str(wt), "rev-parse", "--git-path", "agentflow-active"])
    if r.returncode != 0 or not r.stdout.strip():
        return None
    marker = Path(r.stdout.strip())
    return marker if marker.is_absolute() else wt / marker


def _worktree_is_locked(wt: Path) -> bool:
    """Whether a human pinned this checkout with ``git worktree lock``. Resolved through git (the
    :func:`_active_marker` idiom) rather than composed from the basename, so a duplicate basename
    cannot answer for another worktree's lock."""
    resolved = _run(["git", "-C", str(wt), "rev-parse", "--git-path", "locked"])
    if resolved.returncode != 0 or not resolved.stdout.strip():
        return False
    lock = Path(resolved.stdout.strip())
    return (lock if lock.is_absolute() else wt / lock).exists()


class CheckoutRefused(Exception):
    """A checkout whose *local* state preparation may not disturb, carrying the typed refusal.

    Every other preparation failure is a subprocess exit code: git could not reach the remote,
    ``uv`` could not build the environment, ``worktree add`` fell over. Those are undiagnosed by
    design — retrying is the right answer and the exit code is all anyone has. These two are
    different in kind, and the difference is provable from the checkout alone without a network:
    a live sibling still holds it (ordinary contention that clears itself), or a human pinned it
    with ``git worktree lock`` over state that cannot be archived (nothing clears until they act).
    Classification stays here, beside the predicates that read the evidence, so the coordinator
    consumes one typed answer rather than keeping its own table of git conditions (#406).
    """

    def __init__(self, refusal) -> None:
        super().__init__(refusal.summary())
        self.refusal = refusal


def refuse_unusable_checkout(workdir: str, wt: Path) -> None:
    """Refuse, by name, a registered checkout whose local state preparation cannot clear itself.

    Deliberately runs *before* any fetch or provisioning: both conditions are read from the
    checkout with no network at all, and a daemon that cannot reach its remote would otherwise
    report the fetch failure forever while the real, human-clearable cause sat underneath it.

    Only the two states :func:`archive_stranded_worktree` and the rebuild below genuinely cannot
    get past. A worktree that is absent, orphaned (registered nowhere), or clean and idle is
    rebuilt or reused as it always was — a lock over a *clean* checkout is no obstacle, since
    reuse only resets and cleans it, so refusing there would stall stages this has never stalled.
    """
    # Imported here, not at module scope: the coordinator package reaches back into this module
    # for its git plumbing, so a top-level import closes a cycle at interpreter start.
    from agentflow.coordinator.verification import unprepared

    if not wt.exists() or not _worktree_is_registered(workdir, wt):
        return
    if _worktree_is_active(wt):
        raise CheckoutRefused(unprepared(
            "checkout-busy",
            f"a live sibling session still holds {wt}; waiting for it to finish before taking "
            f"the checkout", expected=True))
    if not _worktree_head(workdir, wt) and _worktree_is_locked(wt):
        raise CheckoutRefused(unprepared(
            "checkout-locked",
            f"{wt} is pinned open by `git worktree lock` and holds uncommitted work that cannot "
            f"be archived while it is pinned; agentflow will not disturb a checkout a human "
            f"deliberately locked", stall=True))


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


def _worktree_head(workdir: str, wt: Path) -> str:
    """The commit an idle, owned, clean worktree is parked on — ``""`` when it is any of
    busy, foreign, dirty, or unreadable. It licenses only *taking the checkout as it stands*:
    removal refuses outright on ``""``, while detached preparation refuses reuse-in-place and
    then earns the right to rebuild separately, by archiving the state to a recovery ref
    (ADR 0050). A ``""`` is never on its own a licence to disturb the worktree."""
    target = os.path.realpath(wt)
    main = os.path.realpath(workdir)
    if target == main:
        return ""
    if _worktree_is_active(wt):
        return ""
    if not _worktree_is_registered(workdir, wt):
        return ""
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if status.returncode != 0 or status.stdout.strip():
        return ""
    head = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    return head.stdout.strip() if head.returncode == 0 else ""


def resettable_head(workdir: str, wt: Path) -> str:
    """The commit an idle, owned worktree is parked on when a ``reset --hard`` may be run in it —
    ``""`` when it is busy, foreign, unreadable, or holds edits a reset would destroy.

    Deliberately more permissive than :func:`_worktree_head`, which backs *removal* and so refuses
    on any untracked file. A reset overwrites tracked content and leaves untracked files exactly
    where they are, so untracked litter — the leftover config or draft a build session routinely
    leaves behind — is not a reason to refuse. Treating it as one stalls the caller forever while
    protecting nothing.
    """
    if os.path.realpath(wt) == os.path.realpath(workdir):
        return ""
    if _worktree_is_active(wt) or not _worktree_is_registered(workdir, wt):
        return ""
    tracked = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=no"])
    if tracked.returncode != 0 or tracked.stdout.strip():
        return ""
    head = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    return head.stdout.strip() if head.returncode == 0 else ""


def _commit_is_on_origin(workdir: str, commit: str) -> bool:
    remote_refs = _run(["git", "-C", workdir, "for-each-ref", "--contains", commit,
                        "--format=%(refname)", "refs/remotes/origin/"])
    return remote_refs.returncode == 0 and bool(remote_refs.stdout.strip())


def _worktree_is_disposable(workdir: str, wt: Path) -> bool:
    head = _worktree_head(workdir, wt)
    return bool(head) and _commit_is_on_origin(workdir, head)


def retain_stranded_commit(workdir: str, wt: Path, commit: str) -> bool:
    """Anchor a commit that exists only in ``wt`` under a durable recovery ref, so moving that
    checkout can never be the thing that loses it.

    A detached session checkout drifts off the remote whenever its branch is rebased or amended:
    the commit it is parked on is real work in git's eyes but is already superseded on the PR.
    Naming it under ``refs/agentflow/stranded/`` keeps it reachable and out of reflog expiry
    without keeping the checkout hostage to it."""
    ref = f"refs/agentflow/stranded/{wt.name}/{commit[:12]}"
    return _run(["git", "-C", workdir, "update-ref", ref, commit]).returncode == 0


def archive_stranded_worktree(workdir: str, wt: Path) -> str:
    """Snapshot everything a stranded session left behind — committed, staged, unstaged, and
    untracked — onto a durable recovery ref, then reclaim the checkout. Returns the ref name, or
    ``""`` when any step failed, in which case the worktree is left exactly as it was found.

    This is what makes a bound on registrations safe (ADR 0050). :func:`remove_worktree_if_safe`
    can only reclaim work git can already prove is durable, so a session that died with edits in
    the tree pins its registration forever. Here safety is redefined: the work must be
    *recoverable*, not the directory *present*. Nothing is ever force-committed onto a branch —
    the snapshot is plumbing only, parented on the checkout's own HEAD and named under
    ``retain_stranded_commit``'s namespace, so no branch, no PR, and no reflog moves.

    The tree is built in a **scratch index**, never the worktree's own. A failure partway through
    must leave no trace: staging into the real index would read as tracked modification forever
    and permanently defeat :func:`resettable_head`, which tolerates untracked litter but not
    staged litter. Gitignored paths are deliberately left out — the archive is the session's work,
    not its provisioning.

    The commit identity is set explicitly because agentflow creates commits nowhere else and
    passes no identity environment: without it, a host with no global git identity would fail
    every archive and the bound would silently never apply.

    A single ``--force`` removes an unclean checkout; a *locked* worktree needs a second one and
    does not get it — a lock is a deliberate human signal, so a locked worktree stays registered.
    It is refused before anything is written rather than only at the removal: a caller that retries
    every cycle would otherwise anchor a fresh recovery ref on each pass, burying the real stranded
    work an operator greps this namespace for under an unbounded pile of dead ones.
    """
    if _worktree_is_locked(wt):
        return ""
    scratch = tempfile.mkdtemp(prefix="agentflow-archive-index-")
    env = {**os.environ, "GIT_INDEX_FILE": os.path.join(scratch, "index")}
    try:
        if _run(["git", "-C", str(wt), "read-tree", "HEAD"], env=env).returncode != 0:
            return ""
        if _run(["git", "-C", str(wt), "add", "-A"], env=env).returncode != 0:
            return ""
        written = _run(["git", "-C", str(wt), "write-tree"], env=env)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    tree = written.stdout.strip() if written.returncode == 0 else ""
    if not tree:
        return ""
    head = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    head_tree = _run(["git", "-C", str(wt), "rev-parse", "HEAD^{tree}"])
    if head.returncode != 0 or head_tree.returncode != 0 or not head.stdout.strip():
        return ""
    if tree == head_tree.stdout.strip():
        commit = head.stdout.strip()  # nothing uncommitted — HEAD itself is the whole snapshot
    else:
        snapshot = _run(["git", "-C", str(wt),
                         "-c", "user.name=agentflow", "-c", "user.email=agentflow@local",
                         "commit-tree", tree, "-p", head.stdout.strip(),
                         "-m", "agentflow: archived stranded session work"])
        commit = snapshot.stdout.strip() if snapshot.returncode == 0 else ""
    if not commit:
        return ""
    ref = f"refs/agentflow/stranded/{wt.name}/{commit[:12]}"
    if _run(["git", "-C", workdir, "update-ref", ref, commit]).returncode != 0:
        return ""
    anchored = _run(["git", "-C", workdir, "rev-parse", ref])
    if anchored.returncode != 0 or anchored.stdout.strip() != commit:
        return ""  # the ref did not take — never reclaim work we cannot prove is anchored
    if _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)]).returncode != 0:
        return ""
    return ref


def _worktree_idle_seconds(wt: Path) -> float:
    """How long this checkout has sat untouched, by the most recent local clock we can read:
    the directory's own mtime, its registration's index, and its HEAD commit's date.

    Only local evidence, so the answer costs nothing and cannot be wrong about a remote. The index
    path is resolved through git (the ``_active_marker`` idiom) rather than composed from the
    basename: git disambiguates duplicate basenames with numeric suffixes, so a hand-built admin
    path can silently read *another* worktree's clock.

    ``0.0`` when nothing is readable — the fail-closed answer, since idleness only ever unlocks
    reclamation.
    """
    stamps: list[float] = []
    try:
        stamps.append(wt.stat().st_mtime)
    except OSError:
        pass
    resolved = _run(["git", "-C", str(wt), "rev-parse", "--git-path", "index"])
    if resolved.returncode == 0 and resolved.stdout.strip():
        index = Path(resolved.stdout.strip())
        try:
            stamps.append((index if index.is_absolute() else wt / index).stat().st_mtime)
        except OSError:
            pass
    committed = _run(["git", "-C", str(wt), "log", "-1", "--format=%ct", "HEAD"])
    if committed.returncode == 0 and committed.stdout.strip().isdigit():
        stamps.append(float(committed.stdout.strip()))
    return max(0.0, time.time() - max(stamps)) if stamps else 0.0


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


# --- commit-time sign-off in a session checkout (ADR 401) ------------------------------------
# One breadcrumb per repository per remedy, so a repository running unenforced says so once
# instead of once per attempt. Keyed on the common git dir — every checkout of one repository
# shares it. Shaped after the daemon's per-repo sweep map.
_SIGNOFF_UNENFORCED: dict[str, str] = {}

_SIGNOFF_HOOK = """#!/bin/sh
# Installed by agentflow when this session checkout was prepared (ADR 401). Certifies a commit
# at the moment it is made, so a session cannot create an unsigned commit and strand its own
# pull request. Signs only for the identity this checkout commits as: a commit authored by
# anyone else is left exactly as written, because a sign-off is a personal certification and
# the engine makes none in a third party's name.
message="$1"
ident=""
if [ -z "$GIT_AUTHOR_EMAIL" ] || [ -z "$GIT_AUTHOR_NAME" ]; then
    ident=$(git var GIT_AUTHOR_IDENT 2>/dev/null)
fi
name="$GIT_AUTHOR_NAME"
email="$GIT_AUTHOR_EMAIL"
[ -n "$name" ] || name=$(printf '%s' "$ident" | sed -n 's/ <[^<]*$//p')
[ -n "$email" ] || email=$(printf '%s' "$ident" | sed -n 's/.*<\\(.*\\)>.*/\\1/p')
configured=$(git config --get user.email)
author_key=$(printf '%s' "$email" | tr 'A-Z' 'a-z')
checkout_key=$(printf '%s' "$configured" | tr 'A-Z' 'a-z')
if [ -n "$email" ] && [ "$author_key" = "$checkout_key" ]; then
    # Keyed on the author's own address, not on the trailer alone: a message already carrying
    # somebody else's sign-off still needs this one, or the check stays red.
    if ! grep -i '^Signed-off-by:' "$message" | grep -qiF "<$email>"; then
        git interpret-trailers --in-place --trailer "Signed-off-by: $name <$email>" "$message"
    fi
fi
"""


def _repository_name(wt: Path, common: Path) -> str:
    """How a repository is named in a log line: its origin URL, or the shared git dir."""
    url = _run(["git", "-C", str(wt), "remote", "get-url", "origin"])
    return url.stdout.strip() if url.returncode == 0 and url.stdout.strip() else str(common)


def _signoff_unenforced(wt: Path, common: Path, remedy: str) -> None:
    """Say once per repository that its session commits are not being signed for it.

    Written straight to the daemon's own stream in the daemon's own line shape rather than
    through :func:`agentflow.daemon.log`: the daemon reaches this module, so importing it back —
    however the import is spelled — is the import ring ``test_dispatch`` refuses. The shape is
    pinned by test rather than by this sentence.
    """
    if _SIGNOFF_UNENFORCED.get(str(common)) == remedy:
        return
    _SIGNOFF_UNENFORCED[str(common)] = remedy
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} agentflow: "
          f"{_repository_name(wt, common)}: session commits are not signed off automatically — "
          f"{remedy}. The pull-request sign-off check still gates every merge.", flush=True)


def _absolute(wt: Path, *rev_parse: str) -> Path:
    """A path git reports for ``wt``, always absolute — ``core.hooksPath`` resolves a relative
    value against the current directory, not the git dir, so a relative one cannot be stored."""
    resolved = _run(["git", "-C", str(wt), "rev-parse", *rev_parse])
    resolved.check_returncode()
    path = Path(resolved.stdout.strip())
    return path if path.is_absolute() else (wt / path)


def _write_hook(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _install_commit_signoff(wt: Path) -> None:
    """Make this checkout sign off its own commits as it makes them, and never raise.

    Instruction alone was observed to not hold (#357 shipped it; a build ran after it and still
    pushed an unsigned commit), and the branch is only legitimately rewritable inside the session
    that authored it — so the sign-off is added mechanically at commit time, here.

    Enforcement is confined to this checkout: a hooks directory under the worktree's own git dir
    (never inside the tree, which the reuse gates read as litter) pointed at by a *per-worktree*
    ``core.hooksPath``, so the maintainer's shared checkout keeps committing exactly as it does
    now. That costs one key in the shared repository config — ``extensions.worktreeConfig`` —
    which is the only thing written there.

    ``core.hooksPath`` replaces the hooks directory wholesale rather than overlaying it, so every
    hook the repository already has is forwarded from the installed directory and the signer hands
    off to an existing commit-msg hook. The hand-off target is read from the *repository* scope at
    install time: on a re-provisioned checkout the effective value is already this directory, and
    chaining off that would make every commit recurse.

    Fails open by contract: :meth:`_WorktreeRunner.provision`'s callers read a raised
    ``CalledProcessError`` as "do not admit this stage", so a checkout that cannot be enforced
    runs unenforced with one breadcrumb per repository. The pull-request sign-off check is the
    outer backstop, unchanged.
    """
    common = wt
    try:
        git_dir = _absolute(wt, "--absolute-git-dir")
        common = _absolute(wt, "--git-common-dir")
        bare = _run(["git", "-C", str(wt), "config", "--local", "--get", "core.bare"])
        rooted = _run(["git", "-C", str(wt), "config", "--local", "--get", "core.worktree"])
        if bare.stdout.strip().lower() == "true" or rooted.stdout.strip():
            # Git's own guidance: these must be relocated before per-worktree config is enabled.
            _signoff_unenforced(wt, common, "its shared configuration sets core.bare or "
                                            "core.worktree, which must be relocated to the main "
                                            "checkout before sign-off can be enforced per session")
            return
        enabled = _run(["git", "-C", str(wt), "config", "--local", "--get",
                        "extensions.worktreeConfig"])
        if enabled.stdout.strip().lower() != "true":
            _run(["git", "-C", str(wt), "config", "--local",
                  "extensions.worktreeConfig", "true"]).check_returncode()
        configured = _run(["git", "-C", str(wt), "config", "--local", "--get", "core.hooksPath"])
        source = Path(configured.stdout.strip()) if configured.stdout.strip() else common / "hooks"
        if not source.is_absolute():
            source = wt / source
        installed = git_dir / "agentflow-hooks"
        shutil.rmtree(installed, ignore_errors=True)  # regenerate, never layer: provision re-runs
        installed.mkdir(parents=True)
        chained = None
        if source.is_dir():
            for entry in sorted(source.iterdir()):
                if entry.name.endswith(".sample") or not os.access(entry, os.X_OK) \
                        or not entry.is_file():
                    continue
                if entry.name == "commit-msg":
                    chained = entry
                    continue
                _write_hook(installed / entry.name,
                            f'#!/bin/sh\nexec {shlex.quote(str(entry))} "$@"\n')
        hand_off = f'exec {shlex.quote(str(chained))} "$@"' if chained else "exit 0"
        _write_hook(installed / "commit-msg", f"{_SIGNOFF_HOOK}{hand_off}\n")
        _run(["git", "-C", str(wt), "config", "--worktree",
              "core.hooksPath", str(installed)]).check_returncode()
    except Exception as e:  # noqa: BLE001 — an unenforced checkout still builds; a raise stops it
        _signoff_unenforced(wt, common, f"preparing its hooks failed ({type(e).__name__}: {e})")


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
            verified, has_open_pr = self._open_pr_for_branch(repo, branch) if repo else (False, False)
            if not verified:
                raise subprocess.CalledProcessError(1, ["gh", "pr", "list"])
            if has_open_pr:
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
        """Ready a prepared checkout for a session: its dependency environment, then commit-time
        sign-off. One failure semantic for callers — a ``CalledProcessError`` means the
        environment could not be built and the stage must not be admitted. The sign-off install
        is deliberately not that: it never raises, so a checkout that cannot be enforced still
        runs (:func:`_install_commit_signoff`)."""
        if (wt / "uv.lock").exists() and not (wt / ".venv" / "bin" / "python").exists():
            _run(["uv", "sync", "--all-extras"], cwd=str(wt)).check_returncode()
        _install_commit_signoff(wt)

    def prepare_worktree_detached(self, workdir: str, ref: str, wt: Path) -> None:
        """A detached worktree at `ref` (e.g. `origin/<pr-branch>`) — for review.

        Detached avoids the "branch already checked out" collision with the
        builder's worktree, which still holds the PR branch.

        A directory that exists on disk but is *not* a registered worktree is
        orphaned — its git metadata was lost (e.g. a daemon killed mid-prepare).
        Git holds no state for it and ``worktree add`` would fail on the existing
        dir every cycle, so it is discarded and rebuilt. A *registered* worktree
        is never force-discarded here: a busy or locked one still fails closed.

        A clean checkout parked on a commit that has left the remote — the ordinary
        aftermath of a rebase or force-push on the branch under review — is moved onto
        ``ref`` rather than left to stall the stage forever. Its old commit is anchored
        under a recovery ref first, so no work is destroyed by the move.

        An *idle but unclean* checkout — the untracked scratch or edits a finished
        session routinely leaves behind — must not stall the stage forever either:
        refusing here would fail every future admission at this path until a human
        deletes the directory. Its entire state is archived to a recovery ref
        (:func:`archive_stranded_worktree`, ADR 0050) and the checkout rebuilt, so
        nothing is lost and the stage proceeds.

        The two states that *cannot* be got past are read first, off the network
        (:func:`refuse_unusable_checkout`), so which one it is survives to the caller
        as a typed refusal instead of an anonymous exit code (#406).
        """
        refuse_unusable_checkout(workdir, wt)
        _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).check_returncode()
        if wt.exists():
            if not _worktree_is_registered(workdir, wt):
                discard_orphaned_worktree(workdir, wt)
            elif head := _worktree_head(workdir, wt):
                if not _commit_is_on_origin(workdir, head) \
                        and not retain_stranded_commit(workdir, wt, head):
                    raise subprocess.CalledProcessError(1, ["git", "update-ref"])
                # Freshen a reused worktree to the (possibly moved) ref — otherwise a
                # re-review after a revise push would inspect a stale checkout.
                _run(["git", "-C", str(wt), "reset", "--hard", ref]).check_returncode()
                # The environment is derived from the lockfile and expensive to recreate.
                # Keep it while removing every other ignored or untracked artifact.
                _run(["git", "-C", str(wt), "clean", "-fdx", "-e", ".venv/"]).check_returncode()
                return
            elif _worktree_is_active(wt) or not archive_stranded_worktree(workdir, wt):
                raise subprocess.CalledProcessError(1, ["git", "status", "--porcelain"])
        wt.parent.mkdir(parents=True, exist_ok=True)
        _run(["git", "-C", workdir, "worktree", "add", "--detach", str(wt), ref]).check_returncode()

    def _open_pr_for_branch(self, repo: str, branch: str) -> tuple[bool, bool]:
        """Whether the open-PR lookup for ``branch`` could be made, and whether one exists.
        Fail-closed: the first element is ``False`` only when the lookup itself failed, so an
        unreadable answer never reads as "no open PR"."""
        from agentflow import github

        prs = github.list_open_prs(repo, head=branch)
        if prs is None:
            return False, False
        return True, bool(prs)

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


def _codebase_memory_mcp_servers() -> dict:
    """The public-safe Codebase Memory server from the operator's Claude configuration.

    Automated sessions deliberately ignore user configuration. Codebase Memory is the one
    operator-local server the pipeline needs to restore, so every other server and every
    environment value stays private. Only its executable shape crosses the launch seam.
    """
    try:
        data = json.loads((Path.home() / ".claude.json").read_text())
    except (OSError, ValueError):
        return {}
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    server = servers.get("codebase-memory-mcp")
    if not isinstance(server, dict) or not isinstance(server.get("command"), str):
        return {}
    public = {"command": server["command"]}
    args = server.get("args")
    if isinstance(args, list) and all(isinstance(arg, str) for arg in args):
        public["args"] = list(args)
    return {"codebase-memory-mcp": public}


def _codex_local_mcp_config(servers: dict) -> list[str]:
    """Render the operator's local MCP servers as Codex ``-c`` config overrides.

    Codex launches with ``--ignore-user-config`` (no personal MCP leaks in), which also drops the
    code-graph server Claude re-supplies via ``--mcp-config``. Codex takes MCP servers under the
    ``mcp_servers.<name>`` config table, so re-supply the *same* map here as dotted ``-c``
    overrides — applied on top of the ignored user config — giving both providers the code-graph
    tool from one source while the account connectors stay excluded. Each ``-c`` *value* is parsed
    as TOML: ``json.dumps`` renders strings and lists as valid TOML scalars/arrays. Environment
    values are never rendered into provider arguments. The server name is used verbatim as a
    dotted-key segment, which is correct for the fleet's bare-word (dash-separated) server names;
    a name containing a ``.`` would nest as sub-tables — not a shape any operator config uses, so
    it is left unescaped rather than guarded speculatively. A server without a ``command`` is
    skipped; an empty map yields no overrides (nothing to attach)."""
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
    return argv


class ClaudeRunner(_WorktreeRunner):
    tool = "claude"
    MODELS = {Complexity.STANDARD: "sonnet", Complexity.DEEP: "opus"}
    # Claude's reasoning ladder for ``--effort`` (ascending). ``max`` is manual-only (ADR 0046).
    _REASONING_LADDER = ("low", "medium", "high", "xhigh", "max")

    def structured_argv(self, prompt: str, model: str, cwd: str,
                        schema: dict | None = None, profile=None,
                        cli_model: str | None = None) -> list[str]:
        """Build the structured Claude command run only by the coordinator launcher.

        A ``schema`` (Intake's or Review's provider-neutral result contract) is wired to
        Claude's native ``--json-schema`` so the final response is validated structured
        output, not free text the parser must scavenge.

        ``--strict-mcp-config`` pins the session to only the MCP servers we hand it, so the
        operator's personal claude.ai connectors can never attach (#240). We then re-supply
        only Codebase Memory's executable configuration, without its environment, so daemon
        sessions keep the code graph (#244). Without that server the MCP set is simply empty.

        A ``profile`` (ADR 0044) narrows the session to its stage: a read-only stage's
        allowlist is handed to ``--tools`` so the withheld edit tools are absent from the
        loaded surface (with a ``permissions.deny`` backstop in the settings), and the turn
        ceiling is handed to ``--max-turns``. The read-only allowlist is the research §3a
        read/search set plus the re-supplied Codebase Memory server: an exploration stage keeps
        the same code-graph access Build has — it is the withheld
        *edit* tools, not the local read-only MCP tools, that a read-only stage loses.

        A build/revise profile carries the session lead's low reasoning effort (ADR 498), handed to
        Claude's first-class ``--effort`` flag; every other stage leaves it ``None``. A
        rung above Claude's ladder clamps to its top rather than failing the launch.
        """
        from agentflow.operational_safety import READ_ONLY_WITHHELD_TOOLS_V1

        from agentflow.routing import routing
        deny: tuple[str, ...] = ()
        argv = ["claude", "-p", _bounded_prompt(prompt, cwd), "--model",
                cli_model or routing.cli_identifier("claude", model),
                "--output-format", "stream-json", "--verbose",
                "--permission-mode", "acceptEdits", "--setting-sources", "project",
                "--strict-mcp-config"]
        servers = _codebase_memory_mcp_servers()
        if servers:
            argv += ["--mcp-config",
                     json.dumps({"mcpServers": servers}, separators=(",", ":"))]
        if profile is not None:
            if profile.allowed_tools is not None:
                tools = list(profile.allowed_tools)
                tools += [f"mcp__{name}" for name in servers]
                argv += ["--tools", ",".join(tools)]
                deny = READ_ONLY_WITHHELD_TOOLS_V1
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

    # Codex's reasoning ladder for ``model_reasoning_effort`` (ascending). ``max`` is manual-only
    # (ADR 0046); the daemon never maps below ``low``, so the sub-``low`` rungs are inert here.
    _REASONING_LADDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")

    def structured_argv(self, prompt: str, model: str, cwd: str,
                        schema: dict | None = None, profile=None,
                        cli_model: str | None = None) -> list[str]:
        """Build the structured Codex command run only by the coordinator launcher.

        A ``schema`` (Intake's or Review's provider-neutral result contract) is wired to
        Codex's native ``--output-schema`` file so the final response is validated structured
        output, not free text the parser must scavenge.

        A read-only ``profile`` (ADR 0044) launches the session in the ``read-only`` Codex
        sandbox so it cannot edit the checkout — Codex's own shape of the read-only stage
        surface (there is no Claude-style per-tool allowlist flag). Codex runs
        ``--ignore-user-config`` so no personal MCP leaks in, which also drops the code-graph
        server; we re-supply *only* Codebase Memory's executable configuration as
        ``mcp_servers`` ``-c`` overrides, so both providers get the code graph from one source
        while account connectors and environment values stay excluded, on every stage including
        read-only ones. The wall ceiling is applied per-record by the launcher, the same as for
        Claude.

        A legacy Codex build/revise profile carries the session lead's low reasoning effort. Codex has no
        ``--effort`` flag — reasoning effort is a config override — so it is appended as another
        ``-c model_reasoning_effort=<level>`` alongside the existing ``-c`` overrides, before the
        positional prompt. Every other stage leaves it ``None`` (provider default); a rung above
        Codex's ladder clamps to its top rather than failing the launch.
        """
        from agentflow.routing import routing

        codex_bin = os.environ.get("AGENTFLOW_CODEX_BIN", "codex")
        worktree = os.path.realpath(cwd)
        common = _run(["git", "-C", worktree, "rev-parse", "--path-format=absolute",
                       "--git-common-dir"])
        writable_roots = json.dumps([common.stdout.strip()]) if common.returncode == 0 else "[]"
        approval_policy = 'approval_policy="on-request"'
        read_only = profile is not None and profile.allowed_tools is not None
        sandbox = "read-only" if read_only else "workspace-write"
        argv = [codex_bin, "exec", "-m",
                cli_model or routing.cli_identifier("codex", model), "--json",
                "--sandbox", sandbox, "--cd", worktree,
                "--ignore-user-config", "-c", approval_policy,
                "-c", 'approvals_reviewer="auto_review"',
                "-c", f"auto_review.policy={json.dumps(_CODEX_AUTO_REVIEW_POLICY)}",
                "-c", "sandbox_workspace_write.network_access=true",
                "-c", f"sandbox_workspace_write.writable_roots={writable_roots}",
                "--skip-git-repo-check"]
        argv.insert(argv.index("-c"), "--ephemeral")
        argv += _codex_local_mcp_config(_codebase_memory_mcp_servers())
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
        gate = (
            os.environ.get("AGENTFLOW_CAPACITY_HELPER")
            or os.environ.get("AGENTFLOW_TRIAGE_GATE")
        )
        if gate is None:
            return None
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


def codex_spent_at_render() -> bool:
    """True when :meth:`CodexRunner.account_fact` reports Codex currently rate-limited, for a
    session-lead brief to state up front at render time. Fails open (False) whenever the
    capacity seam is missing or unreadable — an absent fact is not evidence Codex is spent
    (ADR 0030); ``account_fact`` already applies that rule, this only reads its typed result."""
    fact = CodexRunner().account_fact()
    return isinstance(fact, dict) and fact.get("kind") == "rate_limited"



def _pr_state_for_branch(repo: str, branch: str) -> str | None:
    """The current state of the most recent PR for this branch across all states
    (OPEN, MERGED, or CLOSED), or None when no PR has ever been opened for it — or when
    the lookup could not be made (fail-closed)."""
    from agentflow import github

    rows = github.prs_for_branch(repo, branch)
    if not rows:
        return None
    return rows[0].state or None


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


def _intake_done(repo: str, issue: int) -> bool:
    """An intake is finished when the issue is closed or carries a post-triage label, and is
    no longer mid-triage. Fail-closed: an unreadable state or label set reads as not-done."""
    from agentflow import github

    state = github.issue_state(repo, issue)
    labels = github.issue_labels(repo, issue)
    if state is None or labels is None:
        return False
    return (state == "CLOSED" or bool(labels & {
        "ready-for-agent", "agentflow:needs-grilling", "agentflow:needs-mockup"
    })) and "agentflow:triaging" not in labels


def _review_done(repo: str, pr: int) -> bool:
    """A review is finished when its PR has merged/closed or a parked-for-human note was posted.
    Fail-closed: an unreadable state or comment thread reads as not-done."""
    from agentflow import github

    state = github.pr_state(repo, pr)
    comments = github.pr_comments(repo, pr)
    if state is None or comments is None:
        return False
    return state in ("MERGED", "CLOSED") or any(
        "agentflow: parked for human review" in comment.body for comment in comments)


def _mockup_done(repo: str, issue: int) -> bool:
    """A mockup is finished when it is no longer being drawn and its variants have been posted.
    Fail-closed: an unreadable label set or comment thread reads as not-done."""
    from agentflow import github

    labels = github.issue_labels(repo, issue)
    comments = github.issue_comments(repo, issue)
    if labels is None or comments is None:
        return False
    if "agentflow:drawing-mockup" in labels:
        return False
    return any("mockup variants" in comment.body for comment in comments)


def _build_done(repo: str, issue: int, branch: str) -> bool:
    """A build is finished when the issue is no longer building and a PR exists for its branch.
    Fail-closed: an unreadable label set (or PR state) reads as not-done."""
    from agentflow import github

    labels = github.issue_labels(repo, issue)
    if labels is None:
        return False
    if "agentflow:building" in labels:
        return False
    return _pr_state_for_branch(repo, branch) in ("OPEN", "MERGED", "CLOSED")


def _session_is_complete(repo: str, ref: WorktreeRef, branch: str | None) -> bool:
    """Whether the recognized agentflow session at ``ref`` (with git ``branch``, ``None`` when
    detached) has finished all its work. The path's typed parts fix the kind and issue/PR number;
    each kind's completion is confirmed against GitHub and fails closed. An intake and a review run
    on detached checkouts (no branch); the build and mockup kinds must sit on the branch the layout
    derives for them. Kinds with no completion rule (research, converse) are never auto-complete."""
    if ref.kind is WorktreeKind.INTAKE:
        return branch is None and _intake_done(repo, ref.number)
    if ref.kind is WorktreeKind.REVIEW:
        return branch is None and _review_done(repo, ref.number)
    if ref.kind is WorktreeKind.MOCKUP:
        return branch == ref.branch and _mockup_done(repo, ref.number)
    if ref.kind is WorktreeKind.BUILD:
        return branch == ref.branch and _build_done(repo, ref.number, branch)
    return False


def _legacy_session_tool(owned_path: str, branch: str | None) -> str | None:
    """The tool owning a pre-``agentflow/``-prefix legacy session, recognized by a
    ``{tool}/{slug}`` branch whose tool names the checkout's lane and whose slug is the checkout's
    own directory name; ``None`` for anything else (including the main checkout). Such a branch is
    not a shape :class:`WorktreeRef` models, so it is matched here rather than through the layout
    type."""
    if branch is None:
        return None
    lane = Path(owned_path).parent.name
    if lane not in ("claude", "codex"):
        return None
    if re.fullmatch(rf"{re.escape(lane)}/[^/]+", branch) and Path(owned_path).name == branch.rsplit("/", 1)[-1]:
        return lane
    return None


# The two kinds excluded from reclamation outright. Neither has a completion rule, so neither can
# ever read as finished; a conversation's checkout is reused across turns while each turn's record
# retires, so between turns it has no store protection under any definition — and that checkout is
# the conversation's *only* durable output. Archiving them would reclaim every idle Ask and every
# research checkout in the fleet. Both populations are small and human-driven, so leaving them out
# does not reopen the unbounded growth this bound exists to close.
_UNRECLAIMABLE_KINDS = (WorktreeKind.RESEARCH, WorktreeKind.CONVERSE)


def recover_stale_worktrees(repo: str, workdir: str,
                            protected: set[str] = frozenset()) -> WorktreeRecovery:
    """Prune stale registrations, remove completed agentflow-owned sessions, and archive the
    stranded ones a repository can no longer afford to keep registered (ADR 0050).

    Git's registry establishes repository ownership; the path is used only after
    ownership is known to recognize agentflow's current and legacy session names.
    Completion lookups and the final clean/pushed checks all fail closed. ``protected`` contains
    the coordinator's *live-state* owned sources; recovery retains them even when they are clean
    and no provider is currently alive, because a waiting continuation still owns that worktree. A
    held record's source is not in that set: a maintainer resume rebuilds the checkout from the
    branch anyway, and held records are never retired, so protecting them would exempt the very
    population that grows without bound.

    Two classes of session are otherwise kept forever, and both are reclaimed here through the
    same archive-then-remove path:

    - one whose completion cannot be confirmed (including one GitHub simply would not answer for),
      which deliberately narrows the old "unknown → retain forever" rule; and
    - one that *is* complete but whose removal fails safety — routine, not exceptional, since a
      single untracked file or a squash-merged branch whose commits origin has pruned is enough.

    Neither is reclaimed on age alone. A session must be idle past :data:`STRANDED_IDLE_SECONDS`
    to be eligible at all, and then only the oldest beyond :data:`RETAINED_WORKTREE_CAP` are
    archived, at most :data:`SWEEP_ARCHIVE_BUDGET` per sweep. An archive that fails leaves its
    worktree registered and reported as retained — work is never lost, only relocated.

    Removal of *completed* sessions is deliberately not idle-gated: it has never been, it destroys
    nothing git cannot already reproduce, and gating it would leave finished checkouts lying
    around for a day for no gain.
    """
    _run(["git", "-C", workdir, "worktree", "prune"])
    registered = _registered_worktrees(workdir)
    if registered is None:
        return WorktreeRecovery((), ())
    removed: list[str] = []
    retained: list[str] = []
    stranded: list[tuple[float, str]] = []
    for path, branch in registered:
        owned_path = os.path.realpath(path)
        ref = WorktreeRef.parse(owned_path)
        legacy_tool = _legacy_session_tool(owned_path, branch)
        if ref is None and legacy_tool is None:
            continue  # the main checkout or a path agentflow does not own
        tool = ref.tool if ref is not None else legacy_tool
        if owned_path in protected:
            retained.append(path)
            continue
        if _worktree_is_active(Path(path)):
            retained.append(path)
            continue
        # Read the idle clock *before* anything else touches this checkout. The disposability
        # check below runs `git status` inside it, which refreshes the stat cache and rewrites the
        # index — resetting the very clock the floor reads, so a complete-but-undisposable session
        # would appear freshly worked on at every sweep and never age out.
        idle = _worktree_idle_seconds(Path(path))
        reclaimable = tool in ("claude", "codex") and (
            ref is None or ref.kind not in _UNRECLAIMABLE_KINDS)
        complete = (_session_is_complete(repo, ref, branch) if ref is not None
                    else _pr_state_for_branch(repo, branch) in ("OPEN", "MERGED", "CLOSED"))
        if not complete:
            if reclaimable:
                stranded.append((idle, path))
            elif tool in ("claude", "codex"):
                retained.append(path)
            continue
        if remove_worktree_if_safe(workdir, Path(path)):
            removed.append(path)
        elif reclaimable:
            stranded.append((idle, path))
        else:
            retained.append(path)

    archived: list[tuple[str, str]] = []
    eligible = [path for seconds, path in sorted(stranded, reverse=True)
                if seconds >= STRANDED_IDLE_SECONDS]
    over_cap = eligible[:-RETAINED_WORKTREE_CAP] if RETAINED_WORKTREE_CAP else eligible
    budgeted = over_cap[:SWEEP_ARCHIVE_BUDGET]  # oldest first, so successive sweeps converge
    for path in budgeted:
        stranded_ref = archive_stranded_worktree(workdir, Path(path))
        if stranded_ref:
            archived.append((path, stranded_ref))
        else:
            retained.append(path)
    retained += [path for _seconds, path in stranded if path not in set(budgeted)]
    return WorktreeRecovery(tuple(removed), tuple(retained), tuple(archived))


def dispatch_preflight(repo: str, workdir: str, protected: set[str], _log=None) -> bool:
    """Whether this repository's environment can still carry a new session — asked before any
    cold work is submitted into it (ADR 0050).

    The daemon's own git calls are unsandboxed, so it never sees the failure it causes: past a
    machine-dependent count of registered worktrees (53 listed in the 2026-07-31 incident, #442)
    the provider's sandbox profile exceeds the OS exec-argument limit and every session in that
    repository dies on its first shell command, with no PR, no comment, and no way to say why.
    Three attempts were burned that way on one issue before a human diagnosed it. So the daemon
    checks the *precondition* rather than waiting for victims.

    Count-only and local: one ``git worktree list``, no GitHub call, no mutation. Reclamation is
    the heartbeat's job — this only refuses. The refusal names the breakdown because the remedy
    differs: reclamation can only reach agentflow's own sessions, so a breach driven by *foreign*
    registrations (another tool's worktrees, hand-cut checkouts) is one the sweep will never fix
    and the operator has to prune by hand.

    A `git` we cannot read fails **open**. A broken git in the daemon is its own outage; freezing
    the whole fleet on it would trade a repository-scoped failure for a fleet-wide one.

    The ceiling is the maximum registration count that may still exist *after* the session this
    admits opens its worktree. A cold submission adds exactly one registration, so admission
    reserves that slot: it holds only while the current count is strictly below the ceiling. At
    the ceiling, admitting would push the repository to ceiling+1 — the range #442 measured dead
    shells in — so it refuses.
    """
    _log = _log or (lambda _line: None)
    registered = _registered_worktrees(workdir)
    if registered is None:
        _log(f"{repo}: worktree preflight could not read the registry — dispatching anyway")
        return True
    if len(registered) < WORKTREE_DISPATCH_CEILING:
        return True
    owned = sum(1 for path, branch in registered
                if WorktreeRef.parse(os.path.realpath(path)) is not None
                or _legacy_session_tool(os.path.realpath(path), branch) is not None)
    held_by_store = sum(1 for path, _ in registered if os.path.realpath(path) in protected)
    _log(f"{repo}: REFUSING to dispatch — {len(registered)} registered worktrees reaches the "
         f"{WORKTREE_DISPATCH_CEILING} ceiling, leaving no slot for this session's worktree "
         f"({owned} agentflow-owned, "
         f"{len(registered) - owned} foreign, {held_by_store} protected by live records). "
         "Sessions in this repository would lose their shell before running a command. "
         "Reclamation only reaches agentflow-owned sessions; prune foreign worktrees by hand.")
    return False
