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

[#510](https://github.com/ConnorGriffin/agentflow/issues/510) made the worker rungs those nested
calls need actually reachable: the Claude sandbox's `network.allowedDomains` now allowlists the
Codex CLI's own API hosts (`chatgpt.com`, `*.chatgpt.com`, `auth.openai.com`, `api.openai.com`),
so a `codex exec` worker can launch at all instead of failing closed against the sandbox. It also
gives the parent a fallback for the case this ADR's ledger bypass leaves uncovered: when a Codex
worker fails to launch or dies on a provider error rather than the work itself, or when the
render-time capacity facts already show Codex spent, the session lead brief closes that ladder's
Codex rungs and enters at the first Claude rung instead, recording the substitution in the final
handoff. The ledger bypass itself is unchanged; this only keeps a spent or unreachable Codex
account from stalling the session.

## Consequences

Build throughput is temporarily Claude-gated. The coordinator's attempt-level lineage pinning is
unchanged, and durable pre-#498 Codex records finish on their pinned tool rather than being stranded
by an upgrade.
