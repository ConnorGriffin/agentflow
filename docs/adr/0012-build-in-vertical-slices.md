# ADR 0012 — Build in vertical slices, dogfooded on a live repo

- Status: Accepted
- Date: 2026-07-09

## Context

agentflow is a tightly-coupled system — daemon ↔ dispatch ↔ pools ↔ dashboard,
with real interfaces between each. Two tempting build strategies were on the table:
one-shot the whole thing, or decompose it and fan parallel subagents at the chunks.
One-shot can't hold that many interfaces coherently in a single pass. Blind parallel
fan-out is worse: parallel agents authoring the shared spine concurrently is the
exact collision hazard [ADR 0009](0009-collision-safety.md) exists to prevent —
self-inflicted. And a big-bang build contradicts the product's own philosophy of
proving incrementally and ratcheting trust ([ADR 0007](0007-decisive-intake-graduated-autonomy.md)).

## Decision

**Build in thin vertical slices, not horizontal layers.** Each slice closes the
loop end-to-end for a real repo before the next one thickens it. The first slice
auto-merges one issue in one vibe-code repo; later slices add the second pool +
balancer, the not-clean paths, the dashboard, `guarded`/grounding, and the ratchet.
The concrete ladder is a living plan in [ROADMAP.md](../../ROADMAP.md) — this ADR
fixes the *method*, not the sequence.

**One owner of the integration spine.** The daemon and its interfaces are authored
by a single lead (Connor's driving session, or one lead agent). Subagents fan out
**only** for independent leaves (a headroom reader, a config schema, a dashboard
widget) and for an adversarial review pass per slice — never for the spine.

**Dogfood on a live repo.** Every slice runs against a real vibe-code repo, not a
mock. The build's first customer is the product's first customer.

**Two standing quality gates on every slice:**

1. **UI → `/ui-mockups` first.** Any user-facing surface — the operator dashboard
   ([ADR 0010](0010-operator-dashboard.md)) and its controls — gets a repo-grounded
   `/ui-mockups` pass to a **locked** visual spec *before* it is implemented. No
   inventing UI at build time.
2. **Interfaces → the deep-module discipline** (`/improve-codebase-architecture`,
   `/codebase-design`). Every module is designed **deep** — interface far simpler
   than its implementation. Apply the **deletion test**; treat the **interface as
   the test surface**; use the vocabulary exactly (module / interface / depth /
   seam / adapter / leverage / locality). Non-obvious module shapes get the
   design-it-twice pass. Shallow modules do not ship.

## Alternatives considered

- **One-shot the whole system.** Rejected: too many interfaces to hold coherently
  at once; nothing is testable until the end.
- **Decompose + blind parallel subagent fan-out.** Rejected: parallel authors on
  the shared spine is [ADR 0009](0009-collision-safety.md)'s collision problem,
  self-inflicted.

## Consequences

- Slower to a *complete* system, faster to a *working* one — the first slice ships
  real value (a vibe-code repo self-merging) rather than scaffolding.
- Subagent use is bounded and purposeful (leaves + adversarial review), keeping the
  spine's interfaces coherent.
- The two quality gates are a per-slice checklist, not aspirational: a slice with an
  un-mocked UI surface or a shallow module interface is not done.
