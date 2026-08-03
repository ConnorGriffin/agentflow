"""Dashboard v2 web server (ADR 0023) — FastAPI console over the daemon's snapshot.

Serves the built Svelte console and read-only endpoints whose bodies are the projections the
daemon last published (ADR 0026): `GET /api/snapshot` is the fleet snapshot and
`GET /api/workspace` is the Project-workspace read model (ADR 0033). This server **never queries
GitHub and never opens either SQLite store**: the daemon produces every projection once per tick
and writes it to a state file; any number of tabs and servers read that same file for free.

Writes are different (ADR 0033): `POST /api/command` is a thin transport to the daemon's local
command channel. The web server validates transport concerns and enqueues the command with its
idempotency key and expected aggregate revision — it never applies a domain transition itself. If
the daemon is down the command fails *unavailable*; there is no direct-write fallback.

    uv run agentflow-web        # build the console first: see agentflow/webui/README.md
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agentflow import live
from agentflow.coordinated_converse import read_projection
from agentflow.workspace import channel

DIST = Path(__file__).parent / "webui" / "dist"

# The command fields the transport requires per kind. The web layer validates only shape — the
# daemon owns every domain rule (ADR 0033).
_REQUIRED = {
    "open_ask": ("key", "repo", "conversation_id", "prompt"),
    "send_turn": ("key", "repo", "conversation_id", "prompt", "expected_revision"),
    "approve_proposal": ("key", "repo", "conversation_id", "content_hash"),
    "discard_proposal": ("key", "repo", "conversation_id"),
}

# What the console sees before any daemon has published a workspace projection: an empty
# workspace with no projects — the same contract shape, never an error (ADR 0033).
EMPTY_WORKSPACE = {"workspace": {"read_model_at": None, "revision": 0, "available": False},
                   "projects": []}

# What the console sees before any daemon has ever published: an empty fleet with no
# freshness stamp — the same contract shape, never an error (ADR 0026). `repositories`/
# `fleet` are the additive schema-v2 fields (ADR 0036): empty arrays, no `github.status`
# claiming freshness that was never verified. `attention` is the daemon-decided operator
# queue (#373): no rows and a zero total, so the briefing renders its honest-empty state
# rather than a count the browser made up.
NEVER_RAN = {
    "dispatch": {"enabled": False},
    "daemon": {"enabled": False, "last_cycle_at": None,
               "poll_seconds": None, "gh_fresh_at": None},
    "pools": [],
    "running": [],
    "repos": [],
    "schema_version": 2,
    "generated_at": None,
    "repositories": [],
    "fleet": {"recent_landed": []},
    "attention": {"rows": [], "total": 0},
}


def create_app(
    read: Callable[[], dict | None] = live.read_snapshot,
    *,
    dist: Path = DIST,
    read_workspace: Callable[[], dict | None] = read_projection,
    available: Callable[[], bool] = channel.daemon_available,
    enqueue: Callable[[dict], None] = channel.enqueue,
):
    """Build the FastAPI app: the daemon-published projections, the command transport, and the
    built console."""
    from fastapi import Body, FastAPI
    from fastapi.responses import JSONResponse, PlainTextResponse
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="agentflow console", docs_url=None, redoc_url=None)

    @app.get("/api/snapshot")
    def api_snapshot():
        snap = read()
        return NEVER_RAN if snap is None else snap

    @app.get("/api/workspace")
    def api_workspace():
        """The daemon-published workspace read model, file-only (ADR 0033). Before any daemon has
        published one, an empty workspace — the same contract shape, never an error."""
        projection = read_workspace()
        return EMPTY_WORKSPACE if projection is None else projection

    @app.post("/api/command")
    def api_command(command: dict = Body(...)):
        """Transport one workspace command to the daemon's command channel (ADR 0033). Validates
        only transport shape — the daemon applies every domain rule. Fails *unavailable* when the
        daemon is down; there is no direct-write fallback."""
        kind = command.get("kind") if isinstance(command, dict) else None
        required = _REQUIRED.get(kind)
        if required is None:
            return JSONResponse({"status": "rejected", "error": "unknown command kind"},
                                status_code=400)
        missing = [f for f in required if command.get(f) in (None, "")]
        if missing:
            return JSONResponse(
                {"status": "rejected", "error": f"missing fields: {', '.join(missing)}"},
                status_code=400)
        if not available():
            return JSONResponse(
                {"status": "unavailable",
                 "error": "the daemon is not running; no command was applied"},
                status_code=503)
        try:
            enqueue(command)
        except channel.InvalidCommandKey:
            return JSONResponse(
                {"status": "rejected", "error": "invalid command key"},
                status_code=400)
        except channel.CommandChannelUnavailable:
            return JSONResponse(
                {"status": "unavailable",
                 "error": "the command channel is unavailable; no command was applied"},
                status_code=503)
        return JSONResponse({"status": "queued", "key": command["key"]}, status_code=202)

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
