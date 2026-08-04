"""Decision Map projection shaping (ADR 0036) — pure classification, handoff verification, and
ADR-link scraping, exercised through the module's public functions on typed GitHub rows.

The last two sections work one layer down, against raw GraphQL responses shaped exactly like the
live ones (ADR 374). They exist because a typed-row fixture cannot see a wire-shape regression:
the handoff label filter was proven at the row level for a month while the live read handed it an
empty label set on every repository in the fleet."""

from __future__ import annotations

import json
import subprocess

from agentflow import cli, decision_maps, github


def _child(number=1, *, state="OPEN", assigned=False, blocked_by_open=0, blocked_by_closed=0,
          blocked_by_total=None, handoff_candidates=(),
          handoff_edges_total=None) -> github.MapChildRow:
    total = blocked_by_total if blocked_by_total is not None else blocked_by_open + blocked_by_closed
    return github.MapChildRow(
        number=number, title=f"child {number}", url=f"https://github.com/o/r/issues/{number}",
        state=state, assigned=assigned, blocked_by_open=blocked_by_open,
        blocked_by_closed=blocked_by_closed, blocked_by_total=total,
        handoff_candidates=tuple(handoff_candidates),
        handoff_edges_total=(len(handoff_candidates) if handoff_edges_total is None
                             else handoff_edges_total))


def _candidate(number=100, *, repo="o/r", labels=(), body="",
               node_id=None) -> github.HandoffCandidateRow:
    return github.HandoffCandidateRow(
        id=node_id or f"I_{repo}#{number}",
        number=number, title=f"handoff {number}", url=f"https://github.com/{repo}/issues/{number}",
        body=body, labels=frozenset(labels), repo=repo)


def _map(number=179, *, body="", children=(), children_total=None) -> github.MapRow:
    return github.MapRow(number=number, title="Map: x", url=f"https://github.com/o/r/issues/{number}",
                         updated_at="2026-07-30T00:00:00Z", body=body, children=tuple(children),
                         children_total=(len(children) if children_total is None
                                         else children_total))


def _links(*rows, error=None) -> github.HandoffLinksRead:
    return github.HandoffLinksRead(links={r.number: r for r in rows}, cost=1, remaining=4990,
                                   error=error)


def _attempt(number, *, state="OPEN", merged_at=None) -> github.HandoffAttemptRow:
    return github.HandoffAttemptRow(number=number, url=f"https://github.com/o/r/pull/{number}",
                                    state=state, merged_at=merged_at)


def _merged(number, at) -> github.HandoffAttemptRow:
    return _attempt(number, state="MERGED", merged_at=at)


# --- classify_child ---------------------------------------------------------------------

def test_closed_child_is_done():
    assert decision_maps.classify_child(_child(state="CLOSED")) == "done"


def test_open_unassigned_unblocked_child_is_frontier():
    assert decision_maps.classify_child(_child()) == "frontier"


def test_open_assigned_unblocked_child_is_claimed():
    assert decision_maps.classify_child(_child(assigned=True)) == "claimed"


def test_open_child_with_an_open_blocker_is_blocked_even_if_assigned():
    assert decision_maps.classify_child(_child(assigned=True, blocked_by_open=1)) == "blocked"


def test_open_child_with_only_closed_blockers_is_frontier():
    assert decision_maps.classify_child(_child(blocked_by_closed=2)) == "frontier"


def test_truncated_blocker_edges_are_unknown_never_frontier():
    child = _child(blocked_by_closed=1, blocked_by_total=5)  # 4 more blockers never fetched
    assert decision_maps.classify_child(child) == "unknown"


# --- verified_handoffs -------------------------------------------------------------------

def test_handoff_verified_from_a_closed_terminal_child():
    marker = decision_maps.handoff_marker(179)
    candidate = _candidate(body=f"Some text.\n\n{marker}\n")
    terminal = _child(1, state="CLOSED", handoff_candidates=[candidate])
    handoffs, overflow = decision_maps.verified_handoffs(_map(179, children=[terminal]), repo="o/r")
    assert [h.number for h in handoffs] == [candidate.number]
    assert overflow == 0


def test_candidate_from_an_open_child_is_not_a_handoff():
    marker = decision_maps.handoff_marker(179)
    candidate = _candidate(body=marker)
    frontier_child = _child(1, state="OPEN", handoff_candidates=[candidate])
    handoffs, _ = decision_maps.verified_handoffs(_map(179, children=[frontier_child]), repo="o/r")
    assert handoffs == []


def test_candidate_missing_the_marker_is_rejected():
    candidate = _candidate(body="no marker here")
    terminal = _child(1, state="CLOSED", handoff_candidates=[candidate])
    handoffs, _ = decision_maps.verified_handoffs(_map(179, children=[terminal]), repo="o/r")
    assert handoffs == []


def test_candidate_with_a_wayfinder_label_is_rejected():
    marker = decision_maps.handoff_marker(179)
    candidate = _candidate(body=marker, labels=["wayfinder:research"])
    terminal = _child(1, state="CLOSED", handoff_candidates=[candidate])
    handoffs, _ = decision_maps.verified_handoffs(_map(179, children=[terminal]), repo="o/r")
    assert handoffs == []


def test_candidate_from_a_different_repository_is_rejected():
    marker = decision_maps.handoff_marker(179)
    candidate = _candidate(body=marker, repo="o/other")
    terminal = _child(1, state="CLOSED", handoff_candidates=[candidate])
    handoffs, _ = decision_maps.verified_handoffs(_map(179, children=[terminal]), repo="o/r")
    assert handoffs == []


def test_handoffs_are_deduplicated_and_bounded_with_overflow():
    marker = decision_maps.handoff_marker(179)
    candidates = [_candidate(number=100 + i, body=marker) for i in range(25)]
    dup = _candidate(number=100, body=marker)  # the same issue, via a second closed child
    terminal_a = _child(1, state="CLOSED", handoff_candidates=candidates)
    terminal_b = _child(2, state="CLOSED", handoff_candidates=[dup])
    handoffs, overflow = decision_maps.verified_handoffs(
        _map(179, children=[terminal_a, terminal_b]), repo="o/r", limit=20)
    assert len(handoffs) == 20
    assert overflow == 5, "25 verified, 20 shown — the count that did not fit"
    assert len({h.number for h in handoffs}) == 20  # deduplicated, not 26 raw candidates


def test_a_foreign_issue_sharing_a_number_never_displaces_the_real_handoff():
    # Issue numbers are unique only inside one repository. A dependency edge can point at
    # another repository's #505, and if that arrives first and deduplication keys on the
    # number, the real handoff is silently dropped as a duplicate of something rejected.
    marker = decision_maps.handoff_marker(179)
    foreign = _candidate(number=505, repo="o/elsewhere", body=marker)
    real = _candidate(number=505, repo="o/r", body=marker)
    terminal = _child(1, state="CLOSED", handoff_candidates=[foreign, real])
    handoffs, _ = decision_maps.verified_handoffs(_map(179, children=[terminal]), repo="o/r")
    assert [h.url for h in handoffs] == ["https://github.com/o/r/issues/505"]


def _edge_bounded_view(*children):
    return decision_maps.map_view(_map(179, children=children), repo="o/r",
                                  handoff_links=_links(), open_prs=[], merged_prs=[])


def test_a_full_but_untruncated_edge_page_is_complete():
    # Exactly the bound returned, and GitHub says that is all there is (ADR 0036's 10 edges).
    terminal = _child(1, state="CLOSED", handoff_edges_total=10,
                      handoff_candidates=[_candidate(number=200 + i) for i in range(10)])
    assert _edge_bounded_view(terminal)["handoffs_incomplete"] is False


def test_an_eleventh_edge_behind_the_bound_marks_handoffs_incomplete():
    terminal = _child(1, state="CLOSED", handoff_edges_total=11,
                      handoff_candidates=[_candidate(number=200 + i) for i in range(10)])
    view = _edge_bounded_view(terminal)
    assert view["handoffs_incomplete"] is True
    assert view["frontier_incomplete"] is False, (
        "a handoff that might be hiding says nothing about which decision is takeable")


def test_a_truncated_edge_page_on_an_open_child_is_not_a_handoff_gap():
    # Handoffs are only read off settled decisions, so an open child's truncated links hide
    # nothing — and they say nothing about the frontier either, which reads the other edge.
    open_child = _child(1, handoff_edges_total=99, handoff_candidates=[_candidate()])
    assert _edge_bounded_view(open_child)["handoffs_incomplete"] is False


# --- frontier_state ----------------------------------------------------------------------

def test_an_unclaimed_unblocked_child_names_the_frontier():
    assert decision_maps.frontier_state(["done", "frontier", "claimed"],
                                        children_truncated=False) == "named"


def test_every_child_settled_means_no_open_decision_remains():
    assert decision_maps.frontier_state(["done", "done"], children_truncated=False) == "none_open"


def test_open_children_with_nothing_takeable_are_blocked():
    assert decision_maps.frontier_state(["done", "blocked", "claimed"],
                                        children_truncated=False) == "blocked"


def test_one_childs_unreadable_blockers_leaves_the_whole_map_unverified():
    # Fails closed even beside a perfectly clean frontier child: the unread blockers could
    # belong to a decision that outranks it.
    assert decision_maps.frontier_state(["frontier", "unknown"],
                                        children_truncated=False) == "unverified"


def test_a_truncated_child_list_can_name_a_frontier_but_never_claim_none_is_open():
    assert decision_maps.frontier_state(["frontier", "done"], children_truncated=True) == "named"
    assert decision_maps.frontier_state(["done", "done"], children_truncated=True) == "unverified"
    assert decision_maps.frontier_state(["blocked"], children_truncated=True) == "unverified"


# --- landed evidence ---------------------------------------------------------------------

def _evidence(*attempts, count=None, error=None, open_prs=(), merged_prs=()):
    """One handoff's published landed evidence, taken the whole way through `map_view` so the
    contract under test is the one the snapshot actually carries."""
    marker = decision_maps.handoff_marker(179)
    candidate = _candidate(number=343, body=marker)
    terminal = _child(1, state="CLOSED", handoff_candidates=[candidate])
    link = github.HandoffLinkRow(number=343, attempts=tuple(attempts),
                                 attempt_count=count if count is not None else len(attempts))
    view = decision_maps.map_view(
        _map(179, children=[terminal]), repo="o/r",
        handoff_links=_links(link, error=error) if not error else _links(error=error),
        open_prs=list(open_prs), merged_prs=list(merged_prs))
    return view["handoffs"][0]


def test_the_newest_merged_closing_pull_request_is_the_landed_evidence():
    # #343 was closed twice: an earlier merge and a later one. The later one is what landed.
    handoff = _evidence(_merged(358, "2026-07-29T08:25:41Z"), _merged(361, "2026-07-30T17:12:56Z"))
    assert handoff["pipeline"]["state"] == "merged"
    assert handoff["pipeline"]["pr_number"] == 361
    assert handoff["attempt_count"] == 2


def test_a_merged_attempt_beats_a_newer_open_one():
    handoff = _evidence(_merged(358, "2026-07-29T08:25:41Z"), _attempt(400))
    assert handoff["pipeline"]["pr_number"] == 358


def test_with_nothing_merged_the_newest_open_attempt_speaks():
    handoff = _evidence(_attempt(358), _attempt(400))
    assert (handoff["pipeline"]["state"], handoff["pipeline"]["pr_number"]) == ("pr_open", 400)


def test_with_nothing_open_the_newest_attempt_still_says_something_was_tried():
    handoff = _evidence(_attempt(358, state="CLOSED"), _attempt(400, state="CLOSED"))
    assert (handoff["pipeline"]["state"], handoff["pipeline"]["pr_number"]) == ("pr_closed", 400)


def test_a_landing_older_than_the_pull_request_window_still_reads_as_landed():
    # The console's PR listings are bounded; the closing reference is not. Evidence that
    # depended on the listing would silently downgrade an old landing to "still building".
    handoff = _evidence(_merged(55, "2026-01-02T00:00:00Z"))
    assert handoff["pipeline"]["state"] == "merged"
    assert handoff["pipeline"]["pr_url"] == "https://github.com/o/r/pull/55"
    assert handoff["pipeline"]["merge_commit"] is None, "no listing row, so no commit to claim"


def test_a_handoff_with_no_closing_reference_is_still_building():
    assert _evidence()["pipeline"]["state"] == "building"


def test_a_failed_closing_reference_read_says_the_evidence_is_unavailable():
    handoff = _evidence(_merged(361, "2026-07-30T17:12:56Z"),
                        error="Something went wrong while executing your query.")
    assert handoff["pipeline"]["state"] == "unavailable", (
        "a read that failed knows nothing — least of all that the build is still running")
    assert handoff["url"] == "https://github.com/o/r/issues/343", "the handoff stays reachable"


# --- adr_links ------------------------------------------------------------------------

def test_adr_links_scraped_only_from_decisions_so_far_section():
    body = (
        "## Summary\nSee [ADR 12](docs/adr/0012-elsewhere.md) which must be ignored.\n\n"
        "## Decisions so far\n- [ADR 36](docs/adr/0036-bounded-repository-map-projection.md)\n\n"
        "## Handoffs\nnothing here")
    links, overflow = decision_maps.adr_links(body)
    assert links == [{"label": "ADR 36", "url": "docs/adr/0036-bounded-repository-map-projection.md"}]
    assert overflow == 0


def test_adr_links_deduplicated_and_bounded():
    lines = "\n".join(f"- [ADR {i}](docs/adr/{i:04d}-x.md)" for i in range(15))
    body = f"## Decisions so far\n{lines}\n- [ADR 0](docs/adr/0000-x.md)\n"  # a duplicate url
    links, overflow = decision_maps.adr_links(body, limit=12)
    assert len(links) == 12
    assert overflow == 3  # 15 unique urls - 12 kept


def test_no_decisions_section_yields_no_links():
    links, overflow = decision_maps.adr_links("## Summary\n[ADR 1](docs/adr/0001-x.md)")
    assert links == [] and overflow == 0


# --- map_view / maps_component ---------------------------------------------------------

def test_map_view_reports_frontier_tickets_and_progress():
    children = [_child(1, state="CLOSED"), _child(2), _child(3, assigned=True)]
    view = decision_maps.map_view(
        _map(179, children=children), repo="o/r", handoff_links=_links(), open_prs=[],
        merged_prs=[])
    assert view["progress"] == {"total": 3, "closed": 1}
    assert [t["status"] for t in view["tickets"]] == ["done", "frontier", "claimed"]
    assert view["frontier_state"] == "named"
    assert [f["number"] for f in view["frontier"]] == [2]
    assert view["frontier_incomplete"] is False
    assert view["totals"]["children"] == {"shown": 3, "total": 3}


def test_an_unverified_map_publishes_no_frontier_for_anyone_to_claim():
    # One child's blockers were truncated; another is clean. The clean one is still listed in
    # the outline, but the map hands the console nothing to present as the frontier.
    children = [_child(1, blocked_by_closed=1, blocked_by_total=6), _child(2)]
    view = decision_maps.map_view(
        _map(179, children=children), repo="o/r", handoff_links=_links(), open_prs=[],
        merged_prs=[])
    assert view["frontier_state"] == "unverified"
    assert view["frontier"] == []
    assert view["frontier_incomplete"] is True
    assert [t["status"] for t in view["tickets"]] == ["unknown", "frontier"]
    assert {"label": "incomplete — blocker data on 1 decision",
            "url": "https://github.com/o/r/issues/179"} in view["support"]


def test_every_truncation_becomes_a_counted_link_onto_github():
    marker = decision_maps.handoff_marker(179)
    terminals = [
        _child(i, state="CLOSED", handoff_edges_total=11,
               handoff_candidates=[_candidate(number=400 + i, body=marker)])
        for i in range(21)
    ]
    body = "## Decisions so far\n" + "\n".join(
        f"- [ADR {i}](docs/adr/{i:04d}-x.md)" for i in range(14))
    view = decision_maps.map_view(
        _map(179, body=body, children=terminals, children_total=23), repo="o/r",
        handoff_links=_links(), open_prs=[], merged_prs=[])
    assert view["totals"] == {"children": {"shown": 21, "total": 23},
                              "handoffs": {"shown": 20, "total": 21},
                              "adrs": {"shown": 12, "total": 14}}
    labels = [s["label"] for s in view["support"]]
    assert "incomplete — 2 more decisions on GitHub" in labels
    assert "incomplete — handoff links on 21 settled decisions" in labels
    assert "1 more handoff on GitHub" in labels
    assert "2 more decision records on GitHub" in labels
    assert all(s["url"].startswith("https://github.com/") for s in view["support"]), (
        "an overflow the operator cannot open is not an explicit overflow")
    assert view["handoffs_incomplete"] is True


def test_a_relative_adr_link_is_resolved_against_the_repository():
    body = "## Decisions so far\n- [ADR 36](docs/adr/0036-bounded.md)\n"
    view = decision_maps.map_view(_map(179, body=body), repo="o/r", handoff_links=_links(),
                                  open_prs=[], merged_prs=[])
    assert view["support"][0] == {
        "label": "ADR 36", "url": "https://github.com/o/r/blob/HEAD/docs/adr/0036-bounded.md"}


def test_an_absolute_adr_link_is_left_exactly_as_the_map_wrote_it():
    url = "https://github.com/o/r/blob/main/docs/adr/0036-bounded.md#decision"
    view = decision_maps.map_view(_map(179, body=f"## Decisions so far\n- [ADR 36]({url})\n"),
                                  repo="o/r", handoff_links=_links(), open_prs=[], merged_prs=[])
    assert view["support"][0]["url"] == url


def test_map_view_joins_handoff_pipeline_evidence():
    marker = decision_maps.handoff_marker(179)
    candidate = _candidate(number=200, body=marker)
    terminal = _child(1, state="CLOSED", handoff_candidates=[candidate])
    link = github.HandoffLinkRow(
        number=200, attempts=(_merged(55, "2026-07-30T00:00:00Z"),), attempt_count=1)
    merged_pr = github.PipelinePrRow(
        number=55, title="Build #200", head_ref_name="agentflow/claude/issue-200-x",
        url="https://github.com/o/r/pull/55", merged_at="2026-07-30T00:00:00Z",
        merge_commit_oid="deadbeef", review_decision="APPROVED", ci_rollup=[])
    view = decision_maps.map_view(
        _map(179, children=[terminal]), repo="o/r", handoff_links=_links(link),
        open_prs=[], merged_prs=[merged_pr])
    assert view["handoffs"][0]["pipeline"] == {
        "state": "merged", "pr_number": 55, "pr_url": "https://github.com/o/r/pull/55",
        "merged_at": "2026-07-30T00:00:00Z", "merge_commit": "deadbeef", "review": "approved",
        "ci": None}


def test_maps_component_carries_githubs_total_for_overflow():
    read = github.MapsRead(maps=(_map(1),), total_count=7, cost=3, remaining=4990)
    component = decision_maps.maps_component(
        read, repo="o/r", handoff_links=_links(), open_prs=[], merged_prs=[])
    assert component["active_total"] == 7
    assert len(component["active"]) == 1


# --- the wire: what GitHub actually sends back --------------------------------------------
# One map (#179) with two decision children. The closed child #184 links onward to two issues:
# an ordinary Build Issue carrying the handoff marker, and a map artifact carrying a
# `wayfinder:` label that must never be presented as a handoff. Labels arrive nested under
# `nodes` — the connection shape the query asks for — which is the shape that used to be parsed
# as a flat list and silently came back empty.

MARKER = decision_maps.handoff_marker(179)

_CANDIDATE_BUILD = {
    "id": "I_kwDOB505", "number": 505, "title": "Make the map reads trustworthy",
    "url": "https://github.com/o/r/issues/505",
    "body": f"Some brief text.\n\n{MARKER}\n",
    "labels": {"nodes": [{"name": "ready-for-agent"}, {"name": "agentflow:building"}]},
    "repository": {"nameWithOwner": "o/r"},
}
_CANDIDATE_MAP_ARTIFACT = {
    "id": "I_kwDOB374", "number": 374, "title": "Wayfinder research on the same map",
    "url": "https://github.com/o/r/issues/374",
    "body": f"Grounding for the map.\n\n{MARKER}\n",
    "labels": {"nodes": [{"name": "wayfinder:research"}]},
    "repository": {"nameWithOwner": "o/r"},
}

_MAPS_RESPONSE = {
    "data": {
        "rateLimit": {"cost": 5, "remaining": 4990},
        "repository": {"issues": {
            "totalCount": 1,
            "nodes": [{
                "number": 179, "title": "Map: the operator console",
                "url": "https://github.com/o/r/issues/179",
                "updatedAt": "2026-07-30T00:00:00Z",
                "body": "## Decisions so far\n- [ADR 36](docs/adr/0036-bounded.md)\n",
                "subIssues": {
                    "totalCount": 2,
                    "nodes": [
                        {"number": 184, "title": "Terminal slicing decision",
                         "url": "https://github.com/o/r/issues/184", "state": "CLOSED",
                         "assignees": {"totalCount": 0},
                         "blockedBy": {"totalCount": 1,
                                       "nodes": [{"number": 183, "state": "CLOSED"}]},
                         "blocking": {"totalCount": 2,
                                      "nodes": [_CANDIDATE_BUILD, _CANDIDATE_MAP_ARTIFACT]}},
                        {"number": 405, "title": "The next decision",
                         "url": "https://github.com/o/r/issues/405", "state": "OPEN",
                         "assignees": {"totalCount": 0},
                         "blockedBy": {"totalCount": 0, "nodes": []},
                         "blocking": {"totalCount": 0, "nodes": []}},
                    ],
                },
            }],
        }},
    },
}

_LINKS_RESPONSE = {
    "data": {
        "rateLimit": {"cost": 1, "remaining": 4989},
        "repository": {"i505": {"closedByPullRequestsReferences": {
            "totalCount": 2,
            "nodes": [{"number": 358, "url": "https://github.com/o/r/pull/358",
                       "state": "MERGED", "mergedAt": "2026-07-29T08:25:41Z"},
                      {"number": 361, "url": "https://github.com/o/r/pull/361",
                       "state": "MERGED", "mergedAt": "2026-07-30T17:12:56Z"}]}}},
    },
}


def _counted(maps):
    """The cheap counting answer that precedes a detail read (#497), derived from the detail
    response so a fixture states its maps once. A malformed or failed detail response answers
    the count the same way, so a failure is a failure from the first call."""
    nodes = None
    if isinstance(maps, dict):
        nodes = (((maps.get("data") or {}).get("repository") or {})
                 .get("issues") or {}).get("nodes")
    if nodes is None:
        return maps
    return {"data": {"rateLimit": {"cost": 1, "remaining": 4991},
                     "repository": {"issues": {"totalCount": len(nodes)}}}}


def _wire(monkeypatch, *, maps=_MAPS_RESPONSE, links=_LINKS_RESPONSE, returncode=0, stderr=""):
    """Answer every Decision Map read with real-shaped GraphQL responses — the cheap count, the
    detail read it justifies, and the handoff join — recording each `gh` argument vector the
    module built so a test can state which query ran."""
    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, timeout=None):
        calls.append(list(cmd))
        query = next((a for a in cmd if a.startswith("query=")), "")
        if "closedByPullRequestsReferences" in query:
            payload = links
        elif "subIssues" in query:
            payload = maps
        else:
            payload = _counted(maps)
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(github, "_run", fake_run)
    return calls


def test_candidate_labels_are_read_from_the_label_connection(monkeypatch):
    # The query asks for `labels(first:20){nodes{name}}`; parsing that as a flat list returns
    # nothing, which is what made every live candidate look unlabelled.
    _wire(monkeypatch)
    read = github.decision_maps("o/r")
    candidates = read.maps[0].children[0].handoff_candidates
    assert {c.number: set(c.labels) for c in candidates} == {
        505: {"ready-for-agent", "agentflow:building"},
        374: {"wayfinder:research"}}


def test_a_map_artifact_never_reaches_the_verified_handoffs_from_the_wire(monkeypatch):
    # Both linked issues carry the marker and sit in the same repository, so the label
    # namespace is the only thing keeping the map's own research issue out of the handoff
    # list — and that filter can only fire if the labels survived the parse.
    _wire(monkeypatch)
    read = github.decision_maps("o/r")
    handoffs, overflow = decision_maps.verified_handoffs(read.maps[0], repo="o/r")
    assert [h.number for h in handoffs] == [505]
    assert overflow == 0


def test_a_live_shaped_response_types_the_whole_map(monkeypatch):
    _wire(monkeypatch)
    read = github.decision_maps("o/r")
    assert read.error is None
    assert (read.cost, read.remaining, read.total_count) == (6, 4990, 1), (
        "one point to count the maps, five to read them")
    map_row = read.maps[0]
    assert (map_row.number, map_row.updated_at) == (179, "2026-07-30T00:00:00Z")
    assert map_row.children_total == 2
    assert [decision_maps.classify_child(c) for c in map_row.children] == ["done", "frontier"]


def test_an_enrolled_repository_with_no_maps_reads_empty_not_failed(monkeypatch):
    _wire(monkeypatch, maps={"data": {"rateLimit": {"cost": 1, "remaining": 4999},
                                      "repository": {"issues": {"totalCount": 0, "nodes": []}}}})
    read = github.decision_maps("o/r")
    assert read.maps == () and read.total_count == 0
    assert read.error is None, "no maps is a fact about the repository, not a failed read"


def test_a_graphql_error_response_keeps_githubs_own_words(monkeypatch):
    _wire(monkeypatch, returncode=1,
          maps={"errors": [{"message": "Could not resolve to a Repository with the name 'o/r'."}]})
    read = github.decision_maps("o/r")
    assert read.maps == ()
    assert "Could not resolve to a Repository" in read.error


def test_an_unreadable_map_response_reports_the_command_failure(monkeypatch):
    _wire(monkeypatch, returncode=1, maps="not json at all",
          stderr="gh: HTTP 502: Bad gateway (api.github.com/graphql)")
    read = github.decision_maps("o/r")
    assert read.maps == ()
    assert "502" in read.error and "\n" not in read.error


def test_a_silent_failure_still_carries_a_reason(monkeypatch):
    _wire(monkeypatch, returncode=1, maps="", stderr="")
    assert github.decision_maps("o/r").error == "the map read failed"


def test_handoff_links_type_the_closing_pull_requests_from_the_wire(monkeypatch):
    calls = _wire(monkeypatch)
    read = github.handoff_pr_links_read("o/r", [505])
    assert read.links == {505: github.HandoffLinkRow(
        number=505, attempt_count=2,
        attempts=(github.HandoffAttemptRow(number=358, url="https://github.com/o/r/pull/358",
                                           state="MERGED", merged_at="2026-07-29T08:25:41Z"),
                  github.HandoffAttemptRow(number=361, url="https://github.com/o/r/pull/361",
                                           state="MERGED", merged_at="2026-07-30T17:12:56Z")))}
    assert read.error is None
    query = next(a for a in calls[0] if a.startswith("query="))
    assert "i505:issue(number:505)" in query


def test_the_wire_carries_the_node_ids_deduplication_depends_on(monkeypatch):
    _wire(monkeypatch)
    read = github.decision_maps("o/r")
    candidates = read.maps[0].children[0].handoff_candidates
    assert [c.id for c in candidates] == ["I_kwDOB505", "I_kwDOB374"]


def test_a_failed_handoff_link_read_is_unknown(monkeypatch):
    _wire(monkeypatch, returncode=1,
          links={"errors": [{"message": "Something went wrong while executing your query."}]})
    read = github.handoff_pr_links_read("o/r", [505])
    assert read.links == {}, "a failed join knows nothing, rather than claiming no links exist"
    assert "Something went wrong" in read.error


def test_no_handoffs_asks_github_nothing(monkeypatch):
    calls = _wire(monkeypatch)
    read = github.handoff_pr_links_read("o/r", [])
    assert read.links == {} and read.error is None
    assert calls == []


# --- the read-only probe (`agentflow decision-map-probe`) ----------------------------------

_WRITE_WORDS = {"edit", "create", "comment", "close", "reopen", "merge", "delete", "review"}


def test_the_probe_runs_the_production_queries_and_prints_what_github_answered(
        monkeypatch, capsys):
    calls = _wire(monkeypatch)
    assert cli.main(["decision-map-probe", "--repo", "o/r"]) == 0
    out = capsys.readouterr().out

    queries = [next(a for a in cmd if a.startswith("query=")) for cmd in calls]
    assert queries[0] == f"query={github._MAPS_DISCOVERY_QUERY}", "the count comes first"
    assert queries[1] == f"query={github._MAPS_QUERY}", "then the production detail query"
    assert "i505:issue(number:505)" in queries[2], "and the handoff the maps verified"
    assert '"number": 179' in out and '"wayfinder:research"' in out, "raw responses, whole"
    assert "cost: 1 points, 4991 remaining" in out
    assert "cost: 5 points, 4990 remaining" in out
    assert "cost: 1 points, 4989 remaining" in out
    assert "combined cost: 7 points across 3 requests" in out


def test_the_probe_writes_nothing_to_github(monkeypatch):
    calls = _wire(monkeypatch)
    cli.main(["decision-map-probe", "--repo", "o/r"])
    assert [cmd[:3] for cmd in calls] == [["gh", "api", "graphql"]] * 3
    spoken = {word for cmd in calls for arg in cmd for word in arg.split()}
    assert not spoken & _WRITE_WORDS, "a probe that could mutate anything is not a probe"


def test_the_probe_reports_a_failed_read_instead_of_an_empty_map(monkeypatch, capsys):
    _wire(monkeypatch, returncode=1,
          maps={"errors": [{"message": "API rate limit exceeded"}]})
    cli.main(["decision-map-probe", "--repo", "o/r"])
    out = capsys.readouterr().out
    assert "error: API rate limit exceeded" in out
    assert "not issued" in out, "no maps read means no handoff join to make"
