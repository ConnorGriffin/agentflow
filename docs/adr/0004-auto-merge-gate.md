# ADR 0004 — The auto-merge gate: severity bar, one revise round, drop-to-reviewed

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

**Auto-merge iff both hold:**

1. **CI green** on the branch (rebased on current `main`).
2. **No blocking finding** from the cross-tool reviewer — nothing at or above the
   **correctness / security** severity line.

**Below-line findings** (style, naming, minor perf, test nits) post as PR comments
and do **not** block the merge.

**On any blocking finding — exactly one auto-revise round:**

- The builder addresses the findings, pushes to the same branch, CI re-runs.
- The reviewer re-reviews the new diff.
- Clean now → merge. Still blocking (or the reviewer raises a *new* blocking
  finding) → **drop-to-reviewed**.
- The round is capped at **one**. No revise/re-review loops.

**Drop-to-reviewed** is the escape valve: the PR is demoted to the `reviewed`
policy for that one issue — findings stay posted, the human is pinged, the PR
waits. The machine merges the boring-clean work and hands the human only the diffs
a second model actually doubted.

## Alternatives considered

- **Never auto-merge without one human glance, even in vibe-code.** Rejected: that
  is just the `reviewed` rung — it collapses the top of the dial and removes the
  payoff of `autonomous`. A repo that wants the glance sets `profile: reviewed`.
- **Unlimited revise/re-review until clean.** Rejected: unbounded loops burn tokens
  and mask a builder that's stuck; one round then a human is the honest cutoff.
- **Block on any finding at all, including nits.** Rejected: nits would park nearly
  every PR and train the human to rubber-stamp — defeating the gate.

## Consequences

- The **severity line** (what counts as correctness/security vs a nit) is a
  reviewer-prompt contract the review stage must define crisply; a mushy line
  either parks everything or waves through real bugs.
- Drop-to-reviewed means `autonomous` is never *less* safe than `reviewed` — worst
  case it degrades to it. That is the property that makes the top rung defensible.
- One-round cap bounds cost per issue: build + review + (revise + review) at most.
