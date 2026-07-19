# The spend-per-success measurement contract

Research for [Wayfinder #1: establish the spend-per-success measurement contract](https://github.com/ConnorGriffin/agentflow/issues/227)
(map [#226](https://github.com/ConnorGriffin/agentflow/issues/226)), captured 2026-07-19.
The question: what measurement contract can compare daemon spend without rewarding
cheap failures or penalizing genuinely difficult work?

Tickets [#228](https://github.com/ConnorGriffin/agentflow/issues/228) (Intake step-up
replay), [#229](https://github.com/ConnorGriffin/agentflow/issues/229) (codegraph vs
codegraph+OKF), [#230](https://github.com/ConnorGriffin/agentflow/issues/230)
(model/reasoning-effort matrix), and [#231](https://github.com/ConnorGriffin/agentflow/issues/231)
(session tool profiles/ceilings) apply this contract as written — no reinterpretation.
The ruling itself is [ADR 0040](../adr/0040-spend-per-success-measurement-contract.md).

## The optimization objective is prepaid headroom

Settled by the operator mid-map
([#232 ruling](https://github.com/ConnorGriffin/agentflow/issues/232#issuecomment-5014939240),
2026-07-19): production optimizes **prepaid-plan headroom** — the capacity the
pipeline consumes against each tool's prepaid plan — not provider dollar cost.
The operator does not use API pricing; there is no configurable objective mode.

That gives the contract two spend units with fixed roles:

- **Headroom units (the target).** Per-pool weighted tokens, the same formulas the
  gate already uses — Claude: `input + 1.25 × cache_creation + 5 × output`;
  Codex: `uncached_input + 0.25 × cached_input + 5 × (output + reasoning_output)`.
  Never compare the two pools' values to each other; they are different
  currencies against different plans.
- **Normalized dollar equivalent (the cross-tool comparison signal only).** Claude
  sessions carry a harness-computed `total_cost_usd`; Codex attempts are normalized
  through published per-token prices for the actual model. Used to compare
  experiment arms and to weight mixed-tool aggregates — never the optimization
  target, never a billing claim.

Every experiment reports both, side by side. Raw token counts (uncached input,
cache-create, cache-read, output, reasoning output) are diagnostics only: cache
reads dominate volume (412.8M of the Claude baseline's read tokens) while output
dominates both weighted headroom and cost, so a raw-token headline would reward
exactly the wrong behavior.

## The two metrics

**Cost per verified stage** (the per-stage diagnostic lens):

- *Numerator:* all provider spend of every attempt charged to one logical stage
  record — the coordinator identity `repo|subject|stage|target` — including
  superseded, interrupted, and continuation attempts, and Codex descendant
  sessions charged to their root family.
- *Denominator:* logical stage records that reached `completed` **with the stage's
  verified outcome** as the coordinator's stage tracers define it (ADR 0028/0030):
  a parsed durable verdict for the exact target SHA (Review), a verified pushed
  revision on the owned PR branch (Revise), a durable maintainer answer (Respond),
  a committed scope decision (Intake), an opened PR or durable collision/blocked
  comment (Build). Provider exit status is *not* an outcome — in the baseline all
  215 Claude sessions exited "success", including ones whose stage later parked.
- A stage that exhausts its budget, parks, or retires unverified contributes its
  spend to the issue but never to this denominator.

**Cost per merged issue** (the headline):

- *Numerator:* everything the issue consumed across all stages, attempts, tools,
  and rounds — Intake, Build, every Review, every Revise (finding-driven and
  conflict), Respond, and every failed or superseded attempt along the way.
- *Denominator:* issues that became a **landed change** (PR merged into the
  default branch) in the window.
- In-flight issues are excluded from the denominator and reported separately as
  open exposure (spend at risk), so a snapshot can't hide unfinished spend.

The pairing is the anti-gaming core: an arm that fails cheaply drops its
verified-stage denominator and pushes its wasted spend into the merged-issue
numerator, so **cheap failure makes both metrics worse automatically**. There is
no metric under this contract that a failure improves.

## Quality guardrails

A spend reduction claim is valid only if, against the control cohort, none of
these degrade:

1. **Merge rate** — merged issues per issue attempted.
2. **Hold/park rate** — coordinator handoffs (`pr:parked`,
   `issue:needs-grilling`) per issue.
3. **Review blocking rate** — BLOCK verdicts and blocking-finding counts per PR
   (baseline: 18 BLOCK of 78 parsed verdicts, 23%).
4. **Revise rounds** per merged PR.
5. **Retry pressure** — attempts consumed per completed logical stage.
6. **Time-to-merge** — issue dispatch to merge, median.

## Cohorts — how difficult work is protected

The comparison cell is **stage × tool pool × model × complexity
(standard/deep) × build effort (low/medium/high/extra) × repo**. Arms are
compared within a cell, or across cells only with fixed cell weights. A deep/high
build is never averaged against a standard/low build; an arm cannot look cheap by
receiving easier work. Where assignment is controllable, interleave arms within a
cell by arrival order; where it is not (before/after policy changes), match cells
explicitly and say so.

## The comparison protocol for #228–#231

1. **Declare the cells up front** — which stage/model/effort cells the experiment
   claims to affect. Everything else is out of scope for its conclusion.
2. **Assignment:** interleaved within-cell where the daemon controls routing;
   otherwise matched before/after windows. Arm labels never appear in prompts,
   issue bodies, or PR bodies — the cross-tool reviewer stays blind.
3. **Minimum samples:** ≥10 completed logical stages per compared cell for a
   quantitative claim; 5–9 is directional only and must carry the dagger
   convention from the admission research; <5 is "insufficient", never a win.
4. **Statistics:** medians and P75/P90, not means — the tails are heavy (one
   Intake chain cost $48 against a $2.60 median issue).
5. **"Materially reduces spend"** = ≥20% lower median headroom-weighted spend per
   verified stage in the declared cells, *and* no increase in total spend per
   merged issue.
6. **"No meaningful degradation"** = at these sample sizes: no more than one
   adverse guardrail event beyond the control's count per 10 trials (holds,
   BLOCKs, extra revise rounds, extra retries), and median time-to-merge within
   +25% of control.
7. **Reporting:** headroom units per pool (target) and normalized dollar
   equivalent (comparison signal) side by side, plus raw-token diagnostics and
   every guardrail — even the ones that didn't move.

## Historical baseline (coordinator era, 2026-07-16 → 2026-07-19)

Source: the coordinator's durable session store (282 provider event streams:
216 Claude, 66 Codex) joined to its 195 stage records; 182 streams join by
launch token, the rest by worktree-path inference. Reproduction method below.

- **215 Claude sessions carry a durable result: $387.10 normalized cost, 412.8M
  cache-read tokens, 3.23M output tokens, 30.7m headroom-weighted tokens** (the
  map's cited 213/$386.49 snapshot plus two sessions completed since).
- Model mix of that cost: Opus $362.74 (93.7%), Sonnet $23.67, Haiku $0.69.
- **Opus builds dominate:** 30 joined build stages, $245.17, 14.7m weighted.
  Medians per completed build: extra $21.71 (1.06m weighted, n=3), high $7.93
  (0.58m, n=11), medium $2.87 (0.26m, n=15); Sonnet standard/low $0.70 (0.09m,
  n=12).
- **Intake is systematic burn:** 48 Opus Intakes, $42.23 total, median $0.78
  (0.10m weighted) each — every Intake currently runs deep.
- Review: 49 joined Opus reviews, $31.34, median $0.57. Revise (n=3) median
  $2.07 — a revise round costs roughly four reviews.
- **Codex (no provider dollar cost exists):** 63 streams with usage; builds
  (n=14) median 164k uncached-input and 15.4k output+reasoning tokens; 38.5m
  weighted total on the Codex formula. Not comparable to the Claude 30.7m.
- **Superseded-attempt overhead:** 60 Claude streams no longer referenced by any
  record — $30.16, 3.6m weighted, **7.8% of all Claude spend** went to attempts
  that were retried or superseded. This is the floor of what
  [#225](https://github.com/ConnorGriffin/agentflow/issues/225) (stop identical
  fresh-session retries) can reclaim.
- **Merged-issue denominator:** 62 agentflow-branch PRs merged across the four
  enrolled repos in the window (38 agentflow, 17 ciq-autotune, 6
  home-depot-location-probe, 1 dotfiles) → roughly **$6.24 Claude-normalized per
  merged PR** fleet-wide, before Codex normalization. Per-issue attribution:
  median $2.60, P75 $6.62, max $48.12 across 64 issues with joined Claude spend.
- Quality floor to hold: 18/78 review verdicts BLOCK (23%); 15 held records at
  snapshot (10 parked PRs, 4 needs-grilling issues, 1 other).

### Blind spots the baseline cannot answer

- **Reasoning effort is unrecorded.** No Claude thinking/effort setting is in the
  events; Codex reasoning effort lives in Codex's own rollout config, not the
  coordinator store. #230's matrix needs [#223](https://github.com/ConnorGriffin/agentflow/issues/223)
  per-attempt telemetry to capture it going forward; historically it is a
  confound, not a dimension.
- **Survivorship.** Records keep only their latest launch token, so the 100
  unjoined streams (60 Claude, 40 Codex) attribute only by worktree-path
  inference; sessions before 2026-07-16 live only in raw `~/.claude` /
  `~/.codex` transcripts and were measured under the same weighted proxy in
  [historical-session-demand](historical-session-demand.md), not re-normalized here.
- **Codex cost normalization is constructed**, not provider-reported: prices ×
  tokens for the actual model, and `input_tokens` includes cached input (net it
  out first). Three Codex streams recorded no usage at all (aborted before a
  turn completed) — genuinely free-looking failures the future telemetry must
  still count as attempts.
- **Cache accounting differs per ledger.** Cache reads are near-free in dollars
  and free in the Claude headroom formula, but cache *creation* is weighted at
  1.25×; an arm that thrashes caches shows up in headroom before it shows up in
  dollars.
- **Claude `total_cost_usd` is a harness-computed equivalent** on a prepaid plan
  — a stable normalization signal, not a bill.

What #223's per-attempt persistence adds that this baseline lacks: exact
attempt-to-stage attribution (no path inference), failed and zero-usage attempts
recorded first-class, reasoning-effort and model settings captured at launch, and
durability across record retirement — after which this contract's joins become
lookups.

## Reproducible method

1. Read `~/.agentflow/coordinator/sessions/*.events` (read-only). A stream
   starting `{"type":"system"` is Claude stream-json: take `cwd`, `session_id`,
   and the final `result` event's `total_cost_usd`, `usage`, and `modelUsage`.
   Anything else is Codex `codex exec` output: take `thread.started.thread_id`
   and sum every `turn.completed.usage`.
2. Join each stream's filename (the launch token) to `records.db`
   (`json_extract(data,'$.launch_token')`). For unjoined streams, infer stage and
   subject from the worktree path (`worktrees/<tool>[-<stage>]/(issue|pr)-<n>-…`).
3. Weight headroom with the gate formulas quoted above; keep pools separate.
4. Count verified outcomes from record `state`/`handoff_kind` and the parsed
   verdict JSON in review streams, never from exit status.
5. Merged-issue denominators come from GitHub: PRs on `agentflow/*` branches
   merged in the window, per repo.

## Alternatives rejected

- **Raw token counts as the headline.** Cache reads are 99% of Claude volume and
  near-zero marginal spend; a raw-token target rewards cache-hostile behavior and
  punishes cheap cache reads that make sessions *better*.
- **Provider dollars as the target.** The operator ruled it out — prepaid plans
  are the real constraint, and dollars would optimize a bill nobody pays. Kept
  strictly as the cross-tool comparison signal.
- **Headroom-only with no normalized signal.** The two pool formulas are
  incommensurable; without a normalized signal, #229–#231 could not compare a
  Claude arm against a Codex arm at all.
- **Cost per completed session.** "Completed" at the session level is what every
  Claude stream reports (215/215 "success"); it would declare cheap failures
  successful. Verification must come from the coordinator's stage outcome.
- **A single blended fleet metric.** Averaging across stage/effort cells lets an
  arm win by drawing easier work; the cell structure is the protection for
  genuinely difficult work.
