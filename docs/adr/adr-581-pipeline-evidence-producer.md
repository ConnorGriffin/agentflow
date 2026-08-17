# ADR 581 — Pipeline Evidence producer and lesson miner

Status: Accepted

Date: 2026-08-12

## Context

Pipeline facts are mutable operational records, while Evidence retains only immutable,
redacted observations. A second storage model inside every stage would spread identity
rules and risk retaining source prose.

## Decision

Use one `EvidenceProducer` adapter to derive source-set digests, revision-scoped
revision/claim/criterion producer events, review finding identities, fix lineage, and
settlement producer events before calling Evidence's public `observe` interface. The source set
contains the issue node, locator, captured timestamp, and captured revision plus exactly
the selected comment node, locator, capture timestamp, and revision only in its digest;
Evidence receives no prose. A reread that
does not exactly match the capture, including a deleted artifact, is unavailable.

Findings derive from their revision-scoped claim and criteria. Builds verify their
criteria and derive from the governing revision; attack/redraft records retain their
stable objection references and affected criteria. Fixes carry the complete
actionable-finding set through `addresses` links and do not assert per-finding
causality. Merge and park records settle the exact evaluated head through the linked
fix and retain upstream finding context. Every review finding derives from a typed
failure observation, a closed review action, and its named upstream contract decision.
Every fix revises a typed reviewed-parent revision. A fix-introduced defect derives from
its fix and retains reviewed-parent and fixer-head lineage. Merge and park are typed
dispositions whose bounded public subjects identify the outcome. Attempt telemetry stays
outside Evidence as an immutable join to the governing request revision, and settlement
retains that join by identity. Attack and redraft stages share a stable opaque objection
reference through a typed objection event.

The read-only miner accepts only reproduced, model-judged, or human-validated evidence
and requires two canonical events before it returns a versioned lesson candidate for one
named upstream method. It is injected with a storage-owned `EvidenceReceiptReader` that exposes
only immutable, content-free event receipts through the miner's `EvidenceReceiptQuery` protocol.
The reader is separate from `EvidenceStore`, follows the separate-reader boundary reserved for
#628's `PromotionReceiptReader`, and cannot evaluate evidence, nominate candidates, or edit policy.

## Consequences

Producer callers supply classification and validation; the adapter does not infer either.
The miner reads those facts and the upstream method from Evidence rather than accepting
caller-supplied classifications or methods. The five `EvidenceStore` verbs remain the sole
governed Evidence interface from ADR 580; the storage-owned receipt reader is a bounded,
content-free query seam rather than table access or a sixth verb. No pipeline source content,
prompts, transcripts, or policy mutations are retained.

Issue #571 wires terminal Intake, Attack, Research, Review, and Revise outcomes through this same
producer after each stage's own durable result exists. Intake, Attack, and Research bind their
frozen provider-input digest and exact checkout revision without retaining either input or result
prose. Review records its typed verdict ledger before projection; Revise links its verified pushed
head to that exact finding set. A router callback is projection plumbing only, not a second
Evidence interface or settlement owner.
