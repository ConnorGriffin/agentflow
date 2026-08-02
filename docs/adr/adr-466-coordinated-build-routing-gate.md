# ADR 466 — The coordinated-build route is gated on the dials plus a slice-bearing work order

- Status: Accepted
- Date: 2026-08-02
- Ticket: [#466](https://github.com/ConnorGriffin/agentflow/issues/466)
  (wayfinder map [#463](https://github.com/ConnorGriffin/agentflow/issues/463))
- Constrains: [ADR 464](adr-464-slice-runs-in-session.md) (the switch this fills in),
  [ADR 465](adr-465-work-order-is-the-non-self-scoping-brief.md) (the work order the gate reads),
  [ADR 0046](0046-production-routing-and-spend-policy.md) (adds a route beneath the policy lock,
  changes no cell), [ADR 0041](0041-stage-model-reasoning-matrix.md) (routing matrix unchanged)

## Context

ADR 464 made coordinated build a switch — off by default, set per cell, in committed fleet
configuration a repository may narrow but never widen. It deliberately did not say which cells
are switched on, or what signal turns the route on before a build starts.

The difficulty the ticket names is real: the audited saving lives in **deep builds over 60
turns** — 40 sessions, $375 of the $1,464 window — and turn count is not knowable at dispatch.
The gate must be built from facts that exist before the session opens.

Two such facts exist. The first is the pair of dials the issue already carries. Re-running map
#463's own cohort through the readout attached to
[#467](https://github.com/ConnorGriffin/agentflow/issues/467) reproduces the audit to the cent
($1,464.02 window total; 40 attempts and $375.28 in the >60-turn deep cohort) and resolves the
dials into cells:

| cell (`build` / `claude` / `opus`) | attempts | issues | verified | $ total | median $/issue | turns |
| --- | --- | --- | --- | --- | --- | --- |
| `deep` / `high` | 28 | 15 | 11 | $159.93 | $9.51 | 1,383 |
| `deep` / `medium` | 24 | 17 | 16 | $66.34 | $3.35 | 991 |
| `standard` / `low` | 16 | 11 | 9 | $15.55 | $0.73 | 405 |

`deep`/`high` averages ~49 turns per attempt and $9.51 per issue; `deep`/`medium` averages ~41
turns and $3.35. The map's separate cut — 42 deep high/extra builds at a median 78 turns —
points the same way. The dials separate the cohort about as well as a two-value proxy can, and
nothing cheaper is available before dispatch.

But the dials alone are a blunt gate, and the false-positive cost is not zero. A short deep
build routed to a coordinator pays the slicer's turns and each slice's re-grounding for work
that would have finished in one pass. Map #463 fixes the sensitivity: the modelled 23–50% saving
swings entirely on re-grounding inflation, and **at ≥1.6× the saving is gone**. A false positive
is therefore mildly negative, not neutral.

The second fact removes most of that exposure. ADR 465 has intake write the durable half of a
work order at scope time — including *the judgment that the work is separable at all* — and
lets intake decline to slice a deep issue it judges indivisible. That judgment is recorded
before dispatch, and it is a far better predictor of whether slicing will pay than any dial,
because it is about the work rather than about its size estimate.

## Decision

**A build takes the coordinated route only when both conditions hold: its cell is switched on,
and its brief carries a slice-bearing work order.** The dials are a pre-filter over cells; the
work order is the gate. Neither alone routes a build.

- **The proxy is `complexity: deep` combined with `effort: high` or `effort: extra`.** Confirmed
  against the telemetry above rather than assumed. `deep`/`medium` is excluded: at ~41 turns and
  $3.35 median per issue it sits below the cohort the saving was measured in, and routing it
  would spend coordination overhead on builds that are already cheap.
- **The switched-on set is exactly two cells:**
  `build / claude / opus / deep / high / ConnorGriffin/agentflow` and
  `build / claude / opus / deep / extra / ConnorGriffin/agentflow`.
  **agentflow dogfoods the route alone.** The gate is per-cell and cells are keyed by repository,
  so it is per-repository by construction; no fleet-wide form of the switch exists. agentflow is
  the right first subject because it is `reviewed` — a human merges every coordinated pull
  request while the route is young — and because its own operator reads the telemetry daily.
- **The allowed slice-model set for both switched-on cells is `{sonnet, opus}`**, with `sonnet`
  the default and `opus` reserved for a slice whose acceptance criteria name a judgment the work
  order did not pre-decide. The set is a ceiling under ADR 464, never free choice: it may never
  contain a model the cell's own routed tier does not already permit, so a coordinator can never
  reach a model the reviewed routing matrix would not have given the monolithic build. What the
  coordinator should weigh, in order: whether the slice's invariant tests fully pin its outcome
  (if so, `sonnet`); whether it must choose between two defensible designs (if so, `opus`); and
  never the slice's *size*, since cost is linear in turns and a long mechanical slice is still
  mechanical.
- **The coordinator may decline to decompose, and the fallback continues in place.** If the
  slicer finds the work order's premise does not hold against current `main` — the work is not
  actually separable, or the slices collapse into one — it collapses to a **single slice** and
  the build proceeds monolithically inside the same session. It does **not** cost a fresh
  session: re-dispatching would pay for the context twice, which is the same reasoning ADR 465
  used to keep a gapped worker resumable rather than re-dispatched. The cost of a decline is the
  slicer's turns and nothing else. A decline is recorded on the attempt so the re-review can
  count how often the gate was wrong.
- **Revise on a coordinated pull request is always monolithic.** Revise inherits the original
  builder's complexity and effort (ADR 0041, ADR 0046) but not the coordinated route, and it
  runs on the cell's own routed model rather than the allowed slice set. Revise is 31 attempts
  and $41.64 across the audited window — roughly 3% of build spend — so there is nothing to
  save, and coordinating it would mean authoring a fresh work order for a diff that already
  exists.

## Alternatives considered

- **Gate on the dials alone.** Rejected. It routes every deep/high build, including the
  indivisible ones, and pays coordination overhead on work that cannot be sliced. Intake's
  separability judgment already exists before dispatch and is strictly better information.
- **Gate on intake's separability judgment alone, ignoring the dials.** Rejected. It would route
  cheap separable work — `deep`/`medium` at $3.35 an issue — where the coordination overhead is
  a larger share of the total than the tier premium it saves.
- **Include `deep`/`medium` in the switched-on set.** Rejected on the measurement: ~41 turns and
  $3.35 median per issue is below the cohort the 23–50% model was fitted to, and the inflation
  sensitivity means a mis-sized route there is likelier to cost than to save. Revisit at the
  re-review if the switched-on cells land near the top of the modelled range.
- **Turn the route on fleet-wide for the gated cohort.** Rejected. Ship-first is only defensible
  with a bounded blast radius, and the other eight enrolled repositories include `autonomous`
  cells where no human sees the pull request before it merges.
- **Predict turn count from a model at scope time and gate on the prediction.** Rejected. It
  invents a new estimator with no ground truth to calibrate against, and the dials plus the
  separability judgment already carry the information such an estimator would be reconstructing.
- **Let a declining coordinator re-dispatch as a monolithic build.** Rejected. It pays for the
  same grounding twice and adds a continuation path for a case that is not a failure — the
  session is healthy and holds everything it needs.
- **Coordinate revise rounds too.** Rejected. 3% of build spend, and it would require a work
  order authored against a diff rather than an issue — a second authoring path for a rounding
  error.

## Consequences

- **The gate is only as good as intake's separability judgment**, which is a model judgment made
  once per issue. ADR 465 already requires that instruction to carry hand-curated worked examples
  refreshed at the monthly recalibration pass; this ADR makes that refresh load-bearing rather
  than hygienic.
- **Declines are a measured quantity.** Because a decline is recorded on the attempt, the dated
  re-review ([#469](https://github.com/ConnorGriffin/agentflow/issues/469)) can read the gate's
  false-positive rate directly instead of inferring it from spend.
- **The switched-on cells are narrow, so the cohort fills slowly.** `deep`/`high` on agentflow
  produced 11 verified stages across the audited 13 days — just clear of ADR 0040's ≥10
  quantitative minimum. Whether that suffices by the re-review date is
  [#468](https://github.com/ConnorGriffin/agentflow/issues/468)'s question, and it is why
  "extend, do not judge" has to be an allowed verdict.
- **Nothing in ADR 0041's or ADR 0046's matrices moves.** This adds a route beneath the policy
  lock: the same cell still runs the same model at the same reasoning effort, and the only new
  freedom is which models a coordinator may launch a *slice* on inside an already-routed build.
- **Widening is a reviewed pull request, in both directions.** Adding a cell, adding a repository,
  or enlarging an allowed slice-model set are all edits to committed fleet configuration under
  ADR 464, and a per-repository setting may still only narrow.
