# ADR 0046 — Production routing and spend policy locked

- Status: Accepted
- Date: 2026-07-19
- Amended: 2026-08-03 — deep/`extra` Revise inherits the deep/`extra` Build ceiling of
  300 turns / 60 minutes ([#473](https://github.com/ConnorGriffin/agentflow/issues/473))
- Builds on: [ADR 0040](0040-spend-per-success-measurement-contract.md) (measurement
  contract), [ADR 0041](0041-stage-model-reasoning-matrix.md) (model matrix),
  [ADR 0042](0042-codegraph-okf-complementary-layer.md) (context backend),
  [ADR 0043](0043-recovery-state-before-replay.md) (retry budgets),
  [ADR 0044](0044-stage-session-profiles-and-ceilings.md) (session profiles),
  [ADR 0045](0045-intake-stays-all-deep.md) (no Intake step-up)
- Evidence: wayfinder map [#226](https://github.com/ConnorGriffin/agentflow/issues/226),
  terminal ticket [#232](https://github.com/ConnorGriffin/agentflow/issues/232);
  74-issue effort-calibration backtest (this ticket's grilling session)

## Context

Map #226 asked how to minimize provider spend per merged issue without hurting
delivery. All five evidence tickets settled (ADRs 0040–0045), most of the
resulting mechanism already shipped (#242/#246 profiles, #244 MCP pin, #243
structured output, #225 retry policy, #236 telemetry). #232 is the terminal
lock: compose the findings into one production policy and name what remains
tunable. Two gaps surfaced during the lock: reasoning effort is set nowhere
(no launch flag, no telemetry value), and the operator's original intent —
the `effort` dial configures the **builder's** provider reasoning effort —
was never wired; the dial only ever reached the brief as prose.

## Decision

1. **Ship now, tune later.** The policy locks today on provider-default
   reasoning effort and the ADR 0044 placeholder ceilings. Waiting for
   telemetry to fill cohort cells would block the lock on data that takes
   weeks; the placeholders are safe (1.5–2× observed maxima).
2. **The `effort` dial drives the builder's reasoning effort.** Restoring the
   original intent: `low → Low`, `medium → Medium`, `high → High`,
   `extra → Extra High`, identically on both tools; a rung above a model's
   ladder clamps to its top. **Max and Ultracode are never wired into the
   daemon** — manual-only escape hatches (the operator has never needed Max).
   Revise inherits the original builder's effort, consistent with carrying its
   complexity (ADR 0041). Non-build stages keep provider-default reasoning as
   unset tunable cells. The missing wiring (launch flag on both providers +
   recording `reasoning_effort` in per-attempt telemetry at launch) is a
   defect, filed as its own build issue.
   *(Amended 2026-08-03: inherited effort also selects the corresponding Build session
   ceiling; deep/`extra` Revise therefore receives 300 turns / 60 minutes, while the other
   deep and standard ceilings remain unchanged.)*
3. **Intake coaching is an anchored rubric, not machinery.** A 74-issue
   backtest showed the current one-line-of-prompt dial is already
   well-calibrated: median diff 71 → 183 → 389 → 1823 lines across the four
   rungs, revise rate flat at 4–6%, worst misses one rung off. So the intake
   prompt gains per-rung behavioral anchors drawn from real history (including
   the backtest's misrated examples); no numeric rating system and no
   findings-score feedback loop — they would solve a problem the data says
   barely exists, and a live rubric replay would inherit ADR 0045's
   unscoreable-corpus trap.
4. **Recalibration is a monthly by-hand operator pass.** Each month the
   operator compares any cohort cell that crossed the contract's ≥10-stage
   minimum against control (ADR 0040 metrics + guardrails), tightens
   reasoning-effort and ceiling values, and audits effort misrating from
   telemetry (a `low` that ate revise rounds, a `high` that finished in a few
   turns) to sharpen the rubric anchors. A guardrail breach versus control
   reverts that cell by hand. No automated recalibration or auto-rollback is
   built until hand-tuning has taught what it should encode.

## Alternatives considered

- **Hold #232 until telemetry ratchets the numbers.** Rejected: the policy is
  coherent without tuned cells, and the lock gates nothing else.
- **`extra → Max` on Opus.** Rejected: asymmetric ladders break cross-tool arm
  comparability, Max's reasoning output is 5×-weighted against headroom, and
  the operator has never needed it. The monthly pass may promote later if
  extra-effort builds show quality strain.
- **Stage-keyed reasoning for the builder** (ignore the dial). Rejected: the
  backtest shows the dial tracks real work size monotonically, so it is
  exactly the signal reasoning depth should follow — and it was the design
  intent all along.
- **Numeric effort scoring / findings-score auto-adjustment.** Rejected on the
  backtest: misses are rare and mild; anchors are cheaper and models rate
  anchored rubrics better than formulas.

## Consequences

- Map #226 is complete: objective (headroom), measurement (0040), routing
  matrix (0041 + this), context backend (0042), retry budgets (0043), session
  envelopes (0044), Intake policy (0045), and the tuning loop are all settled.
  Remaining work is ordinary build issues, not decisions.
- Build issues from this lock: wire `effort → builder reasoning effort` (both
  providers, telemetry at launch), add the anchored effort rubric to the
  intake prompt, and a standing monthly recalibration issue.
- The `effort` glossary entry sharpens: the dial now *configures* builder
  reasoning depth rather than only guiding scope in prose.
