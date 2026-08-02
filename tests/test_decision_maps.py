"""Decision Map projection shaping (ADR 0036) — pure classification, handoff verification, and
ADR-link scraping, exercised through the module's public functions on typed GitHub rows."""

from __future__ import annotations

from agentflow import decision_maps, github


def _child(number=1, *, state="OPEN", assigned=False, blocked_by_open=0, blocked_by_closed=0,
          blocked_by_total=None, handoff_candidates=()) -> github.MapChildRow:
    total = blocked_by_total if blocked_by_total is not None else blocked_by_open + blocked_by_closed
    return github.MapChildRow(
        number=number, title=f"child {number}", url=f"https://github.com/o/r/issues/{number}",
        state=state, assigned=assigned, blocked_by_open=blocked_by_open,
        blocked_by_closed=blocked_by_closed, blocked_by_total=total,
        handoff_candidates=tuple(handoff_candidates))


def _candidate(number=100, *, repo="o/r", labels=(), body="") -> github.HandoffCandidateRow:
    return github.HandoffCandidateRow(
        number=number, title=f"handoff {number}", url=f"https://github.com/{repo}/issues/{number}",
        body=body, labels=frozenset(labels), repo=repo)


def _map(number=179, *, body="", children=()) -> github.MapRow:
    return github.MapRow(number=number, title="Map: x", url=f"https://github.com/o/r/issues/{number}",
                         updated_at="2026-07-30T00:00:00Z", body=body, children=tuple(children),
                         children_total=len(children))


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
    assert overflow is False


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
    dup = _candidate(number=100, body=marker)  # same number, reached via a second closed child
    terminal_a = _child(1, state="CLOSED", handoff_candidates=candidates)
    terminal_b = _child(2, state="CLOSED", handoff_candidates=[dup])
    handoffs, overflow = decision_maps.verified_handoffs(
        _map(179, children=[terminal_a, terminal_b]), repo="o/r", limit=20)
    assert len(handoffs) == 20
    assert overflow is True
    assert len({h.number for h in handoffs}) == 20  # deduplicated, not 26 raw candidates


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
        _map(179, children=children), repo="o/r", handoff_links={}, open_prs=[], merged_prs=[])
    assert view["progress"] == {"total": 3, "closed": 1}
    assert [t["status"] for t in view["tickets"]] == ["done", "frontier", "claimed"]
    assert [f["number"] for f in view["frontier"]] == [2]
    assert view["complete"] is True


def test_map_view_joins_handoff_pipeline_evidence():
    marker = decision_maps.handoff_marker(179)
    candidate = _candidate(number=200, body=marker)
    terminal = _child(1, state="CLOSED", handoff_candidates=[candidate])
    link = github.HandoffLinkRow(number=200, pr_numbers=(55,), attempt_count=1)
    merged_pr = github.PipelinePrRow(
        number=55, title="Build #200", head_ref_name="agentflow/claude/issue-200-x",
        url="https://github.com/o/r/pull/55", merged_at="2026-07-30T00:00:00Z",
        merge_commit_oid="deadbeef", review_decision="APPROVED", ci_rollup=[])
    view = decision_maps.map_view(
        _map(179, children=[terminal]), repo="o/r", handoff_links={200: link},
        open_prs=[], merged_prs=[merged_pr])
    assert view["handoffs"][0]["pipeline"] == {
        "state": "merged", "pr_number": 55, "pr_url": "https://github.com/o/r/pull/55",
        "merged_at": "2026-07-30T00:00:00Z", "merge_commit": "deadbeef", "review": "approved",
        "ci": None}


def test_maps_component_carries_githubs_total_for_overflow():
    read = github.MapsRead(maps=(_map(1),), total_count=7, cost=3, remaining=4990)
    component = decision_maps.maps_component(
        read, repo="o/r", handoff_links={}, open_prs=[], merged_prs=[])
    assert component["active_total"] == 7
    assert len(component["active"]) == 1
