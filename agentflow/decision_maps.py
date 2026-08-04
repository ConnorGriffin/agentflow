"""Decision Map projection shaping (ADR 0036) — turns one repository's typed GitHub read
(:mod:`agentflow.github`'s ``MapRow``/``MapChildRow``/``HandoffCandidateRow``) into the bounded,
honest snapshot shape the operator briefing renders: frontier classification, verified handoff
discovery, and contextual ADR-link scraping. Every function here is pure — the GitHub reads that
feed it are exercised live, through :func:`agentflow.github.decision_maps` and
:func:`agentflow.github.handoff_pr_links_read`.
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


def frontier_state(child_statuses: list[str], *, children_truncated: bool) -> str:
    """Which of the four mutually exclusive things a map's frontier line may say, from its
    children's classifications alone (ADR 0036).

    ``unverified`` whenever any child's blocker data was incomplete — a frontier claimed
    beside data we could not read is the exact falsehood this projection exists to avoid, so
    that answer wins over every other. Then ``named`` when at least one child is open,
    unclaimed and provably unblocked. Otherwise ``none_open`` when every child is settled, and
    ``blocked`` when open children remain but none of them is takeable. A truncated child list
    can still name a frontier it did return, but can never claim that nothing is open or that
    everything is blocked — both are statements about children it never saw.
    """
    if "unknown" in child_statuses:
        return "unverified"
    if "frontier" in child_statuses:
        return "named"
    if children_truncated:
        return "unverified"
    if all(status == "done" for status in child_statuses):
        return "none_open"
    return "blocked"


def handoff_marker(map_number: int) -> str:
    """The exact body marker a handoff Build Issue must carry (ADR 0036)."""
    return f"Wayfinder handoff: #{map_number}"


def verified_handoffs(
    map_row: github.MapRow, *, repo: str, limit: int = HANDOFF_LIMIT,
) -> tuple[list[github.HandoffCandidateRow], int]:
    """Handed-off Build Issues for one map: candidates discovered from a *terminal* (closed)
    decision child's native ``blocking`` edge, kept only when the marker, label namespace, and
    repository all agree (ADR 0036) — the native edge is the machine join, the marker is the
    human ledger, and both must say the same thing.

    Admission runs before deduplication, and deduplication keys on GitHub's node ID: an issue
    number is only unique inside one repository, so keying on it lets a foreign issue that
    happens to share a number decide the fate of a real handoff. Newest-number first, bounded
    to ``limit``, with the count that did not fit."""
    marker = handoff_marker(map_row.number)
    seen: dict[str, github.HandoffCandidateRow] = {}
    for child in map_row.children:
        if classify_child(child) != "done":
            continue
        for candidate in child.handoff_candidates:
            if candidate.repo != repo:
                continue
            if any(label.startswith("wayfinder:") for label in candidate.labels):
                continue
            if marker not in candidate.body:
                continue
            seen.setdefault(candidate.id, candidate)
    ordered = sorted(seen.values(), key=lambda c: c.number, reverse=True)
    return ordered[:limit], max(0, len(ordered) - limit)


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


def _selected_attempt(
    attempts: tuple[github.HandoffAttemptRow, ...],
) -> github.HandoffAttemptRow | None:
    """Which of a handoff's returned closing-PR attempts speaks for it. Newest *merged* by
    merge time wins outright, so a handoff that landed on its second try never reads from its
    abandoned first; otherwise the newest still-open attempt, which is the one in flight; and
    otherwise the newest attempt there is, which at least says something was tried."""
    if not attempts:
        return None
    merged = [a for a in attempts if a.merged_at]
    if merged:
        return max(merged, key=lambda a: (a.merged_at, a.number))
    still_open = [a for a in attempts if (a.state or "").upper() == "OPEN"]
    return max(still_open or list(attempts), key=lambda a: a.number)


def _landed_evidence(
    link: github.HandoffLinkRow | None, *, unavailable: bool,
    open_prs: list[github.PipelinePrRow],
    merged_prs: list[github.PipelinePrRow],
) -> dict:
    """One handoff's pipeline and landed state, decided by its native closing references
    (ADR 0036) and never by a branch name. Those references carry landed state themselves, so
    a pull request that merged before the console's PR listing window still reads as landed;
    the listings only add the review and check verdicts they alone hold.

    A failed reference read is ``unavailable``, which is a different fact from a handoff with
    no closing pull request yet — that one is still ``building``."""
    if unavailable:
        return {"state": "unavailable", "pr_number": None, "pr_url": None}
    chosen = _selected_attempt(link.attempts if link else ())
    if chosen is None:
        return {"state": "building", "pr_number": None, "pr_url": None}
    listing = {p.number: p for p in (*open_prs, *merged_prs)}.get(chosen.number)
    review = _review_verdict(listing.review_decision) if listing else None
    ci = _ci_verdict(listing.ci_rollup) if listing else None
    if chosen.merged_at:
        return {"state": "merged", "pr_number": chosen.number, "pr_url": chosen.url,
                "merged_at": chosen.merged_at,
                "merge_commit": listing.merge_commit_oid if listing else None,
                "review": review, "ci": ci}
    if (chosen.state or "").upper() == "OPEN":
        return {"state": "in_review" if review else "pr_open", "pr_number": chosen.number,
                "pr_url": chosen.url, "review": review, "ci": ci}
    return {"state": "pr_closed", "pr_number": chosen.number, "pr_url": chosen.url,
            "review": review, "ci": ci}


def _child_view(child: github.MapChildRow) -> dict:
    return {"number": child.number, "title": child.title, "url": child.url,
            "status": classify_child(child)}


def _adr_url(url: str, repo: str) -> str:
    """A scraped ADR link made reachable. Map bodies write these both ways — an absolute blob
    URL, or the repository-relative path a file in the repository would use — and a relative
    path is not something the console can open, so it is resolved against the repository the
    map lives in."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return f"https://github.com/{repo}/blob/HEAD/{url[url.index('docs/adr/'):]}"


def _counted(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def map_view(
    map_row: github.MapRow, *, repo: str,
    handoff_links: github.HandoffLinksRead,
    open_prs: list[github.PipelinePrRow],
    merged_prs: list[github.PipelinePrRow],
) -> dict:
    """One active Decision Map, shaped for the snapshot: bounded children, which of the four
    things its frontier line may say, verified handoffs with landed evidence, and its
    supporting records. Pure — every GitHub read has already happened by the time this runs.

    The frontier list is published only when the frontier is actually named. Every other state
    hands the console an empty list, so a presentation layer cannot claim a frontier this
    projection has just refused to claim.

    Supporting records carry every way this projection is smaller than the truth (ADR 0036 —
    every overflow is explicit and links to GitHub). A record meaning *we could not read this*
    says ``incomplete`` and names what was cut; one meaning *there is simply more* carries the
    count that did not fit. All of them land on GitHub, which is where the rest actually is."""
    handoffs, handoffs_overflow = verified_handoffs(map_row, repo=repo)
    adrs, adrs_overflow = adr_links(map_row.body)
    children_view = [_child_view(c) for c in map_row.children]
    statuses = [c["status"] for c in children_view]
    children_overflow = max(0, map_row.children_total - len(children_view))
    state = frontier_state(statuses, children_truncated=bool(children_overflow))
    unknown_children = statuses.count("unknown")
    # Only settled decisions are searched for handoffs, so only their truncated outgoing links
    # can hide one — and hiding a handoff says nothing about the frontier, which reads the
    # other edge direction entirely.
    cut_handoff_edges = sum(1 for c in map_row.children if classify_child(c) == "done"
                            and c.handoff_edges_total > len(c.handoff_candidates))
    closed = sum(1 for c in map_row.children if (c.state or "").upper() == "CLOSED")
    support = [{"label": a["label"], "url": _adr_url(a["url"], repo)} for a in adrs]
    for present, label in (
        (children_overflow,
         f"incomplete — {_counted(children_overflow, 'more decision')} on GitHub"),
        (unknown_children,
         f"incomplete — blocker data on {_counted(unknown_children, 'decision')}"),
        (cut_handoff_edges,
         f"incomplete — handoff links on {_counted(cut_handoff_edges, 'settled decision')}"),
        (handoffs_overflow, f"{_counted(handoffs_overflow, 'more handoff')} on GitHub"),
        (adrs_overflow, f"{_counted(adrs_overflow, 'more decision record')} on GitHub"),
    ):
        if present:
            support.append({"label": label, "url": map_row.url})
    handoff_views = [
        {"number": h.number, "title": h.title, "url": h.url,
         "pipeline": _landed_evidence(handoff_links.links.get(h.number),
                                      unavailable=bool(handoff_links.error),
                                      open_prs=open_prs, merged_prs=merged_prs),
         "attempt_count": (handoff_links.links.get(h.number).attempt_count
                           if h.number in handoff_links.links else 0)}
        for h in handoffs
    ]
    return {
        "number": map_row.number, "title": map_row.title, "url": map_row.url,
        "updated_at": map_row.updated_at,
        "progress": {"total": map_row.children_total, "closed": closed},
        "frontier_state": state,
        "frontier": [c for c in children_view if c["status"] == "frontier"]
        if state == "named" else [],
        "frontier_incomplete": bool(children_overflow or unknown_children),
        "handoffs_incomplete": bool(cut_handoff_edges),
        "tickets": children_view,
        "handoffs": handoff_views,
        "totals": {
            "children": {"shown": len(children_view), "total": map_row.children_total},
            "handoffs": {"shown": len(handoff_views),
                         "total": len(handoff_views) + handoffs_overflow},
            "adrs": {"shown": len(adrs), "total": len(adrs) + adrs_overflow},
        },
        "support": support,
    }


def maps_component(
    maps_read: github.MapsRead, *, repo: str,
    handoff_links: github.HandoffLinksRead,
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
