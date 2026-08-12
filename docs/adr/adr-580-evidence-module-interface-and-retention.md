# ADR 580 — Evidence module interface and retention

Status: Accepted

Date: 2026-08-12

## Context

Agentflow needs durable, auditable observations about immutable subject revisions without
retaining prompts, transcripts, source bodies, or another authority over GitHub and checked-in
repository artifacts. Existing coordinator Records have unrelated semantics and must remain
readable unchanged.

## Decision

Create a separate versioned SQLite Evidence database under the coordinator state directory. Its
one public interface is `observe`, `evaluate`, `nominate`, `promote`, and `brief_for`; table
access is unsupported. Canonical event identity is repository, subject, subject revision, failure
class, normalized-signature digest, and normalizer version. Observations append immutable source
references and canonical events never accumulate mutable provenance.

Failure class and validation state are independent closed vocabularies. Evidence envelopes contain
only identifiers, enums, numeric/timestamp facts, versioned digests, authority pointers, and
bounded references. An AuthorityVerifier seam verifies the exact authority revision, hash, and
scope before promotion; concrete GitHub and repository adapters remain outside this decision.

Unpromoted and unreferenced material expires after 90 days. A promoted candidate is retained
while cited by an effective policy and through that policy's one subsequent version.

The database version is fail-closed. New stores create v2 directly. The only accepted upgrade is
the exact original v1 schema; it migrates transactionally into v2 and preserves its rows. Because
v1 promotion receipts did not record authority binding, migrated receipts are retained as
`legacy_unverifiable` and cannot authorize or activate a promotion.

## Alternatives

- Extend coordinator `records.db`: rejected because it conflates durable coordination with
  evidence and changes the existing record model.
- Retain full source material: rejected because it would store unnecessary sensitive and
  reconstructable content.
- Let each provider own its evidence storage: rejected because it loses canonical identity,
  consistent retention, and provider-neutral auditability.

## Consequences

Callers receive a small governed interface with redaction and idempotency centralized in one
module. Effective-policy activation and concrete authority adapters can extend the verifier seam
without making Evidence a second coordinator or decision authority.
