# ADR 627 — Production admission is one atomic, historically recoverable decision

Status: Accepted

Date: 2026-08-14

## Context

Production previously had individually accepted policy, capability, routing, safety, canary, and
promotion-reader contracts without one owner that could decide and persist a launch atomically.
Reconstructing any of those facts after reservation would let mutable routing, checkout state, or
environment change what the provider ran. Holding authority failures terminally would also require
an operator to resubmit work after deploying the missing authority.

This decision composes these exact prerequisite merges:

- #582 durable capability discovery and launch-root materialization:
  `a58dc0c84a7459774631048a67b3e71f8328d144`.
- #585 OperationalSafety and immutable launch authority:
  `bd818fa1d65c92def671192464207e6bc3904a34`.
- #628 effective-policy briefing resolver:
  `ab9c1ffa6f86de149db46f0dca96e89499159172`; the composed contract now distinguishes
  unavailable overlays from invalid immutable overlay authority. Issue #694 adds verified
  successor-chain activation; the amended contract has SHA-256
  `f87266dddb953ee684958d8acef2f65b0aaa22cb812199adcd8d4cf912cbb01f`.
- #641 Store-owned canary attribution:
  `80f5a144621a990953d8ccacc08dd93a76090eaa`.
- #645 capability-ready admission facts:
  `46e0109a10e08a9ea6a8dc0621dcafde5a1d3d2f`.
- #646 immutable RouteCell selection and historical decoder:
  `4ffde0671ff496feb6cad697e7536bb8e4dc0454`.
- #648 production Evaluation authority:
  `b1ae64543761b808f7c0d357eded8551d684db3a`, with promoted Evaluation artifact
  SHA-256 `a0e90b5b41c87ff67f257315cc6578b0b181249037f1ced2bac827cd3670d1ec`
  and Evaluation receipt SHA-256
  `f39ec2e8a6eeff7718ad3db5a58a1bc762aec46f7e59c9cddd6f4b0121707562`.

The composed public contracts are pinned as follows:

- capability manifest SHA-256:
  `aae288b0d75f57505192057245e1bd20f227c0ba4a397c0a8abd575d69608fc2`;
  each `capability-ready-v1` fact additionally carries and validates its own canonical digest.
- effective-policy contract (the #628 pin above):
  `f87266dddb953ee684958d8acef2f65b0aaa22cb812199adcd8d4cf912cbb01f`.
- RouteCell v2 contract:
  `14dc4e949ec2a045816040cbfb553118475a570395bb6ffc26d0e1c40c780c47`.
- OperationalSafety v2 contract:
  `5ef205f7d655a85ef9fa0526ef154d61ba50712d6234e17d7f345d2e6c76d36d`.
- CanaryAttribution v2 contract:
  `f7f64e3fb9a3913713d121d24af39c3f208d39b3cb6afb04b1457dd54b8d0d2f`.
- exact coordinator Store v4 schema fingerprint:
  `a2dd624722d0d4cbe93ffcf381f4de5cf6f52db1ebaa307453f51ede90986f7b`.
- exact coordinator Store v5 schema fingerprint:
  `7103be329c503a9f263ba6e3d4cec882913892b82e2dd0de744b0579f3351dd1`.
- promoted #648 Evaluation artifact and receipt SHA-256 values:
  `a0e90b5b41c87ff67f257315cc6578b0b181249037f1ced2bac827cd3670d1ec` and
  `f39ec2e8a6eeff7718ad3db5a58a1bc762aec46f7e59c9cddd6f4b0121707562`.

## Decision

The daemon acquires its singleton lock and reconciles every code-reachable RouteCell before
worktree recovery or dispatch, in both normal and once modes. Reconciliation may isolate one
unreadable route and continue readable sibling routes; it does not convert a local route failure
into a global daemon failure.

Coordinator captures the exact subject revision and derives model plus immutable route selection
once before first persistence. Continuations, successors, conflicts, manual resumes, restart
resumes, and repooling retain or deliberately replace those facts as one lifecycle transition.
Preparation addresses the captured revision. The source-root capability probe is advisory in the
production composition because preparation can create or repair the launch root. Exactly one
post-preparation probe over the final root is initial admission authority. When that observation
names a deterministic missing pinned destination or runtime, Coordinator gives the record one
enrollment-owned **Capability repair**, then performs exactly one fresh authoritative probe in the
same cycle. Only that fresh fact may admit the record. An unchanged failed repair is fingerprinted
durably and is not attempted again on later clock observations; a changed refusal may earn one new
attempt. Launch-root materialization may likewise restore a missing screenshot harness or replace
one exact manifest-listed historical digest while preparing the final root. Every attempted repair
logs its root, requirements, and ready or failed outcome.

Coordinator resolves a governed briefing for every stage except Converse, then submits one exact
`ReservationIntent` to Store. Coordinator owns no admission transaction, route registration,
manifest parsing, sealed Safety/Canary owner access, Evidence write, or self-admission path.
Recoverable content-free policy, capability, and RouteCell refusals remain WAITING with their
claim retained and are reevaluated on the next cycle or restart. Overlay read errors, timeouts,
and a repository or revision unavailable at read time retain the existing `invalid_overlay`
code and retry behavior. Malformed bytes or failed validation from a successfully read exact Git
object, including an overlay authority mismatch, use `invalid_overlay_authority`; that immutable
refusal and `admission_identity_migration_required` produce a terminal human handoff. Capacity
and lost compare-and-set races are ordinary deferrals. No authority refusal consumes a permit,
attempt, receipt, or attribution.

Capability repair is deliberately narrower than enrollment. Claude destinations are copied only
when every selected `.agents` source matches the manifest and every corresponding `.claude`
destination is absent or already pinned — a fully intact installed runtime does not block that
copy ([ADR 729](adr-729-receipt-repair-convergence.md)). A pinned Playwright runtime is installed
only when its provider-local `node_modules` destination is wholly absent. Launch materialization
restores the screenshot harness only when it is absent or matches a manifest-listed known-old
digest. A missing or verbatim-stale native-discovery receipt is re-proven by the discovery probe
as a tail step of the same repair call (ADR 729, superseding this paragraph's earlier blanket
clause). Existing drift, symlinks, occupied runtimes, unknown harness bytes, unreadable receipts,
and all other incompatible states remain human-owned refusals. Enrollment's per-root durable lock
serializes
writers. Failure rollback is limited to unchanged paths claimed by that attempt; content that
appears or changes concurrently is preserved.

The Store schema before composed admission was v3; its admission migration is v3 to v4. V4 adds a
Store-owned `receipt_digest` to the existing receipt row. That ordinary canonical self-digest
covers `stage_identity` and all nine `AdmissionReceipt` fields. It also adds OperationalSafety's
permanent `safety_admission_history`, keyed by `stage_identity`, whose row contains the admitted
`route_cell_digest`, `safety_state_id`, and its ordinary canonical self-digest. Both tables reject
UPDATE, replacement, and ordinary DELETE, and expose no retention or prune operation. The sole
retirement boundary is Store's existing never-started discard compare-and-set: a durable WAITING
reservation with zero attempts, no successful start fact, and no live provider family. In one
`BEGIN IMMEDIATE` transaction it may retire that identity's admission receipt, safety admission
history, optional canary attribution, optional lesson-use attribution, and coordinator record.
Each owning module temporarily suspends only its own DELETE trigger; commit restores every
trigger, while any failure restores both facts and triggers. This does not weaken the forensic
guarantee: such a reservation has no provider attempt to explain, and retaining its orphan facts
after freeing the identity would instead poison later reads of a genuinely fresh reservation.
Every admitted identity outside this single boundary remains append-only with no prune operation.
This is deliberately the minimum integrity boundary for the single-operator, low-stakes
deployment: no signer, key service, or second public receipt type is introduced, and a coordinated
attacker that can rewrite both facts and their digests is outside the supported threat model.

Store v5 is the sole transaction owner. One `BEGIN IMMEDIATE` validates the durable WAITING
compare-and-set, briefing identity and applicability, capability self-digest/stage/provider,
active nonquarantined exact RouteCell and launch configuration, capacity, OperationalSafety,
optional canary attribution, and any existing exact AdmissionReceipt. Store then inserts the
receipt, writes the ten-field RUNNING successor, and commits the permit and optional attribution
as one unit. Safety alone maps to `safety_refused`; route authority maps to
`route_cell:<missing|stale|mismatched|unreadable|quarantined>`. Every in-transaction callback is
non-reentrant. Malformed authority, database failures, callback mutation, precommit faults, and
races roll back every output. OperationalSafety inserts or validates its history row as a private
participant on Store's already-open connection; it never begins, commits, or rolls back the
transaction.

Issue #571 extends v4 to v5 with one normally append-only `lesson_use_attributions` table. When the
pinned #628 briefing contains one promoted advisory method, Store inserts or validates that
exact briefing, PromotionReceipt, method revision, and self-digest in the admission transaction
before publishing RUNNING. Conflicting attribution refuses admission. UPDATE and ordinary DELETE
are forbidden; only the never-started reservation retirement boundary above may remove its row.
The table is attribution only: it adds no lesson lifecycle, policy decision, routing, Safety,
autonomy, or promotion authority. The v4-to-v5 migration is atomic and does not rewrite historical
Records or admissions.

`AdmissionResult` contains no launch envelope. After commit, Coordinator obtains the immutable
envelope from the committed AdmissionReceipt through Store's public historical decoder. If the
daemon loses the acknowledgement after receipt plus RUNNING commit and before provider start, a
reopened Coordinator detects that receipt during ordinary reconciliation, decodes its exact
historical RouteCell digest, and starts that envelope without reserving again or consulting the
active pointer, routing tables, profile registry, or timeout environment. Provider argv and
supervision consume the same decoded launch configuration. Public receipt reads validate the
Store receipt self-digest and the exact immutable OperationalSafety history tuple; they never
reconstruct authority from the current active or predecessor pointer. Therefore zero, one, or any
later number of approved activations cannot invalidate recovery of an admitted launch.

If either immutable fact is unreadable after commit but before provider start, reconciliation
launches nothing and charges no attempt. It uses Coordinator's existing `_hold` then
`_finalize_hold` path to release the permit and produce the stage-native durable handoff. The
receipt and history remain untouched for forensics, a crash with `hold_pending` resumes that same
handoff, and reconciliation continues with readable sibling stages.

Legacy RUNNING and terminal records remain readable. A legacy WAITING row without all four
subject/route facts is refused as `admission_identity_migration_required`; no owner infers missing
authority. Legacy no-admission reservation is exposed through a distinct compatibility seam and
does not weaken the required composed `ReservationIntent` types.

## Falsifiable consequences

- `tests/test_issue_627_admission.py` fails if the pre-prepare probe blocks preparation, the final
  root is not authoritative, recoverable authority failures become terminal, immutable overlay
  invalidity remains retryable, never-started discard leaves or over-deletes an admission fact,
  trigger restoration is not enforced, or historical lost-ack recovery consults current authority.
- `tests/test_canary_attribution.py` fails if any callback can mutate Store, any precommit cutpoint
  publishes a partial output, a forged receipt or Safety history reads successfully, either
  admission migration is not atomic, an immutable fact is mutable, or an accepted schema
  fingerprint changes.
- `tests/test_effective_policy.py` fails if mutable HEAD affects an exact-revision overlay or a
  present corrupt Git object is inferred to be an absent path.
- `tests/test_route_selection.py` fails if a pointer/config/quarantine race consumes capacity, a
  historical digest cannot decode after pointer change, or one failed reconciliation route stops
  readable siblings.
- `tests/test_daemon.py` fails unless `daemon.run` reaches the real dispatch, production builder,
  Coordinator admission, Store reservation, and provider-command path.
- Stage tracer and coordinator-recovery suites fail if continuation, successor, conflict, manual
  resume, restart, or repool lifecycles lose their exact subject/route facts.

## Alternatives

- Let Coordinator or daemon open the admission transaction: rejected because it duplicates Store
  ownership and exposes sealed authority state.
- Add signed receipts, a key service, or history retention machinery: rejected because it does not
  improve the stated single-operator threat boundary and adds operational machinery without a
  shipped recovery requirement.
- Launch the active route after commit: rejected because pointer changes would alter an already
  admitted execution.
- Persist capability evidence or repair prose: rejected because only the closed failure class is
  durable/visible production state.
- Terminally hold missing authority: rejected because a deployment between cycles must allow the
  existing claim to admit without resubmission.
