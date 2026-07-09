"""Test the Runner through its interface — the pure outcome classifier.

Per the charter: the interface is the test surface. Worktree/CLI spawning lives
behind adapters; the *decision* of what a session outcome means is `classify_build`,
and that is what actually needs to be right.
"""

from agentflow.runner import BuildStatus, ClaudeRunner, CodexRunner, Tier, classify_build


def test_tiers_resolve_to_cost_appropriate_models():
    claude, codex = ClaudeRunner(), CodexRunner()
    # light work must NOT land on the deep-tier model — the whole point.
    assert claude.model_for(Tier.LIGHT) == "haiku"
    assert claude.model_for(Tier.STANDARD) == "sonnet"
    assert claude.model_for(Tier.DEEP) == "opus"
    assert codex.model_for(Tier.LIGHT) == "gpt-5.6-luna"
    assert codex.model_for(Tier.STANDARD) == "gpt-5.6-terra"
    assert codex.model_for(Tier.DEEP) == "gpt-5.6-sol"


def test_every_tier_maps_for_every_tool():
    for runner in (ClaudeRunner(), CodexRunner()):
        for tier in Tier:
            assert runner.model_for(tier)  # no tier left unmapped


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
