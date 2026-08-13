# ADR 606 — Explicit missing metrics and adjudication lineage

Status: accepted
Date: 2026-08-13

## Context

The rejected Evaluation v1 plans disagreed about whether a wholly unavailable result names four or seven missing metrics. They also accepted a self-consistent adjudication digest without proving that its answer key belonged to the canonical case.

## Decision

`missing_metric_names` is the exact sorted set of null arm-metric fields. A wholly unavailable result names all seven metrics in lexicographic order: `duration_ms`, `fix_introduced_defect_count`, `grounded_false_positive_count`, `provider_dollars_micros`, `quality_micros`, `review_rounds`, and `tokens`. A reported result may name only its null optional metrics in lexicographic order: `provider_dollars_micros`, `quality_micros`, `review_rounds`, and `tokens`; its other three metrics are required.

An adjudication is valid only when its `case_id`, exact case-manifest digest, and `answer_key_digest` match the answer-key reference reached through the canonical validated case record. The adjudication digest is the canonical digest of the receipt with its own digest field omitted.

## Alternatives

Listing only the four optional metrics for wholly unavailable results was rejected because it makes three null values implicit. Accepting a receipt's caller-supplied case and answer-key relationship was rejected because rehashing could authenticate fabricated lineage.

## Consequences

Unavailable output is self-describing. Adjudication validation must resolve canonical case lineage before accepting the receipt, and changing the case manifest or answer key invalidates the old binding.
