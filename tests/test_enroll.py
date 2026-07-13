"""Explicit enrollment protects agentflow's working area from Git status."""

import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "enroll-standards.sh"


def _enroll(repo: Path, *, apply: bool) -> subprocess.CompletedProcess:
    args = ["bash", str(SCRIPT)]
    if apply:
        args.append("--apply")
    args.append(str(repo))
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          env={**os.environ, "PATH": "/usr/bin:/bin"})


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


def test_repeated_enrollment_adds_agentflow_rule_exactly_once(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text(".venv/\n")

    _enroll(tmp_path, apply=True)
    _enroll(tmp_path, apply=True)

    assert ignore.read_text().splitlines().count(".agentflow/") == 1
