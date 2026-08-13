# Evidence producer facts and lineage

Status: Wayfinder finding for #592 (2026-08-12)

## Ruling

Evolve the one public `EvidenceStore.observe` interface to accept a **versioned,
tagged Evidence envelope**: retain the v1 `Observation` arm unchanged for classified
failure evidence and add a `producer_fact` arm for content-free facts and typed
lineage. Do not add optional producer fields to `Observation`, and do not add a public
lineage verb or a second store.

This is the smallest deep module: `observe` remains the one write seam and Evidence
continues to own redaction, immutable-id replay, canonicalization, link resolution,
retention, and briefing. Deleting that module would otherwise distribute those rules
among intake, review, Wayfinder, and skills producers. A second public verb instead
splits one append-and-canonicalize operation, while optional fields leave every caller
to infer which sparse combination is meaningful. That loses locality and creates a
caller-specific schema rather than leverage. This applies the charter's depth and
deletion tests. [Engineering charter](../../AGENTS.md), [ADR 580](../adr/adr-580-evidence-module-interface-and-retention.md), [#578](https://github.com/ConnorGriffin/agentflow/issues/578)

The existing `Observation` is intentionally a record of one classified failure
recurrence: its event identity includes `failure_class`, and its initializer rejects
any class outside the closed six-value vocabulary. `contract-v1.json` accepts that
envelope only. A claim, criterion, decision, delegation, fix, or settlement is not a
failure recurrence, so making `failure_class` optional would weaken the meaning of the
current type and still leave action and lineage untyped. [evidence.py](../../agentflow/evidence.py), [contract-v1.json](../evidence/contract-v1.json), [#573](https://github.com/ConnorGriffin/agentflow/issues/573)

## Caller-visible interface

`observe` accepts exactly one value from `EvidenceEnvelopeV1 | EvidenceEnvelopeV2` and
returns the existing canonical `Event`. No caller reads tables, supplies a canonical
event ID, or writes an edge separately.

```python
@dataclass(frozen=True)
class EvidenceEnvelopeV2:
    envelope_kind: Literal["failure_observation", "producer_fact"]
    observation_id: EvidenceId
    subject: SubjectRevision
    source: AuthorityPointer
    observed_at: UnixSeconds
    links: tuple[EvidenceLink, ...] = ()
    # exactly one arm below is present
    failure: FailureFacts | None = None
    producer: ProducerFacts | None = None

@dataclass(frozen=True)
class FailureFacts:
    failure_class: FailureClass
    validation_state: ValidationState
    signature_digest: Digest
    normalizer_version: VocabularyVersion
    reviewed_parent_revision: Sha | None = None
    fixer_revision: Sha | None = None

@dataclass(frozen=True)
class ProducerFacts:
    producer_kind: ProducerKind
    fact_digest: Digest
    normalizer_version: VocabularyVersion
    validation_state: ValidationState
    review_action: ReviewAction | None = None
```

The v1 dataclass remains accepted as the `failure_observation` arm with no links and
the same canonical identity. JSON v2 is a tagged envelope, not an additive v1 object.
V1 fixtures and the skills #31 byte-for-byte pin remain valid until that repository
explicitly elects to consume v2. [#31](https://github.com/ConnorGriffin/skills/issues/31), [#581](https://github.com/ConnorGriffin/agentflow/issues/581)

`AuthorityPointer` and `SubjectRevision` retain their current immutable-revision and
digest checks. `fact_digest` and `signature_digest` are normalizer outputs, never a
free-text finding, summary, prompt, source body, or transcript. Thus the envelope is
not a digest/string side channel: each digest has a typed arm, closed kind, normalizer
version, immutable source, and bounded links.

## Closed facts and applicability

V2 owns these closed, versioned vocabularies; an unknown value is rejected rather than
stored for a caller to interpret:

| Field | Values / rule |
| --- | --- |
| `FailureClass` | The existing six values, unchanged: `original_defect`, `plan_gap`, `slice_scope_error`, `reviewer_false_claim`, `speculative_preference`, `fix_introduced_defect`. |
| `ValidationState` | The existing six values, unchanged. Required for every admitted envelope; a producer kind never implies it. Authoritative structural facts normally state `observed` or `human_validated`. |
| `ProducerKind` | `claim`, `criterion`, `decision`, `disposition`, `objection`, `revision`, `verdict`, `delegation`, `slice`, `decline`, `finding`, `review_action`, `fix`, `verification`, `settlement`. |
| `ReviewAction` | `fix_before_completion`, `necessary_follow_up`, `ask_maintainer`, `discard_preference`; required only by `review_action`, forbidden otherwise. |
| `LineageRelation` | `derives_from`, `governs`, `addresses`, `delegates`, `implements`, `verifies`, `refutes`, `revises`, `settles`. |

`failure_observation` requires `FailureFacts` and forbids `ProducerFacts`; it may carry
links, but `reviewed_parent_revision` and `fixer_revision` remain permitted only for
`fix_introduced_defect`. `producer_fact` requires `ProducerFacts`, forbids
`FailureFacts`, and therefore never needs a failure class. `finding` carries a failure
class only by linking to its separately observed failure; it does not duplicate the
classification. `fix` requires at least one `addresses` link; `settlement` requires at
least one `settles` link; `delegation` or `slice` requires at least one `delegates` or
`derives_from` link. These are module-enforced invariants, not producer conventions.
Every producer fact explicitly carries a validation state; miners reject
`unvalidated` and `refuted` facts and never infer epistemic status from its kind,
authority, or links.

The vocabulary belongs to the versioned Evidence contract, not to a provider. Adding a
semantic kind or relation is a new contract version with fixtures and an ADR-backed
ownership decision; providers cannot mint strings. This keeps the Agentflow glossary's
distinction between evidence events, observations, review actions, and settlement
meaningful. [CONTEXT.md](../../CONTEXT.md), [#575](https://github.com/ConnorGriffin/agentflow/issues/575)

The vocabulary covers every downstream fact already named without caller-minted strings:

| Downstream fact (#581 / skills #31) | `ProducerKind` |
| --- | --- |
| scope or Wayfinder claim; criterion | `claim`; `criterion` |
| decision, disposition, objection | `decision`; `disposition`; `objection` |
| revised artifact or source revision | `revision` |
| planned delegation, slice plan, bounded result | `delegation`; `slice`; `verification` |
| decline or collapse | `decline` |
| review finding and its action | `finding`; `review_action` |
| reproduction, refutation, verification | `verification` (with `verifies` or `refutes` link) |
| review verdict or convergence | `verdict` |
| revise fix | `fix` |
| merge, park, or other evaluated outcome | `settlement` |

`verification` expresses the result of a check and `verdict` the review's bounded
judgment or convergence; neither is overloaded into `settlement`, which records the
subsequent merge or park. This adds the four necessary closed names (`revision`,
`verdict`, `decline`, and the existing `settlement` for outcome) rather than distorting
them into generic decisions or verification.

## Lineage, identity, and bounds

An `EvidenceLink` is:

```python
@dataclass(frozen=True)
class EvidenceLink:
    relation: LineageRelation
    target_event_id: EvidenceId
    ordinal: int  # 0..31, dense and unique across this observation
```

`target_event_id` must already resolve to a canonical Evidence event in the same
transaction; it is not an arbitrary locator or a digest. A producer first observes the
upstream fact from its own immutable authority, then observes the dependent fact with a
link to that event. This rejects forward, missing, cross-store, and deleted-reference
dangling provenance. The dense ordinal makes relation lists deterministic and preserves
the source order where a producer has one (for example, selected criteria or the
complete actionable-finding set); it never claims causality beyond the relation value.
At most 32 links are admitted and link triples are unique. Resolved-only insertion
order makes each edge point from a newly committed canonical event to an earlier one;
therefore a cycle is unreachable and the stored lineage is a DAG without an ancestor
traversal guard. The source pointer remains the reference to the external system of
record, not an Evidence-owned copy of it. This matches #581's rule that a multi-finding
fix references the complete actionable set rather than inventing a one-to-one cause.
[#581](https://github.com/ConnorGriffin/agentflow/issues/581)

Failure-event identity stays exactly as ADR 580 specifies:

```text
repository + subject + subject revision + failure class
  + normalized-signature digest + normalizer version
```

Producer-event identity is instead:

```text
repository + subject + subject revision + producer kind
  + fact digest + normalizer version + ordered (relation, target-event-id) links
```

The link tuple belongs in producer identity: the same digest supported by a different
criterion, decision, or review is a different auditable fact, not a silently merged
provenance side channel. A repeated source observation of either identity is idempotent
and remains auditable. It does not increase failure recurrence; producer facts have no
recurrence measure, and source-observation multiplicity is not a recurrence count.
`fix_introduced_defect` remains a separate failure event and is linked through its
reviewed-parent and fixer-head facts, preserving the distinction required by #573.

All IDs retain the current 128-character token limit; digests retain the existing 32–128
lowercase-hex bound; links cap at 32; and existing candidate event references cap at 32.
Reject any unrecognized envelope field and the existing forbidden content fields, plus
free-form `summary`, `reason`, `payload`, `metadata`, and nested arbitrary objects.
Pointers, IDs, enums, timestamps, normalizer versions, and digests are the complete
durable content budget. [evidence_contract.py](../../agentflow/evidence_contract.py),
[ADR 580](../adr/adr-580-evidence-module-interface-and-retention.md)

## Migration and tests

Keep `contract-v1.json` and every `*-v1.json` fixture immutable. Add
`contract-v2.json`, positive arm fixtures, and negative fixtures for arm mixing,
unknown vocabularies, positive facts with a failure class, review action on another
kind, missing validation state, `unvalidated`/`refuted` miner input, forward or missing
links, non-dense ordering, unbounded links, raw text, and v1/v2 parsing. Public-interface
tests call `observe`, not tables, to prove v1 replay identity, v2 producer identity,
resolved-only link order/DAG construction, rejection, redaction, retention, explicit
validation handling, and a request revision → criterion → finding/fix → merge-or-park
chain. Fixtures for #581 must also prove edited source revisions make new criterion IDs,
and two findings/one fix creates complete-set rather than invented per-finding lineage.

#592 authorizes the required evolution of ADR 580's migration rule. The selected build
must add an ADR amending ADR 580: only an exact v2 schema fingerprint may migrate to
v3; the migration is transactional and atomic; it preserves every v2 row, canonical
event ID, observation ID, and receipt binding status; and unknown or tampered stores
remain fail-closed. An injected migration failure must roll back both schema version and
all data. New stores create v3 only after that amendment; v1 remains supported through
the existing exact v1→v2 migration followed by the exact v2→v3 migration. [ADR 580](../adr/adr-580-evidence-module-interface-and-retention.md)

```yaml
wayfinder_findings:
  candidates:
    - id: evidence-envelope-v2-and-v3-migration-decision
      disposition: handoff_required
      title: Implement typed Evidence envelopes and the authorized v2-to-v3 migration
      outcome: Evidence keeps one observe interface while admitting a v2 tagged failure-or-producer envelope, enforced typed lineage, explicit validation for every fact, immutable v1 compatibility, redaction bounds, and an ADR amendment that permits only the exact atomic v2-to-v3 migration.
    - id: evidence-producer-adapters-v2
      disposition: handoff_required
      title: Emit typed producer facts from methodology and pipeline adapters
      outcome: Intake, methodology skills, review, revise, and settlement producers emit linked claims, criteria, decisions, delegations, findings, actions, fixes, and settlements through the v2 contract, enabling #581's public request-to-settlement and miner tests without a parallel evidence store.
```
