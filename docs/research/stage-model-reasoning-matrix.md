# The stage × model × reasoning-effort matrix

Research for [Wayfinder #3: calibrate the stage model and reasoning-effort matrix](https://github.com/ConnorGriffin/agentflow/issues/230)
(map [#226](https://github.com/ConnorGriffin/agentflow/issues/226)), captured 2026-07-19.
The question: **what model and provider reasoning-effort matrix is cost-appropriate
for each stage and work shape?**

This applies the [spend-per-success measurement contract](spend-per-success-measurement-contract.md)
([ADR 0040](../adr/0040-spend-per-success-measurement-contract.md), on PR
[#233](https://github.com/ConnorGriffin/agentflow/pull/233)) as written. The ruling
itself is [ADR 0041](../adr/0041-stage-model-reasoning-matrix.md).

## Two dials, kept apart

The contract and the map both warn against conflating two different knobs, so this
doc is explicit:

- **Work effort** (`low` / `medium` / `high` / `extra`) — the agentflow dial that
  describes *how big the piece of work is*. It sizes the admission budget a Build
  reserves. It is set from the issue's labels and only exists on Build today.
- **Provider reasoning effort** — a per-request setting that tells the model *how
  hard to think* on a single turn. Agentflow does **not** set this anywhere today,
  and — critically — **it is recorded nowhere** in the coordinator's history.

Everything below that concerns *model* choice can be calibrated from history.
Everything that concerns *reasoning effort* cannot, and is marked so.

## What the model dial actually is today

The daemon does not choose a model directly. It chooses a **complexity**
(`standard` or `deep`) per stage, and a fixed table turns complexity + tool pool
into a concrete model (`agentflow/coordinator/admission.py`, `MODEL_FOR`):

| pool | standard | deep |
|------|----------|------|
| claude | Sonnet | Opus |
| codex | Terra (gpt-5.6) | Sol (gpt-5.6) |

So "which model runs a stage" is really "**which complexity does that stage
carry, on which tool**". The matrix below is expressed in those terms because
those are the only levers the engine has.

How each stage gets its complexity today (`agentflow/coordinated_build.py`):

- **Intake** — always `deep`. (50/50 records in the window are deep.)
- **Build** — from the issue's labels; carries a separate work-effort label.
- **Review** — always `deep`, and deliberately cross-tool: the safety net.
- **Revise (finding-driven)** — carries the **original builder's** complexity
  forward (`review.builder_complexity`).
- **Revise (conflict rebase)** — hardcoded `deep`.
- **Respond** — hardcoded `deep`; does **not** look at the builder's complexity.
- **Converse / Research / Mockup** — always `deep`.

## The historical baseline, by stage and cell

Reproduced read-only from `~/.agentflow/coordinator` (2026-07-16 → 2026-07-19)
with the contract's method: parse each session stream, join to its stage record
by launch token, weight headroom with the gate formulas, count only
tracer-verified completions. The totals reconcile with the contract's baseline
($387.10 Claude normalized; Opus builds $245.17; Intake 48 @ $42.23; Review
median $0.57). All costs are the **normalized dollar equivalent** (the cross-tool
*comparison* signal); **prepaid-plan headroom is the optimization target** — median
weighted-token headroom is shown alongside. Codex arms carry **no dollar cost by
construction** and their headroom is on the Codex formula, *not comparable* to
Claude's.

| Stage | Model (complexity) | Work effort | n | Median $ | Median headroom | Total $ |
|-------|--------------------|-------------|---|---------|-----------------|---------|
| Build | Opus (deep) | extra | 3 | 21.71 | 1.06M | 75.59 |
| Build | Opus (deep) | high | 12 | 7.27 | 0.58M | 83.75 |
| Build | Opus (deep) | medium | 15 | 2.87 | 0.26M | 84.68 |
| Build | Opus (deep) | low | 1 | 1.16 | 0.11M | 1.16 |
| Build | Sonnet (standard) | low | 12 | 0.70 | 0.09M | 9.45 |
| Build | Sonnet (standard) | medium | 2 | 0.94 | 0.15M | 1.89 |
| Build | Sol/Terra (codex) | mixed | 14 | — (no $) | 0.8–1.9M (codex formula) | — |
| Intake | Opus (deep) | — | 48 | 0.78 | 0.10M | 42.23 |
| Review | Opus (deep) | — | 49 | 0.57 | 0.07M | 31.34 |
| Revise (finding) | Opus (deep) | — | 3 | 2.07 | 0.17M | 7.28 |
| Revise (finding) | Sonnet (standard) | — | 2 | 0.61 | 0.11M | 1.22 |
| Revise (conflict) | — | — | **0** | — | — | — |
| Respond | Opus (deep) | — | 4 | 0.30 | 0.03M | 2.46 |
| Converse | Opus (deep) | — | 3 | 0.62 | 0.07M | 1.81 |
| Research | Opus (deep) | — | 1 | 3.04 | 0.26M | 3.04 |
| Mockup | Opus (deep) | — | 1 | 11.05 | 0.61M | 11.05 |

Superseded / unjoined attempts add $30.16 (3.6M headroom) — 7.8% of Claude spend,
the [#225](https://github.com/ConnorGriffin/agentflow/issues/225) floor, unchanged
from the contract.

Against the contract's **≥10 completed stages** bar for a quantitative claim, only
five cells qualify: Opus Build/high, Opus Build/medium, Sonnet Build/low, Opus
Intake, Opus Review. Everything else is directional (5–9) or insufficient (<5).
**Three days of data leaves most of this matrix evidence-free — this document says
so per cell rather than guessing.**

## What history *can* decide: two facts

**1. Spend is concentrated in exactly two places, and both are model-by-complexity
decisions made upstream of the stage.**

- **Opus Build is 63% of all Claude spend** ($245 of $387), and it scales cleanly
  with work effort: medium $2.87 → high $7.27 → extra $21.71. The model (Opus) is
  *correct given `deep`* — the lever that would move this bill is whether the Build
  should have been `deep` at all, i.e. the **complexity Intake assigns**. That is
  precisely [#228](https://github.com/ConnorGriffin/agentflow/issues/228)'s
  experiment (Intake step-up replay), not a per-cell model swap. **This matrix
  leaves Build's model cell unchanged and defers the deep-vs-standard question to
  #228.**
- **Intake is the second bucket** — 48 runs, all `deep` (Opus), $42.23. Each is
  cheap ($0.78 median) but the volume adds up, and there is no evidence any of them
  *needed* deep. This is the strongest candidate in the matrix for a **standard
  (Sonnet) default with a direct-deep trigger** — but again, whether standard-first
  Intake preserves scope quality is #228's replay to measure, not something history
  alone settles (history has zero standard Intakes to compare against).

**2. Review is cheap, so its safety cannot be traded for savings.**

Deep cross-tool Review costs a $0.57 median — it is not where the money is. The
map and contract both name deep Review the baseline safety policy, not an
optimization target. Cutting it would risk the 23% BLOCK-rate guardrail to save
pennies. **Review stays deep and cross-tool. Explicitly unchanged.**

## What history *cannot* decide: reasoning effort, everywhere

Reasoning effort is recorded in **no** coordinator event, Claude or Codex. There
is no historical cell that varies it, so **every reasoning-effort recommendation in
this matrix is evidence-free and pending forward experiment**. The prerequisite is
[#223](https://github.com/ConnorGriffin/agentflow/issues/223) (per-attempt
telemetry) capturing model + reasoning effort at launch. Until then the matrix's
reasoning-effort column is uniformly **"unset — pending #223"**. No cell sets it.

## The carry-complexity question (Respond and conflict Revise)

The ticket asks specifically whether the **original builder's complexity should
carry into Respond and conflict Revise**. History gives a structural answer and no
quantitative one:

- **Finding-driven Revise already carries it** and the data confirms parity: every
  finding Revise's complexity equals its builder's (Opus/deep revises on deep
  builds, Sonnet/Terra standard revises on standard builds). This is working as
  intended; leave it.
- **Conflict Revise is hardcoded deep** — and there were **zero conflict Revises in
  the window**, so there is no evidence at all. A conflict rebase on a
  Sonnet/standard build is mechanical (re-apply a diff over moved `main`), which
  argues for parity with finding Revise (carry builder complexity). But with n=0
  this is a *consistency* recommendation, not a measured one: **directional, adopt
  for symmetry, confirm once #223 records a conflict Revise cohort.**
- **Respond is hardcoded deep and never records builder complexity** (all six
  records carry an empty `builder_complexity`). Only one completed, at a $0.30
  median — the cheapest stage measured. Downshifting it to standard would save
  ~$0.15 per answer: below the contract's materiality floor in absolute terms.
  Carrying builder complexity into Respond is defensible for *consistency* with
  Revise, but the savings are immaterial and Respond talks to a human maintainer.
  **Evidence-free; no change on cost grounds; revisit only if #223 shows Respond
  volume growing.**

## The matrix

Reading: **model** columns are calibrated from history where a cell qualifies;
**reasoning effort** is uniformly pending #223. "Unchanged" means the current
routing stands and the row states what evidence would move it.

| Stage | Complexity / work shape | Model today | Recommendation | Reasoning effort | Evidence |
|-------|-------------------------|-------------|----------------|------------------|----------|
| **Intake** | (always deep) | Opus | **Candidate: standard (Sonnet) default + direct-deep trigger** — decided by #228, not here | Pending #223 | 48 deep runs, $42.23; zero standard runs to compare → #228 replay |
| **Build** | deep, extra | Opus | Unchanged (model correct for deep) | Pending #223 | n=3, $21.71 median; whether it needed deep is #228 |
| **Build** | deep, high | Opus | Unchanged | Pending #223 | n=12 ✓ qualifies; model right for deep |
| **Build** | deep, medium | Opus | Unchanged | Pending #223 | n=15 ✓ qualifies |
| **Build** | deep, low | Opus | Unchanged | Pending #223 | n=1 insufficient |
| **Build** | standard, low | Sonnet | Unchanged (cheap floor, $0.70) | Pending #223 | n=12 ✓ qualifies |
| **Build** | standard, medium | Sonnet | Unchanged | Pending #223 | n=2 directional |
| **Build** | (codex arms) | Sol / Terra | Unchanged; headroom-only, no cross-pool $ | Pending #223 | 14 builds, dollar-incomparable by construction |
| **Review** | (always deep, cross-tool) | Opus / Sol | **Unchanged — safety baseline, not an optimization target** | Pending #223 | n=49 ✓; $0.57 median, guards 23% BLOCK rate |
| **Revise (finding)** | carries builder complexity | Opus or Sonnet | **Unchanged — carry is correct** | Pending #223 | n=5 total; parity confirmed |
| **Revise (conflict)** | hardcoded deep | Opus / Sol | **Directional: carry builder complexity (parity with finding Revise)** | Pending #223 | n=0 in window; adopt for symmetry, confirm via #223 |
| **Respond** | hardcoded deep | Opus / Sol | Unchanged; carry-complexity immaterial on cost | Pending #223 | n=4, $0.30 median — below materiality floor |
| **Converse** | (always deep) | Opus | Unchanged | Pending #223 | n=3 insufficient |
| **Research** | (always deep) | Opus | Unchanged | Pending #223 | n=1 insufficient |
| **Mockup** | (always deep) | Opus | Unchanged | Pending #223 | n=1 insufficient |

### Direct-deep triggers

The one place the matrix proposes a *default downshift* is Intake, and it must be
gated by a direct-deep trigger so hard scoping never loses depth. The trigger set
(to be validated by #228, not asserted here): an issue labeled/flagged
architectural or cross-cutting; an issue that reopens or references a settled ADR;
a work-effort label of `high`/`extra` already on the issue; or an Intake that a
first standard pass marks as needing escalation. Absent a trigger, Intake starts
standard. **This trigger design is a recommendation to #228's experiment, not a
merged routing change.**

## Guardrails to hold (from the contract)

Any change adopted downstream of this matrix must not degrade, versus the current
routing: merge rate, hold/park rate, **Review BLOCK rate (baseline 23%,
18/78 verdicts)**, revise rounds per merged PR, retries per completed stage, and
median time-to-merge. The Intake downshift in particular must be judged on whether
standard-first Intake changes scope quality (holds, re-grills, downstream BLOCKs),
not on its own token line.

## Alternatives rejected

- **Blanket-downshift every "always deep" stage (Intake, Review, Respond,
  Converse) to standard.** Rejected: Review's safety and the immaterial savings on
  the cheap stages (Respond $0.30, Converse $0.62) do not justify the quality risk,
  and Intake's downshift belongs to a controlled replay (#228), not a blind flip.
- **Optimize the Opus Build bill by swapping Build's model.** Rejected: Opus is the
  correct model *for deep*; the real lever is the complexity Intake assigns, which
  is #228's question. Swapping deep Build to Sonnet without re-deciding complexity
  would just mislabel the work.
- **Set reasoning effort now from history.** Rejected outright: it is recorded
  nowhere, so any value would be a guess. All reasoning-effort cells wait for #223.
- **Treat the Codex build headroom as comparable to Claude's** to pick a cheaper
  build pool. Rejected: the two pools' headroom formulas are different currencies
  (contract), and Codex carries no dollar cost by construction — no honest
  cross-pool cost comparison exists in the history.

## What would move the evidence-free cells

Every unchanged/evidence-free cell above moves on the same two inputs:

1. **[#223](https://github.com/ConnorGriffin/agentflow/issues/223) per-attempt
   telemetry** — records model and reasoning effort at launch and captures
   zero-usage failed attempts, turning every reasoning-effort cell from "pending"
   into a measurable arm and giving Respond/Converse/Research/conflict-Revise real
   cohorts.
2. **[#228](https://github.com/ConnorGriffin/agentflow/issues/228) Intake step-up
   replay** — the only way to compare standard-first Intake against the all-deep
   baseline, since history has zero standard Intakes.

Until both land, the honest matrix is: **model choice per stage is as above
(mostly unchanged, with Intake flagged for #228); reasoning effort is unset
everywhere pending #223.**
