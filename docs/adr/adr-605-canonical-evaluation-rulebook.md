# ADR 605 — Canonical Evaluation v1 rulebook

Status: accepted
Date: 2026-08-13

## Context

Independent review reproduced semantic drift when the parent plan, a projected JSON grammar, and verifier constants each encoded overlapping Evaluation v1 rules.

## Decision

Evaluation v1 has one versioned canonical data contract as its semantic authority. AgentFlow and its independent verifier consume that contract. Runtime code owns execution mechanics; generated fixtures and artifact locks prove conformance without restating or redefining thresholds, denominators, truth tables, aggregation, bootstrap, or authority-scope rules.

## Alternatives

Runtime code as the authority and separate stage rulebooks were rejected because either choice leaves independent verification or cross-stage consistency dependent on duplicated semantics.

## Consequences

Any semantic change creates a new reviewed contract version. A checker or fixture that embeds a second semantic registry is invalid even when its tests pass.
