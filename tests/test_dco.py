"""DCO policy is delivered through every commit-capable session prompt."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.coordinator.providers import _durable_prompt
from agentflow.prompts import (BUILD_PROMPT, MOCKUP_DISCLAIMER, PRODUCE_PROMPT, RESPOND_PROMPT,
                               REVISE_PROMPT, SCOPE_GUIDANCE)
from agentflow.reviewer import REVIEW_PROMPT
from agentflow.runner import MockupScope


def _commit_prompts() -> dict[str, str]:
    return {
        "build": BUILD_PROMPT.format(
            repo="o/r", n=357, title="DCO", body="", effort="low", surfaces="none"),
        "revise": REVISE_PROMPT.format(n=357, repo="o/r", findings="- fix", surfaces="none"),
        "respond": RESPOND_PROMPT.format(
            n=357, baseline="abc", comment="fix it", disclaimer="> *reply*"),
        "mockup": PRODUCE_PROMPT.format(
            repo="o/r", n=357, title="DCO", body="", branch="mockup-357", surfaces="none",
            scope_guidance=SCOPE_GUIDANCE[MockupScope.SURFACE], disclaimer=MOCKUP_DISCLAIMER),
        "review": REVIEW_PROMPT.format(
            pr=357, starting_sha="abc", acceptance="DCO", surfaces="none"),
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
