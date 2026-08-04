# ADR 498 — Headroom gates the session lead but does not route workers

- Status: Accepted
- Date: 2026-08-04
- Ticket: [#498](https://github.com/ConnorGriffin/agentflow/issues/498)
- Narrows: [ADR 0020](0020-build-review-under-partial-availability.md)

## Context

ADR 0020 lets either clear pool keep build throughput moving. Capability-routed Build and Revise
have only one parent implementation in this change: Claude/Fable. Choosing Codex when Claude is
blocked would launch no valid parent, while consulting headroom for each nested worker would turn
an empirical capability table back into a quota selector.

## Decision

The existing ceilings, pacing, activity checks, floodgates, and permit ledger remain the launch
gate. Build and Revise launch only when Claude is clear; Codex headroom cannot substitute for a
blocked Claude parent. Once the parent runs, no worker delegation consults the balancer.

Codex workers run through `codex exec` inside the Claude session. Those nested calls deliberately
bypass the coordinator's Codex permit ledger. This is acceptable for the first parent
implementation: the next cycle's Codex capacity facts reflect actual spend, and the Claude parent
still required a clear launch gate. [#509](https://github.com/ConnorGriffin/agentflow/issues/509)
restores ADR 0020's partial-availability throughput and should revisit ledger accounting with a
second real adapter.

## Consequences

Build throughput is temporarily Claude-gated. The coordinator's attempt-level lineage pinning is
unchanged, and durable pre-#498 Codex records finish on their pinned tool rather than being stranded
by an upgrade.
