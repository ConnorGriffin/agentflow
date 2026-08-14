# ADR 585 — Bounded operational self-healing

Status: Accepted

Date: 2026-08-13

## Context

AgentFlow must contain verified operational failures without creating a second policy authority.
GitHub and checked-in decisions own semantics and approval; the coordinator Store owns current
operational state. An automatic response therefore has to be deterministic, reversible, scoped to
one immutable launch identity, and durable across claims and crashes.

This decision begins from exact dependency merges:

- capability parity #582: `a58dc0c84a7459774631048a67b3e71f8328d144`;
- human-governed promotion #584: `ef08dd3d2f691aa154ddaa193e6161b559099396`.

The pinned capability manifest SHA-256 is
`cba84e63be53884e6ed566a534883912f7d22156aad7e4a5590515140d18fcad`. The pinned
promotion scope registry SHA-256 is
`83e02ca43be08e0505d7075c5bdbe8ae032bf28ca50e4074a0632b4fd14a6006`.

## Decision

One `OperationalSafety` module extends the coordinator SQLite Store. Its interface registers and
resolves immutable RouteCells, accepts typed operational observations, reconciles claimed actions,
reopens one exact quarantine from fresh proofs, activates an approved canary, rolls it back to its
declared predecessor, and participates in the existing Store reservation transaction. It has one
variable adapter: the transport-only deterministic-rerun effect. There is no filesystem, GitHub,
prompt, fixture, rubric, policy, routing, effort, autonomy, or merge adapter.

A RouteCell binds repository, stage, provider, model, route ID, and a canonical-JSON SHA-256 launch
configuration artifact. Artifacts and RouteCells are append-only and content-addressed. Dispatch
resolution reads only the active pointer; rollback changes that pointer and never configuration
bytes. The RouteCell contract digest is
`c762ed469c4c2a311391898196713b26a2dbe2985896c262ea05a425368f63a5`.

The code-owned deterministic-check allowlist contains capability parity v1 and route health v1.
Each declaration binds identifier/version, side-effect-free status, subject-revision and RouteCell
requirements, and its success predicate. Its digest is
`66af2cb2c82a3cba92170e0d920f7a4ea9cae8509f482969f90883a42ca47458`.
Unreadable output is transport state: it may claim the one rerun and one transport alert but is
never counted as verified semantic failure. Exact duplicate observations return the existing
intent and cannot add actions or alerts. Two distinct verified failures for one exact
repository/subject/revision/check/version/RouteCell scope claim one quarantine action, append one
route alert, and quarantine only the active matching digest.

Quarantine admission validation runs through
`participate_in_admission(existing_store_connection, route_cell_digest)`. `Store.reserve` invokes
that participant within its existing `BEGIN IMMEDIATE`, before the WAITING-to-RUNNING record write
commits. This is only the shared transaction seam: #627 still owns coordinator dispatch wiring,
briefing and attribution resolution, receipt propagation, and the eventual `Store.admit` interface.
Already-running rows are never selected or changed by quarantine. Reopen compares the exact active
and quarantined digest plus `safety_state_id`, and requires fresh passing capability-parity and
route-health proofs bound to that state.

Every action first has one unique durable intent. Rerun reconciliation reads authoritative effect
evidence for its action ID before applying; a crash after effect but before result insertion thus
cannot replay the effect. Quarantine and rollback effects live entirely in the Store, so their
intent, state change, and result commit in one transaction. The action-state map is:

| Kind | States |
|---|---|
| rerun | `claimed → externally_effected → result_committed` |
| quarantine | `claimed → exact_cell_quarantined + result_committed` |
| rollback | `claimed → predecessor_pointer_restored + result_committed` |

An approved-canary receipt names the exact active bad RouteCell, its immutable predecessor, and the
current disabled generation. Rollback compares active digest and receipt in one Store transaction,
requires the committed quarantine of that active bad RouteCell, restores only its predecessor,
increments the disabled generation, and appends its action result.
A subsequent approval must name the incremented generation; an older approval or rollback cannot
overwrite a later canary or cross a RouteCell key.

Coordinator Store schema v2 adds append-only launch configuration, RouteCell, observation, action,
result, and alert tables plus RouteCell admission and canary activation state. The v1→v2 migration
adds these tables without rewriting existing continuation records or changing running permits.
The complete OperationalSafety contract digest is
`984242b3ea395814490c75d88c293cc20ae7747c0fd0c53f54e77e4ae31d317a`.

Capability parity remains before admission under ADR 582. A non-ready pre-launch capability result
retains its claim on environment-failure hold and consumes no permit, attempt, continuation, attempt
telemetry, or semantic Evidence. Post-launch environment failures keep their existing accounting.

## Alternatives

- Edit mutable routing configuration on quarantine or rollback: rejected because it loses exact
  identity and turns containment into policy mutation.
- Store action intent after an external effect: rejected because a crash can replay the effect.
- Treat unreadable output as check failure: rejected because transport absence is not semantic
  evidence and could quarantine a healthy route.
- Wire full admission here: rejected because briefing delivery and canary attribution belong to
  #627/#628/#635 and must not create overlapping writers.

## Consequences

Automatic authority is limited to one same-revision rerun, one exact RouteCell quarantine, and one
approved-canary predecessor rollback. Fixtures, rubrics, skills, prompts, ordinary routing/effort,
policy promotion, autonomy, merge policy, GitHub decision artifacts, canary reporting, briefing,
UI, and killing in-flight work remain outside this module. The Store is the sole operational writer;
external effect evidence remains content-free and keyed by the durable action ID.
