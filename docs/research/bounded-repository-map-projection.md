# Bounded repository and Decision Map projection

_Research date: 2026-07-17. Scope: Wayfinder [#181](https://github.com/ConnorGriffin/agentflow/issues/181). ADR 0035 is a fixed input._

## Answer

The daemon should publish one disposable, versioned fleet snapshot. For each enrolled
repository it should project a small set of active and recently closed Decision Maps from
GitHub's native issue graph, join explicitly handed-off ordinary Build Issues to their
closing pull requests, and expose only links to contextual ADRs. The web app continues to
read `GET /api/snapshot`; it never queries GitHub, scans git, writes state, or falls back to a
live read.

The missing reliable seam is **map to handoff**. A handoff Build Issue must remain outside
the map's `subIssues` set, but it must carry both:

1. `Wayfinder handoff: #<map>` in its body; and
2. a native `blockedBy` edge to at least one terminal decision child of that map.

The map's `## Handoffs` section remains the human-readable ledger. The projection accepts a
Build Issue as a handoff only when the marker and native edge agree. This preserves
Wayfinder's exact decision-child set while using the dependency graph as the machine join.
The edge is also truthful before handoff: the Build Issue cannot be dispatchable until the
decision closes. An ordinary issue with no `wayfinder:*` label then enters normal intake as
ADR 0027 requires.

## Sources and verified constraints

- GitHub sub-issues are an explicit parent/child hierarchy and expose progress; GitHub
  currently allows up to 100 children and eight nesting levels. Agentflow uses only one
  level. [GitHub: Adding sub-issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues)
- GitHub issue dependencies explicitly represent `blocked by` and `blocking`, and are
  available through the API. [GitHub: Creating issue dependencies](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies),
  [REST dependency endpoints](https://docs.github.com/en/rest/issues/issue-dependencies)
- GraphQL connections require `first` or `last`, accept 1–100, and signal additional pages
  with `pageInfo`. The projection must never silently mistake the first page for the whole
  graph. [GitHub GraphQL pagination](https://docs.github.com/en/graphql/guides/using-pagination-in-the-graphql-api)
- The authenticated GitHub.com GraphQL limit is normally 5,000 points/hour per user; queries
  can return their actual `rateLimit.cost`. GitHub also imposes a 500,000-node call limit and
  may return partial data or timeouts for expensive nesting. [GitHub GraphQL rate and query
  limits](https://docs.github.com/en/graphql/overview/rate-limits-and-query-limits-for-the-graphql-api)
- A pull request can be linked to and close an issue with a closing keyword. The live
  GraphQL schema exposes `closedByPullRequestsReferences(includeClosedPrs: true)`, so the
  join does not need to infer identity from an agentflow branch name. [GitHub: Linking a pull
  request to an issue](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/linking-a-pull-request-to-an-issue),
  [GraphQL Issue reference](https://docs.github.com/en/graphql/reference/objects#issue)
- GitHub recommends avoiding polling, serializing API work to avoid secondary limits, using
  conditional REST requests where appropriate, and honoring `retry-after`/rate-reset rather
  than retrying aggressively. [GitHub REST API best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
- The current daemon has a 15-second fast change probe and a 300-second heartbeat, and is the
  sole atomic snapshot writer. FastAPI reads the last file and returns a stable empty shape
  before the first publish. Failed production already leaves the previous snapshot in place.
  [`daemon.py`](../../agentflow/daemon.py), [`live.py`](../../agentflow/live.py),
  [`webapp.py`](../../agentflow/webapp.py), [ADR 0026](../adr/0026-daemon-owned-snapshot.md)
- Today the dashboard makes many independent `gh` calls, identifies an issue from the
  `agentflow/<tool>/issue-<N>` branch, and keeps 10 recent merges per repository. The build
  prompt already requires `Closes #N`, so a native closing-reference join is compatible with
  the pipeline and strictly less heuristic. [`dashboard_data.py`](../../agentflow/dashboard_data.py),
  [`loop.py`](../../agentflow/loop.py)

Live verification on 2026-07-17 used GitHub CLI 2.95.0 and the GitHub.com GraphQL schema.
`Issue` exposed `subIssues`, `parent`, `blockedBy`, `blocking`,
`closedByPullRequestsReferences`, and `timelineItems`. A representative schema query cost one
point. These observations constrain the initial supported environment; they are not a promise
that GitHub will never change the schema.

## Exact projection contract

### Identity and graph

- **Repository:** one configured `RepoConfig`; repository `id`, `nameWithOwner`, URL, default
  branch, profile, and local daemon state.
- **Decision Map:** an issue with exactly the `wayfinder:map` label. Active means issue state
  `OPEN`; history means recently closed. Its decision set is exactly `subIssues` in GitHub's
  returned order.
- **Frontier ticket:** an open child with no assignee and every `blockedBy` issue closed.
  Missing/partial dependency data makes the ticket `unknown`, never frontier.
- **Blocked ticket:** an open child with at least one open blocker. An assigned, otherwise
  unblocked child is `claimed`, not frontier.
- **Handoff Build Issue:** same-repository ordinary issue with no `wayfinder:*` label, the
  exact `Wayfinder handoff: #<map>` marker, and a native dependency on one or more children of
  that map. Discover candidates from each child's `blocking` connection, then verify marker,
  label namespace, and repository. Deduplicate by GitHub node ID.
- **Pipeline:** join Build Issue to `closedByPullRequestsReferences(includeClosedPrs: true)`.
  Select the merged closing PR when the issue is closed; otherwise select the newest open
  closing PR. Labels provide pre-PR intake/held/build state. Branch parsing is diagnostic only,
  never identity.
- **Landed evidence:** merged PR number/URL, `mergedAt`, `mergeCommit.oid`, issue `closedAt`,
  review decision, and a bounded check-rollup summary (`passing`, `failing`, `pending`, or
  `none`). A merge commit is proof of landing; copied PR prose is not.
- **Contextual ADR:** only explicit Markdown links to `docs/adr/NNNN-*.md` in the map's
  `## Decisions so far` section or a decision child's final resolution. Preserve the URL and
  link text; do not scan the whole repository or copy ADR content into the snapshot.

### Bounds

All limits are per enrolled repository unless stated otherwise:

| Collection | Hard bound | Overflow behavior |
|---|---:|---|
| Active maps | 5, `updatedAt` descending | Return total/overflow count and GitHub Issues link |
| Closed-map history | 5, `closedAt` descending | Return total/overflow count and GitHub Issues link |
| Decision children per map | 50 in native order | `complete: false`; do not claim a complete frontier |
| `blockedBy` or `blocking` edges per child | 10 each | Child/map relationship status becomes `unknown` |
| Verified handoffs per map | 20 | `complete: false`; link to map's Handoffs section |
| Closing PR attempts per handoff | 5 newest | Keep selected attempt plus `attempt_count` |
| Contextual ADR links per map | 12 | Return overflow count; never repository-scan |
| Recent landed PRs | 10 per repository, merged descending | Fleet home shows newest 20 across repositories |
| Check-rollup entries per selected PR | 20 | Preserve aggregate verdict and `truncated: true` |

Caps are product bounds, not pagination bugs. Every bounded connection requests `pageInfo`
and `totalCount`; overflow is explicit. The console links to GitHub for the comprehensive
history.

### Cadence, staleness, and failure

- Refresh the GitHub-backed repository/map projection on the daemon's 300-second heartbeat,
  including dormant mode. The 15-second fast tick may republish local liveness and reuse the
  last map generation, but must not run map queries.
- A change-triggered full pipeline pass may reuse GitHub facts it already obtained; it does
  not independently refresh every map. This prevents active work from multiplying console
  cost.
- Stamp the whole snapshot with `generated_at`, and each repository component with
  `github.attempted_at`, `github.fresh_at`, `github.status`, and `github.error`.
- `fresh` means a successful read no older than two configured heartbeats (10 minutes by
  default). `stale` means older than that or the latest attempt failed. `unavailable` means no
  successful generation exists. Always serve the last verified component with its real age.
- Refreshes are per repository. One repository failure must not discard successful updates
  for the other five. A failed map query preserves that repository's previous map component;
  it never converts unknown into empty.
- Do not retry inside the same projection cycle. Honor `retry-after` or rate reset; otherwise
  retry on the next heartbeat. There is no browser/server live-query fallback and no history
  database.

### Request and point budget

At most one discovery query, five active-map detail queries, and one handoff/pipeline batch
query may run per repository per heartbeat: **42 GraphQL HTTP requests fleet-wide** for the
current six repositories. Each query asks for `rateLimit.cost`; the map/repository refresh has
a separate hard ceiling of **60 GraphQL points per heartbeat**. It stops before a query that
would exceed the ceiling, marks unrefreshed components stale, and preserves at least **1,000
remaining points** for the workflow engine.

At the default 300-second heartbeat this is at most 504 requests and 720 points/hour for the
new map/repository read. Added to ADR 0026's measured existing snapshot cost (~36 calls/cycle),
the console remains below roughly 1,152 calls/points per hour before query batching replaces
those legacy reads. Browser count remains irrelevant. Queries run serially and request only
the fields above.

### Web API shape

Keep one read endpoint and version the body:

```json
{
  "schema_version": 2,
  "generated_at": "2026-07-17T12:00:00Z",
  "daemon": {"last_cycle_at": "...", "heartbeat_seconds": 300},
  "fleet": {"recent_landed": []},
  "repositories": [{
    "id": "R_...",
    "name_with_owner": "owner/repo",
    "url": "https://github.com/owner/repo",
    "github": {"status": "fresh", "attempted_at": "...", "fresh_at": "...", "error": null},
    "maps": {
      "active": [{
        "number": 179,
        "title": "Map: ...",
        "url": "https://github.com/owner/repo/issues/179",
        "updated_at": "...",
        "complete": true,
        "progress": {"total": 4, "closed": 0},
        "frontier": [],
        "tickets": [],
        "handoffs": [],
        "adrs": []
      }],
      "history": [],
      "active_total": 1,
      "history_total": 0
    },
    "workflow": {"ready": [], "held": [], "in_flight": [], "recent_landed": []}
  }]
}
```

`tickets`, `handoffs`, PRs, evidence, and ADRs all carry stable GitHub IDs/numbers and URLs.
The browser derives presentation only; it does not recompute graph membership, frontier,
pipeline identity, freshness, or overflow. Missing initial state returns this same versioned
shape with empty arrays and `github.status: "unavailable"`.

## Implementation consequences

1. Replace branch-name pipeline identity with native closing references, retaining branch
   parsing only as an inconsistency warning.
2. Add a daemon-side repository/map projection module and compose it into the atomic snapshot;
   do not add a web-to-GitHub adapter or projection database.
3. Update the Wayfinder handoff template to write the marker and dependency edge. Existing
   historical maps may be displayed without handoffs until explicitly backfilled; never infer
   them from arbitrary cross-references.
4. Test bounds, partial GraphQL errors, stale preservation, rate-floor stopping, native child
   order, dependency uncertainty, duplicate handoff edges, and multiple closing PR attempts
   through the public snapshot interface.

This is load-bearing enough to record in ADR 0036; it specializes, and does not reopen, ADR
0035's accepted product boundary.
