# ADR 0029 — Static five-permit admission per pool

- Status: Accepted
- Date: 2026-07-15
- Amended: 2026-08-03 — inherited Revise effort leaves its admission demand on the existing
  `n/a` row ([#473](https://github.com/ConnorGriffin/agentflow/issues/473))
- Amended: 2026-08-03 — effort-blind stages normalize supplied effort to `n/a` before exact
  admission matching ([#473](https://github.com/ConnorGriffin/agentflow/issues/473))

## Context

Headroom is cumulative-spend evidence, so two sessions can read the same healthy fact and
start together even when their combined burst reaches a provider limit. The historical
session study found enough separation for conservative demand bands, but not enough stable
data for precise learned weights: intake and Claude review are short, Codex review is
heavier because its root may fan out to subagents, code-writing sessions must not overlap
each other within one pool, and several Codex, high-effort, mockup, and unknown cells are
sparse or heavy.

The concurrency boundary therefore needs to be static, review-controlled, and independent
per pool. It must preserve useful overlap between a near-exclusive writer and a short
read-only stage without treating the headroom proxy as an atomic reservation.

## Decision

### Each pool has five permits

Claude and Codex each have an independent **five-permit budget**. Permits cannot be borrowed
or transferred between pools. A session reserves its whole admission demand atomically on
the pool that will run it; there is no partial reservation, overcommitment, or permit debt.

The minimum code-writing demand is three. Consequently, any two code-writing sessions need
at least six permits and can never run together on one pool. Demand four is near-exclusive:
it leaves room for one intake or Claude review. Demand five is exclusive.

### The reviewed matrix

The current concrete model names validate that the runner selected the model implied by the
complexity dial; they are not a second sizing dial. Effort affects builds only. Intake,
review, mockup, and respond always use the deep model; revise retains the originating
build's complexity and has no independent effort input.
*(Amended 2026-08-03: Revise now durably inherits the original builder's effort for reasoning,
but admission remains effort-blind and continues to use the single `n/a` demand row.)*

The matrix uses ADR 0028's logical stage names. Existing orchestration labels normalize
before lookup: `triage` and `triaging` mean Intake; `review` and `reviewing` mean Review;
and `build`/`building`, `revise`, `mockup`, and `respond` map to their corresponding logical
stage. The logical stage, not a live-board label, disambiguates Build from a Revise currently
reported as `building` and Mockup from a session currently reported as `triaging`. A known
alias never falls into exclusive unknown admission. ADR 0028's continuation record supplies
the logical stage; #93 decides where the normalization and admission interface live.

| Stage | Pool / model | Complexity | Effort | Demand |
| --- | --- | --- | --- | ---: |
| Intake | Claude / Opus | deep | n/a | 1 |
| Intake | Codex / Sol | deep | n/a | 1 |
| Review | Claude / Opus | deep | n/a | 1 |
| Review | Codex / Sol | deep | n/a | 2 |
| Revise | Claude / Sonnet | standard | n/a | 3 |
| Revise | Claude / Opus | deep | n/a | 3 |
| Revise | Codex / Terra | standard | n/a | 4 |
| Revise | Codex / Sol | deep | n/a | 4 |
| Respond | Claude / Opus | deep | n/a | 3 |
| Respond | Codex / Sol | deep | n/a | 5 |
| Mockup | Claude / Opus | deep | n/a | 5 |
| Mockup | Codex / Sol | deep | n/a | 5 |

Build demand is the only effort-sensitive part of the matrix:

| Pool / model | Complexity | Low | Medium | High | Extra |
| --- | --- | ---: | ---: | ---: | ---: |
| Claude / Sonnet | standard | 3 | 4 | 5 | 5 |
| Claude / Opus | deep | 4 | 4 | 5 | 5 |
| Codex / Terra | standard | 4 | 5 | 5 | 5 |
| Codex / Sol | deep | 5 | 5 | 5 | 5 |

The bands are deliberately monotone within a stage: a deeper model or greater effort never
reduces demand. The Codex response and both mockup cells use five because the current-model
history is missing or sparse and their work may include code or committed visual artifacts.
Codex review remains two because the historical measurement already charges all descendants
to the root reservation.

### Missing and unknown classification is conservative

- A build without complexity remains inadmissible under ADR 0018's existing hard gate.
- A build without effort keeps the existing `medium` default and uses that cell.
- Effort attached to a non-build stage does not select a lighter demand; the stage uses its
  `n/a` row.
  *(Amended 2026-08-03: the admission lookup normalizes every effort-blind logical stage to
  `n/a` before exact matching, so inherited Revise effort cannot select exclusive fallback.)*
- If the pool is known but the stage, model, complexity, effort, or combination has no exact
  row, it reserves all five permits and logs that it used exclusive fallback admission.
- If the pool itself is unknown, the session is not admitted because there is no budget to
  charge.

The matrix and budget are production configuration committed in this repository. Runtime
environment variables and per-repository settings may not weaken them. A change requires a
reviewed PR, updated completed-session evidence, and the historical replay required by the
planning map; tests may inject alternatives without creating a production override.

### Permits compose with, rather than replace, existing gates

A provider attempt starts only when every independent constraint passes: current headroom
and reported provider windows, activity pacing, the machine ceiling, the stage cap, and the
pool's permit budget. Headroom continues to choose among otherwise eligible pools and to
bound cumulative spend. Permits are the concurrent-admission correctness boundary.

For a cold stage whose lineage is not yet fixed, a pool that cannot fit the demand is not an
eligible routing choice; the balancer may select the other pool if its normal safety rules
allow it. Code-writing continuations remain pinned to their tool lineage. A read-only
continuation that ADR 0028 allows to move is charged against the destination pool's row.

Crossing a spend ceiling after launch does not revoke permits or kill work. Conversely,
free permits never override a closed headroom gate. Machine and stage caps remain separate
operational safeguards, and merges remain serialized outside provider-session admission.

### Permit lifetime follows the live provider family

Admission reserves permits after pre-spawn preparation and before an attempt is consumed or
the provider process is spawned. A capacity deferral therefore consumes neither permits nor
a continuation attempt. A root reservation includes any subagents or descendants it starts;
descendants do not reserve separately.

The reservation remains until the provider process family is confirmed ended and its final
observations are durable. Transitioning from `running` to `waiting`, `completed`, or `held`
releases it. A waiting continuation owns no permits. A recovered orphan that is still alive
keeps its original demand, and an unreadable or ambiguous running record fails closed until
reconciliation proves the process ended.

### Continuations have strict admission priority, not preemption

On each pool, eligible continuations are considered in ADR 0028's order before cold starts.
The first continuation that cannot pass normal admission remains waiting and stops admission
behind it on that pool for the cycle: later continuations and cold starts do not bypass it.
Running work is never preempted to make room. This gives an older exclusive continuation a
bounded path to all five permits instead of allowing a stream of one-permit cold work to
starve it.

When demand would exceed the remaining budget, the reservation is rejected atomically. The
stage stays `waiting`, retains its existing claim, consumes no attempt, and retries on a
later cycle with an exact capacity log. Admission never silently downshifts the model,
switches code-writing lineage, or starts over-budget work.

This ADR decides the admission policy only. The runner/orchestrator interface and atomic
storage seam remain #93; replaying this policy against history remains #94.

## Alternatives considered

- **Use headroom alone.** Rejected because the known provider-limit episode admitted three
  roots from one stale healthy fact; cumulative evidence is not a reservation.
- **Make every code-writing session demand five.** Rejected because the four-permit band
  safely preserves one short read-only session while the three-permit floor already prevents
  two writers from overlapping.
- **Copy raw percentiles into fine-grained weights.** Rejected because the sample covers six
  changing days and several cells have fewer than five completed roots.
- **Allow runtime tuning.** Rejected because weakening the matrix changes the concurrency
  safety boundary without review or replay evidence.

## Consequences

- At most one code-writing session runs per pool, while the two independent pools may each
  run one in parallel.
- Intake can fan out to five sessions on an otherwise empty pool, subject to the lower
  machine and stage caps; Claude review can do the same, while Codex review admits at most
  two roots per pool.
- High/extra builds, Codex deep builds, mockups, and unclassified work wait for an empty pool.
- Conservative fallback can reduce throughput after a model or stage change until a reviewed
  matrix update lands; it cannot accidentally increase concurrency.
