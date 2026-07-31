# ADR 418 — The argument ends on a redraft, and only a fork reaches the maintainer

- Status: Accepted
- Date: 2026-07-31
- Amends: [ADR 380](adr-380-pre-publish-hardening.md). The argument still happens before
  publication and the round cap still bounds it; what changes is what running out of attackers
  means.

## Context

ADR 380 published a draft only when an attacker returned an empty objection list, and handed the
draft to the maintainer when the round cap was reached with anything still on it. In production
that gate published nothing at all: 15 drafts, 37 attack rounds, zero empty lists, zero briefs.
Eleven issues sat waiting on the maintainer, the build queue was empty, and the fleet ran at 0%
of its budget — not for want of work but because nothing could get through.

Two assumptions in the original shape were wrong.

**Unanimous silence is not a reachable bar.** A competent cold reader given a real brief and told
to break it will find something worth saying. The anti-padding rules — *taste is not an objection*,
*an empty list is a successful attack* — are right and were not the problem; 37 out of 37 rounds
still returned objections.

**The rounds were never converging.** Each round the drafter rewrote to answer the objections and
a *fresh* cold attacker read the *rewritten* draft. "Still contested after three rounds" therefore
never meant three readers agreed the plan was bad — it meant three readers each found something in
three different documents. ADR 380's reasoning ("a plan still contested after three cold attackers
is not going to be settled by a fourth") assumed a fixed target.

What actually accumulated at the cap was not disagreement. Across the eleven held issues there
were 36 surviving objections and **all 36 carried the attacker's own "Cheapest fix"** — usually a
one-sentence replacement for an acceptance criterion. The maintainer was being asked to arbitrate
edits that arrived with their own wording.

## Decision

**The attacker's answer says two things, not one.** Alongside its numbered objections it names its
**forks**: the objections that need the maintainer — a real choice between defensible options that
changes the result, a fact nobody in the loop can supply, or a finding it has watched the drafter
try and fail to answer under `## Answered objections`. Everything else is work with its wording
already written. The attacker is the only party that knows which of its objections it could not
reduce to a fix, so the answer carries the distinction rather than the gate re-reading the prose
to guess at it.

**The argument ends on a redraft, not on an attacker.** At the cap, objections the drafter can
answer get the same redraft every other round's get; that redraft is published, because there is
no attacker left to read it. The published brief says which of the two endings it had. The cap is
unchanged in value and still bounds the argument: it bounds *attackers*, and no objection buys
itself another one.

**Only a fork reaches the maintainer**, and the comment leads with it — the question first,
everything else the round raised as context below. An unreadable final round still holds, under
its own wording: an unread draft is not a settled one, and publishing it on our own say-so remains
the one thing this design refuses to do.

### The new field is not a lever

Making the *fix* the escalation trigger would have paid an attacker to attach a fix to everything.
Naming the *fork* inverts that: declaring one wakes a human to approve a sentence the attacker
already wrote, which the prompt names as a failed attack rather than a thorough one. The drafter
gains nothing new either — it cannot mark its own objection settled, so `## Answered objections`
stays what it was, a claim the next cold reader judges on its evidence.

### Where the branch lives

`apply_objections` keeps its publish / redraft / hold shape and gains one condition. The one new
seam is that intake's own settlement asks the attack module whether a `ready` route is still owed
an attacker before treating it as a draft — the round arithmetic stays in one place, and the
opener that would drive the next attacker enforces the same answer so a transiently failed publish
can never buy a round beyond the cap.

## Alternatives

- **Raise or remove the round cap.** Rejected, and explicitly out of scope: an unbounded argument
  is a way to never ship, which trades one failure mode for the other.
- **Let the gate read the objection prose** and decide for itself which ones look editorial. A
  shallow interface over a fragile implementation, re-deriving something the attacker already
  knew.
- **Publish the draft as it stands with the surviving objections appended.** Cheaper, but it ships
  a brief whose known defects are listed underneath it, and a builder would have to apply them
  itself without ever re-grounding.
- **Show each attacker the prior rounds.** Still rejected, for ADR 380's reason: the attacker is
  then no longer cold.

## Consequences

- A brief the maintainer reads is still the survivor of its argument. What changes is that
  survival no longer requires the last reader to be silent, only that nothing is left needing a
  human.
- A contested deep draft costs one more triage session than before (the redraft that answers the
  final round), and saves the maintainer round trip it used to cost instead.
- The maintainer's inbox becomes forks only. A hold now means "I could not decide this", which is
  the only thing it was ever supposed to mean.
- An attacker that names no fork can no longer stop a plan from being built. That is the point,
  and it is also the risk: an objection that genuinely needed a human but was written up with a
  confident fix will now be applied instead of asked about. The prompt carries that weight.
