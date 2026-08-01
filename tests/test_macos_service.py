from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

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
