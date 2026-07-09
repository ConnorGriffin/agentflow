# ADR 0003 — Cross-tool review is the independence gate

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0002](0002-three-autonomy-levels.md) makes review the load-bearing safety
control for the `autonomous` and `reviewed` rungs. The question is *who* reviews.

The concrete failure this must catch is the one that bit `ciq-autotune` on PRs
#310/#311: a green-CI diff that was confidently, subtly **wrong** (misattribution
edge cases a passing test never exercised). A reviewer that shares the builder's
blind spots will wave that through — the builder already convinced itself.

## Decision

**The reviewer model must differ from the builder model.** Codex builds → Claude
reviews; Claude builds → Codex reviews. Independence from the builder is the whole
point, at every autonomy level.

- The **builder** self-reviews and flags what it's unsure of, but its own sign-off
  never gates a merge.
- The **reviewer** (the other model) is the one whose verdict counts.
- This couples cleanly to runner assignment: once a builder is chosen, the
  reviewer is simply "the other tool."

## Alternatives considered

- **Same-tool fresh-session review.** Cheaper and single-vendor, but shares the
  training and the blind spot that produced the bug. Rejected as the default;
  it is the degraded fallback when only one tool is available (see Consequences).
- **A single dedicated reviewer model** regardless of builder. Rejected: whichever
  model is fixed as reviewer, half the PRs are then same-vendor reviews; "the other
  tool" gives independence for free on every PR.

## Consequences

- **Runner assignment and review are linked:** picking the builder picks the
  reviewer. A later ADR covers how the builder is chosen.
- **Single-tool fallback:** if only one tool is available (the other is rate-limited
  or down), the pipeline degrades to same-tool fresh-session review and **must not**
  auto-merge on it — a `guarded`/`reviewed`-style human check applies until the
  second tool returns. Never silently drop independence.
- What makes a reviewer's verdict *clean enough to auto-merge* (severity bar,
  not-clean fallback, optional auto-revise round) is the next ADR.
