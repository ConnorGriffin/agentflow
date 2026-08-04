# ADR 498 — Review tier follows builder complexity and independence follows the parent

- Status: Accepted
- Date: 2026-08-04
- Ticket: [#498](https://github.com/ConnorGriffin/agentflow/issues/498)
- Weakens: [ADR 0003](0003-cross-tool-review.md)
- Supersedes: [ADR 0018](0018-two-dials-review-by-evidence.md) (every reviewer always runs deep)

## Context

The benchmark's cheap review caught two of three planted defects with no false positives; only the
frontier reviewer caught the silently weakened test. Always-deep review spends frontier capacity
where the original issue already declared standard complexity. Delegation also separates the tool
that may write a diff from the parent accountable for verifying and shipping it.

## Decision

Review remains one single-model session. Its tier reads durable `builder_complexity`, falling back
to the review's own complexity for survivor and re-review records: standard uses Luna on Codex or
Sonnet on Claude; deep uses the pool's frontier model (Sol on Codex, Opus on Claude). Haiku never
reviews. Cheap-review admission demand is explicit and no greater than the frontier row.

Independence is parent-tool-based: reviewer tool differs from the Build/Revise session lead tool.
A Claude-parented change may contain a Codex worker's edits and still receive Codex review. This is
a deliberate weakening of ADR 0003. The parent is the accountable author because it selected the
worker, inspected the result, ran the gate, and decided to ship; a worker subprocess is not the
pipeline stage that makes that decision.

## Consequences

The complexity and effort dials both survive with changed meaning: complexity selects review tier
and existing ceilings, while effort instructs workers. Review/fix exact-head rules and human-merge
taint remain unchanged.
