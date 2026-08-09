# ADR 541 — Native session-lead helpers retain parent accounting

- Status: Accepted
- Date: 2026-08-07
- Ticket: [#541](https://github.com/ConnorGriffin/agentflow/issues/541)
- Extends: [ADR 538](adr-538-automatic-codex-session-lead-fallback.md)

## Context

The Claude/Fable session lead already delegates through native Claude helpers and direct Codex
children under one rendered capability-routing brief. A Codex lead needs the same route without
adding another coordinator, a worker subsystem, or a second permit ledger.

## Decision

Codex session leads mirror Claude’s prompt-led native delegation. The shared lead brief selects
the capability-table rung, including its model and worker reasoning, while the parent owns retry,
escalation, verification, and ladder-top handback. The coordinator continues to own one durable
parent record and its full five-permit Codex reservation; helpers neither submit nor reserve on
their own.

The existing single lead-brief renderer accepts the durable parent provider. It renders native
delegation for that provider, the installed opposite-provider CLI for another provider’s rung,
and a provider failure rule that skips the failed provider to the first remaining rung. The
routing table remains the one source of models, bans, and ladders; no second prompt module is
introduced.

A helper on the parent’s own provider uses that provider’s native delegation. When the shared
ladder reaches the other provider, the parent launches the installed opposite-provider CLI with
the routed model. Thus Fable reaches Codex rungs through `codex exec`, and Sol reaches Claude
rungs through the Claude CLI, without a second worker subsystem.

Parent-stream usage is retained when reported. Missing native-helper usage is explicitly unknown,
never synthesized as zero or treated as free. Per-helper persistence is deferred until it becomes
necessary for a later measured capacity policy.

## Alternatives

Add helper records and a second permit ledger. Rejected because the durable parent reservation
already bounds the session and per-helper persistence has no measured policy consumer.

## Consequences

Sol helper usage remains unknown when the parent stream lacks reported helper attribution; when a
typed attribution distinct from Sol's own parent identity is present, spend reporting treats the
helper coverage as captured.
