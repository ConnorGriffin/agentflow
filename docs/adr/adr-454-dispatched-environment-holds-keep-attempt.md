# ADR 454 — A dispatched environment hold keeps its attempt

- Status: Accepted
- Date: 2026-08-01
- Amends: [386](adr-386-dead-shell-environment-fault.md)

## Context

The coordinator records a provider attempt when its launch handshake proves the session started.
An environment fault then parks that same session for a human. ADR 386 refunded the attempt before
the park, leaving the durable record and public handoff at attempt zero even though the attempt
telemetry proves a session was dispatched.

## Decision

An environment fault still holds immediately and never retries automatically, but a dispatched
session keeps its consumed attempt through the pending handoff and final parked record. A resumed
stage remains a new bounded execution with its own fresh budget.

## Consequences

The durable record, public park message, and per-attempt telemetry all describe the same
dispatched session. Replaying a pending handoff after a daemon restart cannot subtract an attempt
or produce an attempt-zero park.
