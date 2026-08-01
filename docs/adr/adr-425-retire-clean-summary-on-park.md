# ADR 425 — A park retires the current clean review summary

- Status: Accepted
- Date: 2026-08-01
- Applies: [0047](0047-reviewers-ship-clear-fixes.md)
- Follows: [417](adr-417-head-check-gate.md)

## Context

A pull request can retain an `Outcome: clean.` comment after its head changes and a later review
parks it for human action. The earlier comment then looks like the current operator verdict even
though the pull request needs attention. ADR 417 prevents a new clean result on a red reviewed
head, but does not retire a result that was already published.

## Decision

Every clean summary records the exact reviewed head in a machine-readable comment marker.
Publishing for a different head supersedes existing current summaries in place, preserving their
text as evidence. Publishing again for the same head updates its one current summary.

Any park supersedes every current clean summary before writing the park comment. This applies ADR
0047's rule that a pull request has one final public verdict: once human action is needed, the
park is the sole current verdict, including when another review axis had been clean at that head.
If the comment thread cannot be read, or any required supersede edit fails, no park is posted so
the durable handoff retries the whole operation.

## Consequences

The pull request page preserves prior review evidence without presenting it as current. The gate
owns stamps and supersession; review and exhaustion callers continue to use only publish or park.
