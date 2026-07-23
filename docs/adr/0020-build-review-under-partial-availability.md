# ADR 0020 — Running the build/review half under partial tool availability

- Status: Accepted
- Date: 2026-07-09

## Context

The two-pool balancer ([ADR 0006](0006-two-pool-runner-assignment.md)) optimizes
headroom assuming both tools are usable. In practice a tool may be unwired, busy, or out
of credits, and a single-issue run can get stuck or churn. [ADR 0003](0003-cross-tool-review.md)
(cross-tool review) and [ADR 0004](0004-auto-merge-gate.md) set the review/merge rules; this ADR
records the original availability policy, later amended by ADR 0047, and
for not losing work.

## Decision

- **Dispatch:** round-robin across *available* tools (available = wired + has headroom +
  not busy). One available → it builds; **none → do nothing this cycle** (no partial
  work left behind).
- **Review availability (amended by ADR 0047):** autonomous work requires the other tool and holds
  silently without consuming capacity while it is unavailable. Reviewed work may run same-tool
  immediately but remains human-merge-only and labels the final summary accordingly. No tool free
  to review → do nothing, **post nothing**, retry next cycle.
- **Review until clean, with a bail (amended by ADR 0047):** reviewers ship grounded corrections
  and hand each changed exact head to the other tool. Three consecutive mutating passes park once;
  retained work is never discarded.

## Alternatives considered

- **Same-tool review may auto-merge** (throughput first). Rejected: independence is the
  guarantee against "green CI, confidently wrong"; a human glance on the rare one-tool
  round is cheap insurance.
- **Hard one-revise cap** (ADR 0004, literal). Rejected: too tight now that a human
  merges most repos; the bail-to-draft-PR handback bounds the loop without a hard stop.

## Consequences

- ADR 0003's *"same-tool never auto-merges"* is preserved. ADR 0047 restores cross-tool review as
  a hard autonomous gate while retaining immediate same-tool progress for reviewed repositories.
- ADR 0004's original one-round cap is replaced by the three-mutating-review-pass convergence bail.
- A parked or handed-back issue is legible for the ratchet and the needs-you inbox; the
  daemon posts nothing when it simply has no tool free.
