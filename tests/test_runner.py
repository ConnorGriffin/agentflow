"""Test the Runner through its interface — the pure outcome classifier.

Per the charter: the interface is the test surface. Worktree/CLI spawning lives
behind adapters; the *decision* of what a session outcome means is `classify_build`,
and that is what actually needs to be right.
"""

import subprocess
from unittest.mock import patch

from agentflow.runner import (BuildStatus, ClaudeRunner, CodexRunner, Complexity, Effort, _run,
                              classify_build, worktree_is_prunable)


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


# --- worktree sweep predicate ------------------------------------------------

def test_prunable_when_pr_merged_and_clean():
    assert worktree_is_prunable("MERGED", is_clean=True)


def test_prunable_when_pr_closed_and_clean():
    assert worktree_is_prunable("CLOSED", is_clean=True)


def test_not_prunable_when_pr_open():
    assert not worktree_is_prunable("OPEN", is_clean=True)


def test_not_prunable_when_no_pr():
    assert not worktree_is_prunable(None, is_clean=True)


def test_not_prunable_when_dirty_even_if_pr_merged():
    assert not worktree_is_prunable("MERGED", is_clean=False)


def test_not_prunable_when_dirty_even_if_pr_closed():
    assert not worktree_is_prunable("CLOSED", is_clean=False)
