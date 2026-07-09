from agentflow.dashboard_data import pr_stage


def test_pr_stage_from_branch():
    assert pr_stage("agentflow/claude/issue-1-foo") == "claude"
    assert pr_stage("agentflow/codex/issue-2-bar") == "codex"
    assert pr_stage("some-human-branch") == "other"
    assert pr_stage("") == "other"
