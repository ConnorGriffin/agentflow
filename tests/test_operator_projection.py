"""Schema-version-2 operator projection (ADR 0036) — freshness bookkeeping, the fleet-wide
point-budget stop, and stale-preserve-on-failure, exercised through the public entry points."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentflow import github, operator_projection
from agentflow.loop import RepoConfig

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(repo="o/r") -> RepoConfig:
    return RepoConfig(repo=repo, workdir="/nonexistent/does-not-exist")


def _maps_read(cost=5, remaining=4990, maps=(), total_count=0) -> github.MapsRead:
    return github.MapsRead(maps=tuple(maps), total_count=total_count, cost=cost,
                           remaining=remaining)


def _map(number=1) -> github.MapRow:
    return github.MapRow(number=number, title=f"Map {number}",
                         url=f"https://github.com/o/r/issues/{number}",
                         updated_at="2026-08-03T00:00:00Z", body="", children=(),
                         children_total=0)


# --- freshness ---------------------------------------------------------------------------

def test_a_successful_attempt_is_fresh():
    result = operator_projection.freshness(
        previous=None, now=NOW, heartbeat_seconds=300, attempted=True, success=True, error=None)
    assert result == {"status": "fresh", "attempted_at": NOW.isoformat(),
                      "fresh_at": NOW.isoformat(), "error": None}


def test_a_failed_attempt_is_stale_even_when_recently_fresh():
    previous = {"status": "fresh", "fresh_at": (NOW - timedelta(seconds=30)).isoformat(),
                "attempted_at": (NOW - timedelta(seconds=30)).isoformat(), "error": None}
    result = operator_projection.freshness(
        previous=previous, now=NOW, heartbeat_seconds=300, attempted=True, success=False,
        error="the map read failed")
    assert result["status"] == "stale"
    assert result["fresh_at"] == previous["fresh_at"], "a failure never discards the last verified read"
    assert result["error"] == "the map read failed"


def test_never_succeeded_is_unavailable():
    result = operator_projection.freshness(
        previous=None, now=NOW, heartbeat_seconds=300, attempted=True, success=False,
        error="boom")
    assert result["status"] == "unavailable"
    assert result["fresh_at"] is None


def test_a_skip_within_two_heartbeats_stays_fresh():
    previous = {"status": "fresh", "fresh_at": (NOW - timedelta(seconds=400)).isoformat(),
                "attempted_at": (NOW - timedelta(seconds=400)).isoformat(), "error": None}
    result = operator_projection.freshness(
        previous=previous, now=NOW, heartbeat_seconds=300, attempted=False, success=False,
        error=None)
    assert result["status"] == "fresh"  # 400s old, under the 2*300s=600s window
    assert result["attempted_at"] == previous["attempted_at"], "a skip is not a new attempt"


def test_a_skip_beyond_two_heartbeats_goes_stale():
    previous = {"status": "fresh", "fresh_at": (NOW - timedelta(seconds=700)).isoformat(),
                "attempted_at": (NOW - timedelta(seconds=700)).isoformat(), "error": None}
    result = operator_projection.freshness(
        previous=previous, now=NOW, heartbeat_seconds=300, attempted=False, success=False,
        error=None)
    assert result["status"] == "stale"


# --- repository_maps ---------------------------------------------------------------------

def test_repository_maps_publishes_the_fresh_read():
    calls = []

    def read_maps(repo, **kw):
        calls.append(repo)
        return _maps_read()

    result = operator_projection.repository_maps(
        _cfg(), previous_snapshot=None, now=NOW, heartbeat_seconds=300,
        budget={"spent": 0, "stopped": False}, read_maps=read_maps,
        read_links=lambda repo, nums: {}, read_prs=lambda repo, state: [])
    assert calls == ["o/r"]
    assert result["name_with_owner"] == "o/r"
    assert result["url"] == "https://github.com/o/r"
    assert result["github"]["status"] == "fresh"
    assert result["maps"] == {"active": [], "active_total": 0}


def test_repository_maps_preserves_previous_component_on_failed_read():
    previous_snapshot = {"repositories": [
        {"name_with_owner": "o/r", "github": {"status": "fresh", "fresh_at": "2026-07-30T11:55:00+00:00",
                                              "attempted_at": "2026-07-30T11:55:00+00:00", "error": None},
         "maps": {"active": [{"number": 1}], "active_total": 1}}]}
    result = operator_projection.repository_maps(
        _cfg(), previous_snapshot=previous_snapshot, now=NOW, heartbeat_seconds=300,
        budget={"spent": 0, "stopped": False}, read_maps=lambda repo, **kw: None,
        read_links=lambda repo, nums: {}, read_prs=lambda repo, state: [])
    assert result["maps"] == {"active": [{"number": 1}], "active_total": 1}
    assert result["github"]["status"] == "stale"
    assert result["github"]["fresh_at"] == "2026-07-30T11:55:00+00:00"


def test_repository_maps_stopped_budget_never_calls_github_and_preserves_previous():
    stale_fresh_at = (NOW - timedelta(seconds=700)).isoformat()
    previous_snapshot = {"repositories": [
        {"name_with_owner": "o/r", "github": {"status": "fresh", "fresh_at": stale_fresh_at,
                                              "attempted_at": stale_fresh_at, "error": None},
         "maps": {"active": [{"number": 9}], "active_total": 1}}]}

    def boom(*a, **k):
        raise AssertionError("must not call GitHub once the fleet budget is stopped")

    result = operator_projection.repository_maps(
        _cfg(), previous_snapshot=previous_snapshot, now=NOW, heartbeat_seconds=300,
        budget={"spent": 0, "stopped": True}, read_maps=boom, read_links=boom, read_prs=boom)
    assert result["maps"] == {"active": [{"number": 9}], "active_total": 1}
    assert result["github"]["status"] == "stale"
    assert "point budget" in result["github"]["error"]


def test_repository_maps_stops_the_fleet_budget_once_the_point_ceiling_is_reached():
    budget = {"spent": 0, "stopped": False}
    operator_projection.repository_maps(
        _cfg("o/a"), previous_snapshot=None, now=NOW, heartbeat_seconds=300, budget=budget,
        read_maps=lambda repo, **kw: _maps_read(cost=61, remaining=9000),
        read_links=lambda repo, nums: {}, read_prs=lambda repo, state: [])
    assert budget["spent"] == 61
    assert budget["stopped"] is True, "spending past the 60-point ceiling stops the rest of the fleet"


def test_repository_maps_stops_the_fleet_budget_below_the_workflow_floor():
    budget = {"spent": 0, "stopped": False}
    operator_projection.repository_maps(
        _cfg("o/a"), previous_snapshot=None, now=NOW, heartbeat_seconds=300, budget=budget,
        read_maps=lambda repo, **kw: _maps_read(cost=5, remaining=1002),
        read_links=lambda repo, nums: {}, read_prs=lambda repo, state: [])
    assert budget["stopped"] is True, "1002 - 5 = 997 remaining dips under the 1000-point floor"


def test_repository_maps_degrades_handoff_evidence_without_failing_the_map_read():
    # A one-map read, because a repository with no maps makes no pipeline-PR call at all
    # (#497) — with an empty read this would pass without ever exercising the degrade path.
    result = operator_projection.repository_maps(
        _cfg(), previous_snapshot=None, now=NOW, heartbeat_seconds=300,
        budget={"spent": 0, "stopped": False},
        read_maps=lambda repo, **kw: _maps_read(maps=(_map(1),), total_count=1),
        read_links=lambda repo, nums: None, read_prs=lambda repo, state: None)
    assert result["github"]["status"] == "fresh", "the map read itself succeeded"
    assert result["maps"]["active_total"] == 1


def test_repository_with_no_maps_makes_no_pipeline_pr_call():
    def boom(repo, state):
        raise AssertionError("nothing consumes a PR listing for a repository with no maps")

    result = operator_projection.repository_maps(
        _cfg(), previous_snapshot=None, now=NOW, heartbeat_seconds=300,
        budget={"spent": 0, "stopped": False}, read_maps=lambda repo, **kw: _maps_read(),
        read_links=lambda repo, nums: {}, read_prs=boom)
    assert result["github"]["status"] == "fresh"
    assert result["maps"] == {"active": [], "active_total": 0}


# --- build / fleet_recent_landed ----------------------------------------------------------

def test_build_composes_schema_version_and_repositories(monkeypatch):
    monkeypatch.setattr(github, "decision_maps", lambda repo, **kw: _maps_read())
    monkeypatch.setattr(github, "handoff_pr_links", lambda repo, nums: {})
    monkeypatch.setattr(github, "list_pipeline_prs", lambda repo, state: [])
    result = operator_projection.project(
        [_cfg("o/a"), _cfg("o/b")], previous_snapshot=None, heartbeat_seconds=300, now=NOW)
    assert result["schema_version"] == 2
    assert result["generated_at"] == NOW.isoformat()
    assert [r["name_with_owner"] for r in result["repositories"]] == ["o/a", "o/b"]
    assert all(r["github"]["status"] == "fresh" for r in result["repositories"])


def test_starvation_every_repository_reaches_fresh_within_ceil_n_over_k_heartbeats(monkeypatch):
    # Five repositories, a budget that stops after 2 reads (cost 30 each against a 60 ceiling).
    # With the naive top-of-list walk order this never recovers — the same two repos win every
    # heartbeat and the rest stay unavailable forever. With least-recently-fresh-first, every
    # repository must be fresh within ceil(5/2) = 3 heartbeats.
    monkeypatch.setattr(github, "handoff_pr_links", lambda repo, nums: {})
    monkeypatch.setattr(github, "list_pipeline_prs", lambda repo, state: [])
    monkeypatch.setattr(github, "decision_maps", lambda repo, **kw: _maps_read(cost=30, remaining=4990))

    repos = [_cfg(f"o/r{i}") for i in range(5)]
    snapshot = None
    for heartbeat in range(3):
        snapshot = operator_projection.project(
            repos, previous_snapshot=snapshot, heartbeat_seconds=300,
            now=NOW + timedelta(seconds=300 * heartbeat))
    statuses = {r["name_with_owner"]: r["github"]["status"] for r in snapshot["repositories"]}
    assert all(status == "fresh" for status in statuses.values()), statuses


def test_the_whole_real_shaped_fleet_refreshes_in_one_pass_inside_the_point_ceiling(monkeypatch):
    # The enrolled fleet as measured 2026-08-03: nine repositories, seven with no Decision
    # Maps at all, one with three and one with two. Priced the way GitHub bills the two-phase
    # read — 1 point to count, then ~6.6 per map asked for — the whole fleet costs 42 points
    # against the unchanged 60-point ceiling, so every row is fresh every pass (#497).
    costs = {"o/agentflow": (21, (_map(1), _map(2), _map(3))), "o/ciq": (14, (_map(4), _map(5)))}

    def read_maps(repo, **kw):
        cost, maps = costs.get(repo, (1, ()))
        return _maps_read(cost=cost, remaining=4990, maps=maps, total_count=len(maps))

    monkeypatch.setattr(github, "decision_maps", read_maps)
    monkeypatch.setattr(github, "handoff_pr_links", lambda repo, nums: {})
    monkeypatch.setattr(github, "list_pipeline_prs", lambda repo, state: [])

    repos = ([_cfg("o/agentflow"), _cfg("o/ciq")]
             + [_cfg(f"o/quiet{i}") for i in range(7)])
    budget = {"spent": 0, "stopped": False}
    snapshot = {"schema_version": 2, "repositories": [
        operator_projection.repository_maps(cfg, previous_snapshot=None, now=NOW,
                                            heartbeat_seconds=300, budget=budget)
        for cfg in repos]}

    statuses = {r["name_with_owner"]: r["github"]["status"] for r in snapshot["repositories"]}
    assert all(status == "fresh" for status in statuses.values()), statuses
    assert len({r["github"]["fresh_at"] for r in snapshot["repositories"]}) == 1
    assert budget["spent"] == 42
    assert budget["spent"] < operator_projection.POINT_CEILING == 60
    assert budget["stopped"] is False, "the ceiling stop never fires for today's fleet"


def test_never_loaded_repositories_are_read_before_stale_but_once_fresh_ones():
    previous_snapshot = {"repositories": [
        {"name_with_owner": "o/a", "github": {"status": "fresh",
                                              "fresh_at": (NOW - timedelta(seconds=10)).isoformat(),
                                              "attempted_at": (NOW - timedelta(seconds=10)).isoformat(),
                                              "error": None}, "maps": {"active": [], "active_total": 0}},
    ]}
    repos = [_cfg("o/a"), _cfg("o/b")]
    order = operator_projection._walk_order(repos, previous_snapshot=previous_snapshot)
    assert [cfg.repo for cfg in order] == ["o/b", "o/a"], "never-loaded o/b must be read first"


def test_project_output_order_stays_config_order_regardless_of_walk_order(monkeypatch):
    monkeypatch.setattr(github, "decision_maps", lambda repo, **kw: _maps_read())
    monkeypatch.setattr(github, "handoff_pr_links", lambda repo, nums: {})
    monkeypatch.setattr(github, "list_pipeline_prs", lambda repo, state: [])
    previous_snapshot = {"repositories": [
        {"name_with_owner": "o/a", "github": {"status": "fresh",
                                              "fresh_at": (NOW - timedelta(seconds=10)).isoformat(),
                                              "attempted_at": (NOW - timedelta(seconds=10)).isoformat(),
                                              "error": None}, "maps": {"active": [], "active_total": 0}},
    ]}
    result = operator_projection.project(
        [_cfg("o/a"), _cfg("o/b")], previous_snapshot=previous_snapshot, heartbeat_seconds=300,
        now=NOW)
    assert [r["name_with_owner"] for r in result["repositories"]] == ["o/a", "o/b"]


def test_fleet_recent_landed_merges_sorts_and_bounds():
    repo_views = [
        {"repo": "o/a", "recent_merges": [{"number": 1, "merged_at": "2026-07-01T00:00:00Z"}]},
        {"repo": "o/b", "recent_merges": [{"number": 2, "merged_at": "2026-07-15T00:00:00Z"}]},
    ]
    result = operator_projection.fleet_recent_landed(repo_views, limit=1)
    assert result == {"recent_landed": [{"repo": "o/b", "number": 2,
                                         "merged_at": "2026-07-15T00:00:00Z"}]}


# --- the attention queue (#373) -----------------------------------------------------------
# The five ruled conditions, decided daemon-side. Every fixture below is shaped exactly like
# what `dashboard_data.repo_view` and `project()` publish, so these exercise the same rows the
# console renders.

def _view(repo="o/r", *, profile="reviewed", in_flight=(), held=(), parked=(), ratchet=None):
    return {"repo": repo, "profile": profile, "in_flight": list(in_flight),
            "held": list(held), "parked": list(parked),
            "ratchet": ratchet or {"samples": 0, "correction_rate": 0.0,
                                   "ready_to_loosen": False}}


def _pr(number, title="a change", handed_off_at="2026-07-30T09:00:00Z"):
    return {"number": number, "title": title, "builder": "claude",
            "handed_off_at": handed_off_at}


def _held(number, state="needs-grilling", since="2026-07-29T09:00:00Z"):
    return {"number": number, "title": "a fork", "state": state,
            "reason": "a real fork the pipeline couldn't settle", "since": since}


def _parked(number, reason="drop-to-reviewed", since="2026-07-28T09:00:00Z"):
    return {"number": number, "title": "a stopped build", "reason": reason,
            "builder": "claude", "reviewer": "codex", "since": since}


def _repository(repo="o/r", status="fresh", error=None):
    return {"name_with_owner": repo, "url": f"https://github.com/{repo}",
            "github": {"status": status, "error": error}}


def test_attention_orders_the_five_conditions_by_their_ruled_priority():
    result = operator_projection.attention(
        [_view(in_flight=[_pr(20)], held=[_held(10)], parked=[_parked(30)],
               ratchet={"samples": 12, "correction_rate": 0.05, "ready_to_loosen": True})],
        [_repository(status="stale")])
    assert [row["condition"] for row in result["rows"]] == [
        "awaiting-merge", "waiting-on-reply", "parked-build", "ready-to-loosen", "stale-data"]
    assert [row["kind"] for row in result["rows"]] == [
        "Merge", "Held", "Parked", "Trust", "Projection"]
    assert result["total"] == 5


def test_awaiting_merge_ranks_by_repository_profile_alone_then_by_the_longest_wait():
    """A same-tool summary reading 'maintainer merge required' is the ordinary fallback when the
    cross-tool pool has no headroom, so it never discriminates a condition — the profile does,
    and an `autonomous` hand-off still earns its row, ranked last of the three."""
    result = operator_projection.attention([
        _view("o/auto", profile="autonomous", in_flight=[_pr(1)]),
        _view("o/rev", profile="reviewed",
              in_flight=[_pr(2, handed_off_at="2026-07-30T11:00:00Z"),
                         _pr(3, handed_off_at="2026-07-30T08:00:00Z")]),
        _view("o/guard", profile="guarded", in_flight=[_pr(4)]),
    ], [])
    assert [(row["repo"], row["number"]) for row in result["rows"]] == [
        ("o/guard", 4), ("o/rev", 3), ("o/rev", 2), ("o/auto", 1)]


def test_normal_in_flight_work_the_engine_still_holds_never_reaches_the_queue():
    result = operator_projection.attention(
        [_view(profile="autonomous", in_flight=[_pr(20, handed_off_at=None)]),
         _view("o/other", in_flight=[_pr(21, handed_off_at=None)])], [])
    assert result == {"rows": [], "total": 0}


def test_a_rebased_ready_pr_keeps_its_awaiting_merge_row():
    """The daemon re-rebases and force-pushes open PRs when main advances, moving the head off
    the commit the summary recorded. The PR is still the operator's to merge."""
    result = operator_projection.attention([_view(in_flight=[_pr(20)])], [])
    assert [row["number"] for row in result["rows"]] == [20]


def test_a_parked_pr_carrying_a_live_summary_collapses_to_the_parked_row_alone():
    """Both reachable duplicates: an unanswered maintainer comment on a merge-ready PR, and a
    conflict park posted over a summary nothing retired. One operator action per thing."""
    question = operator_projection.attention(
        [_view(in_flight=[_pr(20)], parked=[_parked(20, reason="open-question")])], [])
    assert [(r["condition"], r["number"]) for r in question["rows"]] == [("parked-build", 20)]
    assert question["total"] == 1
    conflict = operator_projection.attention(
        [_view(in_flight=[_pr(21)], parked=[_parked(21, reason="failed-merge")])], [])
    assert [(r["condition"], r["number"]) for r in conflict["rows"]] == [("parked-build", 21)]


def test_park_reasons_say_what_is_true_of_the_pr_not_what_the_bucket_is_named():
    result = operator_projection.attention([
        _view(parked=[_parked(30, reason="open-question"),
                      _parked(31, reason="drop-to-reviewed")])], [])
    reasons = {row["number"]: row["detail"] for row in result["rows"]}
    assert reasons[30] == "r · your comment on this PR has not been answered"
    assert "stopped the build" not in reasons[30]
    assert reasons[31] == "r · the pipeline stopped and left a decision on the PR"
    assert "drop autonomy" not in reasons[31]


def test_held_issues_rank_needs_grilling_before_needs_mockup():
    result = operator_projection.attention([
        _view(held=[_held(11, state="needs-mockup", since="2026-07-01T00:00:00Z"),
                    _held(10, state="needs-grilling", since="2026-07-29T00:00:00Z")])], [])
    assert [row["number"] for row in result["rows"]] == [10, 11]


def test_a_repository_already_at_the_top_of_the_ladder_never_prompts_a_loosen():
    ready = {"samples": 20, "correction_rate": 0.02, "ready_to_loosen": True}
    result = operator_projection.attention([
        _view("o/auto", profile="autonomous", ratchet=ready),
        _view("o/rev", profile="reviewed", ratchet=ready)], [])
    assert [(row["repo"], row["kind"]) for row in result["rows"]] == [("o/rev", "Trust")]
    assert result["rows"][0]["detail"] == "20 decisions · 2% corrected"


def test_stale_and_never_loaded_repositories_each_earn_one_row_and_a_fresh_one_none():
    result = operator_projection.attention([], [
        _repository("o/alpha", status="unavailable"),
        _repository("o/beta", status="fresh"),
        _repository("o/gamma", status="stale"),
    ])
    assert [(row["repo"], row["title"]) for row in result["rows"]] == [
        ("o/alpha", "alpha briefing data has never loaded"),
        ("o/gamma", "gamma briefing data is stale")]


def test_a_repository_the_daemons_own_refresh_skipped_names_the_daemon_not_github():
    result = operator_projection.attention([], [
        _repository(status="stale", error="point budget reached this heartbeat")])
    assert result["rows"][0]["detail"] == (
        "the daemon's own bounded refresh did not reach this repository this cycle")
    failed = operator_projection.attention([], [
        _repository(status="stale", error="the map read failed")])
    assert failed["rows"][0]["detail"] == "the last read of this repository failed"


def test_every_row_carries_an_authoritative_github_url_for_acting_on_it():
    result = operator_projection.attention(
        [_view(in_flight=[_pr(20)], held=[_held(10)], parked=[_parked(30)],
               ratchet={"samples": 12, "correction_rate": 0.05, "ready_to_loosen": True})],
        [_repository("o/s", status="stale")])
    assert [row["url"] for row in result["rows"]] == [
        "https://github.com/o/r/pull/20",
        "https://github.com/o/r/issues/10",
        "https://github.com/o/r/pull/30",
        "https://github.com/o/r",
        "https://github.com/o/s"]


def test_the_queue_is_bounded_fleet_wide_and_still_reports_its_true_total():
    views = [_view(f"o/r{index:02d}", held=[_held(index)]) for index in range(30)]
    result = operator_projection.attention(views, [])
    assert len(result["rows"]) == operator_projection.ATTENTION_LIMIT == 25
    assert result["total"] == 30


def test_the_order_never_depends_on_the_order_repositories_were_walked():
    views = [_view("o/b", held=[_held(2, since="2026-07-29T00:00:00Z")]),
             _view("o/a", held=[_held(1, since="2026-07-29T00:00:00Z")])]
    forward = operator_projection.attention(views, [])
    backward = operator_projection.attention(list(reversed(views)), [])
    assert forward == backward
    assert [row["repo"] for row in forward["rows"]] == ["o/a", "o/b"]


# --- the stage-drought condition (#430) ---------------------------------------------------
# A stage that spent sessions and produced nothing across a whole window of finished attempts.
# Fixtures are shaped exactly like what `stage_health.stage_health()` publishes.

def _stage(stage="pre-publish attack", *, produces="published a hardened brief",
           check="held drafts and ready-for-agent holdbacks", attempts=10, produced=0,
           sessions_spent=37):
    return {"stage": stage, "produces": produces, "check": check, "window": 10,
            "attempts": attempts, "produced": produced, "sessions_spent": sessions_spent}


def test_a_stage_that_produced_nothing_all_window_raises_one_row_ahead_of_everything_else():
    """The regression this condition exists to prevent: a silently broken stage outranks any
    single item it starves, because none of those items can show that it is broken."""
    result = operator_projection.attention(
        [_view(in_flight=[_pr(20)], held=[_held(10)], parked=[_parked(30)],
               ratchet={"samples": 12, "correction_rate": 0.05, "ready_to_loosen": True})],
        [_repository(status="stale")], stage_health=[_stage()])
    assert [row["condition"] for row in result["rows"]] == [
        "stage-drought", "awaiting-merge", "waiting-on-reply", "parked-build",
        "ready-to-loosen", "stale-data"]
    row = result["rows"][0]
    assert row["kind"] == "stage drought"
    assert row["title"] == "pre-publish attack"
    assert row["detail"] == ("0 of its last 10 finished attempts published a hardened brief "
                             "· 37 sessions spent")


def test_a_drought_row_says_where_to_look_instead_of_offering_an_action():
    """A stage is not something you can open on GitHub, so the third column is plain text."""
    result = operator_projection.attention([], [], stage_health=[_stage()])
    assert result["rows"][0]["url"] is None
    assert result["rows"][0]["note"] == (
        "What to check: held drafts and ready-for-agent holdbacks")


def test_a_stage_inside_its_first_window_leaves_the_queue_exactly_as_it_was():
    """Absence is load-bearing: a quiet fleet has to be provably quiet, so a stage that has not
    yet finished a whole window contributes nothing at all."""
    views, repositories = [_view(in_flight=[_pr(20)])], [_repository()]
    shipped = operator_projection.attention(views, repositories)
    assert operator_projection.attention(
        views, repositories, stage_health=[_stage(attempts=9, sessions_spent=12)]) == shipped


def test_a_stage_that_produced_anything_at_all_leaves_the_queue_exactly_as_it_was():
    views, repositories = [_view(in_flight=[_pr(20)])], [_repository()]
    shipped = operator_projection.attention(views, repositories)
    assert operator_projection.attention(
        views, repositories, stage_health=[_stage(produced=1)]) == shipped


def test_two_stages_in_drought_stack_as_two_rows():
    result = operator_projection.attention([], [], stage_health=[
        _stage(),
        _stage("review", produces="recorded a review verdict",
               check="open PRs waiting on a verdict", sessions_spent=41)])
    assert [row["title"] for row in result["rows"]] == ["pre-publish attack", "review"]
    assert result["rows"][1]["detail"] == ("0 of its last 10 finished attempts recorded a "
                                           "review verdict · 41 sessions spent")
    assert result["rows"][1]["note"] == "What to check: open PRs waiting on a verdict"
    assert result["total"] == 2


def test_a_drought_row_counts_against_the_fleet_wide_bound_like_any_other_row():
    views = [_view(f"o/r{index:02d}", held=[_held(index)]) for index in range(25)]
    result = operator_projection.attention(views, [], stage_health=[_stage()])
    assert len(result["rows"]) == operator_projection.ATTENTION_LIMIT == 25
    assert result["total"] == 26
    assert result["rows"][0]["condition"] == "stage-drought"
