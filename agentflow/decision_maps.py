"""Decision Map projection shaping (ADR 0036) — turns one repository's typed GitHub read
(:mod:`agentflow.github`'s ``MapRow``/``MapChildRow``/``HandoffCandidateRow``) into the bounded,
honest snapshot shape the operator briefing renders: frontier classification, verified handoff
discovery, and contextual ADR-link scraping. Every function here is pure — the GitHub reads that
feed it are exercised live, through :func:`agentflow.github.decision_maps` and
:func:`agentflow.github.handoff_pr_links`.
"""

from __future__ import annotations

import re

from agentflow import github

ACTIVE_MAP_LIMIT = 5
CHILDREN_LIMIT = 50
EDGES_LIMIT = 10
HANDOFF_LIMIT = 20
ADR_LIMIT = 12

_ADR_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]*docs/adr/([\w-]+)\.md[^)\s]*)\)")
_DECISIONS_SECTION = re.compile(r"##\s*Decisions so far\b(.*?)(?=\n##\s|\Z)", re.S | re.I)


def classify_child(child: github.MapChildRow) -> str:
    """One decision child's frontier status — the exact ADR 0036 rule.

    ``done`` for a closed child; ``unknown`` when its blocker edges were truncated (missing
    dependency data never produces a claimed frontier); ``blocked`` when at least one returned
    blocker is still open; ``claimed`` when assigned with no open blocker; otherwise
    ``frontier``.
    """
    if (child.state or "").upper() == "CLOSED":
        return "done"
    seen = child.blocked_by_open + child.blocked_by_closed
    if child.blocked_by_total > seen:
        return "unknown"
    if child.blocked_by_open > 0:
        return "blocked"
    if child.assigned:
        return "claimed"
    return "frontier"


def handoff_marker(map_number: int) -> str:
    """The exact body marker a handoff Build Issue must carry (ADR 0036)."""
    return f"Wayfinder handoff: #{map_number}"


def verified_handoffs(
    map_row: github.MapRow, *, repo: str, limit: int = HANDOFF_LIMIT,
) -> tuple[list[github.HandoffCandidateRow], bool]:
    """Handed-off Build Issues for one map: candidates discovered from a *terminal* (closed)
    decision child's native ``blocking`` edge, kept only when the marker, label namespace, and
    repository all agree (ADR 0036) — the native edge is the machine join, the marker is the
    human ledger, and both must say the same thing. Deduplicated by issue number, newest-number
    first, bounded to ``limit`` with an explicit overflow flag."""
    marker = handoff_marker(map_row.number)
    seen: dict[int, github.HandoffCandidateRow] = {}
    for child in map_row.children:
        if classify_child(child) != "done":
            continue
        for candidate in child.handoff_candidates:
            if candidate.number in seen:
                continue
            if candidate.repo != repo:
                continue
            if any(label.startswith("wayfinder:") for label in candidate.labels):
                continue
            if marker not in candidate.body:
                continue
            seen[candidate.number] = candidate
    ordered = sorted(seen.values(), key=lambda c: c.number, reverse=True)
    return ordered[:limit], len(ordered) > limit


def adr_links(body: str, *, limit: int = ADR_LIMIT) -> tuple[list[dict], int]:
    """Contextual ADR links from a map's ``## Decisions so far`` section only — never the
    whole body or a repository scan (ADR 0036). Deduplicated by URL, in document order,
    bounded with an explicit overflow count."""
    section = _DECISIONS_SECTION.search(body or "")
    text = section.group(1) if section else ""
    seen: dict[str, dict] = {}
    for m in _ADR_LINK.finditer(text):
        label, url = m.group(1), m.group(2)
        if url not in seen:
            seen[url] = {"label": label, "url": url}
    links = list(seen.values())
    return links[:limit], max(0, len(links) - limit)


# The review/CI verdict shaping mirrors `dashboard_data._review_verdict`/`_ci_verdict` exactly.
# Duplicated rather than imported: `dashboard_data` composes the v1 snapshot that
# `operator_projection` (this module's only caller) runs alongside, and importing back would
# create a cycle for two five-line pure mappings.
def _review_verdict(decision) -> str | None:
    d = (decision or "").upper()
    if d == "APPROVED":
        return "approved"
    if d == "CHANGES_REQUESTED":
        return "changes_requested"
    return None


def _ci_verdict(rollup) -> str | None:
    checks = rollup or []
    if not checks:
        return None
    states = [(c.get("conclusion") or c.get("state") or "").upper() for c in checks]
    if any(s in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED") for s in states):
        return "failing"
    if any(s in ("", "PENDING", "IN_PROGRESS", "QUEUED", "EXPECTED", "WAITING") for s in states):
        return "pending"
    return "passing"


def _pipeline_for(
    pr_numbers: tuple[int, ...],
    open_prs: list[github.PipelinePrRow],
    merged_prs: list[github.PipelinePrRow],
) -> dict:
    """The pipeline state for one handoff, from the PR numbers its closing references named —
    merged wins, else the newest open attempt, else still building. The same merged-wins-over-
    open rule as `dashboard_data.pipeline_state`, joined by number (ADR 0036's native closing
    reference) instead of by branch name."""
    by_number = {p.number: p for p in (*open_prs, *merged_prs)}
    matches = [by_number[n] for n in pr_numbers if n in by_number]
    merged = next((p for p in matches if p.merged_at), None)
    if merged is not None:
        return {"state": "merged", "pr_number": merged.number, "pr_url": merged.url,
                "merged_at": merged.merged_at, "merge_commit": merged.merge_commit_oid,
                "review": _review_verdict(merged.review_decision),
                "ci": _ci_verdict(merged.ci_rollup)}
    openpr = max((p for p in matches if not p.merged_at), key=lambda p: p.number, default=None)
    if openpr is not None:
        review = _review_verdict(openpr.review_decision)
        return {"state": "in_review" if review else "pr_open", "pr_number": openpr.number,
                "pr_url": openpr.url, "review": review, "ci": _ci_verdict(openpr.ci_rollup)}
    return {"state": "building", "pr_number": None, "pr_url": None}


def _child_view(child: github.MapChildRow) -> dict:
    return {"number": child.number, "title": child.title, "url": child.url,
            "status": classify_child(child)}


def map_view(
    map_row: github.MapRow, *, repo: str,
    handoff_links: dict[int, github.HandoffLinkRow],
    open_prs: list[github.PipelinePrRow],
    merged_prs: list[github.PipelinePrRow],
) -> dict:
    """One active Decision Map, shaped for the snapshot: bounded children, the current
    frontier, verified handoffs with pipeline evidence, and contextual ADR links. Pure —
    every GitHub read has already happened by the time this runs."""
    handoffs, handoffs_overflow = verified_handoffs(map_row, repo=repo)
    adrs, adrs_overflow = adr_links(map_row.body)
    children_view = [_child_view(c) for c in map_row.children]
    frontier = [c for c in children_view if c["status"] == "frontier"]
    closed = sum(1 for c in map_row.children if (c.state or "").upper() == "CLOSED")
    handoff_views = []
    for h in handoffs:
        link = handoff_links.get(h.number)
        pipeline = _pipeline_for(link.pr_numbers if link else (), open_prs, merged_prs)
        handoff_views.append({"number": h.number, "title": h.title, "url": h.url,
                              "pipeline": pipeline,
                              "attempt_count": link.attempt_count if link else 0})
    return {
        "number": map_row.number, "title": map_row.title, "url": map_row.url,
        "updated_at": map_row.updated_at,
        "complete": map_row.children_total <= len(map_row.children),
        "progress": {"total": map_row.children_total, "closed": closed},
        "frontier": frontier,
        "tickets": children_view,
        "handoffs": handoff_views, "handoffs_overflow": handoffs_overflow,
        "adrs": adrs, "adrs_overflow": adrs_overflow,
    }


def maps_component(
    maps_read: github.MapsRead, *, repo: str,
    handoff_links: dict[int, github.HandoffLinkRow],
    open_prs: list[github.PipelinePrRow],
    merged_prs: list[github.PipelinePrRow],
) -> dict:
    """The repository's whole ``maps`` snapshot field: every active map, shaped, plus GitHub's
    own total for explicit overflow (ADR 0036's active-map bound)."""
    active = [
        map_view(m, repo=repo, handoff_links=handoff_links, open_prs=open_prs,
                merged_prs=merged_prs)
        for m in maps_read.maps
    ]
    return {"active": active, "active_total": maps_read.total_count}
