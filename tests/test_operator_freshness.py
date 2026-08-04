"""How old the briefing is allowed to claim it is — the publish stamp, the read-time aging, and
what the console serves when the published body cannot be trusted (ADR 0036, ADR 376).

Everything below goes through a public entry point: the stamp the daemon writes, or the whole
`GET /api/snapshot` answer a browser would receive.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from agentflow import daemon, live, operator_freshness, operator_projection, webapp

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)
PUBLISHED_AT = NOW.isoformat()


def _client(read, *, now=NOW):
    return TestClient(webapp.create_app(read, now=lambda: now))


def _component(status="fresh", *, fresh_at=PUBLISHED_AT, error=None) -> dict:
    return {"status": status, "attempted_at": PUBLISHED_AT, "fresh_at": fresh_at, "error": error}


def _repository(repo="o/r", *, github=None, maps=None) -> dict:
    return {"name_with_owner": repo, "url": f"https://github.com/{repo}", "profile": "reviewed",
            "github": github or _component(),
            "maps": maps or {"active": [], "active_total": 0}}


def _published(*repositories, heartbeat=300, **over) -> dict:
    body = {"dispatch": {"enabled": True},
            "daemon": {"enabled": True, "last_cycle_at": PUBLISHED_AT, "poll_seconds": 15,
                       "gh_fresh_at": PUBLISHED_AT},
            "pools": [{"tool": "claude", "clear": True, "spent_pct": 10, "headroom_pct": 90,
                       "running": 1, "reason": None}],
            "running": [], "repos": [{"repo": "o/r", "profile": "reviewed"}],
            "schema_version": 2, "generated_at": PUBLISHED_AT,
            "heartbeat_seconds": heartbeat,
            "repositories": list(repositories),
            "fleet": {"recent_landed": []},
            "attention": {"rows": [], "total": 0}}
    body.update(over)
    return body


def _serve(body, *, now=NOW) -> dict:
    return _client(lambda: body, now=now).get("/api/snapshot").json()


# --- the publish-time stamp (relocated from operator_projection, unchanged) ------------------

def test_a_successful_attempt_is_fresh():
    result = operator_freshness.component(
        previous=None, now=NOW, heartbeat_seconds=300, attempted=True, success=True, error=None)
    assert result == {"status": "fresh", "attempted_at": NOW.isoformat(),
                      "fresh_at": NOW.isoformat(), "error": None}


def test_a_failed_attempt_is_stale_even_when_recently_fresh():
    previous = {"status": "fresh", "fresh_at": (NOW - timedelta(seconds=30)).isoformat(),
                "attempted_at": (NOW - timedelta(seconds=30)).isoformat(), "error": None}
    result = operator_freshness.component(
        previous=previous, now=NOW, heartbeat_seconds=300, attempted=True, success=False,
        error="the map read failed")
    assert result["status"] == "stale"
    assert result["fresh_at"] == previous["fresh_at"], (
        "a failure never discards the last verified read")
    assert result["error"] == "the map read failed"


def test_never_succeeded_is_unavailable():
    result = operator_freshness.component(
        previous=None, now=NOW, heartbeat_seconds=300, attempted=True, success=False,
        error="boom")
    assert result["status"] == "unavailable"
    assert result["fresh_at"] is None


def test_a_skip_within_two_heartbeats_stays_fresh():
    previous = {"status": "fresh", "fresh_at": (NOW - timedelta(seconds=400)).isoformat(),
                "attempted_at": (NOW - timedelta(seconds=400)).isoformat(), "error": None}
    result = operator_freshness.component(
        previous=previous, now=NOW, heartbeat_seconds=300, attempted=False, success=False,
        error=None)
    assert result["status"] == "fresh"  # 400s old, under the 2*300s=600s window
    assert result["attempted_at"] == previous["attempted_at"], "a skip is not a new attempt"


def test_a_skip_beyond_two_heartbeats_goes_stale():
    previous = {"status": "fresh", "fresh_at": (NOW - timedelta(seconds=700)).isoformat(),
                "attempted_at": (NOW - timedelta(seconds=700)).isoformat(), "error": None}
    result = operator_freshness.component(
        previous=previous, now=NOW, heartbeat_seconds=300, attempted=False, success=False,
        error=None)
    assert result["status"] == "stale"


# --- read-time aging, downgrade-only --------------------------------------------------------

def test_a_projection_nobody_republished_stops_reading_fresh():
    """The regression this slice exists for: a body published two hours ago, served now with
    no new publish, neither reports itself fresh nor presents a verified frontier."""
    served = _serve(_published(_repository()), now=NOW + timedelta(hours=2))
    assert served["freshness"]["state"] == "stale"
    assert served["freshness"]["message"] == (
        "This projection is stale. Open GitHub before acting on a frontier.")
    assert served["repositories"][0]["github"]["status"] == "stale", (
        "an aged component may not still be offered as a verified map read")


def test_a_fixed_projection_ages_out_of_fresh_at_the_stamped_window():
    body = _published(_repository())
    inside = _serve(body, now=NOW + timedelta(seconds=599))
    outside = _serve(body, now=NOW + timedelta(seconds=601))
    assert inside["freshness"]["state"] == "fresh"
    assert outside["freshness"]["state"] == "stale"


def test_the_window_comes_from_the_published_heartbeat_not_the_server():
    """Move the stamped heartbeat and the same clock reads the same body differently — the
    daemon and the console are launched separately and can be running different code, so the
    window has to travel in the body."""
    at_ten_minutes = _serve(_published(_repository(), heartbeat=300),
                            now=NOW + timedelta(seconds=900))
    at_thirty_minutes = _serve(_published(_repository(), heartbeat=1800),
                               now=NOW + timedelta(seconds=900))
    assert at_ten_minutes["freshness"]["state"] == "stale"
    assert at_thirty_minutes["freshness"]["state"] == "fresh"


def test_a_failed_component_with_a_recent_stamp_is_never_promoted_back_to_fresh():
    """Aging is downgrade-only: the published status is a ceiling. A read that failed thirty
    seconds ago still carries a thirty-second-old verified stamp."""
    recent = (NOW - timedelta(seconds=30)).isoformat()
    served = _serve(_published(_repository(
        github=_component("stale", fresh_at=recent, error="the map read failed"))))
    assert served["repositories"][0]["github"]["status"] == "stale"
    assert served["freshness"]["state"] == "stale"


def test_a_never_verified_component_is_never_promoted_either():
    served = _serve(_published(_repository(
        github=_component("unavailable", fresh_at=None, error="boom"))))
    assert served["repositories"][0]["github"]["status"] == "unavailable"
    assert served["freshness"] == {
        "state": "incomplete", "label_prefix": "Projection unavailable",
        "message": "One or more repositories have never published a verified Decision Map "
                   "read. Open GitHub for authoritative state.",
        "verified_at": None}


def test_a_partly_verified_projection_says_so_and_keeps_its_verified_age():
    served = _serve(_published(
        _repository("o/verified"),
        _repository("o/never", github=_component("unavailable", fresh_at=None, error="boom"))))
    assert served["freshness"] == {
        "state": "incomplete", "label_prefix": "Partial projection · verified",
        "message": "Dependency data is incomplete. A decision frontier is not verified.",
        "verified_at": PUBLISHED_AT}


def test_the_masthead_age_is_the_oldest_component_not_the_newest():
    older = (NOW - timedelta(seconds=500)).isoformat()
    served = _serve(_published(_repository("o/new"),
                               _repository("o/old", github=_component(fresh_at=older))))
    assert served["freshness"]["state"] == "fresh"
    assert served["freshness"]["verified_at"] == older, (
        "the masthead may not claim an age newer than the least current thing under it")


def test_the_publish_stamp_is_the_fallback_only_when_no_component_carries_one():
    served = _serve(_published(_repository(
        github={"status": "stale", "attempted_at": PUBLISHED_AT, "fresh_at": "not a date",
                "error": "the map read failed"})))
    assert served["freshness"]["state"] == "stale"
    assert served["freshness"]["verified_at"] == PUBLISHED_AT, "falls back to the publish stamp"


# --- untrusted timestamps in a durable file ------------------------------------------------

def test_unusable_timestamps_are_infinitely_old_and_never_an_error():
    """A stamp that cannot be dated is infinitely old; no stamp at all was never verified.
    Neither may reach the browser as an error page."""
    undatable = (12345, "", "not a date", "2026-07-30T12:00:00")  # the last one is naive
    for stamp in undatable + (None,):
        response = _client(lambda: _published(
            _repository(github=_component(fresh_at=stamp)))).get("/api/snapshot")
        assert response.status_code == 200, f"{stamp!r} produced an error page"
        expected = "unavailable" if stamp is None else "stale"
        assert response.json()["repositories"][0]["github"]["status"] == expected, (
            f"{stamp!r} was treated as datable")


# --- the version and shape guard -------------------------------------------------------------

_UNREADABLE = "The operator projection could not be read. Open GitHub for authoritative state."


def _assert_v2_unavailable(served, message):
    assert served["freshness"] == {"state": "incomplete",
                                   "label_prefix": "Projection unavailable",
                                   "message": message, "verified_at": None}
    assert served["repositories"] == []
    assert served["fleet"] == {"recent_landed": []}
    assert served["attention"] == {"rows": [], "total": 0}


def test_a_v1_body_keeps_every_v1_tab_and_makes_only_the_briefing_unavailable():
    """Server new, daemon old: the console can be running code the resident daemon has not
    picked up (ADR 0051 defers its restart while fleet work is live)."""
    v1 = {"dispatch": {"enabled": True},
          "daemon": {"enabled": True, "last_cycle_at": PUBLISHED_AT, "poll_seconds": 15,
                     "gh_fresh_at": PUBLISHED_AT},
          "pools": [{"tool": "claude", "clear": True, "spent_pct": 10, "headroom_pct": 90,
                     "running": 1, "reason": None}],
          "running": [{"repo": "o/r", "stage": "build"}],
          "repos": [{"repo": "o/r", "profile": "reviewed", "recent_merges": []}]}
    served = _serve(v1)
    assert served["repos"] == v1["repos"] and served["running"] == v1["running"]
    assert served["pools"] == v1["pools"] and served["dispatch"] == v1["dispatch"]
    assert served["daemon"]["gh_fresh_at"] == PUBLISHED_AT, "the header stamp still reads"
    _assert_v2_unavailable(served, _UNREADABLE)


def test_an_unrecognised_or_windowless_v2_body_is_unavailable_too():
    unrecognised = _published(_repository(), schema_version=99)
    windowless = _published(_repository())
    del windowless["heartbeat_seconds"]
    for body in (unrecognised, windowless):
        served = _serve(body)
        assert served["repos"] == body["repos"], "the v1 fields still serve"
        _assert_v2_unavailable(served, _UNREADABLE)


def test_zero_repositories_is_fail_closed_input_never_a_fresh_empty_fleet():
    """The configuration rejects a fleet with no repositories, so a body claiming none has
    been perturbed — a hand edit, a partial write, a truncated file."""
    _assert_v2_unavailable(_serve(_published()), _UNREADABLE)


def test_a_body_whose_repositories_are_the_wrong_shape_is_unavailable():
    _assert_v2_unavailable(_serve(_published("not a repository")), _UNREADABLE)
    _assert_v2_unavailable(_serve(_published({"name_with_owner": "o/r"})), _UNREADABLE)


def test_no_daemon_published_projection_says_that_rather_than_could_not_read():
    served = _serve(None)
    _assert_v2_unavailable(
        served, "No daemon-published projection yet. Open GitHub for authoritative state.")
    assert served["repos"] == [] and served["daemon"]["gh_fresh_at"] is None


def test_a_corrupt_or_truncated_file_reads_as_unavailable_over_http_200(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    client = _client(live.read_snapshot)
    (tmp_path / "snapshot.json").write_text('{"repositories": [{"name_with_ow')
    response = client.get("/api/snapshot")
    assert response.status_code == 200
    _assert_v2_unavailable(
        response.json(),
        "No daemon-published projection yet. Open GitHub for authoritative state.")


# --- daemon carry-forward is narrower than the serving guard --------------------------------

def test_carry_forward_keeps_a_recognised_body_that_predates_the_stamped_window():
    windowless = _published(_repository())
    del windowless["heartbeat_seconds"]
    assert operator_freshness.carry_forward(windowless) == windowless


def test_carry_forward_rejects_an_absent_or_unrecognised_schema_version():
    assert operator_freshness.carry_forward(None) is None
    assert operator_freshness.carry_forward({"repos": []}) is None
    assert operator_freshness.carry_forward(_published(_repository(), schema_version=99)) is None


def test_the_first_upgraded_publish_preserves_a_failed_repository_as_stale(tmp_path, monkeypatch):
    """A daemon that has just been restarted onto this code reads a body its predecessor wrote
    without the window. If that body were discarded, a repository whose read fails on this very
    cycle would be republished as never-loaded instead of stale."""
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    previous = _published(_repository())
    del previous["heartbeat_seconds"]
    live.write_snapshot(previous)

    def failing_read(repo, **kw):
        from agentflow import github as gh
        return gh.MapsRead(maps=(), total_count=0, cost=None, remaining=None,
                           error="the map read failed")

    monkeypatch.setattr("agentflow.github.decision_maps", failing_read)
    published = operator_projection.project(
        [_repo_config("o/r")], previous_snapshot=operator_freshness.carry_forward(previous),
        heartbeat_seconds=300, now=NOW + timedelta(seconds=30))
    assert published["repositories"][0]["github"]["status"] == "stale"
    assert published["repositories"][0]["github"]["fresh_at"] == PUBLISHED_AT


def _repo_config(repo):
    from agentflow.loop import RepoConfig
    return RepoConfig(repo=repo, workdir="/nonexistent/does-not-exist")


# --- daemon publication → the endpoint: failure and recovery --------------------------------

def test_a_failed_read_preserves_its_last_verified_data_then_recovers(tmp_path, monkeypatch):
    """The whole loop through the public surface: a repository reads cleanly, then fails and
    keeps the map data it last verified as stale with its reason, then succeeds again and
    reads fresh with no reason left over."""
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    monkeypatch.setattr(daemon, "FULL_PASS_SECONDS", 300)
    from agentflow import github as gh

    good = gh.MapsRead(
        maps=(gh.MapRow(number=7, title="Map: the briefing", url="https://github.com/o/r/issues/7",
                        updated_at="2026-07-30T09:00:00Z", body="", children=(),
                        children_total=0),),
        total_count=1, cost=5, remaining=4990)
    bad = gh.MapsRead(maps=(), total_count=0, cost=None, remaining=None,
                      error="Something went wrong while executing your query.")
    reads = [good, bad, good]

    monkeypatch.setattr(gh, "decision_maps", lambda repo, **kw: reads.pop(0))
    monkeypatch.setattr(gh, "handoff_pr_links_read",
                        lambda repo, numbers: gh.HandoffLinksRead(links={}, cost=1,
                                                                  remaining=4980))
    monkeypatch.setattr(gh, "list_pipeline_prs", lambda repo, state: [])

    # One reading clock throughout: what changes between the three answers below is the
    # publish, never the time of day.
    client = _client(live.read_snapshot, now=NOW)

    def publish():
        daemon.publish_snapshot([_repo_config("o/r")],
                                produce=lambda repos, dispatch_enabled: {
                                    "dispatch": {"enabled": dispatch_enabled},
                                    "daemon": {}, "pools": [], "running": [],
                                    "repos": [{"repo": "o/r", "profile": "reviewed"}]})
        return client.get("/api/snapshot").json()

    verified = publish()
    assert verified["freshness"]["state"] == "fresh"
    assert verified["repositories"][0]["maps"]["active"][0]["title"] == "Map: the briefing"

    failed = publish()
    component = failed["repositories"][0]
    assert failed["freshness"]["state"] == "stale"
    assert component["github"]["status"] == "stale"
    assert component["github"]["error"] == "Something went wrong while executing your query."
    assert component["maps"]["active"][0]["title"] == "Map: the briefing", (
        "the last verified map data is preserved, not blanked")

    recovered = publish()
    assert recovered["freshness"]["state"] == "fresh"
    assert recovered["repositories"][0]["github"]["error"] is None, "the reason is dropped"


def test_the_endpoint_never_reaches_github(tmp_path, monkeypatch):
    """The serving path stays file-only (ADR 0026): no live query, no graph reconstruction."""
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")

    def explode(*a, **kw):
        raise AssertionError("the web server queried GitHub")

    from agentflow import github as gh
    for name in ("decision_maps", "handoff_pr_links_read", "list_pipeline_prs", "run_gh"):
        if hasattr(gh, name):
            monkeypatch.setattr(gh, name, explode)
    live.write_snapshot(_published(_repository()))
    assert _client(live.read_snapshot).get("/api/snapshot").json()["freshness"]["state"] == "fresh"
