# ADR 374 — Raise the Decision Map heartbeat budget to 63 requests / 250 points

- Status: Accepted
- Date: 2026-08-04
- Amends: [ADR 0036](0036-bounded-repository-map-projection.md) (its budget numbers only; the
  product bounds, join, freshness, and failure semantics stand unchanged)

## Context

[ADR 0036](0036-bounded-repository-map-projection.md) budgeted the Decision Map projection at
"at most 42 GraphQL requests and 60 reported GraphQL points per heartbeat", stopping "while at
least 1,000 points remain for the workflow engine", sized against the six repositories enrolled
at the time. It set that ceiling alongside product bounds of 5 active maps per repository, 50
children per map, and 10 dependency edges per child.

GitHub does not charge what a query returns; it charges what a query *could* return, by its
pre-execution formula — maximum requested node count divided by 100. Under that formula the
map read costs roughly **25 points per repository**, and stays there even with label and
blocking nesting stripped out of the query: the cost is set by the requested first/last
arguments the product bounds require, not by the data actually present. The fleet is now
**nine enrolled repositories**, so one full refresh is about **225 points** and 63 requests
(7 per repository, ADR 0036's own per-repository request shape).

The 60-point ceiling therefore cannot hold. Nor can it be met by refreshing a subset each
heartbeat: at 60 points only two repositories fit, the walk takes five heartbeats to come
around, and ADR 0036's own contract — fresh is at most two heartbeats old — would report seven
of nine repositories stale at any moment.

## Decision

**The Decision Map projection's ceiling is 63 GraphQL requests and 250 reported GraphQL points
per 300-second heartbeat**, sized to refresh all nine repositories in a single heartbeat. ADR
0036's product bounds are unchanged: 5 active maps, 50 children, 10 `blockedBy` edges per child.

The amended arithmetic: 12 heartbeats per hour × 250 points = **3,000 points per hour** against
GitHub's 5,000-point hourly GraphQL budget, leaving 2,000 per hour. That still clears ADR
0036's 1,000-point workflow-engine reserve with 1,000 points of headroom. Everything else in
ADR 0036 — the least-recently-fresh read order, the fail-closed frontier, the two-heartbeat
freshness window, the stop-on-reserve rule — is untouched.

## Alternatives considered

- **Cut the product bounds to fit 60 points.** Rejected: the per-repository cost is driven by
  requested node count, so fitting the ceiling means asking for fewer children — and a real
  50-child map would render truncated. The console would be cheap and wrong.
- **A demand-driven two-stage probe** — a cheap query for changed maps, then a detailed read
  only for those. Rejected: it buys points back at the cost of variable per-repository
  freshness and a second query path to keep honest. The flat raise fits inside the existing
  budget with room to spare; simplicity and uniform freshness are worth more than the points.

## Consequences

- Hourly GraphQL consumption roughly triples, from about 720 points per hour to 3,000, and the
  reserve check rather than the ceiling becomes the operative brake in an outage.
- Enrolling repositories beyond roughly ten will exceed 250 points per heartbeat; at that point
  the walk goes partial again under ADR 0036's least-recently-fresh order, and the
  two-heartbeat freshness window is what breaks first. That is the signal to revisit this ADR.
- The cost model is now explicit: budget the projection by requested node count, not by
  observed response size, so a bounds change is a budget change.
