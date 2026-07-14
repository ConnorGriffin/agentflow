"""The operator dashboard server (ADR 0010) — read-only fleet console over GitHub.

Zero-dependency stdlib http.server. `GET /` serves the dashboard page; `GET
/api/snapshot` returns the live fleet snapshot. It only READS — controls
(merge / ratchet / pause) are the next slice. Run:

    uv run python -m agentflow.server      # then open http://localhost:8787
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentflow.dashboard_data import snapshot
from agentflow.loop import RepoConfig
from agentflow.webapp import SnapshotCache

PORT = int(os.environ.get("AGENTFLOW_DASH_PORT", "8787"))
SNAPSHOT_TTL = 120.0  # seconds a produced snapshot is reused (ADR 0026 hotfix)
_PAGE = (Path(__file__).parent / "static" / "dashboard.html").read_text()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — stdlib name
        if self.path.startswith("/api/snapshot"):
            self._send(200, "application/json",
                       json.dumps(self.server.snapshot_cache.get()).encode())
        elif self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", _PAGE.encode())
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):  # quiet
        pass


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        repos: list[RepoConfig],
        dispatch_enabled: Callable[[], bool],
    ) -> None:
        self.repos = repos
        self.dispatch_enabled = dispatch_enabled
        # A watched tab polls every few seconds; without this reuse window every poll
        # pays the full ~6-GraphQL-queries-per-repo price and one open tab can exhaust
        # the hourly quota by itself (ADR 0026).
        self.snapshot_cache = SnapshotCache(
            lambda: snapshot(repos, dispatch_enabled=dispatch_enabled()),
            ttl=SNAPSHOT_TTL,
        )
        super().__init__(address, Handler)


@contextmanager
def dashboard(
    repos: list[RepoConfig],
    dispatch_enabled: Callable[[], bool],
    *,
    port: int | None = None,
) -> Iterator[tuple[str, int]]:
    """Serve the console in the background and stop it when its owner exits."""
    httpd = DashboardHTTPServer(
        ("127.0.0.1", PORT if port is None else port), repos, dispatch_enabled
    )
    thread = threading.Thread(
        target=httpd.serve_forever,
        name="agentflow-dashboard",
        daemon=True,
    )
    thread.start()
    try:
        yield httpd.server_address
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def main() -> None:
    # Imported here so the reusable server has no dependency back on its daemon owner.
    from agentflow.daemon import ENABLE_FLAG, REPOS

    with dashboard(REPOS, lambda: ENABLE_FLAG.exists()) as (_, port):
        print(f"agentflow dashboard on http://localhost:{port}", flush=True)
        threading.Event().wait()


if __name__ == "__main__":
    main()
