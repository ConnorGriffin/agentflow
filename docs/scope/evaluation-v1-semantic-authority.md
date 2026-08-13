# Evaluation v1 semantic authority

## Decisions

- The one official Evaluation v1 rulebook is a versioned canonical data contract consumed by AgentFlow and its independent verifier. Runtime code owns execution mechanics only; fixtures and locks prove conformance but cannot redefine semantics. This prevents thresholds, denominators, truth tables, aggregation, bootstrap, and authority scope from drifting across copies. → ADR

## Open questions

None.

## Spawned tasks

- [Prove evaluation artifact closure and portable CI](https://github.com/ConnorGriffin/agentflow/issues/607) — resolved; one committed canonical lock binds the reviewed artifact set and CI command.
- [Define the executable evaluation prerequisite gate](https://github.com/ConnorGriffin/agentflow/issues/608) — resolved; one two-phase facts checker binds clean review, exact merges, digests, and ADR 583.
- [Decide one executable authority for Evaluation v1 semantics](https://github.com/ConnorGriffin/agentflow/issues/605) — this interview owns the remaining policy choice.
- [Map the README and documentation split for the learning pipeline](https://github.com/ConnorGriffin/agentflow/issues/610) — resolved and merged; every README section now has one planned home and learning details remain gated on shipped behavior.
