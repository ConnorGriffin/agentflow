# ADR 498 — Build and revise use a capability-routed session lead

- Status: Accepted
- Date: 2026-08-04
- Ticket: [#498](https://github.com/ConnorGriffin/agentflow/issues/498)
- Supersedes: [ADR 0014](0014-cost-appropriate-model-tiers.md) (headroom-complement tier selection),
  [ADR 0018](0018-two-dials-review-by-evidence.md) (complexity selecting the builder model), and
  [ADR 0029](0029-static-per-pool-admission.md) (`MODEL_FOR` as build/revise validation)

## Context

The 2026-08-03 replay benchmark found that capability varies by area rather than by one
cheap-to-frontier ordering. Terra matched merged hermetic fixes, Sonnet tied Opus on verified
exploration, and the safe escalation path was one retry with findings followed by one rung up.
Headroom-based model sizing could not express those results.

## Decision

Every Build and Revise launches one Claude/Fable **session lead** at low session reasoning.
The lead plans, delegates all exploration/implementation/fix work, verifies citations and the
repository test gate, and ships only verified work. It never writes the implementation itself.
Workers enter the provenance-stamped capability ladder at its first rung. After a failed
verification the lead retries the same rung with findings; after the second failure it starts a
fresh worker one rung higher; at the top it stops and hands off both failures. Existing Build and
Revise native handoffs remain the terminal route.

The issue effort dial becomes the worker reasoning instruction (`low`, `medium`, `high`, `xhigh`)
while the parent stays low. Complexity no longer sizes the builder: it retains the established
session ceilings and selects the review tier. Fable is lead-only and never a delegate target.

One routing module owns config validation, ladder walking, stage-model resolution, provider CLI
identifiers, and rendered lead instructions. Deleting it would spread the empirical table across
dispatch, admission, prompts, and both runners; that locality is why the module earns its seam.

## Consequences

The configuration carries the benchmark date, price snapshot, routes, bans, and a launchable CLI
identifier for every named model. Claude aliases remain unversioned, so the stamp is provenance,
not a claim that an alias pins a snapshot. A Sol/Codex parent is deliberately deferred to
[#509](https://github.com/ConnorGriffin/agentflow/issues/509).
