# ADR 648 — Production Evaluation authority publication

Status: Accepted

Date: 2026-08-14

## Context

The effective-policy resolver requires one exact, human-promoted Evaluation receipt before
admission, but normal daemon composition must not acquire promotion or Evidence-write powers.
Production state initially has no Evidence database.

## Decision

Ship the locked bootstrap manifest as package data and expose a dedicated operator command that
builds its complete SQLite v4 Evidence store through the governed `observe`, `evaluate`,
`nominate`, and `promote` verbs. The command validates the derived event and complete receipt
against the pinned policy, then atomically creates the target inode without replacing a winner.

Existing targets are read only. Exact authority is current; every other readable or unreadable
target is a conflict. Status is likewise read-only and returns only a closed readiness result.
`PromotionReceiptReader.for_production()` delays its existing query-only validation until the
resolver performs its first read, so missing or corrupt state can become the resolver's existing
closed hold rather than a composition exception.

Issue #694 defines the published Evaluation receipt as the version-1 anchor of the same canonical
fleet-policy store, not its terminal contents. Later human-governed promotions append consecutive
verified receipts to that store. Deploy and status continue to validate the immutable Evaluation
anchor without replacing a store that contains valid successors; the production resolver validates
the complete successor chain before activating any learned method.

## Consequences

Normal daemon startup never creates, migrates, repairs, nominates, promotes, or rewrites
authority. #627 can compose the lazy reader as a prerequisite consumer without gaining any
authority-write capability.
