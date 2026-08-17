# ADR 729 — Receipt-staleness repair converges before admission

Status: Accepted
Date: 2026-08-16

## Context

Every provider native-discovery receipt hashes the entire pinned capability manifest, so a single
manifest edit invalidates every receipt in the fleet at once. Repair knew how to re-prove discovery
and re-record a missing or stale receipt (#713), but that branch was unreachable on any root whose
requirements include the pinned browser runtime and whose runtime was already installed: one early
return conflated "runtime healthy, nothing to install" with "runtime occupied, symlinked, or
half-installed," and answered both with "no repair available." The same return also swallowed the
absent-pinned-destination repair on runtime-intact Claude roots. Records refused preflight every
cycle for an hour, then parked for a human, while the launch-root materialization log announced
`outcome=ready` for a launch root it never rechecked.

[ADR 582](adr-582-capability-parity-and-environment-failure.md) and
[ADR 627](adr-627-composed-operational-admission.md) both recorded that invalid discovery receipts
are never rewritten — which merged behavior has contradicted since #713.

## Decision

One repair call does every deterministic repair it can and then proves discovery. The runtime
early return is split into its two real meanings by the existing runtime contract discriminator:
a fully intact installed runtime is simply not runtime work, and repair continues; a present but
not-intact `node_modules` (occupied, symlinked, or partial) is still no repair and still a
human-owned refusal; a wholly absent runtime is installed exactly as before.

After the deterministic work — materializing absent pinned destinations and/or installing an
absent runtime — and equally when there was none, repair reads the receipt. Only a **missing**
receipt or one carrying the verbatim stale/incompatible evidence triggers the proven discovery
probe, which re-records the receipt on success. Every other receipt state (valid, unreadable,
provider unavailable) leaves the receipt bytes untouched and runs no probe; a repair that
materialized content still reports success naming only what it materialized, and the coordinator's
re-probe fails closed onto the existing one-repair-then-park path. A probe that fails after a
successful materialization reports failure with the materialization named; the copied files are
not rolled back. A failed probe preserves any pre-existing receipt bytes (so stale remains stale);
only a successful probe replaces them, while a previously missing receipt remains missing.

A missing or stale receipt is therefore repairable by re-running the proven probe, and an absent
pinned destination on a runtime-intact root is repairable — superseding the "invalid discovery
receipts are never rewritten" clause in ADR 582 and the "invalid discovery receipts … remain
human-owned refusals" clause in ADR 627. Unreadable, occupied, drifted, or unknown content is
still never rewritten; that boundary stays.

The word `ready` in a `capability repair` log line is earned by observation: the launch-root
materialization audit reports only what it copied (`outcome=materialized`), and the only emitter
that may say `ready` is the coordinator, which re-probes the prepared launch root before printing
and never says it after a failed re-probe.

## Alternatives

- **Repair one fault class per call.** A manifest bump normally produces "stale receipt plus
  something else" on every UI-bearing root, and the coordinator grants exactly one repair per
  refusal fingerprint — so a single-fault repair still parks the realistic trigger.
- **Branch on receipt status alone.** `drifted` covers both a stale receipt and an unreadable
  one; the verbatim evidence sentence is the only discriminator, and sending an unreadable
  receipt into the probe would delete operator-authored bytes.
- **Relax preflight to accept stale receipts.** That weakens the manifest fingerprint as part of
  receipt identity and was rejected outright.

## Consequences

A fleet-wide manifest bump converges in one cycle per record: repair re-proves discovery at the
enrolled capability root, and every launch worktree of that root shares the re-recorded receipt
through the git common directory. Roots whose runtime is occupied or partial, and receipts that
are unreadable, keep clocking toward human escalation unchanged.
