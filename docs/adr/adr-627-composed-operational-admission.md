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
  `ab9c1ffa6f86de149db46f0dca96e89499159172`, with effective-policy contract
  SHA-256 `ea12ea2c28622dcbf2aeed7fa060f54250de3903d3942bfc8f6b8a04ffd53cef`.
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
  `cba84e63be53884e6ed566a534883912f7d22156aad7e4a5590515140d18fcad`;
  each `capability-ready-v1` fact additionally carries and validates its own canonical digest.
- effective-policy contract (the #628 pin above):
  `ea12ea2c28622dcbf2aeed7fa060f54250de3903d3942bfc8f6b8a04ffd53cef`.
- RouteCell v2 contract:
  `14dc4e949ec2a045816040cbfb553118475a570395bb6ffc26d0e1c40c780c47`.
- OperationalSafety v2 contract:
  `5ef205f7d655a85ef9fa0526ef154d61ba50712d6234e17d7f345d2e6c76d36d`.
- CanaryAttribution v2 contract:
  `f7f64e3fb9a3913713d121d24af39c3f208d39b3cb6afb04b1457dd54b8d0d2f`.
- exact coordinator Store v4 schema fingerprint:
  `39733092eb2c3a6110fe0d8299d0aa1fb356021448ee3c6cd46e534902f91060`.
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
post-preparation probe over the final root is admission authority.

Coordinator resolves a governed briefing for every stage except Converse, then submits one exact
`ReservationIntent` to Store. Coordinator owns no admission transaction, route registration,
manifest parsing, sealed Safety/Canary owner access, Evidence write, or self-admission path.
Content-free policy, capability, and RouteCell refusals remain WAITING with their claim retained
and are reevaluated on the next cycle or restart. Capacity and lost compare-and-set races are
ordinary deferrals. No authority refusal consumes a permit, attempt, receipt, or attribution.

Store v4 is the sole transaction owner. One `BEGIN IMMEDIATE` validates the durable WAITING
compare-and-set, briefing identity and applicability, capability self-digest/stage/provider,
active nonquarantined exact RouteCell and launch configuration, capacity, OperationalSafety,
optional canary attribution, and any existing exact AdmissionReceipt. Store then inserts the
receipt, writes the ten-field RUNNING successor, and commits the permit and optional attribution
as one unit. Safety alone maps to `safety_refused`; route authority maps to
`route_cell:<missing|stale|mismatched|unreadable|quarantined>`. Every in-transaction callback is
non-reentrant. Malformed authority, database failures, callback mutation, precommit faults, and
races roll back every output.

`AdmissionResult` contains no launch envelope. After commit, Coordinator obtains the immutable
envelope from the committed AdmissionReceipt through Store's public historical decoder. If the
daemon loses the acknowledgement after receipt plus RUNNING commit and before provider start, a
reopened Coordinator detects that receipt during ordinary reconciliation, decodes its exact
historical RouteCell digest, and starts that envelope without reserving again or consulting the
active pointer, routing tables, profile registry, or timeout environment. Provider argv and
supervision consume the same decoded launch configuration.

Legacy RUNNING and terminal records remain readable. A legacy WAITING row without all four
subject/route facts is refused as `admission_identity_migration_required`; no owner infers missing
authority. Legacy no-admission reservation is exposed through a distinct compatibility seam and
does not weaken the required composed `ReservationIntent` types.

## Falsifiable consequences

- `tests/test_issue_627_admission.py` fails if the pre-prepare probe blocks preparation, the final
  root is not authoritative, named authority failures become terminal, or historical lost-ack
  recovery consults current authority.
- `tests/test_canary_attribution.py` fails if any callback can mutate Store, any precommit cutpoint
  publishes a partial output, a forged receipt reads successfully, or the v4 fingerprint changes.
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
- Launch the active route after commit: rejected because pointer changes would alter an already
  admitted execution.
- Persist capability evidence or repair prose: rejected because only the closed failure class is
  durable/visible production state.
- Terminally hold missing authority: rejected because a deployment between cycles must allow the
  existing claim to admit without resubmission.
