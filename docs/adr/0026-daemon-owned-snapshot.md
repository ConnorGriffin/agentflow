# ADR 0026 — The daemon owns the snapshot: web servers never query GitHub

- Status: Accepted
- Date: 2026-07-13

## Context

[ADR 0023](0023-dashboard-replatform-control-plane.md)'s two clocks made the
console server query GitHub on demand behind a ~15s TTL cache. In practice that
shape exhausted the GitHub GraphQL quota (5,000 points/hr) the first evening it
ran for real:

- One snapshot production is ~36 GraphQL queries (≈6 per enrolled repo × 6
  repos: open PRs, merged PRs, ready issues, two held-label lists, plus
  comments per open agentflow PR).
- A poll-driven 15s cache means one open browser tab sustains ~4 productions a
  minute — **~8,600 queries/hr from a single dashboard**, over the quota alone.
- The stdlib dashboard (8787) had **no cache at all** — every browser poll paid
  full price — and it ran alongside the v2 console (8788), each with an
  independent cache. Burn multiplied per server and per tab.

The exhausted quota then starved the *pipeline itself*: intake couldn't list
issues, recheck couldn't list PRs, and the whole fleet deferred. The
observability layer took down the thing it observes.

The daemon already owns this pattern for live sessions: it writes
`live-sessions.json` atomically and readers treat a missing file as "fleet
idle" ([ADR 0023](0023-dashboard-replatform-control-plane.md) M4).

## Decision

**The GitHub-backed snapshot becomes daemon-produced state, exactly like the
live-session file.** Per cycle (including dormant cycles — dormant is when the
operator is watching), the daemon produces `dashboard_data.snapshot()` and
writes it atomically to a state file next to `live-sessions.json`.

**The web server becomes a pure file reader.** `GET /api/snapshot` serves the
file's contents verbatim; it never runs `gh`. The response contract is
unchanged — same body the cache used to serve, including `gh_fresh_at`.

Freshness is honest, not enforced: with the daemon down, the console serves the
last snapshot and the existing footer "updated" stamp shows the data's real age
(from `gh_fresh_at`). No fallback to live GitHub queries — that would resurrect
the burn exactly when the system is least healthy. A missing file (daemon never
ran) reads as an empty fleet with no freshness stamp, never an error.

**The stdlib dashboard (8787, `server.py` + `static/dashboard.html`) is
retired** rather than retrofitted. Its reason to exist was continuity until v2
parity; with Inbox, Live, Fleet and History landed, keeping an uncached second
consumer is pure quota risk.

## Alternatives considered

- **Longer TTL on the server cache (120s).** Shipped as the same-day hotfix,
  but it's a dial on a broken shape: cost still scales per server and stays
  coupled to browser polling. Rejected as the durable fix.
- **Make one production cheaper (batch GraphQL, drop merged-PR/comment
  reads).** Worth doing someday, but orthogonal: any poll-driven server-side
  producer still scales burn with watchers. Rejected as the primary fix.
- **Fall back to live queries when the file is stale.** Rejected: reintroduces
  unbounded burn precisely during incidents, and makes cost depend on daemon
  health — the failure mode that motivated this ADR.
- **SQLite control-plane store.** Still deferred, as in ADR 0023 — a JSON file
  with atomic writes is enough state for one machine, and this ADR narrows the
  file's writer to one process.

## Consequences

- GitHub cost of observability is **bounded and constant**: ~36 queries per
  daemon cycle (~430/hr at the 300s poll), regardless of how many tabs, servers,
  or operators are watching.
- Snapshot freshness is now the daemon's poll cadence (300s), not the hotfix's
  120s TTL — the console can be up to ~5 minutes behind GitHub. Tunable via
  `AGENTFLOW_POLL_SECONDS` if that reads as slow.
- The console works with the daemon down (last known state, honestly aged) —
  and only with a daemon that has run at least once.
- `SnapshotCache` in `webapp.py` loses its caller and is deleted (deletion
  test, charter). The stdlib dashboard, its launch entry, and its docs
  references go with it.
- Amends [ADR 0023](0023-dashboard-replatform-control-plane.md)'s two-clocks
  model: the browser still polls every few seconds, but the second clock is the
  daemon's cycle, not a server-side gh cache.
