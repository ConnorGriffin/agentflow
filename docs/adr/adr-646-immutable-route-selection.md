# ADR 646 — Route selection is an immutable admitted launch artifact

Status: Accepted

Date: 2026-08-14

## Context

Routing, provider adapters, and launcher supervision previously reconstructed overlapping launch
policy from mutable registries and environment at different times. That made one admitted RouteCell
unable to reproduce the policy it actually launched and risked turning admission into a second
routing authority.

## Decision

Routing materializes one profile-specific `RouteSelection` with a closed, canonical
`agentflow-launch-v1` artifact before admission. OperationalSafety alone validates, encodes,
digests, registers, activates, and decodes that artifact. One Store-decoded envelope supplies both
provider argv policy and launcher supervision; neither consumer rereads admitted routing, profile,
schema, or timeout values. Reconciliation registers all code-reachable governed cells explicitly,
while ordinary admission never registers or activates a cell. Configuration changes create a new
inactive digest under the same logical route and still require receipt-backed canary approval.

Record/schema migration, daemon invocation, restart recovery, and production admission composition
remain owned by issue #627.

## Alternatives

- Recompute launch policy during provider and launcher calls: rejected because mutable inputs could
  make argv and supervision disagree with the admitted RouteCell.
- Register or activate missing cells during admission: rejected because admission would invent
  defaults and acquire routing-policy authority.

## Consequences

Route registration now accepts only a validated `LaunchConfigV1`; legacy arbitrary launch
mapping callers must materialize the frozen artifact first. Production daemon composition stays
deferred to #627, so this ticket exposes only the selection, reconciliation, and consumption
seams that that handoff will compose.
