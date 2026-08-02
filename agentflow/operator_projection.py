"""Schema-version-2 operator projection (ADR 0036) — the daemon-owned bounded Decision Map read,
composed additively alongside the existing v1 snapshot :func:`agentflow.dashboard_data.snapshot`
already produces. The freshness bookkeeping, fleet-wide point-budget stop, and stale-preserve-on-
failure logic below are the pure test surface; :func:`build` is the one impure entry point that
walks the fleet and calls :mod:`agentflow.github`.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agentflow import decision_maps, github
from agentflow.loop import RepoConfig
from agentflow.repo_facts import repo_profile

SCHEMA_VERSION = 2

# Per ADR 0036: at most 60 GraphQL points spent on this refresh per heartbeat, and it stops
# before a query that would leave fewer than 1,000 points for the workflow engine's own reads.
POINT_CEILING = 60
WORKFLOW_FLOOR = 1000

FLEET_RECENT_LANDED_LIMIT = 20


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def freshness(
    *, previous: dict | None, now: datetime, heartbeat_seconds: int,
    attempted: bool, success: bool, error: str | None,
) -> dict:
    """One repository's maps-component freshness stamp (ADR 0036): ``fresh`` only when the
    latest attempt succeeded and the verified read is no older than two heartbeats; ``stale``
    when the latest attempt failed or that window has passed; ``unavailable`` when no read has
    ever succeeded. Always preserves the last verified ``fresh_at`` — a failure never invents
    one, and it never discards a still-honest earlier success."""
    prev = previous or {}
    if success:
        return {"status": "fresh", "attempted_at": _iso(now), "fresh_at": _iso(now),
                "error": None}
    fresh_at = prev.get("fresh_at")
    attempted_at = _iso(now) if attempted else prev.get("attempted_at")
    if fresh_at is None:
        return {"status": "unavailable", "attempted_at": attempted_at, "fresh_at": None,
                "error": error}
    if attempted:  # the latest attempt ran this cycle and failed
        return {"status": "stale", "attempted_at": attempted_at, "fresh_at": fresh_at,
                "error": error}
    parsed = _parse(fresh_at)
    age = (now - parsed).total_seconds() if parsed else float("inf")
    if age <= 2 * heartbeat_seconds:  # not attempted this cycle, but still within the window
        return {"status": "fresh", "attempted_at": attempted_at, "fresh_at": fresh_at,
                "error": None}
    return {"status": "stale", "attempted_at": attempted_at, "fresh_at": fresh_at, "error": error}


def _repo_url(repo: str) -> str:
    return f"https://github.com/{repo}"


def _previous_component(previous_snapshot: dict | None, repo: str) -> dict | None:
    if not previous_snapshot:
        return None
    for entry in previous_snapshot.get("repositories") or []:
        if entry.get("name_with_owner") == repo:
            return entry
    return None


def repository_maps(
    cfg: RepoConfig, *, previous_snapshot: dict | None, now: datetime,
    heartbeat_seconds: int, budget: dict,
    read_maps=None, read_links=None, read_prs=None,
) -> dict:
    """One repository's schema-v2 entry: identity, bounded Decision Maps, and honest
    freshness. Consumes and updates the shared fleet-wide ``budget`` dict (``spent``/
    ``stopped``) in place so repositories walked in order never exceed the per-heartbeat point
    ceiling or dip below the workflow engine's reserved floor (ADR 0036) — once stopped, every
    later repository this cycle preserves its previous component untouched, never an empty one.

    A failed handoff-links or pipeline-PR read degrades only the handoff evidence it feeds
    (each handoff falls back to ``building``); it does not fail the map read that already
    succeeded, since the map's frontier and tickets are the primary fact this projects.

    ``read_*`` default to the live :mod:`agentflow.github` calls, looked up at call time (not
    bound as parameter defaults) so a test can monkeypatch the module and have ``build()``'s
    fleet walk pick it up without threading fakes through every layer."""
    read_maps = read_maps or github.decision_maps
    read_links = read_links or github.handoff_pr_links
    read_prs = read_prs or github.list_pipeline_prs
    prev = _previous_component(previous_snapshot, cfg.repo)
    prev_github = (prev or {}).get("github")
    prev_maps = (prev or {}).get("maps") or {"active": [], "active_total": 0}
    identity = {"name_with_owner": cfg.repo, "url": _repo_url(cfg.repo),
                "profile": repo_profile(cfg.workdir)}

    if budget.get("stopped"):
        fresh = freshness(previous=prev_github, now=now, heartbeat_seconds=heartbeat_seconds,
                          attempted=False, success=False,
                          error="point budget reached this heartbeat")
        return {**identity, "github": fresh, "maps": prev_maps}

    maps_read = read_maps(cfg.repo, limit=decision_maps.ACTIVE_MAP_LIMIT,
                          children_limit=decision_maps.CHILDREN_LIMIT,
                          edges_limit=decision_maps.EDGES_LIMIT)
    if maps_read is None:
        fresh = freshness(previous=prev_github, now=now, heartbeat_seconds=heartbeat_seconds,
                          attempted=True, success=False, error="the map read failed")
        return {**identity, "github": fresh, "maps": prev_maps}

    if maps_read.cost is not None:
        budget["spent"] += maps_read.cost
    if (budget["spent"] >= POINT_CEILING
            or (maps_read.remaining is not None
                and maps_read.remaining - budget["spent"] < WORKFLOW_FLOOR)):
        budget["stopped"] = True

    handoff_numbers: set[int] = set()
    for m in maps_read.maps:
        verified, _overflow = decision_maps.verified_handoffs(m, repo=cfg.repo)
        handoff_numbers.update(h.number for h in verified)

    links = read_links(cfg.repo, sorted(handoff_numbers))
    open_prs = read_prs(cfg.repo, "open")
    merged_prs = read_prs(cfg.repo, "merged")
    component = decision_maps.maps_component(
        maps_read, repo=cfg.repo, handoff_links=links or {},
        open_prs=open_prs or [], merged_prs=merged_prs or [])
    fresh = freshness(previous=prev_github, now=now, heartbeat_seconds=heartbeat_seconds,
                      attempted=True, success=True, error=None)
    return {**identity, "github": fresh, "maps": component}


def project(
    repos: list[RepoConfig], *, previous_snapshot: dict | None, heartbeat_seconds: int,
    now: datetime | None = None,
) -> dict:
    """The schema-version-2 fields this publish cycle contributes, additive alongside the
    existing v1 snapshot (ADR 0036): repository identity and bounded Decision Maps. Composed
    once per publish, daemon-side only — the browser never queries GitHub."""
    now = now or datetime.now(timezone.utc)
    budget = {"spent": 0, "stopped": False}
    repositories = [
        repository_maps(cfg, previous_snapshot=previous_snapshot, now=now,
                        heartbeat_seconds=heartbeat_seconds, budget=budget)
        for cfg in repos
    ]
    return {"schema_version": SCHEMA_VERSION, "generated_at": _iso(now),
            "repositories": repositories}


def fleet_recent_landed(repo_views: list[dict], *, limit: int = FLEET_RECENT_LANDED_LIMIT) -> dict:
    """The fleet-wide recently-landed list (ADR 0036): each repository's already-bounded
    ``recent_merges`` (v1's existing 10-per-repo cap), merged newest-first and capped fleet-
    wide. Pure — every input row already carries the daemon's own read."""
    merged = [
        {"repo": rv.get("repo"), **row}
        for rv in repo_views for row in rv.get("recent_merges") or []
    ]
    merged.sort(key=lambda row: row.get("merged_at") or "", reverse=True)
    return {"recent_landed": merged[:limit]}
