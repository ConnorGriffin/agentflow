"""Build the issue-373 screenshot config: the locked mockup's five states, and the built
console rendering fixtures whose attention rows come from the real daemon reducer.

    .venv/bin/python docs/screenshots/issue-373/shotgen.py <out-dir>
    node scripts/screenshots.mjs <out-dir>/shots.json

The console fixtures are published snapshots — v1 repository views plus the schema-v2
repository entries — run through `operator_projection.attention` exactly as the daemon's
publish step does, so a shot can never picture a queue the daemon would not produce.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))

from agentflow import operator_projection  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
CONSOLE = (ROOT / "agentflow" / "webui" / "dist" / "index.html").as_uri()
MOCKUP = (ROOT / "mockups" / "operator-surface-finalist.html").as_uri()
BRIEFING_TAB = "nav .tab:nth-child(5)"
THEME_TOGGLE = ".briefing button"
# At a phone width the console's tab strip is wider than the viewport, so reaching the fifth
# tab scrolls the document right. Bringing the briefing masthead back into view puts the page
# back at its left edge before the shot.
LEFT_EDGE = ".briefing .wordmark"

WIDE = {"width": 1280, "height": 1500}
NARROW = {"width": 375, "height": 2400}
# The overflow row sits below twenty-five ruled rows, so the full-queue evidence shots need
# room for the whole section rather than the first viewport the locked states are cut to.
TALL = {"width": 1280, "height": 2400}
TALL_NARROW = {"width": 375, "height": 5200}


def repo(name, *, profile="reviewed", held=(), parked=(), in_flight=(), ready=1,
         merges=5, ratchet=None):
    return {
        "repo": name, "profile": profile,
        "ready": [{"number": 900 + i, "title": "ready"} for i in range(ready)],
        "held": list(held), "parked": list(parked), "in_flight": list(in_flight),
        "recent_merges": [{"number": 800 + i, "title": "landed",
                           "merged_at": f"2026-07-{28 - i:02d}T12:00:00Z"}
                          for i in range(merges)],
        "ratchet": ratchet or {"samples": 8, "correction_rate": 0.05,
                               "ready_to_loosen": False},
    }


def pr(number, title, at="2026-07-30T09:00:00Z"):
    return {"number": number, "title": title, "builder": "claude", "handed_off_at": at}


HELD_REASON = {
    "needs-grilling": "a real fork the pipeline couldn't settle — needs your call",
    "needs-mockup": "a user-facing surface that needs a mockup before it's built",
}


def held(number, title, state="needs-grilling", since="2026-07-29T09:00:00Z"):
    return {"number": number, "title": title, "state": state, "since": since,
            "reason": HELD_REASON[state]}


def parked(number, title, reason="drop-to-reviewed", since="2026-07-28T09:00:00Z"):
    return {"number": number, "title": title, "reason": reason, "builder": "claude",
            "reviewer": "codex", "since": since}


def entry(name, status="fresh", error=None):
    return {"name_with_owner": name, "url": f"https://github.com/{name}",
            "profile": "reviewed", "github": {"status": status, "error": error},
            "maps": {"active_total": 0, "active": []}}


MAP = {
    "number": 179, "title": "Unified read-only operator console",
    "url": "https://github.com/ConnorGriffin/agentflow/issues/179",
    "updated_at": "2026-07-30T00:00:00Z", "complete": True,
    "progress": {"total": 5, "closed": 3},
    "frontier": [{"number": 185, "title": "Implement the successor",
                  "url": "https://github.com/ConnorGriffin/agentflow/issues/185"}],
    "tickets": [{"number": n, "title": t, "status": s,
                 "url": f"https://github.com/ConnorGriffin/agentflow/issues/{n}"}
                for n, t, s in ((183, "Locked visual specification", "done"),
                                (185, "Product implementation", "frontier"),
                                (186, "Schema-v2 projection", "open"))],
    "handoffs": [], "handoffs_overflow": False,
    "adrs": [{"label": "ADR 0036", "url": "https://github.com/ConnorGriffin/agentflow"}],
    "adrs_overflow": 0,
}

POOLS = [
    {"tool": "claude", "clear": True, "spent_pct": 38, "headroom_pct": 62, "running": 3,
     "reason": None},
    {"tool": "codex", "clear": False, "spent_pct": 12, "headroom_pct": 0, "running": 0,
     "reason": "weekly spend at 98% exceeds 80.0% released for unattended work"},
]


def snapshot(views, entries):
    """One published snapshot, its attention queue reduced by the daemon's own reducer."""
    with_maps = []
    for e in entries:
        e = dict(e)
        if e["github"]["status"] == "fresh":
            e["maps"] = {"active_total": 1, "active": [MAP]}
        with_maps.append(e)
    return {
        "dispatch": {"enabled": True},
        "daemon": {"enabled": True, "last_cycle_at": "2026-07-30T12:00:00+00:00",
                   "poll_seconds": 300, "gh_fresh_at": "2026-07-30T11:56:00+00:00"},
        "pools": POOLS, "running": [], "repos": views,
        "schema_version": 2, "generated_at": "2026-07-30T11:56:00+00:00",
        "repositories": with_maps,
        "fleet": operator_projection.fleet_recent_landed(views),
        "attention": operator_projection.attention(views, with_maps),
    }


AGENTFLOW = "ConnorGriffin/agentflow"
FLEET = [
    repo(AGENTFLOW,
         in_flight=[pr(183, "Lock the unified operator surface")],
         held=[held(186, "Publish the schema-v2 projection")],
         parked=[parked(191, "Verify the bounded refresh path")]),
    repo("ConnorGriffin/dotfiles", profile="guarded", merges=7),
    repo("ConnorGriffin/wayfinder", merges=0, ready=0),
    repo("ConnorGriffin/ledger", profile="autonomous", merges=4, ready=0,
         ratchet={"samples": 24, "correction_rate": 0.02, "ready_to_loosen": True}),
]
FRESH = [entry(v["repo"]) for v in FLEET]

TYPICAL = snapshot(FLEET, FRESH)
STALE = snapshot(FLEET, [
    entry(AGENTFLOW), entry("ConnorGriffin/dotfiles"),
    entry("ConnorGriffin/wayfinder", "stale", "point budget reached this heartbeat"),
    entry("ConnorGriffin/ledger")])
INCOMPLETE = snapshot(FLEET, [
    entry(AGENTFLOW), entry("ConnorGriffin/dotfiles"),
    entry("ConnorGriffin/wayfinder", "unavailable", "the map read failed"),
    entry("ConnorGriffin/ledger")])
EMPTY = snapshot([repo(AGENTFLOW, merges=0, ready=0)], [entry(AGENTFLOW)])

# Non-locked evidence fixture: all five conditions visible at once, and the overflow row.
# Twenty-eight rows against a bound of twenty-five — the bound cuts from the bottom by
# priority, so the extra rows are drawn from the last condition rather than from a middle one.
READY = {"samples": 30, "correction_rate": 0.04, "ready_to_loosen": True}
FULL = [
    repo("ConnorGriffin/brewgen", profile="guarded", ratchet=READY,
         in_flight=[pr(700 + i, f"guarded change {i}", f"2026-07-2{i}T09:00:00Z")
                    for i in range(3)]),
    repo(AGENTFLOW, ratchet=READY,
         in_flight=[pr(710 + i, f"reviewed change {i}", f"2026-07-2{i}T09:00:00Z")
                    for i in range(4)],
         held=[held(720, "A real fork nobody settled"),
               held(721, "A surface with no mockup", "needs-mockup")],
         parked=[parked(730, "An unanswered question", "open-question"),
                 parked(731, "A build that stopped", "drop-to-reviewed"),
                 parked(732, "A merge that failed", "failed-merge"),
                 parked(733, "A UI change with no proof", "ui-evidence")]),
    repo("ConnorGriffin/ledger", profile="autonomous", ratchet=READY,
         in_flight=[pr(740, "A same-tool review, maintainer merge required")]),
    repo("ConnorGriffin/dotfiles", profile="reviewed", ratchet=READY,
         held=[held(750 + i, f"held fork {i}", since=f"2026-07-1{i}T09:00:00Z")
               for i in range(3)],
         parked=[parked(760 + i, f"stopped build {i}", since=f"2026-07-1{i}T09:00:00Z")
                 for i in range(4)]),
    repo("ConnorGriffin/homelab", merges=0, ready=0),
    repo("ConnorGriffin/notes", merges=0, ready=0),
]
FULL_QUEUE = snapshot(FULL, [
    entry("ConnorGriffin/brewgen"), entry(AGENTFLOW),
    entry("ConnorGriffin/ledger", "stale", "point budget reached this heartbeat"),
    entry("ConnorGriffin/dotfiles", "unavailable", None),
    entry("ConnorGriffin/homelab", "stale", "the map read failed"),
    entry("ConnorGriffin/notes", "unavailable", None)])


def console(name, snap, *, theme="light", viewport=None):
    viewport = viewport or WIDE
    clicks = [BRIEFING_TAB] + ([THEME_TOGGLE] if theme == "dark" else [])
    if viewport["width"] < 700:
        clicks.append(LEFT_EDGE)
    return {"url": CONSOLE, "theme": theme, "out": f"build-{name}.png",
            "viewport": viewport, "settle": 300, "clicks": clicks,
            "fetchStub": {"api/snapshot": snap}}


def main(out_dir: str) -> None:
    out = pathlib.Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    mock_cfg = json.loads((ROOT / "mockups" / "operator-surface.screenshots.json").read_text())
    shots = []
    for shot in mock_cfg["shots"]:
        name = pathlib.Path(shot["out"]).stem.replace("operator-surface-finalist-", "")
        shots.append({**shot, "url": MOCKUP, "out": f"mock-{name}.png"})
    shots += [
        console("typical-light", TYPICAL),
        console("typical-dark", TYPICAL, theme="dark"),
        console("stale", STALE),
        console("incomplete", INCOMPLETE),
        console("empty", EMPTY),
        console("narrow", TYPICAL, viewport=NARROW),
        console("full-queue", FULL_QUEUE, viewport=TALL),
        console("full-queue-narrow", FULL_QUEUE, viewport=TALL_NARROW),
    ]
    for shot in shots:
        shot["out"] = str(out / shot["out"])
    (out / "shots.json").write_text(json.dumps({"shots": shots}, ensure_ascii=False, indent=1))
    print(f"attention totals: typical={TYPICAL['attention']['total']} "
          f"stale={STALE['attention']['total']} incomplete={INCOMPLETE['attention']['total']} "
          f"empty={EMPTY['attention']['total']} full={FULL_QUEUE['attention']['total']} "
          f"(shown {len(FULL_QUEUE['attention']['rows'])})")


if __name__ == "__main__":
    main(sys.argv[1])
