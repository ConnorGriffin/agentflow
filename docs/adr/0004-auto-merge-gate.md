# ADR 0004 — The auto-merge gate

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0002](0002-three-autonomy-levels.md) lets the `autonomous` rung merge on
"green CI + clean review"; [ADR 0003](0003-cross-tool-review.md) makes the
reviewer an independent model. This ADR pins what *clean* means and what happens
when a review is not clean — the entire safety story for letting a repo merge
without a human.

## Decision

Applies to the **`autonomous`** rung only. `reviewed` and `guarded` always stop
for a human at the merge, regardless of how clean the review is.

**Auto-merge iff all hold:**

1. **CI green** on the branch (rebased on current `main`).
2. **A clean cross-tool verdict** proves the exact current head and records no unresolved
   fix-before-completion or maintainer-decision action.
3. **The current reviewer differs from the current change author.** Independence follows
   reviewer-authored fixes; it is not pinned to the original builder.
4. **No same-tool taint** remains.

ADR 0047 replaces the old severity split with four actions. Review ships clear grounded in-scope
fixes, validates necessary out-of-scope follow-ups, asks the maintainer only for unresolved product
intent, and discards unsupported preference. Only the latter creates no work or gate.

**When Review changes the branch:**

- The reviewer pushes the completed fix and becomes the current change author.
- The other tool reviews that exact head and may itself ship a grounded correction.
- The chain ends when the other tool makes no change. Three consecutive mutating review passes
  park once as drift/disagreement.

Agentflow never converts uncertainty into permission to merge. It posts one final clean summary or
one two-section park; intermediate review findings remain private.

## Alternatives considered

- **Never auto-merge without one human glance, even in vibe-code.** Rejected: that
  is just the `reviewed` rung — it collapses the top of the dial and removes the
  payoff of `autonomous`. A repo that wants the glance sets `profile: reviewed`.
- **Unlimited fix/re-review until clean.** Rejected: unbounded loops burn tokens and mask reviewer
  disagreement; three consecutive change-making passes is the bounded cutoff.
- **Turn every observation into work.** Rejected: unsupported reviewer preference would create
  churn and train the human to rubber-stamp.

## Consequences

- Exact-head provenance and current authorship are gate inputs, not prose conventions.
- Autonomous work waits for independent review rather than degrading through an outage.
- The three-mutating-pass cap bounds disagreement without limiting unchanged verification passes.

## Amendment (2026-07-09) — review-integrity invariants (from the adversarial pass)

An adversarial refutation of the reviewer ([ADR 0012](0012-build-in-vertical-slices.md)'s
per-slice attack) found real routes to a false `clean`. The gate now depends on these
invariants, enforced in `reviewer.py`:

- **Verdict lives *outside* the reviewed tree** (a fresh temp dir), so a builder
  cannot commit a forged `verdict.json` into its own PR checkout and have it read as
  the independent review.
- **The reviewer must actually run** — `launch()` must succeed; a rate-limited or
  crashed reviewer is not-clean, never a leftover/stale PASS.
- **Proof-of-work** — the verdict must carry the PR **head SHA** being merged, or it's
  not-clean (a rubber-stamp that never fetched the diff fails).
- **Finding parsing is fail-safe** — malformed or unsupported actions never become a clean verdict.
- **The parser never raises and never emits a false clean** — malformed containers,
  duplicate keys, and any exception → not-clean.
- **Still owed by the gate + loop (next):** CI-green AND-ed with `clean`, and
  reviewer-tool ≠ builder-tool (independence). The reviewer returns `reviewer_tool`
  for the gate to check.
