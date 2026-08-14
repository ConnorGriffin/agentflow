# ADR 596 — Typed Evidence envelopes and lineage

Status: Accepted

Date: 2026-08-12

## Context

ADR 580 made Evidence failure-only and created SQLite schema v2 directly. Agentflow and its
methodology producers also need to preserve typed claims, criteria, decisions, review actions,
fixes, verification, and settlements without adding free text, a second store, or another public
verb. Their provenance must remain deterministic, bounded, auditable, and safe across retention
and schema evolution.

## Decision

Keep the five-verb Evidence interface and admit either the existing Python `Observation` or a
tagged `EvidenceEnvelopeV2` through `observe`. A v2 envelope contains exactly one strict failure
arm or producer arm. Only producer facts own at most 32 dense ordered Evidence links; each target
must already resolve in the same store and repository, and the closed source/relation/target
matrix fails closed. Producer identity includes the complete immutable subject, typed fact,
review action when applicable, and ordered lineage in a canonical NUL-separated SHA-256 preimage.

Repository-qualified briefings select roots by explicit validation state and add their transitive
target closure. Closure-only events are contextual projections with minimized observation
provenance. Retention first expires age/policy relations, then marks remaining observation,
evaluation, and candidate roots plus their target closure before sweeping unmarked links and
events.

ADR 596 introduced exact SQLite schema v3. Only an exact schema-v2 fingerprint may enter the
transactional v2→v3 migration. An exact v1 store commits the existing v1→v2 migration before a
distinct v2→v3 transaction; a first-leg fault leaves exact v1 and a second-leg fault leaves exact,
row-preserving, reopenable v2. Migrated empty subject metadata is an immutable `legacy_unknown`
sentinel and is never backfilled or treated as knowledge the old schema did not retain.
ADR 584 subsequently extends new stores to schema v4 with a promotion-contract receipt marker and
an exact transactional v3→v4 migration; the v3 envelope and lineage contract is unchanged.

JSON contract v1 remains byte-compatible. Contract v2 is a separately routed normative tagged
wire contract with fail-closed duplicate-key, redaction, shape, type, vocabulary, suffix,
manifest, JSON, and I/O errors whose CLI rendering never echoes rejected content.

## Consequences

Evidence remains one deep module: producers learn the existing five verbs while identity,
replay, target resolution, retention closure, migrations, and wire validation stay local. Pipeline
and methodology producer adapters remain separate follow-up work and must supply complete
domain-specific finding sets where their own contracts require them.
