"""DCO policy: asked of every commit-capable session prompt, and enforced when a session
checkout is prepared (ADR 401)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import runner, stage_worktree
from agentflow.coordinator.providers import _durable_prompt
from agentflow.coordinator.record import Record
from agentflow.prompts import (BUILD_PROMPT, MOCKUP_DISCLAIMER, PRODUCE_PROMPT, RESPOND_PROMPT,
                               REVISE_PROMPT, SCOPE_GUIDANCE)
from agentflow.reviewer import REVIEW_PROMPT
from agentflow.runner import ClaudeRunner, MockupScope


def _commit_prompts() -> dict[str, str]:
    return {
        "build": BUILD_PROMPT.format(
            repo="o/r", n=357, title="DCO", body="", effort="low", surfaces="none", screenshot_entry_note=""),
        "revise": REVISE_PROMPT.format(n=357, repo="o/r", findings="- fix", surfaces="none", screenshot_entry_note=""),
        "respond": RESPOND_PROMPT.format(
            n=357, baseline="abc", comment="fix it", disclaimer="> *reply*",
            screenshot_entry_note=""),
        "mockup": PRODUCE_PROMPT.format(
            repo="o/r", n=357, title="DCO", body="", branch="mockup-357", surfaces="none",
            screenshot_entry_note="",
            scope_guidance=SCOPE_GUIDANCE[MockupScope.SURFACE], disclaimer=MOCKUP_DISCLAIMER),
        "review": REVIEW_PROMPT.format(
            pr=357, issue=356, starting_sha="abc", acceptance="DCO", surfaces="none", screenshot_entry_note=""),
    }


def test_every_commit_capable_prompt_keeps_the_dco_contract_on_continuation():
    """Render each public prompt path, then append recovery facts as the provider does."""
    for name, prompt in _commit_prompts().items():
        continued = _durable_prompt(SimpleNamespace(
            input_ptr=prompt, recovery_envelope="Recovery: inspect retained work before resuming."))
        assert "git commit -s" in continued, name
        assert "git commit --amend -s" in continued, name
        assert "Signed-off-by" in continued, name
        assert "author email" in continued, name
        assert "inspect" in continued and "history" in continued, name
        assert "separate sign-off-only" in continued, name


def _git(repo: Path, *args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, text=True,
                          capture_output=True, **kwargs)


def _repository_with_base(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "config", "user.name", "AgentFlow")
    _git(repo, "config", "user.email", "agent@example.com")
    (repo / "work.txt").write_text("base\n")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-qm", "base")
    return repo, _git(repo, "rev-parse", "HEAD").stdout.strip()


def _dco_check(repo: Path, base: str) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).parents[1] / "scripts" / "check-dco.py"
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return subprocess.run([sys.executable, str(script), base, head], cwd=repo, text=True,
                          capture_output=True)


def test_git_commit_signoff_passes_the_dco_checker(tmp_path):
    repo, base = _repository_with_base(tmp_path)
    (repo / "work.txt").write_text("signed\n")
    _git(repo, "add", "work.txt")
    _git(repo, "commit", "-s", "-m", "AgentFlow change")

    result = _dco_check(repo, base)

    assert result.returncode == 0, result.stderr
    assert "DCO check passed for 1 commit" in result.stdout


@pytest.mark.parametrize("author", [None, "Other <other@example.com>"])
def test_dco_checker_rejects_unsigned_or_author_mismatched_commit(tmp_path, author):
    repo, base = _repository_with_base(tmp_path)
    (repo / "work.txt").write_text("invalid\n")
    _git(repo, "add", "work.txt")
    command = ["commit", "-m", "Unsigned change"]
    if author is not None:
        command = ["commit", "-s", "--author", author, "-m", "Mismatched sign-off"]
    _git(repo, *command)

    result = _dco_check(repo, base)

    assert result.returncode == 1
    assert "DCO sign-off missing or does not match" in result.stderr


# --- enforcement: a prepared session checkout signs off its own commits ------------------

_SLUG = "issue-401-signoff"
_BRANCH = f"agentflow/claude/{_SLUG}"


@pytest.fixture(autouse=True)
def _forget_unenforced_repositories():
    """The one-line-per-repository breadcrumb is process-local; no test inherits another's."""
    runner._SIGNOFF_UNENFORCED.clear()
    yield
    runner._SIGNOFF_UNENFORCED.clear()


def _session_checkout(tmp_path: Path) -> tuple[Path, Path, str]:
    """A repository with an origin, plus the branch checkout a session would be handed."""
    repo, base = _repository_with_base(tmp_path)
    _git(repo, "remote", "add", "origin", "git@github.com:o/r.git")
    wt = repo / ".agentflow" / "worktrees" / "claude" / _SLUG
    _git(repo, "worktree", "add", "-q", "-b", _BRANCH, str(wt))
    return repo, wt, base


def _provisioned(tmp_path: Path, times: int = 1) -> tuple[Path, Path, str]:
    repo, wt, base = _session_checkout(tmp_path)
    for _ in range(times):
        ClaudeRunner().provision(wt)
    return repo, wt, base


def _commit(wt: Path, text: str, *args: str) -> None:
    (wt / "work.txt").write_text(text)
    _git(wt, "add", "work.txt")
    _git(wt, "commit", *args)


def _message(wt: Path) -> str:
    return _git(wt, "show", "-s", "--format=%B", "HEAD").stdout


def _record(wt: Path) -> Record:
    return Record(identity="o/r|401|build|-", stage="build", pool="claude", demand=5,
                  repo="o/r", subject="401", source=str(wt), claim=True, lineage="claude")


def test_a_prepared_checkout_signs_off_a_commit_that_did_not_ask_for_it(tmp_path):
    """The stall this exists to end: a session commits without `-s` and the branch is stuck."""
    _repo, wt, base = _provisioned(tmp_path)

    _commit(wt, "signed\n", "-m", "AgentFlow change")

    result = _dco_check(wt, base)
    assert result.returncode == 0, result.stderr
    assert _message(wt).count("Signed-off-by: AgentFlow <agent@example.com>") == 1


@pytest.mark.parametrize("existing", [
    ["-s", "-m", "Already certified"],
    ["-m", "Certified by somebody else\n\nSigned-off-by: Other <other@example.com>"],
])
def test_the_authors_own_signoff_is_added_exactly_once(tmp_path, existing):
    """Keyed on the author's address, not on the trailer: a foreign sign-off satisfies nothing."""
    _repo, wt, base = _provisioned(tmp_path)

    _commit(wt, "once\n", *existing)

    assert _dco_check(wt, base).returncode == 0
    assert _message(wt).count("Signed-off-by: AgentFlow <agent@example.com>") == 1


def test_a_commit_by_a_different_author_is_left_exactly_as_written(tmp_path):
    """A sign-off is a personal certification; the engine makes none in a third party's name."""
    _repo, wt, base = _provisioned(tmp_path)

    _commit(wt, "theirs\n", "--author", "Other <other@example.com>", "-m", "Their change")

    assert _message(wt) == "Their change\n\n"
    assert _dco_check(wt, base).returncode == 1  # still red, still a human's call


def _install_repository_hooks(repo: Path, marker: Path) -> None:
    """The live hooks all nine enrolled repositories already carry."""
    for name in ("post-commit", "commit-msg"):
        hook = repo / ".git" / "hooks" / name
        hook.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        hook.chmod(0o755)


@pytest.mark.parametrize("provisionings", [1, 2])
def test_the_repositorys_own_hooks_still_fire_exactly_once(tmp_path, provisionings):
    """Preparation must chain the existing hooks, and re-preparation must not layer or recurse."""
    marker = tmp_path / "fired.txt"
    repo, wt, base = _session_checkout(tmp_path)
    _install_repository_hooks(repo, marker)
    for _ in range(provisionings):
        ClaudeRunner().provision(wt)

    _commit(wt, "hooked\n", "-m", "AgentFlow change")

    assert marker.read_text().split() == ["commit-msg", "post-commit"]
    assert _dco_check(wt, base).returncode == 0
    assert _message(wt).count("Signed-off-by:") == 1


def test_preparation_leaves_the_session_checkout_clean(tmp_path):
    """Any leftover in the tree reads as litter to the checkout-reuse gates."""
    _repo, wt, _base = _provisioned(tmp_path, times=2)

    assert _git(wt, "status", "--porcelain", "--untracked-files=all").stdout == ""


def test_the_shared_checkout_keeps_committing_as_it_does_now(tmp_path):
    """One named key in the shared config is the whole cost; the maintainer sees nothing else."""
    repo, base = _repository_with_base(tmp_path)
    before = set(_git(repo, "config", "--local", "--list").stdout.splitlines())
    hooks_before = sorted(p.name for p in (repo / ".git" / "hooks").iterdir())
    wt = repo / ".agentflow" / "worktrees" / "claude" / _SLUG
    _git(repo, "worktree", "add", "-q", "-b", _BRANCH, str(wt))

    ClaudeRunner().provision(wt)

    after = set(_git(repo, "config", "--local", "--list").stdout.splitlines())
    assert after - before == {"extensions.worktreeconfig=true"}
    assert sorted(p.name for p in (repo / ".git" / "hooks").iterdir()) == hooks_before
    _commit(repo, "maintainer\n", "-m", "Maintainer change")
    assert "Signed-off-by" not in _message(repo)
    assert _dco_check(repo, base).returncode == 1


def _breadcrumbs(capsys) -> list[str]:
    """What an operator reading the daemon's log sees, in the daemon's own line shape."""
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    for line in lines:
        assert re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d agentflow: ", line), line
    return lines


def test_preparation_refuses_rather_than_reconfigure_a_repository_and_says_so_once(
        tmp_path, capsys):
    """core.bare/core.worktree must be relocated by a human before per-session config is safe."""
    repo, wt, _base = _session_checkout(tmp_path)
    _git(repo, "config", "core.worktree", str(repo))
    capsys.readouterr()

    ClaudeRunner().provision(wt)
    ClaudeRunner().provision(wt)

    assert "extensions.worktreeconfig" not in _git(repo, "config", "--local", "--list").stdout
    assert not (repo / ".git" / "worktrees" / _SLUG / "agentflow-hooks").exists()
    lines = _breadcrumbs(capsys)
    assert len(lines) == 1
    assert "git@github.com:o/r.git" in lines[0]
    assert "core.bare or core.worktree" in lines[0] and "relocated" in lines[0]


def test_a_checkout_whose_hooks_cannot_be_installed_still_runs_its_stage(tmp_path, capsys):
    """Fail open: a raised install would silently stop a repository producing pull requests."""
    repo, wt, base = _session_checkout(tmp_path)
    git_dir = repo / ".git" / "worktrees" / _SLUG
    git_dir.chmod(0o555)                       # a genuine failure: the hooks cannot be written
    capsys.readouterr()

    try:
        assert stage_worktree.worktree_ready(_record(wt))
        assert stage_worktree.worktree_ready(_record(wt))
    finally:
        git_dir.chmod(0o755)
    _commit(wt, "unenforced\n", "-m", "AgentFlow change")

    assert _dco_check(wt, base).returncode == 1  # unenforced, as the breadcrumb says
    lines = _breadcrumbs(capsys)
    assert len(lines) == 1
    assert "git@github.com:o/r.git" in lines[0] and "hooks" in lines[0]


def test_the_breadcrumb_reads_as_a_daemon_log_line(tmp_path, capsys):
    """It is written straight to the daemon's stream, so nothing but this pins the shape."""
    from agentflow import daemon

    repo, wt, _base = _session_checkout(tmp_path)
    _git(repo, "config", "core.bare", "true")
    capsys.readouterr()

    ClaudeRunner().provision(wt)
    daemon.log("o/r: reference line")

    # Both lines are held to one shape above: if the daemon's format moves, this fails here
    # rather than leaving the unenforced-repository line silently out of step with it.
    unenforced, reference = _breadcrumbs(capsys)
    assert reference.endswith(" agentflow: o/r: reference line")
    assert "session commits are not signed off automatically" in unenforced
