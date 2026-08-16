# ADR 0003 — Cross-tool review is the independence gate

- Status: Accepted — weakened by
  [ADR 498](adr-498-tiered-parent-independent-review.md): independence is measured against the
  accountable session lead, so a change a delegated worker of the reviewing tool wrote may still
  be reviewed by that tool
- Date: 2026-07-09

## Context

[ADR 0002](0002-three-autonomy-levels.md) makes review the load-bearing safety
control for the `autonomous` and `reviewed` rungs. The question is *who* reviews.

The concrete failure this must catch is a green-CI diff that was confidently,
subtly **wrong** because passing tests missed attribution edge cases. A reviewer
that shares the builder's
blind spots will wave that through — the builder already convinced itself.

## Decision

**The reviewer model must differ from the current change author's model.** Codex-authored
changes receive Claude review; Claude-authored changes receive Codex review. ADR 0047 extends
that rule beyond the original builder: when a reviewer pushes, authorship moves with the changed
head and the other tool reviews it.

**Amendment (#515): the current author is the durable exact-head provenance fact, never the
branch lane or the pool that opened an earlier stage.** A Build or Revise records its accountable
session lead as it opens; a reviewer push records that reviewer as the new head's author. Every
later reviewer selection reads that fact, falling back only for pre-session-led records to their
durable `builder_lineage`. If neither field names a known tool, Agentflow must not claim cross-tool
coverage: it parks the affected completed or diverged review for a maintainer instead of inferring
authorship from the branch name.

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
- **Single-tool availability (amended by ADR 0047):** autonomous work holds without consuming
  capacity until the other tool returns. Reviewed work may use a fresh same-tool review but remains
  human-merge-only and says so explicitly. A maintainer-forced same-tool autonomous review is
  tainted until the other tool reviews the still-open exact head cleanly.
- What makes a reviewer's verdict *clean enough to auto-merge* (severity bar,
  not-clean fallback, optional auto-revise round) is the next ADR.
