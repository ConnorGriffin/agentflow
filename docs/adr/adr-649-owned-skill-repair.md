# ADR 649 — Owned vendored skills may be repaired to their pin

Status: Accepted
Date: 2026-08-16

## Context

Enrollment materializes vendored skills into provider-local directories. A path and its digest
alone cannot establish whether AgentFlow or an operator created the directory, while convergence
needs a safe way to recover an AgentFlow materialization that has drifted.

## Decision

Each materialized provider-local destination has a separate schema-1 marker under
`.agentflow/skill-ownership`. The marker names the exact destination and binds both the pinned
files manifest digest and the complete materialized tree digest. Missing, malformed, symlinked,
path-mismatched, manifest-mismatched, or tree-mismatched markers prove nothing.

During convergence, AgentFlow may replace only a regular destination that is `drifted` and still
has a valid marker. It builds the pinned replacement beside the destination, revalidates status
and ownership immediately before the rename, then swaps the complete tree and refreshes the
marker. Unowned, incompatible, and symlinked content is reported and never touched.

Enrollment rollback journals only `.agentflow/skill-ownership`; it neither snapshots nor restores
the daemon's broader runtime root. Rollback that removes a materialized destination also removes
its marker.

## Consequences

An operator replacement invalidates provenance and requires manual resolution. Durable daemon
state remains outside enrollment rollback. The marker is outside the digest-checked skill tree,
so it does not affect `skill_destination_status`.
