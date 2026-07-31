# ADR 418 — A fully-remedied final round publishes; only a genuine fork reaches the maintainer

**Status:** accepted
**Originating issue:** #418
**Amends:** [ADR 380](adr-380-pre-publish-hardening.md) — the argument still happens before
publication; what changes is the ending at the round cap.

## Context

Publication required `survived`: an empty objection list from a cold attacker. In the gate's
entire production lifetime that never happened once — 15 drafts, 37 rounds, zero survivals, zero
published. Every draft reached the round cap and was handed to the maintainer as contested; the
`ready-for-agent` queue emptied while 99% of the fleet's budget sat unused.

The surviving objections were overwhelmingly not decisions. Across the 11 held issues, all 36
surviving objections carried the attacker's own "Cheapest fix" — usually a one-sentence rewording
of an acceptance criterion. The maintainer was being asked to arbitrate edits that came with
their own patch. Two structural facts made unanimity unreachable: a competent adversarial reader
given a non-trivial brief always finds something worth saying, and each round rewrites the draft
for a *fresh* cold reader, so the rounds never converge on one fixed text.

## Decision

**The attacker's answer is typed, and the type is the gate's whole input.** Beside its free-text
objections the attacker states two facts only it can state: `remedied` — every objection above
carries a fix complete enough that applying it verbatim settles it — and `fork` — the one
genuine either/or (or missing fact) only a human can settle, as the question itself. The gate
never parses objection prose to guess which class it is; the attacker knows which of its
objections it has answered, so it says so.

**At the round cap, a fully-remedied, fork-free answer publishes.** The fixes are appended to
the brief under `## Final-round objections — apply these fixes`, addressed to the builder —
appended rather than redrafted, because a redraft would face a fresh reader on new text, which
is the exact non-convergence the cap exists to stop. The hardening note says which ending it
was. A draft with a fork, an unremedied objection, or an unreadable final answer holds exactly
as before, and the hold now **leads with the fork** when one is named.

**The cap itself is unchanged** in value and meaning. Mid-round, remedied objections still
redraft normally — the new ending exists only where the clock runs out.

**The fail-safe direction is unchanged.** `remedied` and `fork` default to the contested side:
an answer recorded before this change, or one the parser cannot read, still holds for a human.
Publishing on a fact nobody stated remains refused.

**The attacker is told the stakes**, as the counter to the obvious gaming incentive: marking
remedied ships the fixes verbatim into the brief, so a manufactured fix ships wrong — attach one
only when you would build from it yourself, and an unremedied objection is a legitimate answer.

## Consequences

- A draft whose final round ends in editorial-with-patch objections becomes a published brief the
  same cycle, and the maintainer's queue holds only genuine forks.
- The published brief can now carry unabsorbed objections. Accepted: the builder self-scopes from
  the brief, and the fixes are stated against the brief's own criteria.
- An attacker could still under-claim (never marking remedied) and recreate the old stall; the
  prompt names remedied as a first-class answer, and the publication rate remains the operator's
  signal that the gate is broken.
- Answers recorded before this change never publish at the cap — they re-enter through a fresh
  round when the maintainer's reply restarts triage.
