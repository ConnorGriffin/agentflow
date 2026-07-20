# ADR 0039 — Open PRs drain first

- Status: Accepted
- Date: 2026-07-18
- Amends: [ADR 0038](0038-conflict-resolution-as-revise.md) (subsumes its
  queue-jump clause); complements [ADR 0034](0034-methodology-session-orchestration.md)
  (interactive turns stay on top)

## Context

Admission ordered continuations ahead of cold submissions, but a *fresh* review,
revise, or respond record is itself cold — it queued alongside brand-new builds,
ordered only by identity. Under load, the pipeline would happily start new issues
while finished work sat one review away from merging. The operator's stated
principle: **an open PR is the #1 thing to get over the finish line — whatever it
needs**: a review, a revise round, an answer to a maintainer comment, a conflict
rebase. Starting new work while finished work decays also *creates* the conflicts
ADR 0038 then has to resolve — every open PR is exposure to `main` moving.

## Decision

Admission ranks waiting records in three tiers, within both the continuation and
cold queues:

1. **Interactive turns** — the operator's real-time conversation (ADR 0034,
   unchanged, still exempt from headroom/pacing).
2. **PR-bound stages** — any stage whose subject is an open PR: review, revise
   (finding-driven or conflict), and respond. Ties keep the existing
   eligible-at/created-at ordering.
3. **Issue-bound stages** — build, mockup, intake: work that *opens* new exposure
   rather than retiring it.

ADR 0038's "conflict revises jump the queue" clause is subsumed: all PR-bound
stages jump, uniformly.

## Consequences

- Finished work drains before new work starts: the pipeline self-limits its
  open-PR count under load, which in turn reduces survivor-rebase churn and
  conflict revises — the tiers reinforce ADR 0009's merge floor rather than
  fighting it.
- New-issue latency grows when many PRs are in flight. Accepted deliberately:
  a queued build loses minutes; a decaying PR loses its mergeability.
- The tier is a property of the stage, not a per-record flag — no new state, no
  new knob, and reviews migrating across pools (#202) keep their tier.

## Amendment (2026-07-20, #293) — enforce the order at the permit ledger

Per-cycle tier ordering is not enough once effort is wired to permits (ADR 0046):
a single high-effort build reserves all five permits of a pool, and while it runs
no review can start — a review needs one permit and there are zero free. Because
the ledger is shared per pool across every enrolled repo, one repo's high-effort
build starves reviews fleet-wide, and when the other tool is also out of headroom
(e.g. codex weekly pacing) the reviews cannot migrate either. The tier sort only
helps when both records are waiting in the *same* cycle; it does nothing for a
build that already holds the pool, or a build *continuation* (which admits ahead
of a cold review regardless of tier, because continuations drain before cold work).

So the drain-first order is now also enforced at the reservation gate: an
issue-bound stage (build, mockup, intake) may not reserve permits on a pool while
any PR-bound stage (review, revise, respond) is waiting to start on that **same
pool**. When no PR-bound work is waiting, an issue-bound stage may still claim the
whole pool — high-effort builds are unaffected when the review queue is empty.

The check is scoped to the same pool only. It deliberately does not reach across
pools or account for migratable reviews: the existing review-migration path already
re-places a review whose home pool cannot seat it, and coupling the two pools here
would invite a cross-pool deadlock.

**Why this cannot deadlock.** Reviews are the *product* of builds, so "builds wait
behind reviews" cannot deadlock in the normal case. A genuinely stuck review is
bounded: it exhausts its attempt budget and parks (`pr:parked`), leaving the
waiting set — at which point builds resume. The worst case is a fleet-wide build
pause until a stuck review parks, never a permanent freeze. A build held back this
way yields the pool without reserving anything, so it does not block the pool
head-of-line — the very review it yields to is reached in the same cycle.

## Alternatives considered

- **Status quo (identity-ordered cold queue).** Rejected: arbitrary interleaving
  let new builds starve nearly-merged PRs — the observed failure this decision
  answers.
- **Per-stage permit reservations.** Rejected: adds a capacity-partitioning knob
  when ordering alone achieves the drain-first behavior.
- **Priority only for conflict revises (ADR 0038 as written).** Rejected as too
  narrow the same day it was accepted: the finish-line argument applies to every
  PR-bound stage equally.
