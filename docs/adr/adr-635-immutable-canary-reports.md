# ADR 635 — Immutable canary reports

Status: Accepted

Date: 2026-08-14

## Context

Reconciliation and snapshot projection need one durable, content-free conclusion for each
logical canary stage and report version. Recomputing from mutable attempt telemetry after a
crash or retry would let a later observation change the conclusion those downstream readers use.
Attribution already belongs to the coordinator Store, and telemetry already has one durable
AttemptTelemetry decoder.

## Decision

`CanaryReporter.report(stage_identity, "canary-report-v1")` is the only reporting operation.
It validates Store-read canary attribution through its owner, reads the existing decoded
AttemptTelemetry directory once, and projects only typed content-free facts. Unreadable,
non-object, partial, or malformed telemetry is absent; it never supplies terminal evidence.
Zero attempts therefore leave duration, token, cost, and evidence age explicitly missing.

The separate `canary-reports.db` has one immutable final table keyed by stage identity plus
report version and exactly two immutability triggers. A compatible existing row returns before
either source is reread. Otherwise, the reporter takes an attribution and telemetry snapshot,
then one immediate transaction inserts the immutable row or returns the concurrent winner. A
pre-commit crash leaves no row and a later call resnapshots; a post-commit crash leaves the row
that every retry returns unchanged.

The three results remain advisory: a verified attempt is an observation; otherwise permanent
terminal evidence recommends rollback; all other telemetry recommends a block. This record does
not execute or retry an action.

## Alternatives

Lifecycle states, leases, signatures, pruning, telemetry backfill, report migrations, and a
general reporting platform were rejected. They add mechanisms without improving this single-user,
low-stakes, one-row-per-stage/version requirement.

## Consequences

Reconciliation can consume the immutable report key and result, and snapshots can carry only its
content-free pointers, measure missingness, and finalized-at age source. Missing or refused
reports remain missing rather than becoming fabricated block recommendations.
