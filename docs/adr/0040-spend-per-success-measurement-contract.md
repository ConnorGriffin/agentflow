# ADR 0040 — Spend-per-success measurement contract

- Status: Accepted
- Date: 2026-07-19
- Complements: [ADR 0025](0025-activity-adaptive-spend-ceiling.md) (headroom as
  the cumulative signal), [ADR 0028](0028-stage-scoped-continuations.md) /
  [ADR 0030](0030-session-coordinator-seam.md) (verified stage outcomes)
- Evidence: [spend-per-success research](../research/spend-per-success-measurement-contract.md),
  wayfinder ticket [#227](https://github.com/ConnorGriffin/agentflow/issues/227)
  (map [#226](https://github.com/ConnorGriffin/agentflow/issues/226))

## Context

Map #226 runs four spend experiments (#228–#231) whose results feed one terminal
routing-policy decision. Without a fixed measurement contract, an experiment
could claim savings from cheap failures (spend less by finishing less) or be
punished for drawing genuinely difficult work. The operator separately ruled
([#232](https://github.com/ConnorGriffin/agentflow/issues/232#issuecomment-5014939240))
that the production optimization objective is prepaid-plan headroom, not
provider dollar cost.

## Decision

All four experiments and the terminal policy decision measure spend under one
contract:

- **Two metrics, always together.** *Cost per verified stage* (numerator: every
  attempt charged to the logical stage identity, including superseded and failed
  attempts and Codex descendants; denominator: stages completing with their
  tracer-verified outcome — never provider exit status) and *cost per merged
  issue* (numerator: everything the issue consumed across stages, tools, and
  rounds; denominator: landed changes). Failed spend always lands in a
  numerator, so failure can never improve a metric.
- **Headroom is the target; dollars are the comparison signal.** Spend is
  denominated in per-pool headroom-weighted tokens (the gate formulas), never
  compared across pools; the normalized dollar equivalent is reported alongside
  strictly for cross-tool comparison. Raw token counts are diagnostics only.
- **Guardrails gate every savings claim:** merge rate, hold/park rate, review
  BLOCK rate and blocking findings, revise rounds, retries per completed stage,
  and time-to-merge must not degrade versus control.
- **Cohort cells protect difficult work:** comparisons happen within stage ×
  pool × model × complexity × effort × repo cells (or with fixed cell weights),
  with blinded reviewers, ≥10 completed stages per quantitative cell (5–9
  directional, <5 insufficient), medians and P75/P90.
- **Success is operationalized:** "materially reduces spend" = ≥20% lower median
  headroom spend per verified stage in the declared cells with no increase in
  spend per merged issue; "no meaningful degradation" = at most one adverse
  guardrail event beyond control per 10 trials and median time-to-merge within
  +25%.

## Alternatives considered

- **Raw tokens as the headline.** Rejected: cache reads dominate volume at
  near-zero marginal spend; it rewards cache-hostile behavior.
- **Dollar cost as the target.** Rejected by operator ruling: prepaid capacity is
  the real constraint; dollars survive only as the normalization signal.
- **Headroom only.** Rejected: the pool formulas are incommensurable, making
  cross-tool experiment arms uncomparable.
- **Per-session completion as success.** Rejected: every historical Claude
  session exited "success", including ones whose stage parked — only
  tracer-verified stage outcomes count.
- **One blended fleet metric.** Rejected: lets an arm win by drawing easier
  work.

## Consequences

- #228–#231 apply the contract as written; results that break cell minimums are
  "insufficient", not wins.
- The historical baseline (coordinator era 2026-07-16 → 2026-07-19) is fixed in
  the research doc with its blind spots named; claims beyond it wait for
  [#223](https://github.com/ConnorGriffin/agentflow/issues/223) per-attempt
  telemetry, which should persist exactly this contract's dimensions (attempt →
  stage identity, model, effort, reasoning effort, tokens, weighted headroom,
  normalized cost, verified outcome).
- Superseded-attempt overhead (7.8% of Claude headroom in the baseline) is the
  measured floor for [#225](https://github.com/ConnorGriffin/agentflow/issues/225).
