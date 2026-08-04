"""Build the issue-430 screenshot config: the locked drought mockup's four states, and the
built console rendering the same four, its rows produced by the real daemon reducer.

    .venv/bin/python docs/screenshots/issue-430/shotgen.py
    node scripts/screenshots.mjs docs/screenshots/issue-430/<ROUND>/shots.json

Each capture round writes into its own directory named for the branch head it was taken at,
so a later round can never repaint the images an earlier PR comment points at. Bump ``ROUND``
below when re-capturing.

The console fixtures are published snapshots — v1 repository views, the schema-v2 repository
entries, and the per-stage production fact — run through `operator_projection.attention`
exactly as the daemon's publish step does, so a shot can never picture a queue the daemon
would not produce. The mockup states come from its own `?fixture=` switcher, which is
mock-only and not part of the real app.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from agentflow import operator_projection  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
ROUND = "c164883"
OUT = pathlib.Path(__file__).resolve().parent / ROUND
CONSOLE = (ROOT / "agentflow" / "webui" / "dist" / "index.html").as_uri()
MOCKUP = (ROOT / "mockups" / "operator-briefing-drought.html").as_uri()
BRIEFING_TAB = "nav .tab:nth-child(5)"
THEME_TOGGLE = ".briefing button"

WIDE = {"width": 1280, "height": 1500}


def repo(name, *, profile="reviewed", held=(), parked=(), in_flight=(), ready=1, merges=5):
    return {
        "repo": name, "profile": profile,
        "ready": [{"number": 900 + i, "title": "ready"} for i in range(ready)],
        "held": list(held), "parked": list(parked), "in_flight": list(in_flight),
        "recent_merges": [{"number": 800 + i, "title": "landed",
                           "merged_at": f"2026-08-{3 - i:02d}T12:00:00Z"}
                          for i in range(merges)],
        "ratchet": {"samples": 8, "correction_rate": 0.05, "ready_to_loosen": False},
    }


def pr(number, title, at="2026-08-03T09:00:00Z"):
    return {"number": number, "title": title, "builder": "claude", "handed_off_at": at}


def held(number, title, since="2026-08-02T09:00:00Z"):
    return {"number": number, "title": title, "state": "needs-grilling", "since": since,
            "reason": "a real fork the pipeline couldn't settle — needs your call"}


def parked(number, title, since="2026-08-01T09:00:00Z"):
    return {"number": number, "title": title, "reason": "drop-to-reviewed",
            "builder": "claude", "reviewer": "codex", "since": since}


def entry(name):
    return {"name_with_owner": name, "url": f"https://github.com/{name}",
            "profile": "reviewed", "github": {"status": "fresh", "error": None},
            "maps": {"active_total": 1, "active": [MAP]}}


MAP = {
    "number": 418, "title": "Publish nothing a builder would waste a session on",
    "url": "https://github.com/ConnorGriffin/agentflow/issues/418",
    "updated_at": "2026-08-03T00:00:00Z", "complete": True,
    "progress": {"total": 4, "closed": 3},
    "frontier": [{"number": 430, "title": "Show a stage that produced nothing as broken",
                  "url": "https://github.com/ConnorGriffin/agentflow/issues/430"}],
    "tickets": [{"number": n, "title": t, "status": s,
                 "url": f"https://github.com/ConnorGriffin/agentflow/issues/{n}"}
                for n, t, s in ((418, "Let a remedied draft publish", "done"),
                                (430, "Show a silently broken stage", "frontier"),
                                (472, "Per-attempt telemetry", "done"))],
    "handoffs": [], "handoffs_overflow": False,
    "adrs": [{"label": "ADR 0048", "url": "https://github.com/ConnorGriffin/agentflow"}],
    "adrs_overflow": 0,
}

POOLS = [
    {"tool": "claude", "clear": True, "spent_pct": 38, "headroom_pct": 62, "running": 3,
     "reason": None},
    {"tool": "codex", "clear": True, "spent_pct": 12, "headroom_pct": 88, "running": 1,
     "reason": None},
]

AGENTFLOW = "ConnorGriffin/agentflow"
FLEET = [
    repo(AGENTFLOW,
         in_flight=[pr(503, "Lock the stage-drought Attention row")],
         held=[held(486, "Which conditions the Attention queue shows")],
         parked=[parked(499, "Retire a merge hand-off the engine could not")]),
    repo("ConnorGriffin/dotfiles", profile="guarded", merges=7),
    repo("ConnorGriffin/wayfinder", merges=0, ready=0),
]
ENTRIES = [entry(view["repo"]) for view in FLEET]


def stage(name, produces, check, *, attempts, produced, sessions):
    return {"stage": name, "produces": produces, "check": check, "window": 10,
            "attempts": attempts, "produced": produced, "sessions_spent": sessions}


def attack(**over):
    return stage("pre-publish attack", "published a hardened brief",
                 "held drafts and ready-for-agent holdbacks",
                 **{"attempts": 10, "produced": 0, "sessions": 37, **over})


REVIEW = stage("review", "recorded a review verdict", "open PRs waiting on a verdict",
               attempts=10, produced=0, sessions=41)
# The healthy stages that ride alongside every fixture: published on the snapshot, judged, and
# silent — which is the whole of what "absence is load-bearing" has to look like.
HEALTHY = [
    stage("build", "opened a pull request",
          "issues marked ready-for-agent with no pull request behind them",
          attempts=10, produced=9, sessions=23),
    stage("triage", "routed an issue", "open issues still carrying no state label",
          attempts=10, produced=10, sessions=14),
]

STATES = {
    "drought": [attack()] + HEALTHY,
    "under-window": [attack(attempts=4, sessions=12)] + HEALTHY,
    "all-healthy": HEALTHY,
    "multi-drought": [attack(), REVIEW] + HEALTHY,
}


def snapshot(health):
    """One published snapshot, its attention queue reduced by the daemon's own reducer."""
    return {
        "dispatch": {"enabled": True},
        "daemon": {"enabled": True, "last_cycle_at": "2026-08-04T12:00:00+00:00",
                   "poll_seconds": 300, "gh_fresh_at": "2026-08-04T11:56:00+00:00"},
        "pools": POOLS, "running": [], "repos": FLEET,
        "schema_version": 2, "generated_at": "2026-08-04T11:56:00+00:00",
        "repositories": ENTRIES, "stage_health": health,
        "fleet": operator_projection.fleet_recent_landed(FLEET),
        "attention": operator_projection.attention(FLEET, ENTRIES, stage_health=health),
    }


def console(name, health, *, theme="light"):
    clicks = [BRIEFING_TAB] + ([THEME_TOGGLE] if theme == "dark" else [])
    return {"url": CONSOLE, "theme": theme, "out": f"build-{name}-{theme}.png",
            "viewport": WIDE, "settle": 300, "clicks": clicks,
            "fetchStub": {"api/snapshot": snapshot(health)}}


def mock(name, *, theme="light"):
    return {"url": f"{MOCKUP}?fixture={name}", "theme": theme,
            "out": f"mock-{name}-{theme}.png", "viewport": WIDE, "settle": 300}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shots = []
    for name, health in STATES.items():
        for theme in ("light", "dark"):
            shots.append(mock(name, theme=theme))
            shots.append(console(name, health, theme=theme))
    for shot in shots:
        shot["out"] = str(OUT / shot["out"])
    (OUT / "shots.json").write_text(json.dumps({"shots": shots}, ensure_ascii=False, indent=1))
    for name, health in STATES.items():
        queue = snapshot(health)["attention"]
        droughts = [r["title"] for r in queue["rows"] if r["condition"] == "stage-drought"]
        print(f"{name}: total={queue['total']} drought rows={droughts}")


if __name__ == "__main__":
    main()
