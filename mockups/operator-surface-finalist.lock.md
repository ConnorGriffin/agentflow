# Lock manifest — operator-surface-finalist (issue #183, extended by #375, #373, #374, #430)

Backfilled for the charter's now-mandatory lock-manifest gate (`standards/CHARTER.md`):
`operator-surface-finalist.html` shipped as `★ LOCKED` under #183 with no manifest of
its own. This file captures that original contract's terms (1–7) verbatim from the
mockup's header comment, then adds the fleet-section terms #375 settles (8–11).

**Precedence:** where a numbered term below conflicts with `DESIGN.md`/`PRODUCT.md`
defaults (e.g. accent color, type scale), the term in this manifest wins for this
surface — `Briefing.svelte` deliberately reopens its own token set scoped to
`.briefing`, not `:root` (ADR 0035).

## Gate terms (from the original #183 contract)

1. **Thesis** — a calm, continuous operator briefing turns exceptions into one short
   reading path: attention, current decision frontier, then fleet health.
2. **User path** — open authoritative GitHub action; expand a map's compact ticket
   outline; disclose supporting records only when needed; confirm fleet status last.
3. **First viewport** — masthead and freshness, three attention rows, active map
   summaries, and the beginning of the fleet.
4. **Visual rules** — light-first neutral palette, product-native sans typography,
   ruled rows rather than cards, sparse semantic color, no terminal or topology styling.
5. **Interactions/states** — native map/support disclosures, explicit external actions,
   theme toggle, honest stale/incomplete banners. All controls retain keyboard and
   focus semantics.
6. **Screenshot states** — typical, stale, incomplete, empty, narrow.
7. **Out of scope** — mutation, live GitHub reads, graph reconstruction, planning/chat,
   general backlog.

## Gate terms added by #375 (fleet section: landings, honest capacity, ordering)

8. **Landing cell** — each fleet row carries a fifth cell reading
   `<count> recent · latest <age>` (age via the surface's existing relative-time
   helper), or the verbatim `no landings yet` when a repository has never landed. Two
   repositories with equal counts and different newest-landing ages must render
   visibly different cells — this is the term the count-alone regression exists to
   catch.
9. **Capacity wording** — a pool taking work reads availability, then running count,
   then utilization: `taking work, <n> running, <pct>% used` (or `taking work, nothing
   running, <pct>% used` at zero). A paused pool reads `paused, <n> running` (or
   `paused, nothing running`) followed by `· ` and the daemon's block-reason sentence
   **verbatim** — never the published utilization percentage relabelled as "% used",
   never a headroom figure, and never a slot count in either state.
10. **Row order** — repositories with held or parked work first, then the rest;
    alphabetical case-insensitively within each group, full `owner/name` as the
    tiebreak. Grouping keys off held/parked work only, never off Decision Map
    freshness — a fixture where every repository is unobserved must still split into
    two groups by that same held/parked signal.
11. **Row health in words** — each row states, in text, whether it's healthy or how
    many items need the operator, and — only when that repository's map read is not
    fresh — appends `map data stale` or `map data unverified`. Never colour-only.

## Gate terms added by #373 (Attention section: which conditions, in what order)

12. **The five conditions** — the Attention section shows exactly these, and nothing
    else: an open PR awaiting the operator's merge; a held issue waiting on their
    reply; a parked build; a repository whose trust dial is ready to loosen; a
    repository whose briefing data is stale or has never loaded. Normal in-flight
    work the engine is still handling never appears; neither does a capacity/idle-pool
    row nor a blocked-ticket row (both re-settled out — see below).
    *Amended by #430 (`operator-briefing-drought.lock.md`): a sixth condition — a
    pipeline stage that produced no stage outcome across its last 10 finished
    attempts — is added on that manifest's terms. Nothing else is.*
13. **Priority order** — rows appear in exactly that condition order. Within a
    condition the sub-kind ranks first — condition 1 by repository profile alone
    (`guarded`, then `reviewed`, then `autonomous`), condition 2 needs-grilling before
    needs-mockup — then least-recently-touched first where a clock exists, then
    repository name, then item number. The order is total and never depends on the
    order repositories happen to be walked.
    *Amended by #430 (`operator-briefing-drought.lock.md`): stage-drought rows rank
    ahead of all five conditions above — a silently broken stage outranks any single
    item it starves. The five keep their order relative to each other.*
14. **Row kind labels** — the four the surface already ships (`Merge`, `Held`,
    `Parked`, `Trust`) keep their wording; the stale/never-loaded condition uses the
    locked incomplete state's own `Projection` kind and its row shape (a short
    statement as the title, the cause as the detail). The overflow row's kind reads
    `More`.
15. **One action per underlying thing** — a parked PR is a parked build and never
    also an awaiting-merge row, however finished the engine's own summary says it is.
    The fleet-wide freshness banner is unaffected: it is posture, term 5's honest
    stale/incomplete banner, and the per-repository row is the action with a link.
16. **Reason wording** — a parked build's reason says what is actually true of it.
    `open-question` reads `your comment on this PR has not been answered` — never that
    a question stopped the build, since it fires on a merge-ready PR the operator
    commented on. `drop-to-reviewed` reads `the pipeline stopped and left a decision
    on the PR` — never that a builder wants to drop autonomy, which is false for the
    unresolved-review, red-check and budget-exhaustion parks that share that bucket.
17. **Bound and overflow** — the collection is bounded fleet-wide at 25 rows. When it
    truncates, one final ruled row in the section's existing row treatment reads
    `<n> more operator actions not shown`, and both the section count and the tab
    badge report the true total, never the truncated length. Empty attention reads
    `No operator actions in this projection.`

## Gate terms added by #374 (Decision Maps: frontier trust, evidence, overflow)

18. **Frontier copy** — a map's frontier line renders exactly one of: the named
    frontier as `#<number> <title>`; the verbatim `No open decision remains` when
    every child is closed; `blocked` when open children exist but none is
    unclaimed-with-all-blockers-closed; `Not verified` when the frontier cannot be
    computed. No other frontier wording exists.
19. **Incomplete frontier fails closed** — partial, truncated, or unreadable
    dependency data never produces a claimed frontier: the map renders `Not
    verified` and its supporting records carry an `incomplete` qualifier naming
    the truncated collection. Distinct from term 5's whole-surface banners, which
    keep their own wording.
20. **Landed evidence unavailable** — a handoff whose closing-PR evidence read
    failed renders the verbatim `landed evidence unavailable` with a link to the
    handoff issue — never an optimistic in-progress word for a read failure.
21. **Overflow counts in supporting records** — every non-zero truncation
    (handoffs, ADR links, children) appears as a supporting-record link whose
    label carries the explicit numeric overflow count and whose URL lands on
    GitHub. A truncation with no count-bearing link is a gate failure.
22. **Attempt count** — when more than one closing PR attempted a handoff, the
    landed-evidence label includes the attempt count; a single-attempt handoff
    shows no count.
23. **Screenshot states** — two captured states beyond term 6's five:
    `map-frontier-matrix` (one render exercising all three term-18 non-named
    states plus a named frontier) and `map-overflow-evidence` (overflow counts,
    attempt count > 1, and the `landed evidence unavailable` case in one render).

## Fixture obligations

- Fixtures must join the two source lists the daemon actually publishes (`repos[]`
  keyed by `repo`, `repositories[]` keyed by `name_with_owner`) — never assume same
  order or length.
- The `typical` fixture keeps term 3's first-viewport shape of three attention rows.
  A separate non-locked `full-queue` fixture, captured at the same viewports and
  attached as evidence only, must exercise all five conditions and the overflow row.
- At least one fixture must carry a PR holding both a live clean-review summary and a
  park classification, to prove term 15 collapses it to the parked row alone.
- At least one fixture must carry a repository already at `autonomous` whose trust
  ratchet is ready, to prove term 12 emits no loosen row for it.
- At least one fixture must carry two repositories with equal landing counts and
  different newest-landing ages (term 8).
- At least one fixture must carry a paused pool whose `reason` names a materially
  different number than its `spent_pct` (term 9's regression case).
- At least one fixture must mix `Brewgen`-style mixed-case names among lowercase
  names, and at least one fixture must have every repository unobserved, to prove
  term 10's grouping and case-insensitive sort independently.
- The `map-frontier-matrix` fixture must carry four maps in one render: one with a
  named frontier, one all-children-closed (`No open decision remains`), one
  `blocked`, and one `Not verified` whose dependency data is truncated and whose
  supporting records carry the `incomplete` qualifier (terms 18–19).
- The `map-overflow-evidence` fixture must carry a handoff-overflow count link, an
  ADR-overflow count link, a handoff with two merged closing-PR attempts (its
  label showing the attempt count), and a handoff whose evidence read failed
  rendering `landed evidence unavailable` (terms 20–22).

## Declared deviations (re-settled by #373)

- The `Blocked` row in the mockup's typical state — **removed**. Decision Maps shows
  blocked tickets on its own frontier, so a second copy in Attention was a duplicate
  action, not a condition.
- The `Capacity` row in the typical state — **removed**. Fleet health shows the pools,
  and a paused pool names nothing the operator acts on.
- The overflow row — **new**, because the bound is new. Rendered inside the section's
  existing ruled-row treatment rather than as a new element, so term 4's ruled-row rule
  still holds.
- The `stale` state's empty Attention section — **re-settled**. The locked stale
  fixture showed the banner with zero attention rows; condition 5 makes that
  combination unreachable, so that state now carries its stale-repository rows.

## Verbatim strings

- `No operator actions in this projection.`
- `more operator actions not shown`
- `your comment on this PR has not been answered`
- `the pipeline stopped and left a decision on the PR`
- `no landings yet`
- `taking work, `
- `paused, `
- `nothing running`
- `map data stale`
- `map data unverified`
- `No open decision remains`
- `Not verified`
- `incomplete`
- `landed evidence unavailable`
