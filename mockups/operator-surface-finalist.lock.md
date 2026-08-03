# Lock manifest — operator-surface-finalist (issue #183, extended by #375)

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

## Fixture obligations

- Fixtures must join the two source lists the daemon actually publishes (`repos[]`
  keyed by `repo`, `repositories[]` keyed by `name_with_owner`) — never assume same
  order or length.
- At least one fixture must carry two repositories with equal landing counts and
  different newest-landing ages (term 8).
- At least one fixture must carry a paused pool whose `reason` names a materially
  different number than its `spent_pct` (term 9's regression case).
- At least one fixture must mix `Brewgen`-style mixed-case names among lowercase
  names, and at least one fixture must have every repository unobserved, to prove
  term 10's grouping and case-insensitive sort independently.

## Verbatim strings

- `no landings yet`
- `taking work, `
- `paused, `
- `nothing running`
- `map data stale`
- `map data unverified`
