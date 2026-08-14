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

The RouteCell contract is versioned independently of the launch artifact. In v2 the logical key
hashes repository, stage, provider, and route ID; model belongs only to the immutable version
digest alongside the launch-config digest. This lets a model replacement become an inactive
candidate under the existing lane instead of silently creating a newly active lane. Claude and
Codex remain separate logical lanes. Review and Attack route IDs include their standard/deep tier;
Build and Revise retain tier plus effort, and every other stage retains its exact stage token.

Opening a materialized v1 safety ledger in an admission-enabled Store fails without mutation and
names operator reconciliation. Only an empty ledger can begin using v2 automatically: code cannot
choose successor pointers, merge tiered lanes, clear quarantine, or reinterpret old approvals.
The v1 contract digests stay pinned for historical attribution verification.

Store validates a selection and computes its three persistence identities before any write, then
uses the same OperationalSafety materializer during registration. Fresh admission has one coded
active resolver. Restart recovery can decode an exact committed digest even after it becomes
inactive, while active-pointer verification and capacity consumption share one SQLite write-lock
boundary across Store instances.

Reconciliation gives ordinary governed repositories every reachable cell. Workspace-only
repositories receive Converse cells only; a repository in both sets keeps ordinary coverage plus
one deduplicated Converse profile.

Record/schema migration, daemon invocation, restart recovery, and production admission composition
remain owned by issue #627.

## Alternatives

- Recompute launch policy during provider and launcher calls: rejected because mutable inputs could
  make argv and supervision disagree with the admitted RouteCell.
- Register or activate missing cells during admission: rejected because admission would invent
  defaults and acquire routing-policy authority.
- Rewrite populated v1 logical keys automatically: rejected because no deterministic migration can
  choose a successor model, preserve independent Review/Attack tiers, or reuse an old approval as
  authority for a v2 pointer.

## Consequences

Route registration now accepts only a validated `LaunchConfigV1`; legacy arbitrary launch
mapping callers must materialize the frozen artifact first. Production daemon composition stays
deferred to #627, so this ticket exposes only the selection, reconciliation, and consumption
seams that that handoff will compose.
