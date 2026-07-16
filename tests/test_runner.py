"""Provider command construction and fail-closed worktree plumbing."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agentflow import runner as runner_mod
from agentflow.runner import (ClaudeRunner, CodexRunner, Complexity,
                              Effort, _run, recover_stale_worktrees,
                              remove_worktree_if_safe,
                              worktree_session)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


def _repo_with_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "agentflow@example.com")
    _git(repo, "config", "user.name", "agentflow test")
    (repo / "README.md").write_text("start\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "start")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return repo


def _branch_worktree(repo: Path, path: Path, branch: str, *, push: bool = True,
                     dirty: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", branch, str(path), "origin/main")
    _git(path, "config", "user.email", "agentflow@example.com")
    _git(path, "config", "user.name", "agentflow test")
    (path / "result.txt").write_text(branch)
    _git(path, "add", "result.txt")
    _git(path, "commit", "-m", branch)
    if push:
        _git(path, "push", "-u", "origin", branch)
    if dirty:
        (path / "result.txt").write_text("dirty")


def _detached_worktree(repo: Path, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(path), "origin/main")


def test_complexity_resolves_to_cost_appropriate_models():
    claude, codex = ClaudeRunner(), CodexRunner()
    assert claude.model_for(Complexity.STANDARD) == "sonnet"
    assert claude.model_for(Complexity.DEEP) == "opus"
    assert codex.model_for(Complexity.STANDARD) == "gpt-5.6-terra"
    assert codex.model_for(Complexity.DEEP) == "gpt-5.6-sol"


def test_every_complexity_maps_for_every_tool():
    for runner in (ClaudeRunner(), CodexRunner()):
        for complexity in Complexity:
            assert runner.model_for(complexity)  # no complexity left unmapped


def test_claude_command_confines_the_session_to_its_assigned_worktree(tmp_path):
    repo = _repo_with_origin(tmp_path)
    branch = "agentflow/claude/issue-7-owned"
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-7-owned"
    _branch_worktree(repo, wt, branch)
    cmd = ClaudeRunner().structured_argv("build it", "sonnet", str(wt))
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in cmd
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["enableWeakerNetworkIsolation"] is True
    prompt = cmd[cmd.index("-p") + 1]
    assert str(wt.resolve()) in prompt and branch in prompt

    assert "--output-format" in cmd


def test_codex_command_confines_the_session_to_its_assigned_worktree(tmp_path):
    repo = _repo_with_origin(tmp_path)
    branch = "agentflow/codex/issue-8-owned"
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-8-owned"
    _branch_worktree(repo, wt, branch)
    cmd = CodexRunner().structured_argv("build it", "terra", str(wt))
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("--cd") + 1] == str(wt.resolve())
    assert "--ignore-user-config" in cmd and "--ephemeral" in cmd
    assert 'approval_policy="never"' in cmd
    assert "sandbox_workspace_write.network_access=true" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    prompt = cmd[-1]
    assert str(wt.resolve()) in prompt and branch in prompt

    structured = CodexRunner().structured_argv(
        "build it", "gpt-5.6-terra", str(wt))
    assert "--dangerously-bypass-approvals-and-sandbox" not in structured
    assert structured[structured.index("--sandbox") + 1] == "workspace-write"
    assert structured[structured.index("--cd") + 1] == str(wt.resolve())
    assert "--json" in structured
    assert str(wt.resolve()) in structured[-1]


def test_codex_account_fact_uses_typed_limit_windows(monkeypatch):
    payload = json.dumps({
        "windows": [
            {"used_percent": 100, "window_minutes": 300, "resets_at": 1234},
            {"used_percent": 60, "window_minutes": 10080, "resets_at": 9999},
        ]
    })

    def fake_run(cmd, **kwargs):
        assert cmd[-1] == "limits"
        assert kwargs["env"]["TRIAGE_AGENT"] == "codex"
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    assert CodexRunner().account_fact() == {
        "kind": "rate_limited", "reset_at": 1234}


def test_effort_has_four_levels():
    assert [e.value for e in Effort] == ["low", "medium", "high", "extra"]


def test_run_timeout_returns_nonzero_and_does_not_propagate():
    """A hung subprocess is killed and classified as a failure, not an exception."""
    def raise_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
    with patch("subprocess.run", raise_timeout):
        r = _run(["sleep", "100"], timeout=1)
    assert r.returncode != 0
    assert "timed out" in r.stderr


def test_generic_runner_refuses_provider_commands():
    with pytest.raises(RuntimeError, match="coordinator launcher"):
        _run(["codex", "exec", "do work"])


def test_public_session_lifecycle_bounds_registrations_across_every_lane(tmp_path):
    repo = _repo_with_origin(tmp_path)
    root = repo / ".agentflow" / "worktrees"
    completed = [
        root / "claude-intake" / "issue-1",
        root / "codex-review" / "pr-2-reviewed",
        root / "claude" / "issue-3-built",
        root / "codex" / "mockup-4-drawn",
        root / "claude" / "issue-5-responded",
        root / "codex" / "issue-6-rebased",
    ]
    _detached_worktree(repo, completed[0])
    _detached_worktree(repo, completed[1])
    for path, branch in zip(completed[2:], [
        "agentflow/claude/issue-3-built",
        "agentflow/codex/mockup-4-drawn",
        "agentflow/claude/issue-5-responded",
        "agentflow/codex/issue-6-rebased",
    ], strict=True):
        _branch_worktree(repo, path, branch)

    dirty = root / "claude" / "issue-7-dirty"
    unpushed = root / "codex" / "issue-8-unpushed"
    active = root / "claude" / "issue-9-active"
    _branch_worktree(repo, dirty, "agentflow/claude/issue-7-dirty", dirty=True)
    _branch_worktree(repo, unpushed, "agentflow/codex/issue-8-unpushed", push=False)
    _branch_worktree(repo, active, "agentflow/claude/issue-9-active")

    foreign_root = tmp_path / "foreign-lifecycle"
    foreign_root.mkdir()
    foreign = _repo_with_origin(foreign_root)
    foreign_wt = root / "dotfiles" / "open-pr"
    foreign_wt.parent.mkdir(parents=True, exist_ok=True)
    _git(foreign, "worktree", "add", "-b", "codex/open-pr", str(foreign_wt), "origin/main")

    assert all(remove_worktree_if_safe(str(repo), path) for path in completed)
    assert remove_worktree_if_safe(str(repo), dirty) is False
    assert remove_worktree_if_safe(str(repo), unpushed) is False
    with worktree_session(active):
        assert remove_worktree_if_safe(str(repo), active) is False
    assert remove_worktree_if_safe(str(repo), foreign_wt) is False

    registered = _git(repo, "worktree", "list", "--porcelain")
    assert registered.count("worktree ") == 4  # main + dirty + unpushed + active
    assert foreign_wt.exists()


def test_reuse_refuses_recoverable_work_and_github_uncertainty(tmp_path):
    repo = _repo_with_origin(tmp_path)
    runner = ClaudeRunner()
    branch = "agentflow/claude/issue-7-retry"
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-7-retry"
    _branch_worktree(repo, wt, branch, push=False)
    head = _git(wt, "rev-parse", "HEAD")

    runner._open_pr_for_branch = lambda *_: (True, None)
    with pytest.raises(subprocess.CalledProcessError):
        runner.prepare_worktree(str(repo), branch, wt, "owner/repo")
    assert wt.exists() and _git(wt, "rev-parse", "HEAD") == head

    detached = repo / ".agentflow" / "worktrees" / "codex-intake" / "issue-8"
    _detached_worktree(repo, detached)
    _git(detached, "config", "user.email", "agentflow@example.com")
    _git(detached, "config", "user.name", "agentflow test")
    (detached / "recovery.txt").write_text("keep me")
    _git(detached, "add", "recovery.txt")
    _git(detached, "commit", "-m", "recoverable intake progress")
    detached_head = _git(detached, "rev-parse", "HEAD")

    with pytest.raises(subprocess.CalledProcessError):
        runner.prepare_worktree_detached(str(repo), "origin/main", detached)
    assert detached.exists() and _git(detached, "rev-parse", "HEAD") == detached_head

    _git(wt, "push", "-u", "origin", branch)
    runner._open_pr_for_branch = lambda *_: (False, None)
    with pytest.raises(subprocess.CalledProcessError):
        runner.prepare_worktree(str(repo), branch, wt, "owner/repo")
    assert wt.exists() and _git(wt, "rev-parse", "HEAD") == head


def test_recovery_removes_completed_owned_sessions_and_retains_uncertain_or_foreign_work(
        tmp_path, monkeypatch):
    repo = _repo_with_origin(tmp_path)
    root = repo / ".agentflow" / "worktrees"
    completed = root / "codex" / "issue-10-done"
    legacy = root / "codex" / "legacy-fix"
    legacy_two = root / "codex" / "second-legacy-fix"
    dirty = root / "codex" / "issue-11-dirty"
    unpushed = root / "codex" / "issue-12-unpushed"
    active = root / "codex" / "issue-13-active"
    active_open_pr = root / "codex" / "issue-14-active-open-pr"
    active_legacy = root / "codex" / "active-legacy"
    intake = root / "claude-intake" / "issue-10"
    intake_two = root / "claude-intake" / "issue-15"
    intake_three = root / "codex-intake" / "issue-16"
    review = root / "codex-review" / "pr-20-done"
    review_two = root / "claude-review" / "pr-21-done"
    _branch_worktree(repo, completed, "agentflow/codex/issue-10-done")
    _branch_worktree(repo, legacy, "codex/legacy-fix")
    _branch_worktree(repo, legacy_two, "codex/second-legacy-fix")
    _branch_worktree(repo, dirty, "agentflow/codex/issue-11-dirty", dirty=True)
    _branch_worktree(repo, unpushed, "agentflow/codex/issue-12-unpushed", push=False)
    active.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-b", "agentflow/codex/issue-13-active", str(active),
         "origin/main")
    _branch_worktree(repo, active_open_pr, "agentflow/codex/issue-14-active-open-pr")
    _branch_worktree(repo, active_legacy, "codex/active-legacy")
    _detached_worktree(repo, intake)
    _detached_worktree(repo, intake_two)
    _detached_worktree(repo, intake_three)
    _detached_worktree(repo, review)
    _detached_worktree(repo, review_two)

    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign = _repo_with_origin(foreign_root)
    foreign_wt = root / "dotfiles" / "foreign-open-pr"
    foreign_wt.parent.mkdir(parents=True, exist_ok=True)
    _git(foreign, "worktree", "add", "-b", "codex/foreign-open-pr", str(foreign_wt),
         "origin/main")

    original_run = runner_mod._run

    def fake_run(cmd, cwd=None, timeout=None):
        if cmd[:3] == ["gh", "pr", "list"]:
            branch = cmd[cmd.index("--head") + 1]
            state = {
                "agentflow/codex/issue-10-done": "OPEN",
                "codex/legacy-fix": "MERGED",
                "codex/second-legacy-fix": "MERGED",
                "agentflow/codex/issue-11-dirty": "OPEN",
                "agentflow/codex/issue-12-unpushed": "OPEN",
                "agentflow/codex/issue-14-active-open-pr": "OPEN",
                "codex/active-legacy": "OPEN",
            }.get(branch, "")
            return subprocess.CompletedProcess(cmd, 0, f"{state}\n" if state else "", "")
        if cmd[:3] == ["gh", "pr", "view"]:
            body = ('{"state":"OPEN","comments":['
                    '{"body":"> *agentflow: parked for human review.*"}]}')
            return subprocess.CompletedProcess(cmd, 0, body, "")
        if cmd[:3] == ["gh", "issue", "view"]:
            issue = int(cmd[3])
            label = "agentflow:building" if issue == 14 else "ready-for-agent"
            body = f'{{"state":"OPEN","labels":[{{"name":"{label}"}}],"comments":[]}}'
            return subprocess.CompletedProcess(cmd, 0, body, "")
        return original_run(cmd, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(runner_mod, "_run", fake_run)
    child = subprocess.Popen(["sleep", "30"])
    try:
        with worktree_session(active_legacy):
            marker = runner_mod._active_marker(active_legacy)
            assert marker is not None
            marker.write_text(str(child.pid))
            runner_mod._ACTIVE_WORKTREES.clear()  # simulate a freshly started recovery process
            report = recover_stale_worktrees(
                "owner/repo", str(repo), protected={os.path.realpath(legacy_two)})
    finally:
        child.terminate()
        child.wait()

    assert set(report.removed) == {
        str(completed), str(legacy), str(intake), str(intake_two),
        str(intake_three), str(review), str(review_two),
    }
    assert len(report.removed) == 7
    assert (dirty.exists() and unpushed.exists() and active.exists() and
            active_open_pr.exists() and active_legacy.exists() and legacy_two.exists())
    assert foreign_wt.exists()
    registered = _git(repo, "worktree", "list", "--porcelain")
    assert str(completed) not in registered and str(legacy) not in registered
    assert str(foreign_wt) not in registered  # ownership comes from the foreign repo's metadata
