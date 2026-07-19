# ADR 0041 — Stage model and reasoning-effort matrix

- Status: Accepted
- Date: 2026-07-19
- Builds on: [ADR 0040](0040-spend-per-success-measurement-contract.md)
  (the measurement contract this matrix applies) — **note: ADR 0040 lands on PR
  [#233](https://github.com/ConnorGriffin/agentflow/pull/233), unmerged at time of
  writing; this ADR takes the next free number, 0041**
- Relates to: [ADR 0028](0028-stage-scoped-continuations.md) /
  [ADR 0038](0038-conflict-resolution-as-revise.md) (Revise carries builder
  complexity), [ADR 0029](0029-admission-permit-ledger.md) (`MODEL_FOR` is a
  validation, not a second sizing dial)
- Evidence: [stage-model-reasoning matrix research](../research/stage-model-reasoning-matrix.md),
  wayfinder ticket [#230](https://github.com/ConnorGriffin/agentflow/issues/230)
  (map [#226](https://github.com/ConnorGriffin/agentflow/issues/226))

## Context

Map #226 asks which model and provider reasoning-effort each pipeline stage should
run, per work shape, to spend prepaid-plan headroom well without hurting delivery.
Two facts constrain the answer. First, **provider reasoning effort is recorded
nowhere** in the coordinator's history — so history can calibrate *model* choice
per stage but can say nothing about reasoning effort. Second, the daemon does not
pick a model directly: it picks a **complexity** (`standard`/`deep`) per stage, and
a fixed table (`MODEL_FOR`) maps complexity + pool to a concrete model. "Which
model runs a stage" is therefore "which complexity that stage carries." Three days
of coordinator data (2026-07-16 → 2026-07-19) leaves most cells below the
contract's sample minimums.

## Decision

The stage × complexity/work-shape → model matrix is fixed as in the research doc.
Its load-bearing rulings:

- **Model per stage is mostly unchanged**, because the two spend concentrations —
  Opus Build (63% of Claude spend) and all-deep Intake — are governed by the
  *complexity assigned upstream*, not by a per-cell model swap. Opus is the correct
  model for `deep`; the question of whether the work needed `deep` belongs to
  [#228](https://github.com/ConnorGriffin/agentflow/issues/228), not here.
- **Intake is flagged as the prime downshift candidate** — a standard (Sonnet)
  default with a direct-deep trigger set — but the decision is deferred to #228's
  step-up replay, since history contains zero standard Intakes to compare against.
  This ADR records the recommended cell shape and trigger design; it does not
  change routing.
- **Deep cross-tool Review stays, unchanged**, as the baseline safety policy. It
  costs a $0.57 median — cutting it trades the 23% BLOCK-rate guardrail for
  pennies. It is explicitly not an optimization target.
- **Finding-driven Revise keeps carrying the original builder's complexity**
  (confirmed working). **Conflict Revise** (hardcoded deep, zero samples in the
  window) should carry builder complexity too, for parity — a directional
  consistency ruling to confirm once #223 records a conflict cohort. **Respond**
  (hardcoded deep, $0.30 median, immaterial) is left unchanged on cost grounds.
- **Every reasoning-effort cell is "unset — pending #223."** Reasoning effort is
  recorded nowhere, so no cell sets it from history. It becomes measurable only
  when [#223](https://github.com/ConnorGriffin/agentflow/issues/223) captures model
  and reasoning effort per attempt at launch.

## Alternatives considered

- **Blanket-downshift every always-deep stage to standard.** Rejected: Review's
  safety and immaterial savings on Respond/Converse do not justify the risk, and
  Intake's downshift needs a controlled replay, not a blind flip.
- **Swap Build's model to cut the Opus bill.** Rejected: Opus is correct for
  `deep`; the lever is the complexity Intake assigns (#228).
- **Set reasoning effort now.** Rejected: recorded nowhere; any value is a guess.
- **Compare Codex build headroom against Claude's to pick a cheaper pool.**
  Rejected: incommensurable pool formulas and no Codex dollar cost by construction.

## Consequences

- No engine routing changes ship from this ticket. Build/Review/Revise-finding/
  Respond/Converse/Research/Mockup model cells stand as-is.
- #228 owns the Intake standard-first decision and inherits this doc's trigger
  design as its hypothesis.
- Conflict Revise carrying builder complexity is a directional recommendation for a
  future small change, gated on #223 producing a cohort.
- All reasoning-effort tuning is blocked on #223 telemetry; the matrix's
  reasoning-effort column stays uniformly unset until then.
