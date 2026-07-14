"""Dashboard v2 web server (ADR 0023) — FastAPI console over the same snapshot.

The first slice of the operator-dashboard re-platform. It serves the built Svelte
console and one read-only endpoint, `GET /api/snapshot`, whose body is exactly what
`dashboard_data.snapshot()` produces today (dispatch + pools + repos). The stdlib
`server.py` dashboard keeps running untouched until parity is reached.

Two clocks (ADR 0023): the browser polls every few seconds, but every GitHub-backed
snapshot is reused for ~15s so a fast poll never multiplies `gh` calls. That reuse is
the whole point of `SnapshotCache`, whose one method hides the TTL, the timestamp, and
the single-flight lock behind `get()`.

    uv run agentflow-web        # build the console first: see agentflow/webui/README.md
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DIST = Path(__file__).parent / "webui" / "dist"


class SnapshotCache:
    """Serve one freshly-produced value for `ttl` seconds, then produce the next.

    A burst of callers that all miss together get a single production, not one each:
    the first caller through holds the lock while it produces, and everyone else waits
    and reads that same fresh value. `get()` is the entire interface — callers never
    see the timestamp, the lock, or whether their call was the one that refreshed.
    """

    def __init__(
        self,
        produce: Callable[[], Any],
        *,
        ttl: float = 15.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._produce = produce
        self._ttl = ttl
        self._clock = clock
        self._lock = threading.Lock()
        self._value: Any = None
        self._made_at: float | None = None

    def get(self) -> Any:
        with self._lock:
            now = self._clock()
            if self._made_at is None or now - self._made_at >= self._ttl:
                self._value = self._produce()
                self._made_at = now
            return self._value


def create_app(
    repos: list,
    dispatch_enabled: Callable[[], bool],
    *,
    ttl: float = 15.0,
    clock: Callable[[], float] = time.monotonic,
    dist: Path = DIST,
):
    """Build the FastAPI app: the cached snapshot endpoint plus the built console."""
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    from agentflow.dashboard_data import snapshot

    cache = SnapshotCache(
        lambda: snapshot(repos, dispatch_enabled=dispatch_enabled()),
        ttl=ttl,
        clock=clock,
    )

    app = FastAPI(title="agentflow console", docs_url=None, redoc_url=None)

    @app.get("/api/snapshot")
    def api_snapshot():
        return cache.get()

    # The console SPA is a static build; mount it last so /api/* wins. Serving is a
    # no-op until `npm run build` has produced dist/ (see agentflow/webui/README.md).
    if (dist / "index.html").exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="console")
    else:
        @app.get("/", response_class=PlainTextResponse)
        def unbuilt():
            return "console not built — run `npm ci && npm run build` in agentflow/webui\n"

    return app


def main() -> None:
    import uvicorn

    # Imported here so the app has no import-time dependency on its daemon owner.
    from agentflow.daemon import ENABLE_FLAG, REPOS

    app = create_app(REPOS, lambda: ENABLE_FLAG.exists())
    port = 8788  # one past the stdlib dashboard's 8787 — both can run side by side
    print(f"agentflow console (v2) on http://localhost:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
