"""Churn ranking must find sessions by a repo's configured workdir, not a hardcoded
per-user path pattern — the whole point of moving this out of a personal dotfiles skill."""

from __future__ import annotations

import json

from agentflow import churn
from agentflow.loop import RepoConfig


def _claude_line(**overrides):
    base = {"type": "assistant", "message": {"usage": {}, "content": []}}
    base.update(overrides)
    return json.dumps(base)


def _write_claude_session(claude_root, workdir, worktree_label, lines):
    encoded = churn._encode_cwd(str(workdir)) + f"--agentflow-worktrees-{worktree_label}"
    project_dir = claude_root / encoded
    project_dir.mkdir(parents=True)
    session = project_dir / "session.jsonl"
    session.write_text("\n".join(lines) + "\n")
    return session


def _assistant_tool_use(name, tool_id, tool_input, output_tokens=10):
    return json.dumps({
        "type": "assistant",
        "message": {
            "usage": {"output_tokens": output_tokens},
            "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}],
        },
    })


def _tool_result(tool_id, *, is_error=False, content="ok"):
    return json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_id,
                                  "is_error": is_error, "content": content}]},
    })


def test_claude_session_dirs_matches_repos_configured_workdir_not_a_username_pattern(tmp_path):
    # A workdir far from any /Users/connor/Code/ConnorGriffin/<repo> shape must still match —
    # this is exactly what breaks with a hardcoded per-user prefix pattern.
    workdir = tmp_path / "checkouts" / "sample-app"
    workdir.mkdir(parents=True)
    claude_root = tmp_path / "claude-projects"
    session = _write_claude_session(
        claude_root, workdir, "issue-1",
        [_assistant_tool_use("Bash", "1", {"command": "echo hi"})],
    )

    repos = [RepoConfig(repo="acme/sample-app", workdir=str(workdir))]
    dirs = list(churn.claude_session_dirs(repos, str(claude_root)))

    assert dirs == [(str(session.parent), "acme/sample-app")]


def test_collect_claude_scores_errors_and_repeats_as_churn(tmp_path):
    workdir = tmp_path / "checkouts" / "sample-app"
    workdir.mkdir(parents=True)
    claude_root = tmp_path / "claude-projects"
    lines = []
    for i in range(4):
        lines.append(_assistant_tool_use("Bash", f"c{i}", {"command": "npm test"}))
        lines.append(_tool_result(f"c{i}", is_error=True, content="FAIL"))
    _write_claude_session(claude_root, workdir, "issue-1", lines)

    repos = [RepoConfig(repo="acme/sample-app", workdir=str(workdir))]
    sessions = churn.collect_claude(repos, str(claude_root))

    assert len(sessions) == 1
    s = sessions[0]
    assert s["tool_errors"] == 4
    assert s["repeat_cmd_calls"] == 2  # 4 identical calls, beyond the first 2
    assert s["churn_score"] > 0
    assert s["fleet"] is True


def test_collect_sessions_excludes_manual_worktrees_unless_requested(tmp_path):
    workdir = tmp_path / "checkouts" / "sample-app"
    workdir.mkdir(parents=True)
    claude_root = tmp_path / "claude-projects"
    encoded = churn._encode_cwd(str(workdir)) + "-worktrees-manual-spin"
    project_dir = claude_root / encoded
    project_dir.mkdir(parents=True)
    (project_dir / "session.jsonl").write_text(
        _assistant_tool_use("Bash", "1", {"command": "echo hi"}) + "\n"
    )

    repos = [RepoConfig(repo="acme/sample-app", workdir=str(workdir))]

    fleet_only = churn.collect_sessions(repos, claude_root=str(claude_root),
                                         codex_root=str(tmp_path / "empty-codex"))
    assert fleet_only == []

    with_manual = churn.collect_sessions(repos, include_manual=True, claude_root=str(claude_root),
                                          codex_root=str(tmp_path / "empty-codex"))
    assert len(with_manual) == 1
    assert with_manual[0]["fleet"] is False


def test_format_report_ranks_the_higher_churn_session_first(tmp_path):
    quiet = churn._new_session("claude", "quiet.jsonl", "issue-1", "acme/sample-app")
    quiet["output_tokens"] = 100
    quiet["churn_score"] = 0

    noisy = churn._new_session("claude", "noisy.jsonl", "issue-2", "acme/sample-app")
    noisy["output_tokens"] = 50
    noisy["tool_errors"] = 10
    noisy["churn_score"] = 30

    report = churn.format_report([quiet, noisy], top=5)

    ranking = report.split("== Top sessions by churn score ==")[1].split("==")[0]
    assert ranking.index("issue-2") < ranking.index("issue-1")


def test_format_excerpt_keeps_only_error_windows(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join([
        _assistant_tool_use("Bash", "1", {"command": "echo fine"}),
        _tool_result("1", is_error=False, content="ok"),
        _assistant_tool_use("Bash", "2", {"command": "false"}),
        _tool_result("2", is_error=True, content="command failed"),
    ]) + "\n")

    excerpt = churn.format_excerpt(str(path), max_chars=9000, window=1)

    assert "ERR" in excerpt
    assert "command failed" in excerpt
    assert "2 tool calls, 1 errors" in excerpt
