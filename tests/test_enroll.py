"""Explicit enrollment protects agentflow's working area from Git status."""

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "enroll-standards.sh"
CHARTER = Path(__file__).parents[1] / "standards" / "CHARTER.md"


def _enroll(repo: Path, *, apply: bool) -> subprocess.CompletedProcess:
    args = ["bash", str(SCRIPT)]
    if apply:
        args.append("--apply")
    args.append(str(repo))
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          env={**os.environ, "PATH": "/usr/bin:/bin"})


def _wire_global(home: Path, *, apply: bool) -> subprocess.CompletedProcess:
    args = ["bash", str(SCRIPT)]
    if apply:
        args.append("--apply")
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          env={**os.environ, "HOME": str(home),
                               "PATH": "/usr/bin:/bin"})


def _shared_global(home: Path) -> Path:
    shared = home / "Code" / "ConnorGriffin" / "dotfiles" / "agents" / "AGENTS.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("# Shared preferences\n")
    return shared


def test_enrollment_dry_run_does_not_create_gitignore(tmp_path):
    _enroll(tmp_path, apply=False)

    assert not (tmp_path / ".gitignore").exists()


def test_enrollment_apply_creates_gitignore_with_agentflow_rule(tmp_path):
    _enroll(tmp_path, apply=True)

    assert (tmp_path / ".gitignore").read_text() == ".agentflow/\n"


def test_enrollment_preserves_existing_ignore_content(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text(".venv/\n*.log\n")

    _enroll(tmp_path, apply=True)

    assert ignore.read_text() == ".venv/\n*.log\n.agentflow/\n"
    assert ignore.with_name(".gitignore.pre-agentflow").read_text() == ".venv/\n*.log\n"


def test_repeated_enrollment_adds_agentflow_rule_exactly_once(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text(".venv/\n")

    _enroll(tmp_path, apply=True)
    _enroll(tmp_path, apply=True)

    assert ignore.read_text().splitlines().count(".agentflow/") == 1


def test_global_wiring_makes_both_tools_share_one_file(tmp_path):
    dotfiles = tmp_path / "Code" / "ConnorGriffin" / "dotfiles"
    shared = _shared_global(tmp_path)

    claude_global = tmp_path / ".claude" / "CLAUDE.md"
    claude_global.parent.mkdir()
    claude_global.symlink_to(dotfiles / "claude" / "CLAUDE.md")

    codex_global = tmp_path / ".codex" / "AGENTS.md"
    codex_global.parent.mkdir()
    codex_global.symlink_to(CHARTER)

    _wire_global(tmp_path, apply=True)

    assert claude_global.readlink() == shared
    assert codex_global.readlink() == shared
    assert f"@{CHARTER}" in shared.read_text()


def test_global_wiring_creates_missing_tool_directories(tmp_path):
    shared = _shared_global(tmp_path)

    _wire_global(tmp_path, apply=True)

    assert (tmp_path / ".claude" / "CLAUDE.md").readlink() == shared
    assert (tmp_path / ".codex" / "AGENTS.md").readlink() == shared


def test_global_wiring_preserves_hand_written_file_and_unknown_link(tmp_path):
    _shared_global(tmp_path)

    claude_global = tmp_path / ".claude" / "CLAUDE.md"
    claude_global.parent.mkdir()
    claude_global.write_text("hand written\n")

    codex_global = tmp_path / ".codex" / "AGENTS.md"
    codex_global.parent.mkdir()
    codex_global.symlink_to("/tmp/unmanaged-agent-instructions")

    _wire_global(tmp_path, apply=True)

    assert claude_global.read_text() == "hand written\n"
    assert claude_global.with_name("CLAUDE.md.pre-agentflow").read_text() == "hand written\n"
    assert codex_global.readlink() == Path("/tmp/unmanaged-agent-instructions")
