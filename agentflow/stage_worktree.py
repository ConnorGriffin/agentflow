"""The checkout a branch-owning stage record runs in, prepared and probed (ADR 0030/0041).

Build, Revise, Respond and Mockup all run in the branch/worktree their own record names, so
they share one preparation: reuse a retained checkout exactly as it is, or recover/create the
branch before admission. Revise and Review both also ask the same question of a checkout after
a push — does this worktree durably own that head, with nothing uncommitted left behind?

:mod:`agentflow.worktree_ref` owns *where* a record's checkout is; this owns getting it ready
and reading its state, so no stage module has to reach into another for either.
"""

from __future__ import annotations

import subprocess

from agentflow.coordinator.verification import PREPARED, unprepared
from agentflow.runner import _run
from agentflow.worktree_ref import source_facts


def worktree_ready(record):
    """Prepare the record's owned branch/worktree before admission (ADR 0030). An existing
    worktree is reused *as it is* — a continuation must keep its local changes, so it is never
    rebuilt. An absent Build worktree may start a new branch from ``origin/main``; a continuation
    stage may only recover the existing branch from its local or remote PR ref. Any git failure
    refuses, so admission is skipped with no permit and no attempt consumed — and it refuses by
    name, so a stage waiting here says which git step said no and what it read (#405).

    A checkout is absent more often than it used to be: reclamation may since have archived a
    long-idle held session to a recovery ref (ADR 0050). That is the intended path, not a
    degradation — the branch survives, so this re-adds from it and the stage restarts from the
    branch tip, with the uncommitted delta recoverable under ``refs/agentflow/stranded/``."""
    from agentflow.runner import ClaudeRunner, CodexRunner, _worktree_is_registered
    parsed = source_facts(record)
    if parsed is None:
        return unprepared("source-unreadable",
                          f"the record's worktree pointer does not parse as this stage's own "
                          f"checkout: {record.source!r}")
    workdir, branch, wt = parsed
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    if wt.exists():
        if not _worktree_is_registered(workdir, wt):
            return unprepared("worktree-unregistered",
                              f"{wt} exists on disk but {workdir} does not list it as a worktree")
        current = _run(["git", "-C", str(wt), "branch", "--show-current"])
        if current.returncode != 0:
            return unprepared("branch-read-failed",
                              f"`git -C {wt} branch --show-current` exited "
                              f"{current.returncode}")
        if current.stdout.strip() != branch:
            return unprepared("branch-mismatch",
                              f"{wt} has {current.stdout.strip() or '(detached)'} checked out, "
                              f"not the record's branch {branch}")
        try:
            runner.provision(wt)
        except subprocess.CalledProcessError as e:
            return unprepared("retained-provision-failed",
                              f"provisioning the retained worktree {wt} exited {e.returncode}")
        return PREPARED  # retained worktree — reuse across the continuation, never recreate it
    wt.parent.mkdir(parents=True, exist_ok=True)
    fetch = _run(["git", "-C", workdir, "fetch", "origin", "--quiet"])
    if fetch.returncode != 0:
        return unprepared("fetch-failed",
                          f"`git -C {workdir} fetch origin` exited {fetch.returncode}")
    have = _run(["git", "-C", workdir, "show-ref", "--quiet",
                 f"refs/heads/{branch}"]).returncode == 0
    add = ["git", "-C", workdir, "worktree", "add"]
    if have:
        add += [str(wt), branch]
    else:
        remote = _run(["git", "-C", workdir, "show-ref", "--quiet",
                       f"refs/remotes/origin/{branch}"]).returncode == 0
        if remote:
            add += ["-b", branch, str(wt), f"origin/{branch}"]
        elif record.stage in {"build", "mockup"}:
            add += ["-b", branch, str(wt), "origin/main"]
        else:
            return unprepared("branch-absent",
                              f"branch {branch} exists neither locally nor on origin, and a "
                              f"{record.stage} may only recover an existing branch")
    added = _run(add)
    if added.returncode != 0:
        return unprepared("worktree-add-failed",
                          f"`git worktree add` for {branch} at {wt} exited {added.returncode}")
    try:
        runner.provision(wt)
    except subprocess.CalledProcessError as e:
        return unprepared("provision-failed",
                          f"provisioning the freshly added worktree {wt} exited {e.returncode}")
    return PREPARED


def worktree_owns_head(wt, head: str) -> bool:
    """Whether the retained worktree durably owns the pushed remote ``head``: its checked-out
    ``HEAD`` equals that SHA and its tree is clean (no dirty tracked file, staged change, or
    untracked new file). Read after fetching the branch, this proves the reviser's own local state
    and the pushed branch agree — a stale or third-party push cannot satisfy it. Any failed read
    fails closed."""
    local = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    if local.returncode != 0 or local.stdout.strip() != head:
        return False
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    return status.returncode == 0 and not status.stdout.strip()
