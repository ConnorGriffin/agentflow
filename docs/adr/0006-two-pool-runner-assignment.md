# ADR 0006 — Runner assignment: a two-pool headroom load balancer

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0003](0003-cross-tool-review.md) links builder and reviewer (pick the
builder, the reviewer is "the other tool") but left *how the builder is picked*
open. The obvious axis — cost — does not apply here: both tools run on **prepaid,
flat-rate subscriptions** (a Claude plan and a Codex/ChatGPT plan). Marginal tokens
are ~free, so "the cheaper tool" is meaningless.

What *is* scarce is **rate-limit headroom** on each plan. Claude has a calibrated
rolling window; Codex reports one or more windows whose shape can change with the
plan. Idle headroom on either plan is capacity already paid for and wasted.
`triage-gate.sh` is the adapter for these facts (`TRIAGE_AGENT=claude|codex`);
agentflow owns the scheduling policy.

## Decision

Runner assignment is a **two-pool load balancer** whose objective is to keep both
prepaid plans maximally utilized in parallel — never leave a plan idle while work
is queued.

- **Default builder = the pool with more headroom right now.** The reviewer is the
  other tool, which draws down the *other* pool — so a single issue spends from both
  budgets, and cross-tool independence doubles as load-spreading.
- **Concurrency across pools.** Because the budgets are independent, Claude builds
  #A while Codex builds #B, and each reviews the other's PR. Two independent pools
  is throughput a single-plan shop can't buy.
- **Work-shape override is for _fit_, not cost.** A per-issue hint may pin the
  builder when a task genuinely favors one tool (visual/UX → Claude builds so Codex
  reviews). Never override for "cheaper."
- **Reserve headroom for interactive use.** The existing per-plan gate holds: a
  headless build yields when its plan is near an interactive session or over the
  reserve threshold. The balancer only spends a plan's *surplus*.

## Amendment (2026-07-13) — Codex windows are dynamic; weekly use is paced

Codex windows are classified by their reported duration, never by whether Codex
calls one `primary` or `secondary`. Every reported known window is an independent
dispatch constraint, and all of them must permit a new unattended session. A
missing, malformed, or unknown fact makes that pool unavailable until fresh facts
arrive.

For a reported 10,080-minute window, unattended Codex allowance is released in
seven equal daily steps measured from the window's own start. The first `80 / 7`
percent is available immediately, another `80 / 7` percent is released at each
24-hour boundary, and the seventh day allows the full 80%. Reported usage must be
strictly below the released allowance. The remaining 20% is reserved for
interactive work. This gates new sessions only; work already in flight finishes.

A reported 300-minute window continues to use the existing short-window policy.
If both durations are present, that short-window decision and weekly pacing must
both permit dispatch. The gate reports usage, duration, and reset facts; it does
not duplicate the weekly pacing decision.

## Amendment (2026-07-22) — Claude is gated the same way: immediate headroom + a paced week

Claude and Codex now assign under the **same two-part rule**: a new unattended
session may start only when both an immediate-window headroom check and a paced
weekly allowance permit it. The two constraints are independent and both must
hold.

- **Immediate headroom.** Claude uses its provider five-hour window (utilization
  plus conservative in-flight reservations must stay below the activity-adaptive
  ceiling, [ADR 0025](0025-activity-adaptive-spend-ceiling.md)); Codex uses its
  reported short window. This is the load-balancing signal and the dashboard
  headroom reading — weekly pacing never replaces it.
- **Paced weekly allowance.** Claude also carries a provider **seven-day** window,
  paced by the identical rule Codex uses: 80% released in seven equal daily steps
  from the window's own start, the first tranche available immediately, one more at
  each 24-hour boundary, and reported usage must be *strictly* below the released
  allowance.

Each window is a separate durable fact with its own utilization, reset time,
observation time, and provenance; updating one never erases the other, and a
reset affects only its own constraint. Both windows are enforced when assigning
builders and reviewers and again immediately before launch, so a queued Claude
session defers if either window loses capacity between assignment and launch.
Missing, malformed, stale, or temporally impossible required facts fail closed.
Interactive turns keep their exemption; work already in flight is never stopped.

## Alternatives considered

- **Cheapest tool builds.** Rejected: no marginal cost under flat-rate plans.
- **Pin the builder per repo.** Rejected: wastes the free independence and the
  second pool whenever the pinned tool is throttled.

## Consequences

- The scheduler tracks **two independent headroom pools** and balances against both,
  rather than one global spend gate.
- If one pool is exhausted, work still flows on the other — but a build there can't
  be cross-reviewed by the exhausted tool, so it falls to the single-tool fallback
  ([ADR 0003](0003-cross-tool-review.md)): same-tool review, no auto-merge, until
  the second pool recovers.
- Utilization, not cost, is the metric to watch: queued work + an idle plan = a bug
  in the balancer.
