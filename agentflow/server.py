"""The operator dashboard server (ADR 0010) — read-only fleet console over GitHub.

Zero-dependency stdlib http.server. `GET /` serves the dashboard page; `GET
/api/snapshot` returns the live fleet snapshot. It only READS — controls
(merge / ratchet / pause) are the next slice. Run:

    uv run python -m agentflow.server      # then open http://localhost:8787
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agentflow.daemon import REPOS
from agentflow.dashboard_data import snapshot

PORT = int(os.environ.get("AGENTFLOW_DASH_PORT", "8787"))
_PAGE = (Path(__file__).parent / "static" / "dashboard.html").read_text()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — stdlib name
        if self.path.startswith("/api/snapshot"):
            self._send(200, "application/json", json.dumps(snapshot(REPOS)).encode())
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


def main() -> None:
    print(f"agentflow dashboard on http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
