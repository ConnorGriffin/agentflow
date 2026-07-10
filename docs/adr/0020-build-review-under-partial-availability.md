# ADR 0020 — Running the build/review half under partial tool availability

- Status: Accepted
- Date: 2026-07-09

## Context

The two-pool balancer ([ADR 0006](0006-two-pool-runner-assignment.md)) optimizes
headroom assuming both tools are usable. In practice a tool may be unwired, busy, or out
of credits, and a single-issue run can get stuck or churn. [ADR 0003](0003-cross-tool-review.md)
(cross-tool review) and [ADR 0004](0004-auto-merge-gate.md) (one revise round,
drop-to-reviewed) set the review/merge rules; this ADR adjusts them for availability and
for not losing work.

## Decision

- **Dispatch:** round-robin across *available* tools (available = wired + has headroom +
  not busy). One available → it builds; **none → do nothing this cycle** (no partial
  work left behind).
- **Review — prefer cross-tool, don't gate on it (amends ADR 0003):** review with
  whichever tool is free; if only the builder's tool is free, it reviews **same-tool
  immediately** rather than stalling. **Cross-tool stays the bar for a hands-off merge:**
  on an `autonomous` repo a same-tool-reviewed PR **parks for the human**; only a
  cross-tool clean review auto-merges. (`reviewed`/`guarded` park either way.) No tool
  free to review → do nothing, **post nothing**, retry next cycle.
- **Revise until clean, with a bail (amends ADR 0004):** address findings and re-review
  until clean; if it isn't converging (~2 unproductive rounds) or the builder hits a
  blocker, stop, mark the issue for the human, and **save progress as a draft PR** —
  never lose work, never loop forever.

## Alternatives considered

- **Same-tool review may auto-merge** (throughput first). Rejected: independence is the
  guarantee against "green CI, confidently wrong"; a human glance on the rare one-tool
  round is cheap insurance.
- **Hard one-revise cap** (ADR 0004, literal). Rejected: too tight now that a human
  merges most repos; the bail-to-draft-PR handback bounds the loop without a hard stop.

## Consequences

- ADR 0003's *"same-tool never auto-merges"* is **preserved**; its *"degrade only when a
  tool is unavailable"* is relaxed to **"prefer, don't gate."**
- ADR 0004's one-round cap becomes a **convergence bail**; drop-to-reviewed generalizes
  to the **draft-PR handback** (progress is never discarded).
- A parked or handed-back issue is legible for the ratchet and the needs-you inbox; the
  daemon posts nothing when it simply has no tool free.
