"""Test the Runner through its interface — the pure outcome classifier.

Per the charter: the interface is the test surface. Worktree/CLI spawning lives
behind adapters; the *decision* of what a session outcome means is `classify_build`,
and that is what actually needs to be right.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agentflow import runner as runner_mod
from agentflow.runner import (BuildStatus, BuildTask, ClaudeRunner, CodexRunner, Complexity,
                              Effort, _run, classify_build, recover_stale_worktrees,
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


class _LifecycleRunner(ClaudeRunner):
    def __init__(self, *, push: bool = True):
        self.push = push

    def provision(self, wt):
        pass

    def launch(self, prompt, cwd, model):
        wt = Path(cwd)
        _git(wt, "config", "user.email", "agentflow@example.com")
        _git(wt, "config", "user.name", "agentflow test")
        (wt / "result.txt").write_text(prompt)
        _git(wt, "add", "result.txt")
        _git(wt, "commit", "-m", prompt)
        if self.push:
            _git(wt, "push", "-u", "origin", "HEAD")
        return True, "done"

    def _pr_for_branch(self, repo, branch):
        return f"https://github.com/{repo}/pull/1"

    def _new_marker_comments(self, repo, issue, since):
        return []


def _task(repo: Path, issue: int) -> BuildTask:
    return BuildTask("owner/repo", str(repo), issue, f"session-{issue}", Complexity.STANDARD,
                     Effort.MEDIUM, f"session {issue}")


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


def test_effort_has_four_levels():
    assert [e.value for e in Effort] == ["low", "medium", "high", "extra"]


def test_pr_opened_is_success():
    out = classify_build("https://github.com/o/r/pull/7", [])
    assert out.status is BuildStatus.PR_OPENED
    assert out.pr_url.endswith("/pull/7")


def test_pr_wins_even_if_a_marker_was_also_posted():
    out = classify_build("https://github.com/o/r/pull/7", ["MISSING-CONTEXT: need a value"])
    assert out.status is BuildStatus.PR_OPENED


def test_marker_comment_is_a_bail():
    out = classify_build(None, ["MISSING-CONTEXT: need the ISF threshold\nmore detail"])
    assert out.status is BuildStatus.BAIL
    assert out.marker == "MISSING-CONTEXT"
    assert out.detail == "MISSING-CONTEXT: need the ISF threshold"


def test_each_marker_recognized():
    for marker in ("MISSING-CONTEXT", "SCOPE-EXPANSION", "INTEGRATION-COLLISION"):
        out = classify_build(None, [f"{marker}: blocked"])
        assert out.status is BuildStatus.BAIL
        assert out.marker == marker


def test_non_marker_comment_is_not_a_bail():
    out = classify_build(None, ["just a normal comment", "LGTM"])
    assert out.status is BuildStatus.INCOMPLETE


def test_nothing_left_behind_is_incomplete():
    out = classify_build(None, [])
    assert out.status is BuildStatus.INCOMPLETE


def test_run_timeout_returns_nonzero_and_does_not_propagate():
    """A hung subprocess is killed and classified as a failure, not an exception."""
    def raise_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
    with patch("subprocess.run", raise_timeout):
        r = _run(["sleep", "100"], timeout=1)
    assert r.returncode != 0
    assert "timed out" in r.stderr


def test_dead_pr_on_branch_classifies_as_no_pr():
    # _pr_for_branch now filters to open PRs only, so a merged/closed PR returns
    # None — the build classifies as INCOMPLETE (stuck handback), not PR_OPENED.
    out = classify_build(None, [])
    assert out.status is BuildStatus.INCOMPLETE
    assert out.pr_url is None


def test_completed_build_sessions_are_removed_but_unpushed_progress_is_retained(tmp_path):
    repo = _repo_with_origin(tmp_path)

    for issue in (1, 2):
        assert _LifecycleRunner().build(_task(repo, issue)).status is BuildStatus.PR_OPENED

    registered = _git(repo, "worktree", "list", "--porcelain")
    assert registered.count("worktree ") == 1

    assert _LifecycleRunner(push=False).build(_task(repo, 3)).status is BuildStatus.PR_OPENED
    registered = _git(repo, "worktree", "list", "--porcelain")
    assert "issue-3-session-3" in registered


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
    with worktree_session(active_legacy):
        report = recover_stale_worktrees("owner/repo", str(repo))

    assert set(report.removed) == {
        str(completed), str(legacy), str(legacy_two), str(intake), str(intake_two),
        str(intake_three), str(review), str(review_two),
    }
    assert len(report.removed) == 8
    assert (dirty.exists() and unpushed.exists() and active.exists() and
            active_open_pr.exists() and active_legacy.exists())
    assert foreign_wt.exists()
    registered = _git(repo, "worktree", "list", "--porcelain")
    assert str(completed) not in registered and str(legacy) not in registered
    assert str(foreign_wt) not in registered  # ownership comes from the foreign repo's metadata
