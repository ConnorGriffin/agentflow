"""Provider command construction and fail-closed worktree plumbing."""

import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from agentflow import runner as runner_mod
from agentflow.coordinator.providers import provider_command
from agentflow.coordinator.record import Record
from agentflow.reviewer import REVIEW_PROMPT
from agentflow.routing import routing
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


def _provider_record(pool: str, source: Path) -> Record:
    model = "opus" if pool == "claude" else "gpt-5.6-sol"
    return Record(
        f"{pool}-build", "build", pool, 1,
        model=model, source=str(source), input_ptr="do the stage",
        complexity="deep", effort="high",
    )


def _provider_prompt(pool: str, command: list[str]) -> str:
    return command[command.index("-p") + 1] if pool == "claude" else command[-1]


def test_complexity_resolves_to_cost_appropriate_models():
    claude, codex = ClaudeRunner(), CodexRunner()
    assert claude.model_for(Complexity.STANDARD) == "sonnet"
    assert claude.model_for(Complexity.DEEP) == "opus"
    assert codex.model_for(Complexity.STANDARD) == "gpt-5.6-terra"
    assert codex.model_for(Complexity.DEEP) == "gpt-5.6-sol"


def test_claude_deep_tier_launches_with_pinned_opus_5_model(tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-1-owned"
    _branch_worktree(repo, wt, "agentflow/claude/issue-1-owned")
    # Internal tier is "opus"; CLI must receive the pinned release identifier.
    cmd = ClaudeRunner().structured_argv("do deep work", "opus", str(wt))
    model_arg = cmd[cmd.index("--model") + 1]
    assert model_arg == "claude-opus-5", f"expected claude-opus-5, got {model_arg!r}"


def test_claude_standard_tier_model_passes_through_unchanged(tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-2-owned"
    _branch_worktree(repo, wt, "agentflow/claude/issue-2-owned")
    cmd = ClaudeRunner().structured_argv("do standard work", "sonnet", str(wt))
    model_arg = cmd[cmd.index("--model") + 1]
    assert model_arg == "sonnet"


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


def test_both_provider_commands_receive_the_same_canonical_charter(tmp_path):
    charter = (Path(__file__).parents[1] / "standards" / "CHARTER.md").read_text()

    prompts = []
    for pool in ("claude", "codex"):
        prompts.append(_provider_prompt(
            pool, provider_command(_provider_record(pool, tmp_path))
        ))

    assert charter in prompts[0]
    assert charter in prompts[1]
    assert prompts[0].count(charter) == prompts[1].count(charter) == 1


def test_provider_command_refuses_missing_canonical_charter(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "_CHARTER_SOURCE_PATH", tmp_path / "missing-charter.md")

    for pool in ("claude", "codex"):
        with pytest.raises(RuntimeError, match="canonical engineering charter unavailable"):
            provider_command(_provider_record(pool, tmp_path))


def test_provider_command_refuses_empty_canonical_charter(monkeypatch, tmp_path):
    charter = tmp_path / "empty-charter.md"
    charter.write_text(" \n\t")
    monkeypatch.setattr(runner_mod, "_CHARTER_SOURCE_PATH", charter)

    for pool in ("claude", "codex"):
        with pytest.raises(RuntimeError, match="canonical engineering charter is empty"):
            provider_command(_provider_record(pool, tmp_path))


def test_claude_keeps_only_codebase_memory_without_private_environment(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "codebase-memory-mcp": {
            "command": "/x/code-graph",
            "args": ["serve"],
            "env": {"PRIVATE_TOKEN": "must-not-cross"},
        },
        "gmail": {
            "command": "/x/mail",
            "env": {"GMAIL_TOKEN": "also-private"},
        },
    }}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)

    cmd = provider_command(_provider_record("claude", tmp_path))
    assert "--strict-mcp-config" in cmd
    mcp = json.loads(cmd[cmd.index("--mcp-config") + 1])
    assert mcp == {"mcpServers": {
        "codebase-memory-mcp": {"command": "/x/code-graph", "args": ["serve"]},
    }}
    assert "gmail" not in " ".join(cmd)
    assert "must-not-cross" not in " ".join(cmd)
    assert "also-private" not in " ".join(cmd)


def test_claude_pins_mcp_empty_when_operator_has_no_local_servers(monkeypatch, tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-10-nomcp"
    _branch_worktree(repo, wt, "agentflow/claude/issue-10-nomcp")

    # No local servers → nothing to re-supply; strict mode alone keeps the set empty.
    monkeypatch.setattr(runner_mod, "_codebase_memory_mcp_servers", lambda: {})
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
    assert 'approval_policy="on-request"' in cmd
    assert not any("approval_policy={granular=" in value for value in cmd)
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


def test_codex_auto_review_policy_allows_only_the_bounded_worker_escalation(tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-557-owned"
    _branch_worktree(repo, wt, "agentflow/codex/issue-557-owned")
    command = CodexRunner().structured_argv("build it", "terra", str(wt))
    config = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-c"]
    policy = json.loads(next(value.removeprefix("auto_review.policy=") for value in config
                             if value.startswith("auto_review.policy=")))

    assert "bare worker command" in policy
    assert "standard launcher-owned `/bin/zsh -lc '<bounded-worker-command>'` envelope" in policy
    assert "worker name must be routed and allowlisted" in policy
    assert "low`, `medium`, `high`, or `extra`" in policy
    assert "integer from 1 through 900" in policy
    assert "private regular file with mode exactly 0600" in policy
    assert "extra arguments" in policy
    assert "other shell segments" in policy
    assert "sandbox-weakening flags" in policy
    assert "drive-local-webapp/driver.mjs browser driver" in policy


def test_codex_auto_review_policy_rejects_broad_codex_or_shell_escalation(tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-557-reject"
    _branch_worktree(repo, wt, "agentflow/codex/issue-557-reject")
    command = CodexRunner().structured_argv("build it", "terra", str(wt))
    config = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-c"]
    policy = json.loads(next(value.removeprefix("auto_review.policy=") for value in config
                             if value.startswith("auto_review.policy=")))

    assert "Reject authored shell wrappers" in policy
    assert "bare `codex` invocations" in policy
    assert "`--dangerously-bypass-approvals-and-sandbox`" in policy
    assert "Reject every other sandbox escalation" in policy
    assert "gh pr" not in policy  # Review's narrow gh reads need no escalation exception.
    assert "any Codex command" not in policy
    assert "any shell command" not in policy


def test_codex_session_lead_worker_command_matches_the_launcher_approval_policy(tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-568-owned"
    _branch_worktree(repo, wt, "agentflow/codex/issue-568-owned")
    launcher = CodexRunner().structured_argv("build it", "terra", str(wt))
    config = [launcher[index + 1] for index, value in enumerate(launcher[:-1])
              if value == "-c"]
    policy = json.loads(next(value.removeprefix("auto_review.policy=") for value in config
                             if value.startswith("auto_review.policy=")))
    brief = routing.session_lead_instructions("build", "medium", parent_provider="codex")

    prompt_file = tmp_path / "worker-prompt"
    prompt_file.write_text("Review the change")
    prompt_file.chmod(0o600)
    inner_command = (
        "agentflow-codex-worker --worker luna --effort medium --timeout 900 "
        f"< \"{prompt_file}\""
    )
    brief_envelope = (
        "/bin/zsh -lc 'agentflow-codex-worker --worker <routed-allowlisted-name> "
        "--effort medium --timeout 900 < \"<absolute-private-prompt-file>\"'"
    )
    assert brief_envelope in brief
    assert prompt_file.is_file() and prompt_file.stat().st_mode & 0o777 == 0o600
    assert "bare worker command" in policy
    assert "/bin/zsh -lc '<bounded-worker-command>'" in policy
    assert "launcher-owned envelope" in policy
    assert "exactly one `agentflow-codex-worker` command" in policy
    assert "one stdin `<` redirection" in policy
    assert "literal absolute path" in brief and "literal absolute path" in policy
    assert "mode exactly 0600" in policy

    # Exercise the public command boundary that Codex's launcher places behind auto-review.
    # The reviewer itself is a Codex service. CI may not install zsh, so use it when present and
    # otherwise exercise the same ``-lc`` + redirection boundary through the platform shell; the
    # assertions above separately pin the production contract to Codex's exact /bin/zsh envelope.
    fake_codex = tmp_path / "codex-provider"
    fake_codex.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "task = sys.stdin.read()\n"
        "raise SystemExit(0 if task == 'Review the change' else 91)\n"
    )
    fake_codex.chmod(0o755)
    worker_bin = Path(sys.executable).with_name("agentflow-codex-worker")
    assert worker_bin.is_file()
    env = os.environ.copy()
    env["AGENTFLOW_CODEX_BIN"] = str(fake_codex)
    env["PATH"] = f"{worker_bin.parent}{os.pathsep}{env['PATH']}"
    command_shell = shutil.which("zsh") or shutil.which("sh")
    assert command_shell is not None
    launched = subprocess.run(
        [command_shell, "-lc", inner_command], cwd=wt, env=env,
        text=True, capture_output=True,
    )
    assert launched.returncode == 0, launched.stderr

    rejected_commands = {
        "/bin/zsh -lc 'echo ready && agentflow-codex-worker --worker luna "
        "--effort medium --timeout 900 < \"/private/tmp/prompt\"'":
            ("chains", "extra commands", "&& <extra-command>"),
        "/bin/bash -lc 'agentflow-codex-worker --worker luna --effort medium "
        "--timeout 900 < \"/private/tmp/prompt\"'":
            ("authored shell wrappers", "/bin/bash -lc"),
        "/bin/zsh -lc 'agentflow-codex-worker --worker luna --effort medium "
        "--timeout 900 --extra < \"/private/tmp/prompt\"'":
            ("extra arguments", "--timeout 900 --extra"),
        "/bin/zsh -lc 'agentflow-codex-worker --worker impostor --effort medium "
        "--timeout 900 < \"/private/tmp/prompt\"'":
            ("unallowlisted workers", "--worker <unallowlisted-worker>"),
        "/bin/zsh -lc 'agentflow-codex-worker --worker luna --effort max "
        "--timeout 900 < \"/private/tmp/prompt\"'": ("invalid effort", "--effort max"),
        "/bin/zsh -lc 'agentflow-codex-worker --worker luna --effort medium "
        "--timeout 901 < \"/private/tmp/prompt\"'": ("invalid timeout", "--timeout 901"),
        "/bin/zsh -lc 'agentflow-codex-worker --worker luna --effort medium "
        "--timeout 900 < \"/private/tmp/mode-0644-prompt\"'":
            ("unsafe stdin", "<mode-0644-file>"),
        "/bin/zsh -lc 'agentflow-codex-worker --worker luna --effort medium "
        "--timeout 900 --dangerously-bypass-approvals-and-sandbox "
        "< \"/private/tmp/prompt\"'": ("sandbox-weakening flags",),
    }
    for rejected_command, rejection_terms in rejected_commands.items():
        assert rejected_command not in brief
        assert all(term in policy for term in rejection_terms)

    unsafe_prompt = tmp_path / "unsafe-prompt"
    unsafe_prompt.write_text("Review the change")
    unsafe_prompt.chmod(0o644)
    rejected_at_worker_boundary = (
        f'agentflow-codex-worker --worker luna --effort max --timeout 900 < "{prompt_file}"',
        f'agentflow-codex-worker --worker luna --effort medium --timeout 901 < "{prompt_file}"',
        f'agentflow-codex-worker --worker impostor --effort medium --timeout 900 < "{prompt_file}"',
        f'agentflow-codex-worker --worker luna --effort medium --timeout 900 --extra < "{prompt_file}"',
        f'agentflow-codex-worker --worker luna --effort medium --timeout 900 < "{unsafe_prompt}"',
    )
    for rejected_command in rejected_at_worker_boundary:
        rejected = subprocess.run(
            [command_shell, "-lc", rejected_command], cwd=wt, env=env,
            text=True, capture_output=True,
        )
        assert rejected.returncode != 0


def test_codex_review_pr_reads_stay_in_the_existing_networked_sandbox(tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex-review" / "pr-42-read"
    _detached_worktree(repo, wt)
    prompt = REVIEW_PROMPT.format(
        pr=42, issue=41, starting_sha="abc123", acceptance="ships a thing",
        surfaces="`agentflow/webui/`")

    command = CodexRunner().structured_argv(prompt, "gpt-5.6-terra", str(wt))
    config = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "-c"]
    policy = json.loads(next(value.removeprefix("auto_review.policy=") for value in config
                             if value.startswith("auto_review.policy=")))
    rendered = command[-1]

    assert command[command.index("--sandbox") + 1] == "workspace-write"
    assert "sandbox_workspace_write.network_access=true" in config
    assert "gh pr" not in policy
    assert "Do not request sandbox escalation for these reads" in rendered
    assert "`gh pr view 42 --json headRefOid,files,body`" in rendered
    assert "`gh pr diff 42`" in rendered
    # The only escalation instruction remains the pre-existing browser-driver recovery.
    assert rendered.count("sandbox_permissions=require_escalated") == 1


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


def test_provider_adapters_supply_each_structured_stage_schema(tmp_path):
    from agentflow.attack import ATTACK_RESULT_SCHEMA
    from agentflow.coordinator.providers import ClaudeProviderAdapter, CodexProviderAdapter
    from agentflow.coordinator.record import Record
    from agentflow.intake import INTAKE_RESULT_SCHEMA
    from agentflow.reviewer import REVIEW_VERDICT_SCHEMA

    repo = _repo_with_origin(tmp_path)
    cases = {
        "intake": INTAKE_RESULT_SCHEMA,
        "attack": ATTACK_RESULT_SCHEMA,
        "review": REVIEW_VERDICT_SCHEMA,
    }
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
    from agentflow.prompts import BUILD_PROMPT, PRODUCE_PROMPT, RESPOND_PROMPT, REVISE_PROMPT

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
        assert 'approval_policy="on-request"' in codex_config
        assert not any("approval_policy={granular=" in value for value in codex_config)
        assert 'approvals_reviewer="auto_review"' in codex_config
        assert 'approval_policy="never"' not in codex_config
        assert "HEADLESS-SANDBOX-BLOCKED" in codex[-1]
        assert "sandbox_permissions=require_escalated" in codex[-1]
        assert "sandbox_permissions" not in claude[claude.index("-p") + 1]


def test_review_sessions_on_both_tools_carry_the_browser_recovery_exactly_once(tmp_path):
    """The blocked-browser recovery used to be attached only by the Codex launcher, so a Claude
    review was asked to report UI verification without ever being taught the procedure or the
    "prescribed recovery" its instructions referenced (#737). The review prompt now carries the
    procedure itself — reaching both tools — and the Codex launcher does not stack its own copy
    on top."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter, CodexProviderAdapter
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import REVIEW_PROMPT

    prompt = REVIEW_PROMPT.format(
        pr=7, issue=3, starting_sha="abc123", acceptance="works",
        surfaces="`frontend/`")
    # The ui_verification instruction no longer references a recovery the prompt does not state:
    # the prescribed recovery is spelled out in the prompt itself, before the field description
    # points back at it.
    assert "That is the prescribed recovery" in prompt
    assert "recovery stated above" in prompt
    assert prompt.index("That is the prescribed recovery") < prompt.index(
        "recovery stated above")

    repo = _repo_with_origin(tmp_path)
    codex = CodexProviderAdapter().command(Record(
        "codex-review", "review", "codex", 1,
        model="terra", source=str(repo), input_ptr=prompt))
    claude = ClaudeProviderAdapter().command(Record(
        "claude-review", "review", "claude", 1,
        model="sonnet", source=str(repo), input_ptr=prompt))

    for launched in (codex[-1], claude[claude.index("-p") + 1]):
        assert launched.count("HEADLESS-SANDBOX-BLOCKED") == 1
        assert "sandbox_permissions=require_escalated" in launched


def test_codex_account_fact_uses_typed_limit_windows(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_CAPACITY_HELPER", "/test/capacity-helper")
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


def test_codex_account_fact_is_unavailable_without_the_optional_capacity_helper(
        monkeypatch):
    monkeypatch.delenv("AGENTFLOW_CAPACITY_HELPER", raising=False)
    monkeypatch.delenv("AGENTFLOW_TRIAGE_GATE", raising=False)
    monkeypatch.setattr(
        runner_mod.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("no personal helper should be invoked"),
    )

    assert CodexRunner().account_fact() is None


def test_claude_settings_allow_codex_cli_provider_hosts_so_the_worker_rungs_are_reachable():
    """A Claude session lead sandboxed with these settings shells out to `codex exec`, so the
    Codex CLI's own API hosts must be reachable or every Codex rung is unreachable regardless of
    what routing says (#498/#510)."""
    settings = json.loads(runner_mod._claude_settings())
    allowed = settings["sandbox"]["network"]["allowedDomains"]

    for host in ("chatgpt.com", "*.chatgpt.com", "auth.openai.com", "api.openai.com"):
        assert host in allowed


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
    foreign_wt = root / "foreign-repo" / "open-pr"
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
    detached_head = _git(detached, "rev-parse", "HEAD")
    (detached / "in-progress.txt").write_text("still being written")

    with worktree_session(detached):
        # A live sibling still holds it — refused, and refused *by name*, so the engine can tell
        # this from a lock nobody but a human will lift (#406).
        with pytest.raises(runner_mod.CheckoutRefused) as busy:
            runner.prepare_worktree_detached(str(repo), "origin/main", detached)
    assert busy.value.refusal.check == "checkout-busy"
    assert busy.value.refusal.expected and not busy.value.refusal.stall
    assert _git(detached, "rev-parse", "HEAD") == detached_head
    assert (detached / "in-progress.txt").read_text() == "still being written"

    _git(wt, "push", "-u", "origin", branch)
    runner._open_pr_for_branch = lambda *_: (False, False)
    with pytest.raises(subprocess.CalledProcessError):
        runner.prepare_worktree(str(repo), branch, wt, "owner/repo")
    assert wt.exists() and _git(wt, "rev-parse", "HEAD") == head


def test_review_checkout_recovers_after_its_branch_is_rebased_away(tmp_path):
    """A rebase strands the commit the review checkout is parked on. The next cycle must move
    the checkout onto the new head on its own, and must not destroy the stranded commit."""
    repo = _repo_with_origin(tmp_path)
    runner = ClaudeRunner()
    branch = "agentflow/claude/issue-11-rebased"
    build = repo / ".agentflow" / "worktrees" / "claude" / "issue-11-rebased"
    _branch_worktree(repo, build, branch)

    review = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-11-rebased"
    _git(repo, "fetch", "origin", "--quiet")
    runner.prepare_worktree_detached(str(repo), f"origin/{branch}", review)
    stranded = _git(review, "rev-parse", "HEAD")

    (build / "result.txt").write_text("amended after review started")
    _git(build, "add", "result.txt")
    _git(build, "commit", "--amend", "--no-edit")
    _git(build, "push", "--force", "origin", branch)
    new_head = _git(build, "rev-parse", "HEAD")
    assert new_head != stranded

    runner.prepare_worktree_detached(str(repo), f"origin/{branch}", review)
    assert _git(review, "rev-parse", "HEAD") == new_head
    retained = _git(repo, "for-each-ref", "--contains", stranded,
                    "--format=%(refname)", "refs/agentflow/stranded/")
    assert retained, "the superseded commit must stay reachable under a recovery ref"
    assert _git(repo, "cat-file", "-t", stranded) == "commit"


def test_idle_review_litter_is_archived_so_admission_can_proceed(tmp_path):
    """A finished review's checkout routinely keeps untracked scratch (a saved diff, a note).
    When the next logical review needs the same path at a new target, that litter must not
    stall admission forever: it is archived to a recovery ref and the checkout is rebuilt."""
    repo = _repo_with_origin(tmp_path)
    runner = ClaudeRunner()
    branch = "agentflow/claude/issue-21-litter"
    build = repo / ".agentflow" / "worktrees" / "claude" / "issue-21-litter"
    _branch_worktree(repo, build, branch)

    review = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-21-litter"
    _git(repo, "fetch", "origin", "--quiet")
    runner.prepare_worktree_detached(str(repo), f"origin/{branch}", review)
    (review / ".pr21.diff").write_text("review scratch")

    (build / "result.txt").write_text("amended after review settled")
    _git(build, "add", "result.txt")
    _git(build, "commit", "--amend", "--no-edit")
    _git(build, "push", "--force", "origin", branch)
    new_head = _git(build, "rev-parse", "HEAD")

    runner.prepare_worktree_detached(str(repo), f"origin/{branch}", review)
    assert _git(review, "rev-parse", "HEAD") == new_head
    assert not (review / ".pr21.diff").exists()
    refs = _git(repo, "for-each-ref", "--format=%(refname)",
                "refs/agentflow/stranded/pr-21-litter/").splitlines()
    assert refs, "the litter must be anchored under a recovery ref before the rebuild"
    assert _git(repo, "show", f"{refs[0]}:.pr21.diff") == "review scratch"


def test_a_locked_review_checkout_still_refuses_and_is_left_alone(tmp_path):
    """Archiving litter must not overrun the operator's escape hatch: a checkout a human pinned
    refuses admission, keeps its contents, and — since the daemon retries every cycle — leaves no
    growing trail of recovery refs behind."""
    repo = _repo_with_origin(tmp_path)
    runner = ClaudeRunner()
    branch = "agentflow/claude/issue-22-pinned"
    build = repo / ".agentflow" / "worktrees" / "claude" / "issue-22-pinned"
    _branch_worktree(repo, build, branch)

    review = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-22-pinned"
    _git(repo, "fetch", "origin", "--quiet")
    runner.prepare_worktree_detached(str(repo), f"origin/{branch}", review)
    parked = _git(review, "rev-parse", "HEAD")
    (review / "operator-notes.md").write_text("why I pinned this")
    _git(repo, "worktree", "lock", str(review))

    for _ in range(2):
        with pytest.raises(runner_mod.CheckoutRefused) as pinned:
            runner.prepare_worktree_detached(str(repo), f"origin/{branch}", review)
        # Named, and declared human-clearable: this is the one preparation refusal nothing in
        # the fleet can resolve on its own, which is what earns it an escalation clock (#406).
        assert pinned.value.refusal.check == "checkout-locked"
        assert pinned.value.refusal.stall and not pinned.value.refusal.expected
        assert str(review) in pinned.value.refusal.detail
    assert _git(review, "rev-parse", "HEAD") == parked
    assert (review / "operator-notes.md").read_text() == "why I pinned this"
    assert _git(repo, "for-each-ref", "--format=%(refname)",
                "refs/agentflow/stranded/pr-22-pinned/") == ""


def test_freshening_review_checkout_keeps_its_ready_environment(tmp_path):
    """Repeated review preparation must not delete a ready environment and rebuild it before
    every admission attempt."""
    repo = _repo_with_origin(tmp_path)
    runner = ClaudeRunner()
    review = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-12-ready"
    _detached_worktree(repo, review)
    excludes = tmp_path / "review-excludes"
    excludes.write_text(".venv/\n")
    _git(review, "config", "core.excludesFile", str(excludes))
    python = review / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("ready")

    runner.prepare_worktree_detached(str(repo), "origin/main", review)

    assert python.read_text() == "ready"


def test_fresh_agentflow_worktree_is_owned_and_carries_no_discovery_probe(tmp_path):
    from agentflow.provider_skills import NATIVE_DISCOVERY_SKILL
    from agentflow.worktree_ownership import worktree_ownership

    repo = _repo_with_origin(tmp_path)
    review = repo / ".agentflow" / "worktrees" / "codex-review" / "pr-1-fresh"

    CodexRunner().prepare_worktree_detached(str(repo), "origin/main", review)

    assert worktree_ownership(review) == {
        "schema": 1,
        "owner": "agentflow",
        "worktree": os.path.realpath(review),
        "disposable": True,
    }
    for location in (".agents", ".claude"):
        assert not (review / location / "skills" / NATIVE_DISCOVERY_SKILL).exists()


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
    foreign_wt = root / "foreign-repo" / "foreign-open-pr"
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


def test_pr_state_for_branch_reads_the_all_state_listing_and_fails_closed(monkeypatch):
    from agentflow import github

    monkeypatch.setattr(github, "prs_for_branch", lambda repo, branch, **k: None)
    assert runner_mod._pr_state_for_branch("o/r", "b") is None  # lookup failed → unknown
    monkeypatch.setattr(github, "prs_for_branch", lambda repo, branch, **k: [])
    assert runner_mod._pr_state_for_branch("o/r", "b") is None  # no PR ever opened
    monkeypatch.setattr(github, "prs_for_branch", lambda repo, branch, **k: [
        github.BranchPrRow(number=1, state="MERGED", head_ref_name=branch, url="")])
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
    monkeypatch.setattr(runner_mod, "_codebase_memory_mcp_servers",
                        lambda: {"codebase-memory-mcp": {"command": "/x/code-graph"}})

    read_only = StageProfile(("Read", "Bash", "Grep", "Glob"), 900, 40)
    for profile, sandbox in ((None, "workspace-write"), (read_only, "read-only")):
        cmd = CodexRunner().structured_argv("do work", "sol", str(wt), profile=profile)
        override = 'mcp_servers.codebase-memory-mcp.command="/x/code-graph"'
        assert override in cmd and cmd[cmd.index(override) - 1] == "-c"
        assert "--ignore-user-config" in cmd            # personal account connectors stay excluded
        assert cmd[cmd.index("--sandbox") + 1] == sandbox  # read-only stage keeps its boundary

    # No operator-local servers → nothing is re-supplied (the personal connectors were the risk).
    monkeypatch.setattr(runner_mod, "_codebase_memory_mcp_servers", lambda: {})
    cmd = CodexRunner().structured_argv("do work", "sol", str(wt))
    assert not any(str(arg).startswith("mcp_servers.") for arg in cmd)


def test_codex_keeps_only_codebase_memory_without_private_environment(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {
        "codebase-memory-mcp": {
            "command": "npx",
            "args": ["-y", "codebase-memory-mcp"],
            "env": {"GRAPH_TOKEN": "must-not-cross"},
        },
        "google-drive": {
            "command": "/x/drive",
            "env": {"DRIVE_TOKEN": "also-private"},
        },
    }}))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)

    cmd = provider_command(_provider_record("codex", tmp_path))
    overrides = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-c"]
    assert 'mcp_servers.codebase-memory-mcp.command="npx"' in overrides
    assert (
        'mcp_servers.codebase-memory-mcp.args=["-y", "codebase-memory-mcp"]'
        in overrides
    )
    assert not any("google-drive" in override or ".env." in override for override in overrides)
    assert "must-not-cross" not in " ".join(cmd)
    assert "also-private" not in " ".join(cmd)


def test_codex_renderer_never_accepts_mcp_environment_values(monkeypatch, tmp_path):
    monkeypatch.setattr(runner_mod, "_codebase_memory_mcp_servers", lambda: {
        "codebase-memory-mcp": {
            "command": "/x/code-graph",
            "env": {"PRIVATE_TOKEN": "must-not-cross"},
        },
    })

    cmd = provider_command(_provider_record("codex", tmp_path))
    assert ".env." not in " ".join(cmd)
    assert "must-not-cross" not in " ".join(cmd)


def test_a_sessions_leftover_untracked_files_do_not_block_reuse_but_edits_do(tmp_path):
    """Re-rebasing a survivor resets its worktree to the PR head. That overwrites tracked content
    and leaves untracked files alone, so a build session's leftover scratch files must not veto the
    reuse — vetoing them stalls the re-rebase forever while protecting nothing. Uncommitted edits
    to tracked files, which the reset would destroy, still do."""
    from agentflow.runner import resettable_head

    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-12-litter"
    _branch_worktree(repo, wt, "agentflow/claude/issue-12-litter")
    head = _git(wt, "rev-parse", "HEAD")

    (wt / "shots-config.json").write_text("{}")
    (wt / "pr-body.md").write_text("draft")
    assert resettable_head(str(repo), wt) == head
    assert remove_worktree_if_safe(str(repo), wt) is False, "removal must still refuse litter"

    (wt / "result.txt").write_text("uncommitted edit a reset would destroy")
    assert resettable_head(str(repo), wt) == ""

    with worktree_session(wt):
        assert resettable_head(str(repo), wt) == ""


# --- bounded retention: archive-then-reclaim (ADR 0050) ---------------------------------

def _age_worktree(wt: Path, seconds: float) -> None:
    """Backdate every clock reclamation reads on this checkout — the directory's own mtime, its
    registration's index, and its HEAD commit. Idleness is seeded explicitly so no test ever
    sleeps for it."""
    old = time.time() - seconds
    stamp = datetime.datetime.fromtimestamp(old).isoformat()
    subprocess.run(["git", "-C", str(wt), "-c", "user.name=agentflow test",
                    "-c", "user.email=agentflow@example.com",
                    "commit", "--amend", "--no-edit", "--allow-empty"],
                   env={**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp},
                   check=True, capture_output=True)
    index = Path(_git(wt, "rev-parse", "--git-path", "index"))
    index = index if index.is_absolute() else wt / index
    os.utime(index, (old, old))
    os.utime(wt, (old, old))


def _stranded_session(repo: Path, number: int, *, hours: float, push: bool = True) -> Path:
    """One session checkout holding both a modified tracked file and an untracked one — the
    shape that pins a registration forever today — with its clocks pushed into the past."""
    wt = repo / ".agentflow" / "worktrees" / "codex" / f"issue-{number}-stranded"
    _branch_worktree(repo, wt, f"agentflow/codex/issue-{number}-stranded", push=push)
    (wt / "result.txt").write_text(f"uncommitted edit {number}")
    (wt / "notes.md").write_text(f"untracked note {number}")
    _age_worktree(wt, hours * 3600)
    return wt


def _incomplete_builds(monkeypatch, *, complete: set[int] = frozenset()) -> None:
    """State every seeded build as still building — except the numbers in ``complete``, whose
    branch has been squash-merged and pruned from the remote."""
    from agentflow import github

    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset(
        {"ready-for-agent"} if issue in complete else {"agentflow:building"}))
    monkeypatch.setattr(runner_mod, "_pr_state_for_branch", lambda repo, branch: (
        "MERGED" if any(f"issue-{n}-" in branch for n in complete) else None))


def test_recovery_bounds_stranded_sessions_and_keeps_their_work_on_a_ref(tmp_path, monkeypatch):
    """The outage bar: a repository seeded past the cap comes back down to it, and every
    reclaimed session's exact content — including the untracked file — survives on its ref."""
    repo = _repo_with_origin(tmp_path)
    _incomplete_builds(monkeypatch, complete={200})
    seeded = [_stranded_session(repo, 100 + i, hours=30 + i)
              for i in range(runner_mod.RETAINED_WORKTREE_CAP + 3)]
    # Complete, but undisposable: an untracked file and a branch origin no longer contains.
    merged = _stranded_session(repo, 200, hours=90)
    _git(repo, "push", "origin", "--delete", "agentflow/codex/issue-200-stranded")
    _git(repo, "fetch", "--prune", "origin")

    report = recover_stale_worktrees("owner/repo", str(repo))

    assert len(report.retained) == runner_mod.RETAINED_WORKTREE_CAP
    assert [path for path, _ref in report.archived] == [
        str(merged), *(str(wt) for wt in reversed(seeded[-3:]))]  # oldest first
    for path, ref in report.archived:
        number = int(Path(path).name.split("-")[1])
        assert not Path(path).exists()
        assert _git(repo, "show", f"{ref}:result.txt") == f"uncommitted edit {number}"
        assert _git(repo, "show", f"{ref}:notes.md") == f"untracked note {number}"
    assert all(Path(path).exists() for path in report.retained)


def test_recovery_never_archives_active_protected_or_fresh_sessions(tmp_path, monkeypatch):
    """Everything with a live claim on it survives, whatever the cap says: a marked session, a
    source a live coordinator record owns, and one that simply has not been idle long enough."""
    repo = _repo_with_origin(tmp_path)
    _incomplete_builds(monkeypatch)
    monkeypatch.setattr(runner_mod, "RETAINED_WORKTREE_CAP", 0)
    protected = _stranded_session(repo, 301, hours=90)
    marked = _stranded_session(repo, 302, hours=90)
    fresh = _stranded_session(repo, 303, hours=1)
    expendable = _stranded_session(repo, 304, hours=90)

    child = subprocess.Popen(["sleep", "30"])
    try:
        with worktree_session(marked):
            runner_mod._active_marker(marked).write_text(str(child.pid))
            runner_mod._ACTIVE_WORKTREES.clear()  # a freshly started recovery process
            report = recover_stale_worktrees("owner/repo", str(repo),
                                             protected={os.path.realpath(protected)})
    finally:
        child.terminate()
        child.wait()

    assert [path for path, _ref in report.archived] == [str(expendable)]
    assert set(report.retained) == {str(protected), str(marked), str(fresh)}
    assert protected.exists() and marked.exists() and fresh.exists()


def test_unknown_completion_is_archived_only_once_idle_and_over_the_cap(tmp_path, monkeypatch):
    """Deliberate narrowing: a session GitHub cannot answer for is no longer retained forever.
    It is still retained while it is under the cap or recently touched — and when it finally
    goes, its work goes to a ref, which is what makes the narrowing safe."""
    from agentflow import github

    repo = _repo_with_origin(tmp_path)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: None)  # unreadable
    monkeypatch.setattr(runner_mod, "_pr_state_for_branch", lambda repo, branch: "OPEN")
    idle = [_stranded_session(repo, 400 + i, hours=30 + i)
            for i in range(runner_mod.RETAINED_WORKTREE_CAP + 1)]
    recent = _stranded_session(repo, 499, hours=2)

    report = recover_stale_worktrees("owner/repo", str(repo))

    assert [path for path, _ref in report.archived] == [str(idle[-1])]
    assert set(report.retained) == {str(wt) for wt in idle[:-1]} | {str(recent)}
    ref = report.archived[0][1]
    assert _git(repo, "show", f"{ref}:notes.md") == "untracked note 412"


def test_archives_are_ordered_by_idleness_not_by_when_the_checkout_was_made(tmp_path, monkeypatch):
    repo = _repo_with_origin(tmp_path)
    _incomplete_builds(monkeypatch)
    monkeypatch.setattr(runner_mod, "RETAINED_WORKTREE_CAP", 1)
    middle = _stranded_session(repo, 501, hours=60)
    oldest = _stranded_session(repo, 502, hours=100)
    newest = _stranded_session(repo, 503, hours=30)

    report = recover_stale_worktrees("owner/repo", str(repo))

    assert [path for path, _ref in report.archived] == [str(oldest), str(middle)]
    assert report.retained == (str(newest),)


def test_a_sweep_archives_no_more_than_its_budget_and_converges(tmp_path, monkeypatch):
    repo = _repo_with_origin(tmp_path)
    _incomplete_builds(monkeypatch)
    monkeypatch.setattr(runner_mod, "RETAINED_WORKTREE_CAP", 1)
    monkeypatch.setattr(runner_mod, "SWEEP_ARCHIVE_BUDGET", 2)
    for i in range(4):
        _stranded_session(repo, 600 + i, hours=100 - i)

    first = recover_stale_worktrees("owner/repo", str(repo))
    assert len(first.archived) == 2 and len(first.retained) == 2

    second = recover_stale_worktrees("owner/repo", str(repo))
    assert len(second.archived) == 1 and len(second.retained) == 1


def test_research_and_conversation_checkouts_are_never_reclaimed(tmp_path, monkeypatch):
    """A conversation's checkout is its only durable output and is reused across turns while each
    turn's record retires — so it is excluded outright, not merely protected."""
    repo = _repo_with_origin(tmp_path)
    _incomplete_builds(monkeypatch)
    monkeypatch.setattr(runner_mod, "RETAINED_WORKTREE_CAP", 0)
    root = repo / ".agentflow" / "worktrees"
    ask = root / "claude" / "ask-2f9c1d"
    research = root / "claude" / "research-77"
    for wt in (ask, research):
        _detached_worktree(repo, wt)
        _age_worktree(wt, 200 * 3600)
    build = _stranded_session(repo, 700, hours=200)

    report = recover_stale_worktrees("owner/repo", str(repo))

    assert [path for path, _ref in report.archived] == [str(build)]
    assert set(report.retained) == {str(ask), str(research)}
    assert ask.exists() and research.exists()


def test_archiving_a_clean_checkout_anchors_its_head_without_inventing_a_commit(tmp_path):
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-800-clean"
    _branch_worktree(repo, wt, "agentflow/codex/issue-800-clean")
    head = _git(wt, "rev-parse", "HEAD")

    ref = runner_mod.archive_stranded_worktree(str(repo), wt)

    assert ref.startswith("refs/agentflow/stranded/issue-800-clean/")
    assert _git(repo, "rev-parse", ref) == head  # HEAD itself, no snapshot commit on top
    assert not wt.exists()


def test_a_failed_archive_leaves_the_checkout_and_its_real_index_untouched(tmp_path, monkeypatch):
    """The snapshot is built in a scratch index. Staging into the real one would read as tracked
    modification forever and permanently stall every later reset of this checkout."""
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-801-anchorless"
    _branch_worktree(repo, wt, "agentflow/codex/issue-801-anchorless")
    head = _git(wt, "rev-parse", "HEAD")
    (wt / "scratch.md").write_text("untracked draft")

    real_run = runner_mod._run

    def refuse_update_ref(cmd, *args, **kwargs):
        if "update-ref" in cmd:
            return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="denied")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(runner_mod, "_run", refuse_update_ref)

    assert runner_mod.archive_stranded_worktree(str(repo), wt) == ""
    assert wt.exists() and (wt / "scratch.md").exists()
    assert runner_mod.resettable_head(str(repo), wt) == head


def test_archiving_works_on_a_host_with_no_git_identity(tmp_path, monkeypatch):
    """agentflow commits nowhere else, so nothing else would notice a missing identity — and the
    bound would silently stop applying on such a host."""
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-802-anonymous"
    _branch_worktree(repo, wt, "agentflow/codex/issue-802-anonymous")
    (wt / "result.txt").write_text("work done by nobody in particular")
    _git(repo, "config", "--unset", "user.email")
    _git(repo, "config", "--unset", "user.name")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    monkeypatch.delenv("GIT_COMMITTER_NAME", raising=False)

    ref = runner_mod.archive_stranded_worktree(str(repo), wt)

    assert ref and not wt.exists()
    assert _git(repo, "show", f"{ref}:result.txt") == "work done by nobody in particular"


def test_a_locked_worktree_is_never_reclaimed(tmp_path):
    """A lock is a deliberate human signal; one --force is not enough to remove it, and it does
    not get a second. Refusing must also anchor nothing, or a caller that retries every cycle
    buries the real stranded work under a pile of dead recovery refs."""
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "codex" / "issue-803-locked"
    _branch_worktree(repo, wt, "agentflow/codex/issue-803-locked")
    (wt / "result.txt").write_text("work in progress the operator pinned")
    _git(repo, "worktree", "lock", str(wt))

    assert runner_mod.archive_stranded_worktree(str(repo), wt) == ""
    assert runner_mod.archive_stranded_worktree(str(repo), wt) == ""
    assert wt.exists() and (wt / "result.txt").read_text().startswith("work in progress")
    assert _git(repo, "for-each-ref", "--format=%(refname)", "refs/agentflow/stranded/") == ""


def test_dispatch_preflight_refuses_a_repository_that_can_no_longer_carry_a_session(
        tmp_path, monkeypatch):
    repo = _repo_with_origin(tmp_path)
    owned = repo / ".agentflow" / "worktrees" / "codex" / "issue-900-owned"
    _branch_worktree(repo, owned, "agentflow/codex/issue-900-owned")
    monkeypatch.setattr(runner_mod, "recover_stale_worktrees",
                        lambda *a, **k: pytest.fail("the gate must never sweep"))
    logs = []

    assert runner_mod.dispatch_preflight("owner/repo", str(repo), set(), _log=logs.append)
    assert logs == []

    before = _git(repo, "worktree", "list", "--porcelain")
    monkeypatch.setattr(runner_mod, "WORKTREE_DISPATCH_CEILING", 1)
    assert not runner_mod.dispatch_preflight(
        "owner/repo", str(repo), {os.path.realpath(owned)}, _log=logs.append)
    assert _git(repo, "worktree", "list", "--porcelain") == before  # count-only: nothing changed
    refusal = logs[-1]
    assert "REFUSING" in refusal
    assert "1 agentflow-owned" in refusal and "1 foreign" in refusal and "1 protected" in refusal

    monkeypatch.setattr(runner_mod, "_registered_worktrees", lambda workdir: None)
    assert runner_mod.dispatch_preflight("owner/repo", str(repo), set(), _log=logs.append)
    assert "could not read the registry" in logs[-1]  # fails open, loudly


def test_the_dispatch_ceiling_refuses_below_the_registration_count_that_killed_sessions(
        monkeypatch):
    """Fails on today's code: the 175 ceiling admitted the launches that died on 2026-07-31
    (#442) at 53/52 listed registrations, when the Claude CLI's sandbox profile — three deny
    paths per linked worktree, embedded in every shell spawn's argv — crossed the OS
    exec-argument limit and every command in those sessions failed to spawn. The ceiling is
    only a guard if it refuses before the measured death point, with margin for the
    registrations concurrent sessions add between preflight and spawn."""
    death_point = [(f"/w/issue-{n}", None) for n in range(52)]
    monkeypatch.setattr(runner_mod, "_registered_worktrees", lambda workdir: death_point)
    logs = []
    assert not runner_mod.dispatch_preflight("owner/repo", "/w", set(), _log=logs.append)
    assert "REFUSING" in logs[-1]


def test_dispatch_preflight_reserves_the_slot_for_the_worktree_it_admits(monkeypatch):
    """Fails on today's `<=` boundary: at exactly WORKTREE_DISPATCH_CEILING registrations a cold
    submission is admitted, and the session it admits opens registration ceiling+1 — back inside
    the range #442 measured dead shells in. The ceiling is the count that may still exist *after*
    the admitted worktree appears, so admission reserves that slot: refuse at the ceiling, admit
    one below it."""
    ceiling = runner_mod.WORKTREE_DISPATCH_CEILING
    at_ceiling = [(f"/w/issue-{n}", None) for n in range(ceiling)]
    monkeypatch.setattr(runner_mod, "_registered_worktrees", lambda workdir: at_ceiling)
    logs = []
    assert not runner_mod.dispatch_preflight("owner/repo", "/w", set(), _log=logs.append)
    assert "REFUSING" in logs[-1]

    one_below = [(f"/w/issue-{n}", None) for n in range(ceiling - 1)]
    monkeypatch.setattr(runner_mod, "_registered_worktrees", lambda workdir: one_below)
    assert runner_mod.dispatch_preflight("owner/repo", "/w", set())
