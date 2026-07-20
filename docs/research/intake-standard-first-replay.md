# Standard-first Intake with typed deep escalation — replay finding

Wayfinder #2 ([#228](https://github.com/ConnorGriffin/agentflow/issues/228)) under map
[#226](https://github.com/ConnorGriffin/agentflow/issues/226), captured 2026-07-19. Applies
the spend-per-success contract ([ADR 0040](../adr/0040-spend-per-success-measurement-contract.md),
[#227](https://github.com/ConnorGriffin/agentflow/issues/227)) as written — no reinterpretation.

**The question.** Can starting Intake on the **standard** model (Sonnet/Terra) and stepping up
to **deep** (Opus/Sol) only on typed triggers cut total Intake spend without degrading route
correctness or Agent Brief quality?

**This is a measurement prototype, not a policy change.** Making standard-first the production
Intake default is [#232](https://github.com/ConnorGriffin/agentflow/issues/232)'s terminal call,
gated on this finding plus the sibling experiments. What this ticket delivers is (1) the reusable
escalation design the winning arm would need, (2) a fixed pinned corpus, (3) an offline replay +
blind-scoring apparatus, and (4) this finding applying the decision threshold.

## What is delivered, and its execution boundary

Delivered as tested code and data:

- **The typed escalation design** (`agentflow/intake.py`, `agentflow/experiments/escalation.py`).
  A closed `EscalationReason` enum (`low-confidence-route`, `unresolved-fork`, `evidence-gap`,
  `complexity-signals`) — never a free-text reason. A typed `escalate` route that carries those
  reasons. Deterministic direct-deep triggers over the pinned issue snapshot. A **bounded**
  evidence carry-forward that hands the deep attempt the standard attempt's grounding digest and
  tentative decision — capped at construction — and a prompt builder that **refuses** to escalate
  without typed reasons and real evidence, so a step-up can never degenerate into a blind rerun of
  the original prompt.
- **The pinned corpus** (`agentflow/experiments/data/intake_replay_corpus.json`, assembled by
  `agentflow/experiments/build_corpus.py`) — 47 real issues, each pinned to one repository SHA,
  with the route/complexity/effort labels the historical all-deep Intake actually applied as the
  scoring reference. Strata below.
- **The replay + scoring apparatus** (`agentflow/experiments/replay.py`, `.../spend.py`). Drives
  all three arms over the identical corpus through the durable Intake contract
  (`intake_prompt` → `parse_intake`), sums arm C's two-attempt spend when it escalates, denominates
  spend in per-pool headroom-weighted tokens (the ADR 0040 gate formulas) with the normalized
  dollar equivalent alongside, and scores each arm **blind** — the scorer is handed an arm-free
  brief, so it cannot see which arm produced it.

**Execution boundary (logged, not silent).** Running the three arms over the corpus is ~90–150
real Intake sessions across Opus and Sonnet tiers. A build session **cannot** produce those: the
coordinator is the only launch owner and the runner refuses to spawn a provider from anywhere
else, this environment is sandboxed with no provider credentials, and blind scoring of the three
judgment axes requires a scorer under the protocol, not a fabricated one. So this finding reports
the **real arm-A baseline that already exists** in the coordinator store and the **a-priori spend
model**; the executed arm-B / arm-C comparison is produced by the committed apparatus when the
operator runs it with recorded transcripts from a real three-arm pass. Per ADR 0040, unexecuted
arms are **"insufficient", never a win** — this finding does not claim a savings it did not
measure.

## The corpus

47 issues from `ConnorGriffin/agentflow`, pinned at `ee24528`, stratified:

| Stratum | Count |
| --- | --- |
| route: ready | 40 |
| route: grill | 6 |
| route: mockup | 1 |
| complexity: deep (ready) | 20 |
| complexity: standard (ready) | 20 |
| effort: low / medium / high / extra | 16 / 13 / 10 / 1 |

The deep-ready stratum was **explicitly truncated** to 20 (the further ready/deep issues beyond
the cap were dropped, and their count is recorded in the corpus notes) to keep the two complexity
cells balanced for a within-cell comparison; that truncation is
recorded in the corpus `notes`, so the sample reads as the balanced 47 it is, never as full
coverage of the population. Holds (grill/mockup) are scarce, so all were taken.

## Arms

- **A — deep** (baseline): one deep attempt per issue, exactly today's policy
  (`intake_submission` hardcodes `complexity="deep"`).
- **B — standard-first**: one standard attempt per issue, no escalation.
- **C — standard-first + typed escalation**: deterministic direct-deep triggers send
  correctness-sensitive / ADR-contradicting / very-long issues straight to a single deep attempt;
  everything else runs a standard attempt that may step up to a *second* deep attempt handed the
  bounded prior evidence. Arm C's per-issue spend is the sum of both attempts when it escalates.

## The real baseline (arm A) and the a-priori model

From the coordinator-era baseline (ADR 0040 research doc): **48 Opus Intakes, $42.23 total, median
$0.78 (0.10m headroom-weighted) each** — every Intake runs deep today. For the standard tier, the
same baseline records Sonnet standard/low work at a **median $0.70 (0.09m weighted)**, but Intake's
grounding workload is heavier than a standard/low build, so the standard-tier Intake cost is an
open measurement, not a read-off.

The a-priori case for running the experiment: on the headroom formula, output tokens dominate
(weighted 5×), so a tier that produces a comparably-sized brief at Sonnet's rate is materially
cheaper per Intake, and arm C only pays the deep premium on the fraction of issues that trip a
direct-deep trigger or self-escalate. Whether that fraction is small enough — and whether the
standard tier's route/brief quality holds — is exactly what the arms measure. **This is motivation
to run the apparatus, not a result.**

## Decision threshold, applied

The rule (ADR 0040 / #228): recommend the standard-first step-up **only if** it materially reduces
median total Intake spend (≥20% lower median headroom per verified stage in the declared cells,
with no increase in spend per merged issue) **and** shows no meaningful degradation in
route/brief quality or downstream correction rate. Otherwise recommend staying all-deep and say so
plainly.

**Recommendation: do not adopt standard-first Intake yet — stay all-deep — pending an executed
run of this apparatus.** The escalation design and the measurement rig are complete and tested,
and the corpus is fixed, but the arm-B and arm-C executions that the threshold needs have not been
run (they require real provider sessions a build session cannot spawn). Under the contract that is
"insufficient", which is not a win: absent measured evidence that the step-up materially reduces
median spend without degradation, the standard-first default stays off. The a-priori model makes
the experiment clearly worth running; it does not settle it.

**What settles it, precisely.** Run `agentflow.experiments.replay.run_experiment` over this corpus
with recorded transcripts from a real three-arm Intake pass (arm labels never in any prompt),
blind-score the three judgment axes (verified evidence quality, acceptance-criteria completeness,
downstream hold/review/revise outcome) alongside the deterministic route/complexity/effort scores,
and compare per-arm median headroom spend and the guardrails against the ≥20% / no-degradation
threshold. That produces the recommend/don't-recommend #232 needs to lift the Intake step-up
question off its blocked list.
