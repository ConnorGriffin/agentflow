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
resolves immutable RouteCells, accepts an `ObservationRequest`, reconciles claimed actions, reopens
one exact quarantine from authority-read evidence references, activates a receipt-approved canary,
rolls it back to its declared predecessor, and participates in the existing Store reservation
transaction. Its injected interfaces are `CheckEvidenceAuthority.read(evidence_ref)`,
`PromotionReceiptAuthority.read(receipt_id)`, and the transport-only `RerunEffect.evidence_for` /
`apply`. A supplied rerun adapter must hold the exact frozen, code-owned
`RERUN_EFFECT_CONTRACT` singleton; an equal caller-created declaration is not authority. Its digest
is `c8b9433f01643550f2dbc560b72aa033895b324e5a5a35dedc0d4b3af13b21b4`, and it requires durable,
atomic coalescing of overlapping effects by `ActionIntent.action_id`. OperationalSafety validates
this binding both when an adapter is supplied and immediately before reconciliation. The production promotion implementation
is `PromotionReceiptReader`, which opens the #584 Evidence store read-only, starts one read snapshot,
and revalidates exact schema v4 on every connection before reading a receipt. It adds no EvidenceStore
verb or promotion write. OperationalSafety accepts only receipts produced by the exact #584
`github-authority/v1` verifier. There is no
filesystem, GitHub, prompt, fixture, rubric, policy, routing, effort, autonomy, or merge adapter.

A RouteCell binds repository, stage, provider, model, route ID, and a canonical-JSON SHA-256 launch
configuration artifact. Artifacts and RouteCells are append-only and content-addressed. Every
RouteCell read reparses canonical JSON, recomputes its digest and cell key, compares all duplicated
columns, and then reparses and recomputes the referenced launch-configuration digest. Dispatch
resolution reads only the active pointer; rollback changes that pointer and never configuration
bytes. Every route-state read recomputes `safety_state_id` from cell key, active digest, quarantined
digest, and generation. The active RouteCell must resolve back to that exact cell key. A quarantine
must name the active digest, retain its quarantine action ID, and bind a committed quarantine result;
an unquarantined row may retain neither pointer nor action. Resolution, state reads, canary reads,
reopen, pointer changes, and admission all use that verifier. Any tamper, cross-cell pointer, or
non-canonical stored value fails closed. The RouteCell contract digest is
`c762ed469c4c2a311391898196713b26a2dbe2985896c262ea05a425368f63a5`.

The code-owned deterministic-check allowlist contains capability parity v1 and route health v1.
Each declaration binds identifier/version, side-effect-free status, subject-revision and RouteCell
requirements, and its success predicate. Its digest is
`66af2cb2c82a3cba92170e0d920f7a4ea9cae8509f482969f90883a42ca47458`.
Callers cannot supply an outcome or verified flag. `CheckEvidenceAuthority` returns the exact
observation ID, identity fields, outcome, proof, evidence reference, RouteCell digest, and reviewed
declaration digest. OperationalSafety accepts semantic pass/fail only when all fields bind the
request and the code-owned declaration. `CheckEvidenceUnavailable` alone becomes unreadable
transport state: it may claim the one rerun and one transport alert but is never counted as
verified semantic failure. Exact duplicate observations return the existing intent and cannot add
actions or alerts. Two distinct authority-verified failures for one exact
repository/subject/revision/check/version/RouteCell scope claim one quarantine action, append one
route alert, and quarantine only the active matching digest.

Quarantine admission validation runs through
`participate_in_admission(existing_store_connection, route_cell_digest)`. `Store.reserve` invokes
that participant within its existing `BEGIN IMMEDIATE`, before the WAITING-to-RUNNING record write
commits. This is only the shared transaction seam: #627 still owns coordinator dispatch wiring,
briefing and attribution resolution, receipt propagation, and the eventual `Store.admit` interface.
Already-running rows are never selected or changed by quarantine. Reopen compares the exact active
and quarantined digest plus `safety_state_id`, and rereads fresh passing capability-parity and
route-health results from `CheckEvidenceAuthority`; each must bind that state, RouteCell, evidence
reference, and the corresponding code-owned declaration digest. Callers supply no proof objects.

Every action first has one unique durable intent. Rerun reconciliation uses three boundaries: a
short Store transaction CAS-claims a versioned, expiring `safety_rerun_claims` lease; the owner asks
for authoritative effect evidence and, if absent, invokes the action-ID-idempotent effect with no
Store lock or transaction held; a second short Store transaction CAS-validates the lease, commits
the result, and deletes the claim. Concurrent reconcilers wait outside transactions while the lease
is live. Unrelated Store writers and reentrant writes from the effect therefore proceed. A caught
failure releases the lease immediately; a process crash leaves a durable lease that can be reclaimed
after expiry. Recovery always asks `evidence_for(action_id)` before applying, and the bound adapter
atomically coalesces an overlapping apply for the same action ID even when a live effect outlasts
its reclaimed lease. A superseded reconciler waits for and returns the one durable result rather
than failing or committing without its lease. Quarantine and rollback effects live entirely in the Store, so
their intent, state change, and result commit in one transaction. The action-state map is:

| Kind | States |
|---|---|
| rerun | `claimed → effect_lease_claimed → idempotent_effect → result_committed` |
| quarantine | `claimed → exact_cell_quarantined + result_committed` |
| rollback | `claimed → predecessor_pointer_restored + result_committed` |

`CanaryActivationRequest` declares the exact active bad RouteCell, its immutable predecessor, and
the current disabled generation. Its canonical digest excludes the receipt ID; an authoritative
#584 promotion receipt must bind that digest through its verified immutable authority pointer,
accepted fleet/repository scope, and policy version. Rollback accepts no caller proof strings. It
compares active digest and receipt in one Store transaction, requires the committed quarantine
action and result of that active bad RouteCell, derives its durable proof from those authorities,
restores only the predecessor, increments the disabled generation, and appends its action result.
An exact duplicate rollback (approval digest plus receipt ID) validates and returns its existing
durable action/result before any receipt-store access or active-pointer compare-and-swap, including
when receipt storage is unavailable or a later approved canary has activated.
A subsequent approval must name the incremented generation; an older approval or rollback cannot
overwrite a later canary or cross a RouteCell key.

Coordinator Store schema v2 adds append-only launch configuration, RouteCell, observation, action,
result, alert, and rerun-lease tables plus RouteCell admission and canary activation state. Before
migration,
the Store accepts only the exact v1 records schema. Before accepting or advancing to v2, it compares
every non-SQLite table/index/trigger definition with the exact expected schema and fails closed on
missing, altered, or additional objects. The v1→v2 migration adds the safety tables without
rewriting existing continuation records or changing running permits.
The complete OperationalSafety contract digest is
`27e095449758459f748a9f8a85ec48afc460cb294bcb859c675dc568e8252d6d`.

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
external effect evidence remains content-free and keyed by the durable action ID. #627 still owns
dispatch/admission wiring beyond the declared shared Store transaction seam. #635 still owns
canary reporting, briefing, UI, policy promotion, and any decision about in-flight work.
