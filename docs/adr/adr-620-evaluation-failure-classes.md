# ADR 620 — Evaluation failure classes

Status: accepted
Date: 2026-08-13

## Context

The findings and disposition from [#573](https://github.com/ConnorGriffin/agentflow/issues/573)
and the evaluation contract requested by [#583](https://github.com/ConnorGriffin/agentflow/issues/583)
require a closed vocabulary for classifying evaluation failures. The vocabulary must preserve
the distinction between a defect in the subject, a planning or slicing mistake, an invalid review
claim, and a defect introduced while fixing one. [ADR 605](adr-605-canonical-evaluation-rulebook.md)
makes the versioned evaluation contract the semantic authority; [ADR 606](adr-606-explicit-missing-metrics-and-adjudication-lineage.md)
defines adjacent evaluation data without changing failure classification.

## Decision

Evaluation v1 uses exactly these six failure-class identifiers and meanings:

| Identifier | Meaning |
| --- | --- |
| `original_defect` | The artifact violated a product, acceptance, security, or charter requirement before review. |
| `plan_gap` | The plan or acceptance criteria omitted, contradicted, or failed to operationalize required behavior. |
| `slice_scope_error` | Decomposition, ownership, or the implementation boundary was wrong. |
| `reviewer_false_claim` | Source, tests, or reproduction disproves a reviewer assertion. |
| `speculative_preference` | A finding lacks product/acceptance/charter grounding or targets an unreachable non-trust-boundary state. |
| `fix_introduced_defect` | A defect was absent at the reviewed head and appeared in a later reviewer/reviser change. |

`validation_state`, review action, and severity are orthogonal to `failure_class`. None of those
dimensions selects, aliases, changes, or implies a failure class: an event may be observed,
reproduced, refuted, model-judged, human-validated, or unvalidated independently of its class,
and action and severity remain separate facts.

The six identifiers are the complete public vocabulary. Aliases are rejected. Classes are not
merged to simplify storage, reporting, or policy. Recording a classified event does not mutate
policy automatically; any policy change remains a separately versioned, digest-identified,
governed proposal with authoritative approval.

## Alternatives

- **Aliases for readable or legacy names:** rejected because multiple identifiers for one meaning
  make the closed contract and cross-checking ambiguous.
- **Merged planning, slicing, and subject-defect classes:** rejected because the disposition
  would lose the causal distinction needed for evaluation and follow-up.
- **Classifying by validation status, review action, or severity:** rejected because those are
  independent dimensions and cannot carry failure semantics.
- **Automatic policy mutation from a classified event:** rejected because evidence is not policy
  authority; policy changes require their own governed approval and version.

## Consequences

The Evaluation v1 canonical contract, its artifacts, and its verifier consume one exact six-value
vocabulary. A new semantic class or a change to a meaning requires a new reviewed contract
version and an ADR; callers cannot mint synonyms or reinterpret an existing identifier.
