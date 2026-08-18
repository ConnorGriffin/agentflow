from __future__ import annotations

import os
import plistlib
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import macos_service
from agentflow.macos_service import ServiceError, install, probe_console


class _SnapshotHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib method name)
        if self.path == "/api/snapshot":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence stdlib per-request logging
        pass


def test_bootstrap_retries_until_launchctl_accepts_the_reloaded_job(monkeypatch, tmp_path):
    calls = []
    sleeps = []
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),  # bootout
            SimpleNamespace(returncode=5, stdout="", stderr="Input/output error"),
            SimpleNamespace(returncode=0, stdout="", stderr=""),
        ]
    )

    monkeypatch.setattr(
        macos_service.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or next(results),
    )
    monkeypatch.setattr(macos_service.time, "sleep", sleeps.append)

    macos_service._bootstrap("agentflow.console", tmp_path / "console.plist")

    assert [command[1] for command in calls] == ["bootout", "bootstrap", "bootstrap"]
    assert sleeps == [macos_service.BOOTSTRAP_BACKOFF_SECONDS[0]]


def test_bootstrap_gives_up_after_a_bounded_number_of_attempts(monkeypatch, tmp_path):
    calls = []
    sleeps = []

    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "bootout":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=5, stdout="", stderr="Input/output error")

    monkeypatch.setattr(macos_service.subprocess, "run", run)
    monkeypatch.setattr(macos_service.time, "sleep", sleeps.append)

    with pytest.raises(ServiceError, match="after 6 attempts: Input/output error"):
        macos_service._bootstrap("agentflow.console", tmp_path / "console.plist")

    bootstrap_calls = [command for command in calls if command[1] == "bootstrap"]
    assert len(bootstrap_calls) == 6
    assert sleeps == list(macos_service.BOOTSTRAP_BACKOFF_SECONDS)


def test_probe_console_confirms_the_snapshot_endpoint_is_reachable():
    server = HTTPServer(("127.0.0.1", 0), _SnapshotHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert probe_console(port=server.server_port) is True
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_probe_console_reports_unreachable_when_nothing_is_listening():
    server = HTTPServer(("127.0.0.1", 0), _SnapshotHandler)
    closed_port = server.server_port
    server.server_close()

    assert probe_console(port=closed_port) is False


def test_probe_console_reports_unreachable_on_a_non_ok_status():
    class _ErrorHandler(_SnapshotHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(503)
            self.end_headers()

    server = HTTPServer(("127.0.0.1", 0), _ErrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert probe_console(port=server.server_port) is False
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture
def installed_plists(tmp_path, monkeypatch):
    """Run ``install()`` against a scratch home with launchctl stubbed out.

    Returns a callable that installs and hands back the parsed daemon and console
    plists, with every ambient ``AGENTFLOW_*`` variable cleared first so tests
    control the operator environment exactly."""
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(macos_service, "_bootstrap", lambda label, plist: None)
    monkeypatch.setattr(macos_service, "_executable", lambda: tmp_path / "agentflow")
    for name in [n for n in os.environ if n.startswith("AGENTFLOW_")]:
        monkeypatch.delenv(name)
    config = tmp_path / "agentflow.toml"
    config.write_text("")

    def run():
        install(config)
        agents = home / "Library" / "LaunchAgents"
        return (
            plistlib.loads((agents / "agentflow.daemon.plist").read_bytes()),
            plistlib.loads((agents / "agentflow.console.plist").read_bytes()),
        )

    return run


def test_install_carries_the_operator_permit_budget_into_the_daemon_job(
    installed_plists, monkeypatch
):
    monkeypatch.setenv("AGENTFLOW_PERMIT_BUDGET", "25")

    daemon, _ = installed_plists()

    assert daemon["EnvironmentVariables"]["AGENTFLOW_PERMIT_BUDGET"] == "25"


def test_install_carries_every_set_runtime_knob_into_the_daemon_job(
    installed_plists, monkeypatch
):
    knobs = {
        "AGENTFLOW_MAX_SESSIONS": "6",
        "AGENTFLOW_TRIAGE_CONCURRENCY": "4",
        "AGENTFLOW_BUILD_CONCURRENCY": "3",
    }
    for name, value in knobs.items():
        monkeypatch.setenv(name, value)

    daemon, _ = installed_plists()

    for name, value in knobs.items():
        assert daemon["EnvironmentVariables"][name] == value


def test_install_without_runtime_knobs_writes_only_the_explicit_values(
    installed_plists, tmp_path
):
    daemon, console = installed_plists()

    assert set(daemon["EnvironmentVariables"]) == {
        "AGENTFLOW_CONFIG",
        "AGENTFLOW_STATE",
        "PATH",
    }
    assert daemon["EnvironmentVariables"]["AGENTFLOW_CONFIG"] == str(
        (tmp_path / "agentflow.toml").resolve()
    )
    assert set(console["EnvironmentVariables"]) == {"AGENTFLOW_STATE", "PATH"}


def test_install_keeps_the_console_job_free_of_dispatch_tuning(
    installed_plists, monkeypatch
):
    monkeypatch.setenv("AGENTFLOW_PERMIT_BUDGET", "25")

    _, console = installed_plists()

    assert set(console["EnvironmentVariables"]) == {"AGENTFLOW_STATE", "PATH"}


def test_install_validates_the_capacity_helper_even_with_pass_through(
    installed_plists, monkeypatch, tmp_path
):
    monkeypatch.setenv("AGENTFLOW_CAPACITY_HELPER", str(tmp_path / "missing-helper"))
    monkeypatch.setenv("AGENTFLOW_PERMIT_BUDGET", "25")

    with pytest.raises(ServiceError, match="AGENTFLOW_CAPACITY_HELPER"):
        installed_plists()


def test_install_rejects_a_capacity_helper_that_does_not_resolve_to_a_file(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("AGENTFLOW_CAPACITY_HELPER", str(tmp_path / "missing-helper"))
    config = tmp_path / "agentflow.toml"
    config.write_text("")

    with pytest.raises(ServiceError, match="AGENTFLOW_CAPACITY_HELPER"):
        install(config)

    # Nothing is left behind for launchctl to load: a bad helper path fails before
    # either service's plist is written or (re)loaded.
    assert not (home / "Library" / "LaunchAgents" / "agentflow.daemon.plist").exists()
    assert not (home / "Library" / "LaunchAgents" / "agentflow.console.plist").exists()


def test_install_rejects_a_relative_capacity_helper(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    helper = tmp_path / "capacity-helper"
    helper.write_text("#!/bin/sh\n")
    monkeypatch.setenv("AGENTFLOW_CAPACITY_HELPER", "capacity-helper")
    config = tmp_path / "agentflow.toml"
    config.write_text("")

    with pytest.raises(ServiceError, match="absolute path"):
        install(config)

    assert not (home / "Library" / "LaunchAgents" / "agentflow.daemon.plist").exists()


def test_install_rejects_a_capacity_helper_with_relative_path_segments(
    tmp_path, monkeypatch
):
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    real = tmp_path / "real"
    real.mkdir()
    helper = real / "capacity-helper"
    helper.write_text("#!/bin/sh\n")
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    monkeypatch.setenv(
        "AGENTFLOW_CAPACITY_HELPER", str(decoy / ".." / "real" / "capacity-helper")
    )
    config = tmp_path / "agentflow.toml"
    config.write_text("")

    with pytest.raises(ServiceError, match="relative path segments"):
        install(config)

    assert not (home / "Library" / "LaunchAgents" / "agentflow.daemon.plist").exists()
