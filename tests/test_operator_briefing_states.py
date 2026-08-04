"""Every locked state of the operator briefing (`mockups/operator-surface-finalist.lock.md`,
terms 6 and 18–23), carried the whole way a real one travels: typed GitHub rows → the daemon's
own shaping → the published snapshot → `GET /api/snapshot` at a fixed reading clock.

Each state is checked in as one fixture the console's own tests render from, so the backend
contract, the served snapshot, and the rendered copy are all pinned against the same bytes. The
fixture is *built* here rather than hand-written: a hand-written one can picture a briefing the
daemon would never produce, and then prove nothing at all. The screenshot capture matrix for
the built console is built from the very same bytes, so a shot can never picture data the
endpoint would not serve.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime

from fastapi.testclient import TestClient

from agentflow import decision_maps, github, live, operator_projection, webapp

FIXTURE = (pathlib.Path(__file__).resolve().parents[1]
           / "agentflow/webui/src/fixtures/operator-briefing-states.json")
SHOTS = (pathlib.Path(__file__).resolve().parents[1]
         / "mockups/operator-surface-build.screenshots.json")

AGENTFLOW = "ConnorGriffin/agentflow"
WAYFINDER = "ConnorGriffin/wayfinder"


# --- the typed rows GitHub hands the daemon ------------------------------------------------

def _child(number, title, *, state="OPEN", assigned=False, open_blockers=0, closed_blockers=0,
           blocker_total=None, candidates=(), repo=AGENTFLOW) -> github.MapChildRow:
    return github.MapChildRow(
        number=number, title=title, url=f"https://github.com/{repo}/issues/{number}",
        state=state, assigned=assigned, blocked_by_open=open_blockers,
        blocked_by_closed=closed_blockers,
        blocked_by_total=(open_blockers + closed_blockers if blocker_total is None
                          else blocker_total),
        handoff_candidates=tuple(candidates), handoff_edges_total=len(candidates))


def _handoff(number, title, *, map_number, repo=AGENTFLOW) -> github.HandoffCandidateRow:
    return github.HandoffCandidateRow(
        id=f"I_kwDO{repo.split('/')[-1]}{number}", number=number, title=title,
        url=f"https://github.com/{repo}/issues/{number}",
        body=f"## What to build\n\n{decision_maps.handoff_marker(map_number)}\n",
        labels=frozenset({"ready-for-agent"}), repo=repo)


def _map(number, title, *, children, body="", total=None, repo=AGENTFLOW) -> github.MapRow:
    return github.MapRow(
        number=number, title=title, url=f"https://github.com/{repo}/issues/{number}",
        updated_at="2026-08-04T09:00:00Z", body=body, children=tuple(children),
        children_total=len(children) if total is None else total)


def _decisions(*links) -> str:
    body = "\n".join(f"- [{label}]({url})" for label, url in links)
    return f"## Summary\nnot scraped from here.\n\n## Decisions so far\n{body}\n"


def _attempt(number, *, repo=AGENTFLOW, state="OPEN", merged_at=None) -> github.HandoffAttemptRow:
    return github.HandoffAttemptRow(number=number, url=f"https://github.com/{repo}/pull/{number}",
                                    state=state, merged_at=merged_at)


def _links(rows, *, error=None) -> github.HandoffLinksRead:
    return github.HandoffLinksRead(links=rows, cost=1, remaining=4980, error=error)


ADR_36 = ("ADR 0036 — bounded projection", "docs/adr/0036-bounded-repository-map-projection.md")
ADR_374 = ("ADR 374 — heartbeat budget", "docs/adr/adr-374-graphql-heartbeat-budget.md")


# --- state one: every frontier a map can have, in one render (terms 18–19) -----------------

def _frontier_matrix_component() -> dict:
    """Four maps: one naming its frontier, one with every decision settled, one with open
    decisions and nothing takeable, and one whose blocker data was cut off mid-read."""
    named = _map(
        343, "Make the Decision Map reads trustworthy",
        body=_decisions(ADR_36, ADR_374),
        children=[
            _child(372, "Classify decisions and verify handoffs", state="CLOSED",
                   candidates=[_handoff(500, "Project verified frontiers", map_number=343)]),
            _child(374, "Say what the projection could not verify", closed_blockers=2),
            _child(375, "Report what the refresh costs", assigned=True),
        ])
    settled = _map(
        179, "Unified read-only operator console",
        body=_decisions(("ADR 0035 — read-only console",
                         "docs/adr/0035-workflow-engine-read-only-operator-console.md")),
        children=[_child(n, t, state="CLOSED") for n, t in (
            (180, "Bound the projection"), (183, "Lock the operator surface"),
            (184, "Settle terminal slicing"), (185, "Build the successor console"))])
    blocked = _map(
        418, "Publish nothing a builder would waste a session on",
        body=_decisions(("ADR 0048 — publishable drafts", "docs/adr/0048-publishable-drafts.md")),
        children=[
            _child(430, "Show a silently broken stage", state="CLOSED"),
            _child(472, "Record per-attempt telemetry", open_blockers=1),
            _child(497, "Pay for the briefing by what a repository has", assigned=True),
        ])
    unverified = _map(
        184, "Terminal slicing for the operator briefing",
        body=_decisions(ADR_36),
        children=[
            _child(183, "Lock the interface", state="CLOSED"),
            # Twelve blockers, ten returned: the two we never saw could be open.
            _child(185, "Slice the terminal decision", closed_blockers=10, blocker_total=12),
            _child(186, "Publish schema-v2 projection"),
        ])
    landed = github.HandoffLinkRow(
        number=500, attempt_count=1,
        attempts=(_attempt(508, state="MERGED", merged_at="2026-08-04T08:12:00Z"),))
    return decision_maps.maps_component(
        github.MapsRead(maps=(named, settled, blocked, unverified), total_count=4, cost=21,
                        remaining=4900),
        repo=AGENTFLOW, handoff_links=_links({500: landed}), open_prs=[], merged_prs=[])


# --- state two: bounded overflow and landed evidence, in one render (terms 20–22) ----------

def _overflow_component() -> dict:
    """One map past every bound it has — more decisions than the read returns, more handoffs
    than the projection shows, more contextual records than it links — carrying a handoff that
    landed on its second attempt and one that landed before the console's pull-request window.
    """
    terminals = [
        _child(400 + i, f"Settled decision {i + 1}", state="CLOSED",
               candidates=[_handoff(400 + i, f"Build {400 + i}", map_number=343)])
        for i in range(21)
    ]
    terminals += [_child(300 + i, f"Settled decision {i + 22}", state="CLOSED")
                  for i in range(28)]
    terminals.append(_child(374, "Say what the projection could not verify"))
    body = _decisions(*[(f"ADR {i:04d} — a settled record", f"docs/adr/{i:04d}-settled.md")
                        for i in range(1, 15)])
    # 52 decisions exist; the read is bounded at 50, so two were never seen.
    overflowing = _map(343, "Make the Decision Map reads trustworthy",
                       body=body, children=terminals, total=52)
    rows = {
        # Closed twice: #358 merged, then a follow-up landed as #361. The later one speaks.
        420: github.HandoffLinkRow(
            number=420, attempt_count=2,
            attempts=(_attempt(358, state="MERGED", merged_at="2026-07-29T08:25:41Z"),
                      _attempt(361, state="MERGED", merged_at="2026-07-30T17:12:56Z"))),
        # Landed in January — long outside any pull-request listing the console reads.
        419: github.HandoffLinkRow(
            number=419, attempt_count=1,
            attempts=(_attempt(55, state="MERGED", merged_at="2026-01-14T11:03:00Z"),)),
        418: github.HandoffLinkRow(number=418, attempt_count=1, attempts=(_attempt(507),)),
    }
    return decision_maps.maps_component(
        github.MapsRead(maps=(overflowing,), total_count=1, cost=21, remaining=4880),
        repo=AGENTFLOW, handoff_links=_links(rows), open_prs=[], merged_prs=[])


def _unreadable_evidence_component() -> dict:
    """A second repository whose closing-reference read failed outright. Its handoffs are
    verified — the map says so — but nothing is known about whether they landed."""
    handed_off = _map(
        500, "Wayfinder map surface", repo=WAYFINDER,
        body=_decisions(ADR_36),
        children=[
            _child(501, "Settle the map schema", state="CLOSED", repo=WAYFINDER,
                   candidates=[_handoff(601, "Build the map schema", map_number=500,
                                        repo=WAYFINDER)]),
            _child(502, "Render the map surface", repo=WAYFINDER),
        ])
    return decision_maps.maps_component(
        github.MapsRead(maps=(handed_off,), total_count=1, cost=14, remaining=4870),
        repo=WAYFINDER,
        handoff_links=_links({}, error="Something went wrong while executing your query."),
        open_prs=[], merged_prs=[])


# --- the typical fleet, freshly read (term 6's `typical` / `narrow`) -----------------------

def _typical_component() -> dict:
    """One map naming its frontier, with a settled decision that handed off and landed."""
    named = _map(
        343, "Make the Decision Map reads trustworthy",
        body=_decisions(ADR_36, ADR_374),
        children=[
            _child(372, "Classify decisions and verify handoffs", state="CLOSED",
                   candidates=[_handoff(500, "Project verified frontiers", map_number=343)]),
            _child(376, "Fail closed for stale and unavailable data", closed_blockers=3),
            _child(377, "Retire the browser's own freshness rule", assigned=True),
        ])
    landed = github.HandoffLinkRow(
        number=500, attempt_count=1,
        attempts=(_attempt(508, state="MERGED", merged_at="2026-07-30T08:12:00Z"),))
    return decision_maps.maps_component(
        github.MapsRead(maps=(named,), total_count=1, cost=14, remaining=4900),
        repo=AGENTFLOW, handoff_links=_links({500: landed}), open_prs=[], merged_prs=[])


def _wayfinder_component() -> dict:
    """A second repository's map, read cleanly, so the fleet section has more than one row."""
    surface = _map(
        500, "Wayfinder map surface", repo=WAYFINDER, body=_decisions(ADR_36),
        children=[_child(501, "Settle the map schema", state="CLOSED", repo=WAYFINDER),
                  _child(502, "Render the map surface", repo=WAYFINDER)])
    return decision_maps.maps_component(
        github.MapsRead(maps=(surface,), total_count=1, cost=11, remaining=4890),
        repo=WAYFINDER, handoff_links=_links({}), open_prs=[], merged_prs=[])


# --- the published snapshot each state travels in ------------------------------------------

# Every state is captured as screenshot evidence, and the surface words landing ages and
# projection age relative to now. Every clock-driven cell is pinned to a value that does not
# move — one fixed publish stamp, and one fixed reading clock four minutes later — so two
# captures of the same state differ only where the code differs.
PUBLISHED_AT = "2026-07-30T12:00:00+00:00"
READ_AT = "2026-07-30T12:04:00+00:00"
# The daemon's heartbeat, stamped into every body it publishes: the reader's freshness window
# is two of these, so a component verified four minutes ago is still comfortably fresh.
HEARTBEAT_SECONDS = 300


def _repo_view(repo: str, **over) -> dict:
    return {"repo": repo, "profile": "reviewed",
            "ready": [], "held": [], "parked": [], "in_flight": [], "recent_merges": [],
            "ratchet": {"samples": 12, "correction_rate": 0.04, "ready_to_loosen": False},
            **over}


def _entry(repo: str, component: dict, *, status: str = "fresh",
           fresh_at: str | None = PUBLISHED_AT, error: str | None = None) -> dict:
    return {"name_with_owner": repo, "url": f"https://github.com/{repo}", "profile": "reviewed",
            "github": {"status": status, "attempted_at": PUBLISHED_AT,
                       "fresh_at": fresh_at, "error": error},
            "maps": component}


def _snapshot(entries: list[dict], views: list[dict] | None = None) -> dict:
    views = views if views is not None else [_repo_view(e["name_with_owner"]) for e in entries]
    return {
        "dispatch": {"enabled": True},
        "daemon": {"enabled": True, "last_cycle_at": PUBLISHED_AT,
                   "poll_seconds": 300, "gh_fresh_at": PUBLISHED_AT},
        "pools": [{"tool": "claude", "clear": True, "spent_pct": 38, "headroom_pct": 62,
                   "running": 3, "reason": None},
                  {"tool": "codex", "clear": True, "spent_pct": 12, "headroom_pct": 88,
                   "running": 1, "reason": None}],
        "running": [], "repos": views,
        "schema_version": operator_projection.SCHEMA_VERSION,
        "generated_at": PUBLISHED_AT,
        "heartbeat_seconds": HEARTBEAT_SECONDS,
        "repositories": entries,
        "fleet": operator_projection.fleet_recent_landed(views),
        "attention": operator_projection.attention(views, entries),
    }


def _busy_views() -> list[dict]:
    """The two fleet rows the typical state reads from: one repository with three things
    waiting on the operator, one with none."""
    return [
        _repo_view(AGENTFLOW,
                   ready=[{"number": 379, "title": "ready one"}],
                   held=[{"number": 381, "title": "Which surface owns the age?",
                          "state": "needs-grilling",
                          "reason": "a real fork the pipeline could not settle",
                          "since": "2026-07-29T09:00:00+00:00"}],
                   in_flight=[{"number": 374, "title": "Say what the projection verified",
                               "builder": "codex",
                               "handed_off_at": "2026-07-30T10:40:00+00:00"}],
                   parked=[{"number": 372, "title": "Classify decisions and verify handoffs",
                            "reason": "ui-evidence", "builder": "claude",
                            "since": "2026-07-30T07:15:00+00:00"}],
                   recent_merges=[
                       {"number": 508, "title": "Project verified frontiers",
                        "merged_at": "2026-07-30T08:12:00+00:00"},
                       {"number": 506, "title": "Show a broken stage",
                        "merged_at": "2026-07-29T12:00:00+00:00"}]),
        _repo_view(WAYFINDER, recent_merges=[
            {"number": 42, "title": "Settle the map schema",
             "merged_at": "2026-07-28T12:00:00+00:00"}]),
    ]


def _served(snapshot: dict | None) -> dict:
    """One published body as `GET /api/snapshot` answers it at the fixed reading clock — the
    read-time aging and the stamped freshness copy included. This is what the browser sees, so
    it is what the fixtures and the screenshot stubs are made of."""
    reading_clock = datetime.fromisoformat(READ_AT)
    client = TestClient(webapp.create_app(lambda: snapshot, now=lambda: reading_clock))
    return client.get("/api/snapshot").json()


def build_published() -> dict:
    """What the daemon writes to the state file for each state, before anyone reads it."""
    typical = _snapshot([_entry(AGENTFLOW, _typical_component()),
                         _entry(WAYFINDER, _wayfinder_component())], views=_busy_views())
    # One repository's last read failed; its previous map data is preserved with the stamp the
    # publisher gave it, so the whole projection reads stale rather than partly fresh.
    stale = _snapshot(
        [_entry(AGENTFLOW, _typical_component(), status="stale",
                fresh_at="2026-07-30T11:31:00+00:00",
                error="point budget reached this heartbeat"),
         _entry(WAYFINDER, _wayfinder_component())], views=_busy_views())
    # One repository has never completed a read at all: verified elsewhere, unavailable here,
    # so no frontier under it may be presented.
    incomplete = _snapshot(
        [_entry(AGENTFLOW, _typical_component()),
         _entry(WAYFINDER, {"active": [], "active_total": 0}, status="unavailable",
                fresh_at=None, error="Something went wrong while executing your query.")],
        views=_busy_views())
    # Zero repositories cannot be published: the configuration requires at least one. A body
    # that says otherwise has been perturbed, so the surface says so instead of drawing an
    # empty-but-fresh fleet (term 6, re-settled by #376).
    empty = _snapshot([], views=[])
    return {
        "typical": typical,
        "stale": stale,
        "incomplete": incomplete,
        "empty": empty,
        "map-frontier-matrix": _snapshot([_entry(AGENTFLOW, _frontier_matrix_component())]),
        "map-overflow-evidence": _snapshot([
            _entry(AGENTFLOW, _overflow_component()),
            _entry(WAYFINDER, _unreadable_evidence_component())]),
    }


def build_states() -> dict:
    """The checked-in fixture: each published state as the endpoint answers it at the fixed
    reading clock. When the published contract changes, regenerate the file rather than
    editing it:

        python tests/test_operator_briefing_states.py
    """
    return {name: _served(body) for name, body in build_published().items()}


# --- the capture matrix for the built console ------------------------------------------------

# The console's own tab strip, then the briefing's own theme control. `#theme` belongs to the
# mockup page and does not exist in the build.
_BRIEFING_TAB = '[role="tablist"] button:has-text("Briefing")'
_THEME_TOGGLE = ".briefing button.theme"

# The two painted backgrounds of the briefing's scoped token set, so a shot proves the theme
# actually took rather than merely that a button was clicked.
_PAPER = {"light": "rgb(247, 247, 245)", "dark": "rgb(24, 26, 25)"}

_CONSOLE = "http://127.0.0.1:8788/"


def _shot(state: str, body: dict, *, theme: str, out: str, width: int, height: int) -> dict:
    clicks = [_BRIEFING_TAB] + ([_THEME_TOGGLE] if theme == "dark" else [])
    return {
        "url": _CONSOLE,
        "theme": theme,
        "clicks": clicks,
        "clock": READ_AT,
        "out": out,
        "viewport": {"width": width, "height": height},
        "settle": 250,
        "fetchStub": {"/api/snapshot": body},
        "assert": {
            "theme": {"selector": ".briefing", "value": theme,
                      "backgroundColor": _PAPER[theme]},
            "noConsoleErrors": True,
            # The console asks Google for its two web fonts. A capture host with no route to
            # them is not the page misbehaving, and the surface renders from its own fallback
            # stack either way — so that one failure is named rather than left to mask a real
            # scripting error.
            "ignoreConsole": ["fonts.googleapis.com", "fonts.gstatic.com"],
            # Measured on the briefing itself: the surrounding v1 console chrome is a
            # desktop control plane with its own width floor, which this slice does not touch.
            "noHorizontalOverflow": ".briefing",
        },
        "state": state,
    }


def build_shots() -> dict:
    """The committed capture matrix for the built console. Every stub body is the endpoint's
    own answer for that state at the fixed reading clock, so a screenshot cannot show data the
    server would not serve — and the page's clock is pinned to the same instant, so the ages it
    words do not move between runs.

    Capture it with `npm run build` in `agentflow/webui`, `uv run agentflow-web`, then
    `node scripts/screenshots.mjs mockups/operator-surface-build.screenshots.json`. Where the
    host cannot bind a local port (an agent sandbox, typically), copy the matrix and point
    every `url` at `agentflow/webui/dist/index.html` as a `file://` URL instead: the build uses
    relative asset paths, the snapshot fetch is stubbed either way, and the rendered bytes are
    the same ones the server would hand over."""
    served = build_states()
    return {"shots": [
        _shot("typical", served["typical"], theme="light",
              out="mockups/screenshots/issue-376/typical.png", width=1280, height=1360),
        _shot("typical", served["typical"], theme="light",
              out="mockups/operator-surface-build-typical.png", width=1280, height=1360),
        _shot("stale", served["stale"], theme="dark",
              out="mockups/screenshots/issue-376/stale.png", width=1280, height=1360),
        _shot("incomplete", served["incomplete"], theme="light",
              out="mockups/screenshots/issue-376/incomplete.png", width=1280, height=1360),
        _shot("empty", served["empty"], theme="dark",
              out="mockups/screenshots/issue-376/empty.png", width=1280, height=900),
        _shot("narrow", served["typical"], theme="light",
              out="mockups/screenshots/issue-376/narrow.png", width=375, height=2100),
    ]}


def _states() -> dict:
    return json.loads(FIXTURE.read_text())


def _maps_of(snapshot: dict, index: int = 0) -> list[dict]:
    return snapshot["repositories"][index]["maps"]["active"]


# --- the fixture is what the daemon shapes --------------------------------------------------

def test_the_checked_in_states_are_exactly_what_the_daemon_shapes():
    assert _states() == build_states(), (
        "the console's fixture has drifted from the projection it claims to picture — "
        "regenerate it from build_states()")


# --- frontier states (terms 18–19) ---------------------------------------------------------

def test_the_four_frontier_states_appear_together_in_one_projection():
    maps = {m["number"]: m for m in _maps_of(_states()["map-frontier-matrix"])}
    assert [maps[n]["frontier_state"] for n in (343, 179, 418, 184)] == [
        "named", "none_open", "blocked", "unverified"]
    assert [f["number"] for f in maps[343]["frontier"]] == [374]


def test_the_unverified_map_names_what_it_could_not_read():
    unverified = {m["number"]: m for m in _maps_of(_states()["map-frontier-matrix"])}[184]
    assert unverified["frontier"] == [], "nothing for the console to present as a frontier"
    assert unverified["frontier_incomplete"] is True
    assert any("incomplete" in s["label"] for s in unverified["support"])
    # The clean decision is still in the outline — it is the map's claim that is withheld.
    assert {"number": 186, "status": "frontier"} in [
        {"number": t["number"], "status": t["status"]} for t in unverified["tickets"]]


def test_a_settled_map_claims_no_frontier_and_no_incompleteness():
    settled = {m["number"]: m for m in _maps_of(_states()["map-frontier-matrix"])}[179]
    assert settled["frontier_state"] == "none_open"
    assert settled["progress"] == {"total": 4, "closed": 4}
    assert settled["frontier_incomplete"] is False


# --- overflow and landed evidence (terms 20–22) --------------------------------------------

def test_every_bound_the_overflow_state_passes_is_counted_and_linked():
    overflowing = _maps_of(_states()["map-overflow-evidence"])[0]
    assert overflowing["totals"] == {"children": {"shown": 50, "total": 52},
                                     "handoffs": {"shown": 20, "total": 21},
                                     "adrs": {"shown": 12, "total": 14}}
    counted = [s["label"] for s in overflowing["support"] if s["label"][0].isdigit()
               or s["label"].startswith("incomplete — 2")]
    assert "incomplete — 2 more decisions on GitHub" in counted
    assert "1 more handoff on GitHub" in counted
    assert "2 more decision records on GitHub" in counted


def test_the_second_attempt_that_landed_is_the_evidence_and_says_so():
    handoffs = {h["number"]: h for h in _maps_of(_states()["map-overflow-evidence"])[0]["handoffs"]}
    assert handoffs[420]["pipeline"]["state"] == "merged"
    assert handoffs[420]["pipeline"]["pr_number"] == 361, "the later landing, not the first"
    assert handoffs[420]["attempt_count"] == 2


def test_a_landing_older_than_the_pull_request_window_is_still_landed():
    handoffs = {h["number"]: h for h in _maps_of(_states()["map-overflow-evidence"])[0]["handoffs"]}
    assert handoffs[419]["pipeline"]["state"] == "merged"
    assert handoffs[419]["pipeline"]["pr_number"] == 55


def test_a_handoff_with_no_closing_pull_request_reads_as_building():
    handoffs = {h["number"]: h for h in _maps_of(_states()["map-overflow-evidence"])[0]["handoffs"]}
    assert handoffs[417]["pipeline"]["state"] == "building"


def test_a_failed_evidence_read_is_published_as_unavailable_not_as_building():
    unreadable = _maps_of(_states()["map-overflow-evidence"], index=1)[0]
    handoff = unreadable["handoffs"][0]
    assert handoff["pipeline"]["state"] == "unavailable"
    assert handoff["url"] == "https://github.com/ConnorGriffin/wayfinder/issues/601"


# --- daemon publish → live snapshot → the endpoint -------------------------------------------

def test_every_state_survives_the_publish_and_read_path_intact(tmp_path, monkeypatch):
    """The whole public path: the daemon writes the snapshot, the server reads the file it
    wrote at the fixed reading clock, and what arrives at the browser is exactly the fixture
    every rendering test renders from."""
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    reading_clock = datetime.fromisoformat(READ_AT)
    client = TestClient(webapp.create_app(live.read_snapshot, now=lambda: reading_clock))
    for name, published in build_published().items():
        live.write_snapshot(published)
        served = client.get("/api/snapshot").json()
        assert served == _states()[name], f"{name} did not survive the publish and read path"


# --- the states the built console is photographed in (term 6) --------------------------------

def test_every_committed_shot_stubs_exactly_what_the_endpoint_would_serve():
    """The screenshot evidence is only evidence if the browser was handed the server's own
    answer. Every committed shot's stub, and the whole capture matrix around it, is rebuilt
    here from the endpoint and compared byte for byte."""
    assert SHOTS.read_text() == _shots_text(), (
        "the committed capture matrix has drifted from what GET /api/snapshot serves — "
        "regenerate it: python tests/test_operator_briefing_states.py")


def test_the_five_locked_states_are_all_captured_in_both_themes():
    shots = json.loads(SHOTS.read_text())["shots"]
    assert {s["state"] for s in shots} == {"typical", "stale", "incomplete", "empty", "narrow"}
    assert {s["theme"] for s in shots} == {"light", "dark"}
    assert [s["viewport"]["width"] for s in shots if s["state"] == "narrow"] == [375]
    for shot in shots:
        assert shot["clicks"][0].endswith('button:has-text("Briefing")')
        if shot["theme"] == "dark":
            assert shot["clicks"][1] == ".briefing button.theme"


def _fixture_text() -> str:
    return json.dumps(build_states(), ensure_ascii=False, indent=1) + "\n"


def _shots_text() -> str:
    return json.dumps(build_shots(), ensure_ascii=False, indent=1) + "\n"


if __name__ == "__main__":  # regenerate the fixture and capture matrix the tests above pin
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(_fixture_text())
    SHOTS.write_text(_shots_text())
    print(f"wrote {FIXTURE}\nwrote {SHOTS}")
