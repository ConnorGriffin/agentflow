"""Churn ranking must find sessions by a repo's configured workdir, not a hardcoded
per-user path pattern — the whole point of moving this out of a personal dotfiles skill."""

from __future__ import annotations

import json

import pytest

from agentflow import churn, cli
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
    # A workdir outside any per-user checkout layout must still match — this is exactly
    # what breaks with a hardcoded per-user prefix pattern.
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


def test_collect_codex_keeps_its_first_metadata_and_malformed_rollout_policy(tmp_path):
    workdir = tmp_path / "checkouts" / "sample-app"
    workdir.mkdir(parents=True)
    root = tmp_path / "codex-sessions" / "2026" / "08" / "16"
    root.mkdir(parents=True)
    (root / "valid.jsonl").write_text("\n".join((
        json.dumps({"type": "session_meta", "payload": {
            "originator": "codex_exec",
            "cwd": str(workdir / ".agentflow" / "worktrees" / "codex" / "issue-1"),
        }}),
        json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 20,
                                  "output_tokens": 30},
        }}}),
        json.dumps({"type": "response_item", "payload": {
            "type": "function_call", "arguments": json.dumps({"command": "pytest -q"}),
        }}),
    )) + "\n")
    (root / "malformed.jsonl").write_text("not json\n" + (root / "valid.jsonl").read_text())

    sessions = churn.collect_codex(
        [RepoConfig(repo="acme/sample-app", workdir=str(workdir))], str(tmp_path / "codex-sessions"))

    assert [(session["label"], session["output_tokens"], session["uncached_input_tokens"])
            for session in sessions] == [("codex/issue-1", 30, 80)]


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


def test_format_excerpt_says_a_codex_rollout_is_unreadable_rather_than_empty(tmp_path):
    # Codex sessions rank in the same table, so an operator drilling into the worst row
    # can land here; reporting "0 tool calls" would read as "nothing happened".
    path = tmp_path / "rollout.jsonl"
    path.write_text("\n".join([
        json.dumps({"type": "session_meta",
                    "payload": {"originator": "codex_exec", "cwd": "/w/.agentflow/worktrees/x"}}),
        json.dumps({"type": "response_item",
                    "payload": {"type": "function_call", "arguments": '{"command": "false"}'}}),
        json.dumps({"type": "response_item",
                    "payload": {"type": "function_call_output", "output": '{"exit_code": 1}'}}),
    ]) + "\n")

    excerpt = churn.format_excerpt(str(path))

    assert "0 tool calls" not in excerpt
    assert "no Claude session records here" in excerpt


def _write_config(tmp_path, entries):
    config = tmp_path / "agentflow.toml"
    config.write_text("".join(
        f'[[repositories]]\nrepo = "{repo}"\nworkdir = "{workdir}"\n\n'
        for repo, workdir in entries
    ))
    return config


def _point_roots_at(monkeypatch, tmp_path, claude_root):
    monkeypatch.setattr(churn, "DEFAULT_CLAUDE_ROOT", str(claude_root))
    monkeypatch.setattr(churn, "DEFAULT_CODEX_ROOT", str(tmp_path / "empty-codex"))


def test_churn_command_reports_only_the_requested_repos_sessions(tmp_path, monkeypatch, capsys):
    claude_root = tmp_path / "claude-projects"
    wanted = tmp_path / "checkouts" / "sample-app"
    other = tmp_path / "checkouts" / "other-app"
    wanted.mkdir(parents=True)
    other.mkdir(parents=True)
    _write_claude_session(claude_root, wanted, "issue-1", [
        _assistant_tool_use("Bash", "1", {"command": "npm test"}),
        _tool_result("1", is_error=True, content="FAIL"),
    ])
    _write_claude_session(claude_root, other, "issue-2", [
        _assistant_tool_use("Bash", "2", {"command": "npm test"}),
    ])
    _point_roots_at(monkeypatch, tmp_path, claude_root)
    config = _write_config(tmp_path, [("acme/sample-app", wanted), ("acme/other-app", other)])

    assert cli.main(["churn", "--config", str(config), "--repo", "acme/sample-app"]) == 0

    out = capsys.readouterr().out
    assert "issue-1" in out
    assert "issue-2" not in out


def test_churn_command_rejects_a_repo_that_is_not_configured(tmp_path, monkeypatch):
    workdir = tmp_path / "checkouts" / "sample-app"
    workdir.mkdir(parents=True)
    _point_roots_at(monkeypatch, tmp_path, tmp_path / "claude-projects")
    config = _write_config(tmp_path, [("acme/sample-app", workdir)])

    with pytest.raises(SystemExit):
        cli.main(["churn", "--config", str(config), "--repo", "acme/absent"])


def test_churn_command_excerpts_a_session_without_reading_the_configuration(tmp_path, capsys):
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join([
        _assistant_tool_use("Bash", "1", {"command": "false"}),
        _tool_result("1", is_error=True, content="command failed"),
    ]) + "\n")

    assert cli.main(["churn", "--excerpt", str(path)]) == 0

    assert "command failed" in capsys.readouterr().out
