"""Provider command construction and fail-closed worktree plumbing."""

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agentflow import runner as runner_mod
from agentflow.runner import (ClaudeRunner, CodexRunner, Complexity,
                              Effort, _run, recover_stale_worktrees,
                              remove_worktree_if_safe,
                              worktree_session)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


def _repo_with_origin(tmp_path: Path, name: str = "repo") -> Path:
    origin = tmp_path / f"{name}.origin.git"
    repo = tmp_path / name
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


def test_claude_command_confines_the_session_to_its_assigned_worktree(tmp_path):
    repo = _repo_with_origin(tmp_path)
    branch = "agentflow/claude/issue-7-owned"
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-7-owned"
    _branch_worktree(repo, wt, branch)
    cmd = ClaudeRunner().structured_argv("build it", "sonnet", str(wt))
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" not in cmd
    assert settings["sandbox"]["enabled"] is True
    assert settings["sandbox"]["failIfUnavailable"] is True
    assert settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert settings["sandbox"]["enableWeakerNetworkIsolation"] is True
    prompt = cmd[cmd.index("-p") + 1]
    assert str(wt.resolve()) in prompt and branch in prompt

    assert "--output-format" in cmd


def test_claude_keeps_only_the_operator_local_mcp_servers(monkeypatch, tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-10-mcp"
    _branch_worktree(repo, wt, "agentflow/claude/issue-10-mcp")
    from agentflow.intake import INTAKE_RESULT_SCHEMA

    # The operator's local code-graph server is re-supplied under strict mode; the personal
    # claude.ai connectors (never in this map) are excluded. Holds with schema (intake/review)
    # and without (build).
    monkeypatch.setattr(runner_mod, "_operator_local_mcp_servers",
                        lambda: {"codebase-memory-mcp": {"command": "/x/code-graph"}})
    for schema in (INTAKE_RESULT_SCHEMA, None):
        cmd = ClaudeRunner().structured_argv("do work", "sonnet", str(wt), schema=schema)
        assert "--strict-mcp-config" in cmd
        mcp = json.loads(cmd[cmd.index("--mcp-config") + 1])
        assert list(mcp["mcpServers"]) == ["codebase-memory-mcp"]


def test_claude_pins_mcp_empty_when_operator_has_no_local_servers(monkeypatch, tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-10-nomcp"
    _branch_worktree(repo, wt, "agentflow/claude/issue-10-nomcp")

    # No local servers → nothing to re-supply; strict mode alone keeps the set empty.
    monkeypatch.setattr(runner_mod, "_operator_local_mcp_servers", lambda: {})
    cmd = ClaudeRunner().structured_argv("do work", "sonnet", str(wt))
    assert "--strict-mcp-config" in cmd
    assert "--mcp-config" not in cmd


def test_codex_command_confines_the_session_to_its_assigned_worktree(tmp_path):
    repo = _repo_with_origin(tmp_path)
    branch = "agentflow/codex/issue-8-owned"
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-8-owned"
    _branch_worktree(repo, wt, branch)
    cmd = CodexRunner().structured_argv("build it", "terra", str(wt))
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert cmd[cmd.index("--cd") + 1] == str(wt.resolve())
    assert "--ignore-user-config" in cmd and "--ephemeral" in cmd
    assert 'approvals_reviewer="auto_review"' in cmd
    assert 'approval_policy="never"' not in cmd
    assert "sandbox_workspace_write.network_access=true" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" not in cmd
    prompt = cmd[-1]
    assert str(wt.resolve()) in prompt and branch in prompt

    structured = CodexRunner().structured_argv(
        "build it", "gpt-5.6-terra", str(wt))
    assert "--dangerously-bypass-approvals-and-sandbox" not in structured
    assert structured[structured.index("--sandbox") + 1] == "workspace-write"
    assert structured[structured.index("--cd") + 1] == str(wt.resolve())
    assert "--json" in structured
    assert str(wt.resolve()) in structured[-1]


def test_claude_wires_the_result_schema_to_its_native_json_schema_flag(tmp_path):
    from agentflow.intake import INTAKE_RESULT_SCHEMA
    from agentflow.reviewer import REVIEW_VERDICT_SCHEMA

    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-9-owned"
    _branch_worktree(repo, wt, "agentflow/claude/issue-9-owned")

    for schema in (INTAKE_RESULT_SCHEMA, REVIEW_VERDICT_SCHEMA):
        cmd = ClaudeRunner().structured_argv("decide", "sonnet", str(wt), schema=schema)
        assert json.loads(cmd[cmd.index("--json-schema") + 1]) == schema

    # A code-writing stage passes no schema, so the flag stays absent.
    assert "--json-schema" not in ClaudeRunner().structured_argv("build it", "sonnet", str(wt))


def test_codex_wires_the_result_schema_to_its_native_output_schema_file(tmp_path):
    from agentflow.intake import INTAKE_RESULT_SCHEMA
    from agentflow.reviewer import REVIEW_VERDICT_SCHEMA

    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-9-owned"
    _branch_worktree(repo, wt, "agentflow/codex/issue-9-owned")

    for schema in (INTAKE_RESULT_SCHEMA, REVIEW_VERDICT_SCHEMA):
        cmd = CodexRunner().structured_argv("decide", "terra", str(wt), schema=schema)
        schema_path = cmd[cmd.index("--output-schema") + 1]
        with open(schema_path) as handle:
            assert json.load(handle) == schema
        # The prompt stays the final positional argument even with the schema option present.
        assert str(wt.resolve()) in cmd[-1] and cmd[-1] != schema_path

    assert "--output-schema" not in CodexRunner().structured_argv("build it", "terra", str(wt))


def test_provider_adapters_supply_the_stage_schema_for_intake_and_review(tmp_path):
    from agentflow.coordinator.providers import ClaudeProviderAdapter, CodexProviderAdapter
    from agentflow.coordinator.record import Record
    from agentflow.intake import INTAKE_RESULT_SCHEMA
    from agentflow.reviewer import REVIEW_VERDICT_SCHEMA

    repo = _repo_with_origin(tmp_path)
    cases = {"intake": INTAKE_RESULT_SCHEMA, "review": REVIEW_VERDICT_SCHEMA}
    for stage, schema in cases.items():
        claude = ClaudeProviderAdapter().command(Record(
            f"claude-{stage}", stage, "claude", 0,
            model="sonnet", source=str(repo), input_ptr="decide"))
        assert json.loads(claude[claude.index("--json-schema") + 1]) == schema

        codex = CodexProviderAdapter().command(Record(
            f"codex-{stage}", stage, "codex", 0,
            model="terra", source=str(repo), input_ptr="decide"))
        with open(codex[codex.index("--output-schema") + 1]) as handle:
            assert json.load(handle) == schema

    # A code-writing stage (build) supplies no structured-result schema to either provider.
    build = ClaudeProviderAdapter().command(Record(
        "claude-build", "build", "claude", 0,
        model="sonnet", source=str(repo), input_ptr="build it"))
    assert "--json-schema" not in build


def test_unattended_stage_submissions_offer_narrow_codex_browser_recovery_only(tmp_path):
    from agentflow.coordinator.providers import ClaudeProviderAdapter, CodexProviderAdapter
    from agentflow.coordinator.record import Record
    from agentflow.loop import BUILD_PROMPT, PRODUCE_PROMPT, RESPOND_PROMPT, REVISE_PROMPT

    repo = _repo_with_origin(tmp_path)
    stage_prompts = [
        ("build", BUILD_PROMPT.format(
            repo="o/r", n=7, title="x", body="", effort="low", surfaces="`frontend/`")),
        ("revise", REVISE_PROMPT.format(
            n=7, repo="o/r", findings="- attach proof", surfaces="`frontend/`")),
        ("respond", RESPOND_PROMPT.format(
            n=7, baseline="abc123", comment="show the screen",
            disclaimer="> *agentflow reply*")),
        ("mockup", PRODUCE_PROMPT.format(
            repo="o/r", n=7, title="x", body="", branch="mockup-7",
            surfaces="`frontend/`", scope_guidance="SCOPE: local",
            disclaimer="> *agentflow mockup*")),
    ]

    for index, (stage, prompt) in enumerate(stage_prompts):
        codex = CodexProviderAdapter().command(Record(
            f"codex-{index}", stage, "codex", index,
            model="terra", source=str(repo), input_ptr=prompt))
        claude = ClaudeProviderAdapter().command(Record(
            f"claude-{index}", stage, "claude", index,
            model="sonnet", source=str(repo), input_ptr=prompt))
        codex_config = [codex[i + 1] for i, arg in enumerate(codex[:-1]) if arg == "-c"]

        assert codex[codex.index("--sandbox") + 1] == "workspace-write"
        assert any("sandbox_approval=true" in value for value in codex_config)
        assert 'approvals_reviewer="auto_review"' in codex_config
        assert 'approval_policy="never"' not in codex_config
        assert "HEADLESS-SANDBOX-BLOCKED" in codex[-1]
        assert "sandbox_permissions=require_escalated" in codex[-1]
        assert "sandbox_permissions" not in claude[claude.index("-p") + 1]


def test_codex_account_fact_uses_typed_limit_windows(monkeypatch):
    payload = json.dumps({
        "windows": [
            {"used_percent": 100, "window_minutes": 300, "resets_at": 1234},
            {"used_percent": 60, "window_minutes": 10080, "resets_at": 9999},
        ]
    })

    def fake_run(cmd, **kwargs):
        assert cmd[-1] == "limits"
        assert kwargs["env"]["TRIAGE_AGENT"] == "codex"
        return subprocess.CompletedProcess(cmd, 0, payload, "")

    monkeypatch.setattr(runner_mod.subprocess, "run", fake_run)
    assert CodexRunner().account_fact() == {
        "kind": "rate_limited", "reset_at": 1234}


def test_effort_has_four_levels():
    assert [e.value for e in Effort] == ["low", "medium", "high", "extra"]


def test_run_timeout_returns_nonzero_and_does_not_propagate():
    """A hung subprocess is killed and classified as a failure, not an exception."""
    def raise_timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
    with patch("subprocess.run", raise_timeout):
        r = _run(["sleep", "100"], timeout=1)
    assert r.returncode != 0
    assert "timed out" in r.stderr


def test_generic_runner_refuses_provider_commands():
    with pytest.raises(RuntimeError, match="coordinator launcher"):
        _run(["codex", "exec", "do work"])


def test_public_session_lifecycle_bounds_registrations_across_every_lane(tmp_path):
    repo = _repo_with_origin(tmp_path)
    root = repo / ".agentflow" / "worktrees"
    completed = [
        root / "claude-intake" / "issue-1",
        root / "codex-review" / "pr-2-reviewed",
        root / "claude" / "issue-3-built",
        root / "codex" / "mockup-4-drawn",
        root / "claude" / "issue-5-responded",
        root / "codex" / "issue-6-rebased",
    ]
    _detached_worktree(repo, completed[0])
    _detached_worktree(repo, completed[1])
    for path, branch in zip(completed[2:], [
        "agentflow/claude/issue-3-built",
        "agentflow/codex/mockup-4-drawn",
        "agentflow/claude/issue-5-responded",
        "agentflow/codex/issue-6-rebased",
    ], strict=True):
        _branch_worktree(repo, path, branch)

    dirty = root / "claude" / "issue-7-dirty"
    unpushed = root / "codex" / "issue-8-unpushed"
    active = root / "claude" / "issue-9-active"
    _branch_worktree(repo, dirty, "agentflow/claude/issue-7-dirty", dirty=True)
    _branch_worktree(repo, unpushed, "agentflow/codex/issue-8-unpushed", push=False)
    _branch_worktree(repo, active, "agentflow/claude/issue-9-active")

    foreign_root = tmp_path / "foreign-lifecycle"
    foreign_root.mkdir()
    foreign = _repo_with_origin(foreign_root)
    foreign_wt = root / "dotfiles" / "open-pr"
    foreign_wt.parent.mkdir(parents=True, exist_ok=True)
    _git(foreign, "worktree", "add", "-b", "codex/open-pr", str(foreign_wt), "origin/main")

    assert all(remove_worktree_if_safe(str(repo), path) for path in completed)
    assert remove_worktree_if_safe(str(repo), dirty) is False
    assert remove_worktree_if_safe(str(repo), unpushed) is False
    with worktree_session(active):
        assert remove_worktree_if_safe(str(repo), active) is False
    assert remove_worktree_if_safe(str(repo), foreign_wt) is False

    registered = _git(repo, "worktree", "list", "--porcelain")
    assert registered.count("worktree ") == 4  # main + dirty + unpushed + active
    assert foreign_wt.exists()


def test_reuse_refuses_recoverable_work_and_github_uncertainty(tmp_path):
    repo = _repo_with_origin(tmp_path)
    runner = ClaudeRunner()
    branch = "agentflow/claude/issue-7-retry"
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-7-retry"
    _branch_worktree(repo, wt, branch, push=False)
    head = _git(wt, "rev-parse", "HEAD")

    runner._open_pr_for_branch = lambda *_: (True, False)
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
    runner._open_pr_for_branch = lambda *_: (False, False)
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

    # State the GitHub facts through the github module's typed helpers (and the runner's own
    # branch→PR-state helper) — never by matching a `gh` command line. Git plumbing stays real.
    from agentflow import github

    def fake_issue_labels(repo, issue):
        return frozenset({"agentflow:building"}) if issue == 14 else frozenset({"ready-for-agent"})

    pr_state_by_branch = {
        "agentflow/codex/issue-10-done": "OPEN",
        "codex/legacy-fix": "MERGED",
        "codex/second-legacy-fix": "MERGED",
        "agentflow/codex/issue-11-dirty": "OPEN",
        "agentflow/codex/issue-12-unpushed": "OPEN",
        "agentflow/codex/issue-14-active-open-pr": "OPEN",
        "codex/active-legacy": "OPEN",
    }

    monkeypatch.setattr(github, "issue_labels", fake_issue_labels)
    monkeypatch.setattr(github, "issue_state", lambda repo, issue: "OPEN")
    monkeypatch.setattr(github, "issue_comments", lambda repo, issue: [])
    monkeypatch.setattr(github, "pr_state", lambda repo, pr: "OPEN")
    monkeypatch.setattr(github, "pr_comments", lambda repo, pr: [
        github.Comment(body="> *agentflow: parked for human review.*", created_at="")])
    monkeypatch.setattr(runner_mod, "_pr_state_for_branch",
                        lambda repo, branch: pr_state_by_branch.get(branch))
    child = subprocess.Popen(["sleep", "30"])
    try:
        with worktree_session(active_legacy):
            marker = runner_mod._active_marker(active_legacy)
            assert marker is not None
            marker.write_text(str(child.pid))
            runner_mod._ACTIVE_WORKTREES.clear()  # simulate a freshly started recovery process
            report = recover_stale_worktrees(
                "owner/repo", str(repo), protected={os.path.realpath(legacy_two)})
    finally:
        child.terminate()
        child.wait()

    assert set(report.removed) == {
        str(completed), str(legacy), str(intake), str(intake_two),
        str(intake_three), str(review), str(review_two),
    }
    assert len(report.removed) == 7
    assert (dirty.exists() and unpushed.exists() and active.exists() and
            active_open_pr.exists() and active_legacy.exists() and legacy_two.exists())
    assert foreign_wt.exists()
    registered = _git(repo, "worktree", "list", "--porcelain")
    assert str(completed) not in registered and str(legacy) not in registered
    assert str(foreign_wt) not in registered  # ownership comes from the foreign repo's metadata


def test_open_pr_lookup_reports_presence_and_fails_closed(monkeypatch):
    from agentflow import github

    runner = ClaudeRunner()
    monkeypatch.setattr(github, "list_open_prs", lambda repo, head=None: None)
    assert runner._open_pr_for_branch("o/r", "b") == (False, False)  # unreadable → unknown
    monkeypatch.setattr(github, "list_open_prs", lambda repo, head=None: [])
    assert runner._open_pr_for_branch("o/r", "b") == (True, False)  # read ok, no open PR
    monkeypatch.setattr(github, "list_open_prs",
                        lambda repo, head=None: [github.PrRow(1, head, "sha")])
    assert runner._open_pr_for_branch("o/r", "b") == (True, True)


def test_pr_state_for_branch_reads_through_the_api_hatch_and_fails_closed(monkeypatch):
    from agentflow import github

    monkeypatch.setattr(github, "api", lambda args, parse_json=False: None)
    assert runner_mod._pr_state_for_branch("o/r", "b") is None  # lookup failed → unknown
    monkeypatch.setattr(github, "api", lambda args, parse_json=False: [])
    assert runner_mod._pr_state_for_branch("o/r", "b") is None  # no PR ever opened
    monkeypatch.setattr(github, "api", lambda args, parse_json=False: [{"state": "MERGED"}])
    assert runner_mod._pr_state_for_branch("o/r", "b") == "MERGED"


def test_recovery_retains_a_session_whose_github_facts_are_unreadable(tmp_path, monkeypatch):
    repo = _repo_with_origin(tmp_path)
    root = repo / ".agentflow" / "worktrees"
    build = root / "codex" / "issue-30-unknown"
    _branch_worktree(repo, build, "agentflow/codex/issue-30-unknown")

    from agentflow import github

    # A PR exists for the branch, but the issue's labels cannot be read, so completion is
    # unknown. A fail-closed recovery keeps the session; it never removes it on an unconfirmed
    # fact (an "empty == done" reading would wrongly delete recoverable work here).
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: None)
    monkeypatch.setattr(runner_mod, "_pr_state_for_branch", lambda repo, branch: "OPEN")
    report = recover_stale_worktrees("owner/repo", str(repo))
    assert str(build) in report.retained
    assert str(build) not in report.removed
    assert build.exists()


def _graph_project(path: Path) -> str:
    """The codebase-memory project name for a checkout: its real path with the leading slash
    dropped and every separator turned into a dash — the same transform the graph itself uses."""
    return os.path.realpath(str(path)).strip("/").replace("/", "-")


def _session_prompt(cmd: list[str]) -> str:
    """The bounded session prompt handed to either provider (Claude's ``-p`` value, Codex's
    trailing argument) — the one argument carrying the session boundary."""
    return next(arg for arg in cmd if "Session boundary" in arg)


def test_daemon_sessions_ground_in_the_maintained_main_checkout_graph(tmp_path):
    """Both providers are told to query the *maintained main-checkout* code graph — named from the
    repository the worktree belongs to, not the empty per-worktree copy — before shell orientation,
    and to keep every read and edit inside the worktree. Proven for two differently-named repos, so
    no owner, path, or project id is hardcoded and the same launch grounds every fleet repository."""
    for name in ("alpha-service", "beta-tool"):
        repo = _repo_with_origin(tmp_path, name)
        wt = repo / ".agentflow" / "worktrees" / "codex" / f"issue-3-{name}"
        _branch_worktree(repo, wt, f"agentflow/codex/issue-3-{name}")

        main_project = _graph_project(repo)
        worktree_project = _graph_project(wt)
        assert main_project != worktree_project  # the worktree is a distinct path/project

        for runner in (ClaudeRunner(), CodexRunner()):
            prompt = _session_prompt(
                runner.structured_argv("build it", runner.model_for(Complexity.DEEP), str(wt)))
            # Names the maintained main-checkout graph and pins queries to it, not the worktree copy.
            assert f"project={main_project}" in prompt
            assert worktree_project not in prompt
            # Graph-first for structural discovery, with the concrete graph tools named.
            assert "code graph" in prompt.lower() and "search_graph" in prompt
            # Reads and edits stay in the worktree even though graph results name the main checkout.
            assert str(wt.resolve()) in prompt
            assert "stays inside your assigned worktree" in prompt


def test_codex_resupplies_the_operator_code_graph_server_on_every_stage(monkeypatch, tmp_path):
    """Codex drops all user config to keep the personal connectors out (``--ignore-user-config``),
    which also drops the code-graph server. It is re-supplied as an ``mcp_servers`` ``-c`` override
    on every stage — Build (workspace-write) and read-only alike — so Codex reaches the same graph
    Claude does, while the account connectors stay excluded and a read-only stage keeps its sandbox
    boundary. With no operator-local servers there is nothing to attach."""
    from agentflow.coordinator.profiles import StageProfile

    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-11-graph"
    _branch_worktree(repo, wt, "agentflow/codex/issue-11-graph")
    monkeypatch.setattr(runner_mod, "_operator_local_mcp_servers",
                        lambda: {"codebase-memory-mcp": {"command": "/x/code-graph"}})

    read_only = StageProfile(("Read", "Bash", "Grep", "Glob"), 900, 40)
    for profile, sandbox in ((None, "workspace-write"), (read_only, "read-only")):
        cmd = CodexRunner().structured_argv("do work", "sol", str(wt), profile=profile)
        override = 'mcp_servers.codebase-memory-mcp.command="/x/code-graph"'
        assert override in cmd and cmd[cmd.index(override) - 1] == "-c"
        assert "--ignore-user-config" in cmd            # personal account connectors stay excluded
        assert cmd[cmd.index("--sandbox") + 1] == sandbox  # read-only stage keeps its boundary

    # No operator-local servers → nothing is re-supplied (the personal connectors were the risk).
    monkeypatch.setattr(runner_mod, "_operator_local_mcp_servers", lambda: {})
    cmd = CodexRunner().structured_argv("do work", "sol", str(wt))
    assert not any(str(arg).startswith("mcp_servers.") for arg in cmd)


def test_codex_resupplies_a_full_server_spec_as_valid_toml_overrides(monkeypatch, tmp_path):
    """A launched server may carry ``args`` and ``env`` (the common ``npx``-style shape), not just a
    bare ``command`` — Claude honors them because it hands the whole map to ``--mcp-config``, so
    Codex must carry the same map across. Each is rendered as a Codex ``-c`` override whose value is
    valid TOML: a string command, a TOML array of args, and one env key per entry (no hand-built
    inline table). This guards the parity that keeps both providers on one server definition."""
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-12-fullspec"
    _branch_worktree(repo, wt, "agentflow/codex/issue-12-fullspec")
    monkeypatch.setattr(runner_mod, "_operator_local_mcp_servers", lambda: {
        "code-graph": {"command": "npx", "args": ["-y", "code-graph-mcp"],
                       "env": {"GRAPH_TOKEN": "abc123"}}})

    cmd = CodexRunner().structured_argv("do work", "sol", str(wt))
    overrides = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-c"]
    assert 'mcp_servers.code-graph.command="npx"' in overrides
    assert 'mcp_servers.code-graph.args=["-y", "code-graph-mcp"]' in overrides
    assert 'mcp_servers.code-graph.env.GRAPH_TOKEN="abc123"' in overrides
