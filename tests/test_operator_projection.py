"""Schema-version-2 operator projection (ADR 0036) — freshness bookkeeping, the fleet-wide
point-budget stop, and stale-preserve-on-failure, exercised through the public entry points."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agentflow import github, operator_projection
from agentflow.loop import RepoConfig

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(repo="o/r") -> RepoConfig:
    return RepoConfig(repo=repo, workdir="/nonexistent/does-not-exist")


def _maps_read(cost=5, remaining=4990) -> github.MapsRead:
    return github.MapsRead(maps=(), total_count=0, cost=cost, remaining=remaining)


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
    result = operator_projection.repository_maps(
        _cfg(), previous_snapshot=None, now=NOW, heartbeat_seconds=300,
        budget={"spent": 0, "stopped": False}, read_maps=lambda repo, **kw: _maps_read(),
        read_links=lambda repo, nums: None, read_prs=lambda repo, state: None)
    assert result["github"]["status"] == "fresh", "the map read itself succeeded"


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


def test_fleet_recent_landed_merges_sorts_and_bounds():
    repo_views = [
        {"repo": "o/a", "recent_merges": [{"number": 1, "merged_at": "2026-07-01T00:00:00Z"}]},
        {"repo": "o/b", "recent_merges": [{"number": 2, "merged_at": "2026-07-15T00:00:00Z"}]},
    ]
    result = operator_projection.fleet_recent_landed(repo_views, limit=1)
    assert result == {"recent_landed": [{"repo": "o/b", "number": 2,
                                         "merged_at": "2026-07-15T00:00:00Z"}]}
