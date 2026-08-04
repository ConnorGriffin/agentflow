# ADR 0036 — Bound the repository and Decision Map projection

- Status: Accepted
- Date: 2026-07-17
- Specializes: [ADR 0035](0035-workflow-engine-read-only-operator-console.md)
- Amended by: [ADR 374](adr-374-graphql-heartbeat-budget.md) — the 42-request/60-point
  heartbeat budget below is superseded by 63 requests/250 points for the nine-repository
  fleet; everything else stands.

## Context

ADR 0035 fixes a read-only fleet → repository → map console over daemon-owned disposable
projections, but leaves the graph join, bounds, failure semantics, and GitHub budget open.
Without those constraints the console could repeat ADR 0026's quota failure or invent a second
map/pipeline source of truth.

## Decision

The daemon projects Decision Maps from GitHub issues labeled `wayfinder:map`. A map's decision
set is exactly its native `subIssues`; native `blockedBy` edges plus assignees determine the
frontier, failing closed on partial data.

A handed-off Build Issue remains an ordinary standalone issue. It carries
`Wayfinder handoff: #<map>` and a native `blockedBy` edge to at least one terminal decision
child. Both facts must agree. The Build Issue joins to pipeline and landed state through native
closing pull-request references; branch-name parsing is diagnostic only. Contextual ADRs are
only explicit links from settled map/ticket text.

The exact product bounds are 5 active and 5 closed maps per repository; 50 children, 20
handoffs, and 12 ADR links per map; 10 dependency edges per child; 5 closing PR attempts and
20 checks per handoff; 10 recent landings per repository and 20 fleet-wide. Every overflow is
explicit and links to GitHub; incomplete dependency data never produces a claimed frontier.

Map reads run only on the daemon's 300-second heartbeat, not the 15-second fast tick. For the
current six repositories they use at most 42 GraphQL requests and 60 reported GraphQL points
per heartbeat, and stop while at least 1,000 points remain for the workflow engine. Failures
preserve the last verified per-repository component with honest timestamps. Fresh is at most
two heartbeats old; older or failed is stale; never-successful is unavailable. There is no live
query fallback or projection history database.

FastAPI continues to expose the atomic file at `GET /api/snapshot`. Schema version 2 contains
per-source freshness, bounded map/ticket/handoff/ADR collections, pipeline and landed evidence,
and explicit totals/truncation. The browser presents this contract and never reconstructs graph
membership or authority.

The complete field contract, evidence, API budget calculation, and capability assumptions are
in [the supporting research](../research/bounded-repository-map-projection.md).

## Alternatives considered

- **Make Build Issues map children.** Rejected: it corrupts the child set Wayfinder uses to
  compute its decision frontier.
- **Infer handoffs from prose, cross-references, or branch names alone.** Rejected: those are
  ambiguous and can silently attach unrelated work.
- **Let the browser fetch map details on demand.** Rejected: cost would scale with tabs and
  failure would couple the console to GitHub again.
- **Fetch every map and all history.** Rejected: GitHub is already the comprehensive browser;
  the console is an operational projection.

## Amendment (2026-08-03)

Reads were walking repositories in config order every heartbeat; once the shared point budget
stopped partway through, the same leading repositories refreshed and the tail never did (#492).
The read order now walks least-recently-fresh first, taken from the previous snapshot's
per-repository `fresh_at`: never-loaded repositories first, then ascending `fresh_at`, ties
broken by config order for a deterministic walk. This guarantees every repository reaches fresh
within `ceil(repository count / repositories read per heartbeat)` heartbeats. The published
`repositories` list is unaffected — it stays in config order, since that is the order the console
renders.

## Consequences

- The handoff workflow gains one body marker and one truthful native dependency edge.
- Historical maps without that pair do not receive inferred pipeline history unless backfilled.
- The daemon needs per-repository partial-refresh state and reported-cost accounting, but no new
  durable project store.
- UI implementation can rely on one bounded, honest-age contract while retaining GitHub deep
  links for overflow and action.
