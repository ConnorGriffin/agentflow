"""The two locked Decision Map states (`mockups/operator-surface-finalist.lock.md`, terms
18–23), carried the whole way a real one travels: typed GitHub rows → the daemon's own shaping
→ the published snapshot → `GET /api/snapshot`.

Both states are checked in as one fixture the console's own tests render from, so the backend
contract, the served snapshot, and the rendered copy are all pinned against the same bytes.
The fixture is *built* here rather than hand-written: a hand-written one can picture a map the
daemon would never produce, and then prove nothing at all.
"""

from __future__ import annotations

import json
import pathlib

from fastapi.testclient import TestClient

from agentflow import decision_maps, github, live, operator_projection, webapp

FIXTURE = (pathlib.Path(__file__).resolve().parents[1]
           / "agentflow/webui/src/fixtures/operator-briefing-states.json")

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


# --- the published snapshot each state travels in ------------------------------------------

# Both states are captured as screenshot evidence, and the surface words landing ages and
# projection age relative to now. Every clock-driven cell is pinned to a value that does not
# move — no landings at all, and one fixed publish stamp — so two captures of the same state
# differ only where the code differs.
PUBLISHED_AT = "2026-07-30T12:00:00+00:00"


def _repo_view(repo: str) -> dict:
    return {"repo": repo, "profile": "reviewed",
            "ready": [], "held": [], "parked": [], "in_flight": [], "recent_merges": [],
            "ratchet": {"samples": 12, "correction_rate": 0.04, "ready_to_loosen": False}}


def _entry(repo: str, component: dict) -> dict:
    return {"name_with_owner": repo, "url": f"https://github.com/{repo}", "profile": "reviewed",
            "github": {"status": "fresh", "attempted_at": PUBLISHED_AT,
                       "fresh_at": PUBLISHED_AT, "error": None},
            "maps": component}


def _snapshot(entries: list[dict]) -> dict:
    views = [_repo_view(e["name_with_owner"]) for e in entries]
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
        "repositories": entries,
        "fleet": operator_projection.fleet_recent_landed(views),
        "attention": operator_projection.attention(views, entries),
    }


def build_states() -> dict:
    """The checked-in fixture, rebuilt from the typed rows above. When the published map
    contract changes, regenerate the file rather than editing it:

        python tests/test_operator_briefing_states.py
    """
    return {
        "map-frontier-matrix": _snapshot([_entry(AGENTFLOW, _frontier_matrix_component())]),
        "map-overflow-evidence": _snapshot([
            _entry(AGENTFLOW, _overflow_component()),
            _entry(WAYFINDER, _unreadable_evidence_component())]),
    }


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

def test_both_states_survive_the_publish_and_read_path_intact(tmp_path, monkeypatch):
    """The whole public path: the daemon writes the snapshot, the server reads the file it
    wrote, and the map contract arrives at the browser byte for byte."""
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    client = TestClient(webapp.create_app(live.read_snapshot))
    for name, snapshot in _states().items():
        live.write_snapshot(snapshot)
        served = client.get("/api/snapshot").json()
        assert served == snapshot, f"{name} did not survive the publish and read path"


if __name__ == "__main__":  # regenerate the fixture the tests above pin
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(json.dumps(build_states(), ensure_ascii=False, indent=1) + "\n")
    print(f"wrote {FIXTURE}")
