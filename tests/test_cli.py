from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from unittest import mock

from agentflow import cli, live


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
    # The unmocked live.mark_cycle write lands inside the private test directory, not wherever
    # AGENTFLOW_STATE pointed when agentflow.live was first imported (issue #396).
    assert live.DAEMON_FILE == tmp_path / "daemon-status.json"
    assert live.daemon_status() != {}


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


def test_floodgates_open_close_status_round_trip(tmp_path):
    env = os.environ | {"AGENTFLOW_STATE": str(tmp_path / "state")}
    run = lambda *args: subprocess.run(
        ["uv", "run", "agentflow", "floodgates", *args],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)

    flag = tmp_path / "state" / "floodgates"

    closed = run("status")
    opened = run("open")
    status_open = run("status")
    still_there = flag.exists()
    contents = flag.read_text().strip() if still_there else ""
    reclosed = run("close")
    gone = flag.exists()
    status_closed = run("status")

    assert closed.returncode == opened.returncode == 0
    assert closed.stdout.strip() == "floodgates CLOSED"
    assert opened.stdout.strip() == "floodgates OPEN"
    assert still_there
    assert contents.isdigit()
    assert status_open.stdout.strip() == "floodgates OPEN (source: file)"
    assert reclosed.stdout.strip() == "floodgates CLOSED"
    assert not gone
    assert status_closed.stdout.strip() == "floodgates CLOSED"


def test_floodgates_status_reports_env_source(tmp_path):
    env = os.environ | {
        "AGENTFLOW_STATE": str(tmp_path / "state"),
        "AGENTFLOW_FLOODGATES": "1",
    }
    result = subprocess.run(
        ["uv", "run", "agentflow", "floodgates", "status"],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30)
    assert result.stdout.strip() == "floodgates OPEN (source: env)"


def test_console_starts_from_the_same_public_command():
    with mock.patch("agentflow.webapp.main") as start_console:
        cli.main(["console"])

    start_console.assert_called_once_with()


def test_status_reports_the_daemon_and_console_independently(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))

    with mock.patch("agentflow.macos_service.probe_console", return_value=True):
        cli.main(["status"])
    reachable = capsys.readouterr().out.splitlines()

    with mock.patch("agentflow.macos_service.probe_console", return_value=False):
        cli.main(["status"])
    unreachable = capsys.readouterr().out.splitlines()

    assert reachable[-1] == "console: reachable"
    assert unreachable[-1] == "console: unreachable"
    # Dispatch pause and daemon process are already independent facts (issue #371); the
    # console line is a third, equally independent fact — none of the three implies another.
    assert reachable[0] == unreachable[0]
    assert reachable[1] == unreachable[1]


def test_public_daemon_selects_the_bundled_capacity_helper(tmp_path, monkeypatch):
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

    helper = Path(os.environ["AGENTFLOW_CAPACITY_HELPER"])
    assert helper.name == "agentflow-capacity-helper"
    assert helper.is_file()
    assert not any(
        "capacity helper not configured" in call.args[0]
        for call in daemon_log.call_args_list
    )
    # The unmocked live.mark_cycle write lands inside the private test directory (issue #396).
    assert live.DAEMON_FILE == tmp_path / "daemon-status.json"
    assert live.daemon_status() != {}


def test_service_install_supervises_the_daemon_with_explicit_runtime_paths(tmp_path):
    home = tmp_path / "home"
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
    helper = tmp_path / "capacity-helper"
    helper.write_text("#!/bin/sh\n")
    helper.chmod(0o755)
    launchctl_log = tmp_path / "launchctl.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$AGENTFLOW_LAUNCHCTL_LOG"\n'
    )
    launchctl.chmod(0o755)
    env = os.environ | {
        "HOME": str(home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        "AGENTFLOW_STATE": str(state),
        "AGENTFLOW_CAPACITY_HELPER": str(helper),
        "AGENTFLOW_LAUNCHCTL_LOG": str(launchctl_log),
    }

    result = subprocess.run(
        [
            "uv",
            "run",
            "agentflow",
            "service",
            "install",
            "--config",
            str(config),
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "daemon service installed and running"
    plist_path = home / "Library" / "LaunchAgents" / "agentflow.daemon.plist"
    with plist_path.open("rb") as stream:
        service = plistlib.load(stream)
    assert service["Label"] == "agentflow.daemon"
    assert Path(service["ProgramArguments"][0]).is_absolute()
    assert service["ProgramArguments"][0].endswith("/agentflow")
    assert service["ProgramArguments"][1:] == ["daemon"]
    assert service["KeepAlive"] is True
    assert "RunAtLoad" not in service
    environment = service["EnvironmentVariables"]
    assert {
        key: environment[key]
        for key in (
            "AGENTFLOW_CAPACITY_HELPER",
            "AGENTFLOW_CONFIG",
            "AGENTFLOW_STATE",
        )
    } == {
        "AGENTFLOW_CAPACITY_HELPER": str(helper.resolve()),
        "AGENTFLOW_CONFIG": str(config.resolve()),
        "AGENTFLOW_STATE": str(state.resolve()),
    }
    path_entries = environment["PATH"].split(os.pathsep)
    assert str(Path(service["ProgramArguments"][0]).parent) in path_entries
    assert str(fake_bin) in path_entries

    console_plist_path = home / "Library" / "LaunchAgents" / "agentflow.console.plist"
    with console_plist_path.open("rb") as stream:
        console_service = plistlib.load(stream)
    assert console_service["Label"] == "agentflow.console"
    assert console_service["ProgramArguments"] == [
        service["ProgramArguments"][0],
        "console",
    ]
    assert console_service["KeepAlive"] is True
    assert "RunAtLoad" not in console_service
    console_environment = console_service["EnvironmentVariables"]
    assert console_environment["AGENTFLOW_STATE"] == str(state.resolve())
    assert "AGENTFLOW_CONFIG" not in console_environment
    assert "AGENTFLOW_CAPACITY_HELPER" not in console_environment
    assert console_service["StandardOutPath"] != service["StandardOutPath"]
    assert Path(console_service["StandardOutPath"]).name == "agentflow-console.log"

    domain = f"gui/{os.getuid()}"
    assert launchctl_log.read_text().splitlines() == [
        f"bootout {domain}/agentflow.daemon",
        f"bootstrap {domain} {plist_path}",
        f"bootout {domain}/agentflow.console",
        f"bootstrap {domain} {console_plist_path}",
    ]
    state.mkdir()
    (state / "enabled").touch()

    removed = subprocess.run(
        ["uv", "run", "agentflow", "service", "remove"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert removed.returncode == 0, removed.stderr
    assert removed.stdout.strip() == "daemon service removed"
    assert not plist_path.exists()
    assert not console_plist_path.exists()
    assert config.exists()
    # Removing the service stops both processes but never touches dispatch pause state:
    # the console and daemon lifecycle is independent of cold submission (issue #371).
    assert (state / "enabled").exists()
    assert launchctl_log.read_text().splitlines()[-2:] == [
        f"bootout {domain}/agentflow.daemon",
        f"bootout {domain}/agentflow.console",
    ]


def test_enroll_audit_and_sync_exit_two_on_a_bad_config(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        '[[repositories]]\nrepo = "o/missing"\nworkdir = "/does/not/exist"\n'
    )
    env = os.environ | {
        "AGENTFLOW_CONFIG": str(config),
        "AGENTFLOW_STATE": str(tmp_path / "state"),
    }
    run = lambda *args: subprocess.run(
        ["uv", "run", "agentflow", "enroll", *args],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
    )

    audit = run("--audit")
    sync = run("--sync")

    assert audit.returncode == 2, audit.stderr
    assert sync.returncode == 2, sync.stderr
    assert "workdir is not a directory" in audit.stderr
    assert "workdir is not a directory" in sync.stderr


def test_enroll_module_and_cli_audit_produce_identical_output(tmp_path):
    checkout = tmp_path / "project"
    checkout.mkdir()
    (checkout / "AGENTS.md").write_text("ui-surfaces: none\n")
    config = tmp_path / "config.toml"
    config.write_text(f'[[repositories]]\nrepo = "o/project"\nworkdir = "{checkout}"\n')
    env = os.environ | {
        "AGENTFLOW_CONFIG": str(config),
        "AGENTFLOW_STATE": str(tmp_path / "state"),
    }

    module = subprocess.run(
        ["uv", "run", "python", "-m", "agentflow.enroll", "audit"],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
    )
    via_cli = subprocess.run(
        ["uv", "run", "agentflow", "enroll", "--audit"],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
    )

    assert module.returncode == 0, module.stderr
    assert via_cli.returncode == 0, via_cli.stderr
    assert module.stdout == via_cli.stdout


def test_enroll_sync_dry_run_exits_zero(tmp_path):
    checkout = tmp_path / "project"
    checkout.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(f'[[repositories]]\nrepo = "o/project"\nworkdir = "{checkout}"\n')
    env = os.environ | {
        "AGENTFLOW_CONFIG": str(config),
        "AGENTFLOW_STATE": str(tmp_path / "state"),
    }

    result = subprocess.run(
        ["uv", "run", "agentflow", "enroll", "--sync"],
        cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "converged / " in result.stdout
    assert "skipped (dirty)" in result.stdout  # tail line always names the bucket
