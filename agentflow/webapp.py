"""Dashboard v2 web server (ADR 0023) — FastAPI console over the daemon's snapshot.

Serves the built Svelte console and one read-only endpoint, `GET /api/snapshot`,
whose body is the fleet snapshot the daemon last published (ADR 0026). This server
**never queries GitHub**: the daemon produces the snapshot once per tick and writes
it to a state file; any number of tabs and servers read that same file for free.
With the daemon down the console serves the last snapshot, honestly aged by its
`gh_fresh_at` stamp; before a daemon has ever run it serves an empty fleet.

    uv run agentflow-web        # build the console first: see agentflow/webui/README.md
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agentflow import live

DIST = Path(__file__).parent / "webui" / "dist"

# What the console sees before any daemon has ever published: an empty fleet with no
# freshness stamp — the same contract shape, never an error (ADR 0026).
NEVER_RAN = {
    "dispatch": {"enabled": False},
    "daemon": {"enabled": False, "last_cycle_at": None,
               "poll_seconds": None, "gh_fresh_at": None},
    "pools": [],
    "running": [],
    "repos": [],
}


def create_app(
    read: Callable[[], dict | None] = live.read_snapshot,
    *,
    dist: Path = DIST,
):
    """Build the FastAPI app: the daemon-published snapshot plus the built console."""
    from fastapi import FastAPI
    from fastapi.responses import PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="agentflow console", docs_url=None, redoc_url=None)

    @app.get("/api/snapshot")
    def api_snapshot():
        snap = read()
        return NEVER_RAN if snap is None else snap

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

    app = create_app()
    port = 8788
    print(f"agentflow console (v2) on http://localhost:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
