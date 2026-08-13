# Evaluation v1 unavailable metrics and adjudication lineage

## Decisions

- `missing_metric_names` is the exact sorted set of null arm-metric fields. A wholly unavailable result lists all seven metrics. A reported result may list only the four optional metrics whose values are null; its three required metrics remain present.
- An adjudication is valid only when its case ID, exact case-manifest digest, and answer-key digest match the canonical validated case record. Its own digest is recomputed from the canonical receipt with that digest field omitted.

## Open questions

None.

## Spawned tasks

- [Decide unavailable metrics and adjudication lineage](https://github.com/ConnorGriffin/agentflow/issues/606) — this interview owns both remaining schema rulings.
- [Decide one executable authority for Evaluation v1 semantics](https://github.com/ConnorGriffin/agentflow/issues/605) — resolved; the versioned canonical data contract is the sole semantic authority.
