"""Schema-version-2 operator projection (ADR 0036) — the daemon-owned bounded Decision Map read,
composed additively alongside the existing v1 snapshot :func:`agentflow.dashboard_data.snapshot`
already produces. The freshness bookkeeping, fleet-wide point-budget stop, and stale-preserve-on-
failure logic below are the pure test surface; :func:`project` is the one impure entry point that
walks the fleet and calls :mod:`agentflow.github`. :func:`decision_map_probe` runs that same set
of reads once, for one repository, and prints what GitHub answered — the read-only evidence
behind a claim about what the refresh costs (ADR 374).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from agentflow import decision_maps, github
from agentflow.loop import RepoConfig
from agentflow.repo_facts import repo_profile

SCHEMA_VERSION = 2

# Per ADR 374 (raising ADR 0036's original 60): at most 250 GraphQL points spent on this refresh
# per heartbeat — enough for all nine enrolled repositories to refresh in one heartbeat, which is
# what keeps every one of them inside the two-heartbeat freshness window. The refresh also stops
# once GitHub's own reported remaining falls under the 1,000 points reserved for the workflow
# engine's reads. That remaining is GitHub's post-spend figure, so it is read as-is: subtracting
# what this refresh has already spent would count the same points twice and stop the fleet early.
POINT_CEILING = 250
WORKFLOW_FLOOR = 1000

FLEET_RECENT_LANDED_LIMIT = 20

ATTENTION_LIMIT = 25


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
    read_links = read_links or github.handoff_pr_links_read
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
    if maps_read.cost is not None:
        budget["spent"] += maps_read.cost
    if (budget["spent"] >= POINT_CEILING
            or (maps_read.remaining is not None and maps_read.remaining < WORKFLOW_FLOOR)):
        budget["stopped"] = True
    if maps_read.error:
        fresh = freshness(previous=prev_github, now=now, heartbeat_seconds=heartbeat_seconds,
                          attempted=True, success=False, error=maps_read.error)
        return {**identity, "github": fresh, "maps": prev_maps}

    handoff_numbers: set[int] = set()
    for m in maps_read.maps:
        verified, _overflow = decision_maps.verified_handoffs(m, repo=cfg.repo)
        handoff_numbers.update(h.number for h in verified)

    links_read = read_links(cfg.repo, sorted(handoff_numbers))
    if isinstance(links_read, github.HandoffLinksRead):
        if links_read.cost is not None:
            budget["spent"] += links_read.cost
        if (budget["spent"] >= POINT_CEILING
                or (links_read.remaining is not None
                    and links_read.remaining < WORKFLOW_FLOOR)):
            budget["stopped"] = True
        if links_read.error:
            fresh = freshness(previous=prev_github, now=now, heartbeat_seconds=heartbeat_seconds,
                              attempted=True, success=False, error=links_read.error)
            return {**identity, "github": fresh, "maps": prev_maps}
        links = links_read.links
    else:
        # Test and alternate adapters may provide the legacy typed mapping directly.
        links = links_read
    # The pipeline PR listings are read per map, so a repository with none has nothing to
    # spend them on — the shaping step below would discard both lists unlooked-at (#497).
    open_prs = read_prs(cfg.repo, "open") if maps_read.maps else []
    merged_prs = read_prs(cfg.repo, "merged") if maps_read.maps else []
    component = decision_maps.maps_component(
        maps_read, repo=cfg.repo, handoff_links=links or {},
        open_prs=open_prs or [], merged_prs=merged_prs or [])
    fresh = freshness(previous=prev_github, now=now, heartbeat_seconds=heartbeat_seconds,
                      attempted=True, success=True, error=None)
    return {**identity, "github": fresh, "maps": component}


def _walk_order(repos: list[RepoConfig], *, previous_snapshot: dict | None) -> list[RepoConfig]:
    """The order the fleet is *read* in, least-recently-fresh first, so a shared per-heartbeat
    budget stop (ADR 0036) never starves the same tail of the config list forever: a repository
    that has never loaded sorts before one that has, and among those that have, the oldest
    ``fresh_at`` sorts first. Ties — including every never-loaded repository — keep the config's
    own order, so the walk is deterministic run to run. This governs only the read order; the
    published ``repositories`` list stays in config order regardless (:func:`project`)."""
    def key(indexed: tuple[int, RepoConfig]) -> tuple[bool, str, int]:
        index, cfg = indexed
        prev_github = (_previous_component(previous_snapshot, cfg.repo) or {}).get("github") or {}
        fresh_at = prev_github.get("fresh_at")
        return (fresh_at is not None, fresh_at or "", index)

    ordered = sorted(enumerate(repos), key=key)
    return [cfg for _index, cfg in ordered]


def project(
    repos: list[RepoConfig], *, previous_snapshot: dict | None, heartbeat_seconds: int,
    now: datetime | None = None,
) -> dict:
    """The schema-version-2 fields this publish cycle contributes, additive alongside the
    existing v1 snapshot (ADR 0036): repository identity and bounded Decision Maps. Composed
    once per publish, daemon-side only — the browser never queries GitHub.

    Reads walk least-recently-fresh first (:func:`_walk_order`) so a shared point budget that
    stops partway through never starves the same repositories every heartbeat; the published
    ``repositories`` list is re-sorted back to config order, since that is the order the console
    renders (#492)."""
    now = now or datetime.now(timezone.utc)
    budget = {"spent": 0, "stopped": False}
    by_repo = {
        cfg.repo: repository_maps(cfg, previous_snapshot=previous_snapshot, now=now,
                                  heartbeat_seconds=heartbeat_seconds, budget=budget)
        for cfg in _walk_order(repos, previous_snapshot=previous_snapshot)
    }
    repositories = [by_repo[cfg.repo] for cfg in repos]
    return {"schema_version": SCHEMA_VERSION, "generated_at": _iso(now),
            "repositories": repositories}


def _probe_section(title: str, read: github.MapQueryRead) -> list[str]:
    reported = "unreported" if read.cost is None else f"{read.cost} points"
    remaining = "" if read.remaining is None else f", {read.remaining} remaining"
    lines = [title, f"cost: {reported}{remaining}"]
    if read.error:
        lines.append(f"error: {read.error}")
    lines.append(json.dumps(read.raw, indent=2, sort_keys=True))
    return lines


def decision_map_probe(repo: str) -> str:
    """The evidence behind ``agentflow decision-map-probe``: the Decision Map reads the daemon
    makes for one repository every heartbeat, run once against ``repo`` and printed whole —
    GitHub's raw answers, the points each one reported, and their combined cost.

    Read-only by construction: it issues exactly the queries :mod:`agentflow.github` already
    builds for the refresh, at the same bounds, and never touches a write command. It also makes
    the same choices the refresh makes — no map detail for a repository that has no maps, no
    handoff join where the maps verify no handoff — so what it prints is what a heartbeat costs,
    not what a heartbeat could cost."""
    maps_read, evidence = github.decision_maps_with_evidence(
        repo, limit=decision_maps.ACTIVE_MAP_LIMIT,
        children_limit=decision_maps.CHILDREN_LIMIT, edges_limit=decision_maps.EDGES_LIMIT)
    handoffs: set[int] = set()
    for m in maps_read.maps:
        verified, _overflow = decision_maps.verified_handoffs(m, repo=repo)
        handoffs.update(h.number for h in verified)

    reads = [(f"map read {index} of {len(evidence)}", response)
             for index, response in enumerate(evidence, start=1)]
    if handoffs:
        reads.append((f"handoff join — closing pull requests for {sorted(handoffs)}",
                      github.handoff_pr_links_response(repo, sorted(handoffs))))

    lines = [f"decision map probe — {repo} (read-only)", ""]
    for title, response in reads:
        lines += _probe_section(title, response)
        lines.append("")
    if not handoffs:
        lines += ["handoff join — not issued: these maps verify no handoff, so the daemon asks "
                  "nothing here either", ""]
    total = sum(response.cost or 0 for _title, response in reads)
    lines.append(f"combined cost: {total} points across {len(reads)} requests")
    return "\n".join(lines)


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


# --- the operator's attention queue (#373) -------------------------------------------------
# The ruled conditions, in their contract priority order. The kind label on each row is the
# locked surface's own; `Projection` is the kind its locked incomplete state already uses.
# A stage in drought leads (#430): a stage that has silently stopped producing anything is
# broken in a way none of the individual items it starves can show, so it outranks all of them.
_CONDITIONS = ("stage-drought", "awaiting-merge", "waiting-on-reply", "parked-build",
               "ready-to-loosen", "stale-data")

# Condition 1's sub-kind is the repository profile alone — the same-tool summary that reads
# "maintainer merge required" is the ordinary fallback when the cross-tool pool has no headroom,
# so its wording never discriminates a condition; the profile does.
_PROFILE_ORDER = {"guarded": 0, "reviewed": 1, "autonomous": 2}
_HELD_ORDER = {"needs-grilling": 0, "needs-mockup": 1}

# Why a parked build is waiting, in the operator's own terms. `open-question` is a comment
# classification, not a pipeline park — an unanswered comment of yours on an otherwise
# merge-ready PR lands here, and saying a question "stopped the build" would be false.
# `drop-to-reviewed` is the catch-all that also receives unresolved review actions, non-clean
# verdicts, red checks and budget exhaustion, so it names the stop, not a wish to drop autonomy;
# the park comment on the PR states the specific reason.
_PARK_REASON = {
    "open-question": "your comment on this PR has not been answered",
    "failed-merge": "the merge failed — it needs a retry or a fix",
    "ui-evidence": "a UI change with no screenshot evidence",
    "drop-to-reviewed": "the pipeline stopped and left a decision on the PR",
}

# Why a repository's briefing data is not current — the cause the freshness stamp already
# recorded, so a row never implies GitHub is at fault when the daemon's own bounded refresh is.
_BUDGET_STOP = "point budget reached this heartbeat"
_FRESHNESS_REASON = {
    _BUDGET_STOP: "the daemon's own bounded refresh did not reach this repository this cycle",
}
# Every other recorded cause is a read that failed, and the stamp now carries GitHub's own words
# for it (ADR 374). Those words are evidence for whoever reads the snapshot, not copy for this
# row: the queue says the same one sentence it always has, whatever GitHub called the failure.
_READ_FAILED_REASON = "the last read of this repository failed"
_FRESHNESS_FALLBACK = {
    "stale": "this repository has not refreshed within two heartbeats",
    "unavailable": "no read of this repository has ever completed",
}


def _short(repo: str) -> str:
    return repo.rsplit("/", 1)[-1]


def attention(repo_views: list[dict], repositories: list[dict], *,
              stage_health: list[dict] | None = None, limit: int = ATTENTION_LIMIT) -> dict:
    """The whole operator attention queue: the six ruled conditions, each underlying thing
    exactly once, in one total order, bounded — plus the true total the bound was taken from.

    Pure, and the only place the queue is decided (#373). Every fact is one the daemon has
    already read: the v1 repository views carry each open PR's hand-off stamp and park
    classification, the held issues and the trust ratchet; the schema-v2 entries carry the
    freshness stamp; ``stage_health`` carries each pipeline stage's production over its last
    window of finished attempts. The page renders these rows as given — it ranks, filters and
    collapses nothing, and shows the overflow as the difference between the total and what it
    was handed.

    A row's clock is least-recently-touched-first where one exists (the hand-off stamp for a
    merge, the existing ``since`` for a held issue or a park); conditions with no clock fall
    straight through to repository name. Repository name then item number is the tiebreak
    throughout, so the order never depends on which order repositories happened to be walked."""
    rows: list[tuple[tuple, dict]] = []

    def add(priority: int, sub: int, clock: str | None, repo: str, number: int | None,
            *, kind: str, title: str, detail: str, url: str | None,
            note: str | None = None) -> None:
        rows.append(((priority, sub, clock or "", repo.lower(), repo, number or 0),
                     {"condition": _CONDITIONS[priority], "kind": kind, "repo": repo,
                      "number": number, "title": title, "detail": detail, "url": url,
                      "note": note}))

    # A stage that spent sessions and produced nothing across a whole window of finished
    # attempts (#430). It is the only condition with no GitHub object behind it — a stage is
    # not a thing you can open — so it carries a plain `note` saying where to look instead of
    # a URL, and the page renders that as text rather than as an action. A stage still inside
    # its first window raises nothing: absence is the signal, so it has to be provable.
    for stage in stage_health or []:
        if stage["attempts"] < stage["window"] or stage["produced"]:
            continue
        add(0, 0, None, "", None, kind="stage drought", title=stage["stage"],
            detail=(f"0 of its last {stage['window']} finished attempts "
                    f"{stage['produces']} · {stage['sessions_spent']} sessions spent"),
            url=None, note=f"What to check: {stage['check']}")

    for view in repo_views or []:
        repo = view.get("repo") or ""
        profile = view.get("profile") or ""
        # One operator action per underlying thing: a parked PR is a parked build, never also
        # an awaiting-merge row, however finished the engine's own summary says it is.
        parked_numbers = {p.get("number") for p in view.get("parked") or []}

        for pr in view.get("in_flight") or []:
            handed_off_at = pr.get("handed_off_at")
            if handed_off_at is None or pr.get("number") in parked_numbers:
                continue
            add(1, _PROFILE_ORDER.get(profile, len(_PROFILE_ORDER)), handed_off_at,
                repo, pr.get("number"), kind="Merge",
                title=f"#{pr.get('number')} {pr.get('title')}",
                detail=f"{_short(repo)} · {profile} · the review finished — yours to merge",
                url=github.pr_url(repo, pr.get("number")))

        for held in view.get("held") or []:
            state = held.get("state") or ""
            add(2, _HELD_ORDER.get(state, len(_HELD_ORDER)), held.get("since"),
                repo, held.get("number"), kind="Held",
                title=f"#{held.get('number')} {held.get('title')}",
                detail=f"{_short(repo)} · {state.replace('-', ' ')} · {held.get('reason') or ''}",
                url=f"https://github.com/{repo}/issues/{held.get('number')}")

        for park in view.get("parked") or []:
            reason = park.get("reason") or ""
            add(3, 0, park.get("since"), repo, park.get("number"), kind="Parked",
                title=f"#{park.get('number')} {park.get('title')}",
                detail=f"{_short(repo)} · {_PARK_REASON.get(reason, reason)}",
                url=github.pr_url(repo, park.get("number")))

        ratchet = view.get("ratchet") or {}
        # A repository at the top of the ladder has nothing left to loosen, so it never prompts.
        if ratchet.get("ready_to_loosen") and profile != "autonomous":
            samples = ratchet.get("samples") or 0
            rate = round((ratchet.get("correction_rate") or 0) * 100)
            add(4, 0, None, repo, None, kind="Trust",
                title=f"{_short(repo)} is ready to loosen",
                detail=f"{samples} decisions · {rate}% corrected", url=_repo_url(repo))

    for entry in repositories or []:
        status = (entry.get("github") or {}).get("status")
        if status not in _FRESHNESS_FALLBACK:
            continue
        repo = entry.get("name_with_owner") or ""
        error = (entry.get("github") or {}).get("error")
        add(5, 0, None, repo, None, kind="Projection",
            title=(f"{_short(repo)} briefing data is stale" if status == "stale"
                   else f"{_short(repo)} briefing data has never loaded"),
            detail=(_FRESHNESS_REASON.get(error)
                    or (_READ_FAILED_REASON if error else _FRESHNESS_FALLBACK[status])),
            url=_repo_url(repo))

    rows.sort(key=lambda row: row[0])
    return {"rows": [row for _key, row in rows[:limit]], "total": len(rows)}
