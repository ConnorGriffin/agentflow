from agentflow.dashboard_data import _complexity_of, _effort_of, pr_stage


def test_complexity_of_labels():
    assert _complexity_of([{"name": "ready-for-agent"},
                           {"name": "agentflow:complexity:standard"}]) == "standard"
    assert _complexity_of([{"name": "agentflow:complexity:deep"}]) == "deep"
    assert _complexity_of([{"name": "bug"}]) is None
    assert _complexity_of([]) is None


def test_effort_of_labels():
    assert _effort_of([{"name": "agentflow:effort:extra"}]) == "extra"
    assert _effort_of([{"name": "bug"}]) is None


def test_pr_stage_from_branch():
    assert pr_stage("agentflow/claude/issue-1-foo") == "claude"
    assert pr_stage("agentflow/codex/issue-2-bar") == "codex"
    assert pr_stage("some-human-branch") == "other"
    assert pr_stage("") == "other"
