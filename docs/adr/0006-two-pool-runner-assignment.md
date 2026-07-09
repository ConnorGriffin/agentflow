# ADR 0006 — Runner assignment: a two-pool headroom load balancer

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0003](0003-cross-tool-review.md) links builder and reviewer (pick the
builder, the reviewer is "the other tool") but left *how the builder is picked*
open. The obvious axis — cost — does not apply here: both tools run on **prepaid,
flat-rate subscriptions** (a Claude plan and a Codex/ChatGPT plan). Marginal tokens
are ~free, so "the cheaper tool" is meaningless.

What *is* scarce is **rate-limit headroom** on each plan: a 5-hour rolling window
plus a weekly cap, tracked per-plan. Idle headroom on either plan is capacity
already paid for and wasted. `triage-gate.sh` already models this per agent
(`TRIAGE_AGENT=claude|codex`: trailing-5h weighted spend vs a calibrated peak for
Claude; `primary`/`secondary` `used_percent` for Codex).

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
