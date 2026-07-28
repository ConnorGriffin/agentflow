from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

from agentflow import cli


ROOT = Path(__file__).parents[1]


def test_check_accepts_a_clean_clone_repository_config(tmp_path):
    checkout = tmp_path / "project"
    checkout.mkdir()
    config = tmp_path / "agentflow.toml"
    config.write_text(
        f"""
[[repositories]]
repo = "owner/project"
workdir = "{checkout}"
workspace = true
""".lstrip()
    )

    result = subprocess.run(
        ["uv", "run", "agentflow", "check", "--config", str(config)],
        cwd=ROOT,
        env=os.environ | {"AGENTFLOW_STATE": str(tmp_path / "state")},
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "configuration valid: 1 repository (1 workspace)"


def test_daemon_once_starts_with_only_the_configured_repositories(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    config = tmp_path / "agentflow.toml"
    config.write_text(
        f"""
[[repositories]]
repo = "owner/first"
workdir = "{first}"

[[repositories]]
repo = "owner/second"
workdir = "{second}"
workspace = true
""".lstrip()
    )
    state = tmp_path / "state"
    events = []

    with (
        mock.patch("agentflow.daemon.STATE_DIR", state),
        mock.patch("agentflow.daemon.LOCK", state / "daemon.lock"),
        mock.patch(
            "agentflow.daemon.recover_worktrees",
            side_effect=lambda repos: events.append(
                ("recover", [repo.repo for repo in repos])
            ),
        ),
        mock.patch(
            "agentflow.daemon.dispatch_cycle",
            side_effect=lambda repos: events.append(
                ("dispatch", [repo.repo for repo in repos])
            ),
        ),
        mock.patch(
            "agentflow.daemon.workspace_cycle",
            side_effect=lambda repos: events.append(
                ("workspace", [repo.repo for repo in repos])
            ),
        ),
        mock.patch(
            "agentflow.daemon.publish_snapshot",
            side_effect=lambda repos: events.append(
                ("publish", [repo.repo for repo in repos])
            ),
        ),
        mock.patch("agentflow.daemon.log"),
    ):
        cli.main(["daemon", "--once", "--config", str(config)])

    assert events == [
        ("recover", ["owner/first", "owner/second"]),
        ("dispatch", ["owner/first", "owner/second"]),
        ("workspace", ["owner/second"]),
        ("publish", ["owner/first", "owner/second"]),
    ]
    assert not (state / "daemon.lock").exists()


def test_resume_status_and_pause_control_cold_submission(tmp_path):
    env = os.environ | {"AGENTFLOW_STATE": str(tmp_path / "state")}

    resumed = subprocess.run(
        ["uv", "run", "agentflow", "resume"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    status = subprocess.run(
        ["uv", "run", "agentflow", "status"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    paused = subprocess.run(
        ["uv", "run", "agentflow", "pause"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert resumed.returncode == status.returncode == paused.returncode == 0
    assert resumed.stdout.strip() == "cold submission resumed"
    assert status.stdout.splitlines()[0] == "cold submission: enabled"
    assert paused.stdout.strip() == "cold submission paused"
    assert not (tmp_path / "state" / "enabled").exists()


def test_console_starts_from_the_same_public_command():
    with mock.patch("agentflow.webapp.main") as start_console:
        cli.main(["console"])

    start_console.assert_called_once_with()


def test_daemon_reports_the_missing_optional_capacity_helper(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTFLOW_CAPACITY_HELPER", raising=False)
    monkeypatch.delenv("AGENTFLOW_TRIAGE_GATE", raising=False)
    checkout = tmp_path / "project"
    checkout.mkdir()
    config = tmp_path / "agentflow.toml"
    config.write_text(
        f"""
[[repositories]]
repo = "owner/project"
workdir = "{checkout}"
""".lstrip()
    )
    state = tmp_path / "state"

    with (
        mock.patch("agentflow.daemon.STATE_DIR", state),
        mock.patch("agentflow.daemon.LOCK", state / "daemon.lock"),
        mock.patch("agentflow.daemon.recover_worktrees"),
        mock.patch("agentflow.daemon.dispatch_cycle"),
        mock.patch("agentflow.daemon.workspace_cycle"),
        mock.patch("agentflow.daemon.publish_snapshot"),
        mock.patch("agentflow.daemon.log") as daemon_log,
    ):
        cli.main(["daemon", "--once", "--config", str(config)])

    assert any(
        "capacity helper not configured" in call.args[0]
        for call in daemon_log.call_args_list
    )
