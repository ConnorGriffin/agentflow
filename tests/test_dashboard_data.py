from agentflow.dashboard_data import _tier_of, pr_stage


def test_tier_of_labels():
    assert _tier_of([{"name": "ready-for-agent"}, {"name": "tier:light"}]) == "light"
    assert _tier_of([{"name": "tier:deep"}]) == "deep"
    assert _tier_of([{"name": "bug"}]) is None
    assert _tier_of([]) is None


def test_pr_stage_from_branch():
    assert pr_stage("agentflow/claude/issue-1-foo") == "claude"
    assert pr_stage("agentflow/codex/issue-2-bar") == "codex"
    assert pr_stage("some-human-branch") == "other"
    assert pr_stage("") == "other"
