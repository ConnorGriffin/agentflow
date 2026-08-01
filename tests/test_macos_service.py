from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from agentflow.macos_service import probe_console


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
