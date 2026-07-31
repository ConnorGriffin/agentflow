# ADR 380 — A triage draft is attacked cold, in rounds, before it is ever published

**Status:** accepted
**Originating issue:** #380

## Context

An issue reaches `ready-for-agent` by one of three routes — the daemon triaging a fresh issue, a
held issue promoted after the maintainer replies, or the maintainer scoping one by hand. In the
daemon's routes, whatever the grounding session talked itself into became the plan a builder was
spent on. Nothing ever argued with it: the durability projection proves the labels, title, comment
and composed body on GitHub match the decision triage returned — a proof about paperwork,
deliberately so — and no check anywhere judged whether the decision was correct. A confidently
wrong brief cost a full build, a review, and a revise round before anyone noticed.

A first cut of this ADR put the argument *after* publication: a cold "plan audit" re-read every
published ready brief and either countersigned it toward build or bounced it back to the
maintainer as a held issue. The maintainer rejected that shape on review, and the objection was
structural, not cosmetic: every disagreement cost the human a round trip, and the objections
argued with a brief the maintainer had already been shown. The hardening has to finish **before**
anything is published, so that what the maintainer and the builder read is already the survivor.

## Decision

Triage no longer publishes what it drafts. A `ready` route from an intake session is a **draft**:
nothing on GitHub changes, and the issue's `triaging` claim is retained. The draft opens an
**attack** — one cold session, on whichever pool has headroom, that carries nothing from the
session that wrote the draft and is asked to break the plan while breaking it is free. It judges
five axes (grounding · acceptance · interface shape · scope and complexity budget · cost) and
answers with numbered objections — each with its evidence, why it breaks the build if unfixed,
and the cheapest fix — or with none. Taste is not an objection, and an empty objection list is a
draft that deserved to survive, not an attacker that slacked.

Objections go to a **redraft**: a fresh intake round that re-grounds from the same frozen issue
snapshot, fixes what landed, defends what didn't, and hands back a complete new draft — which is
attacked again. The loop ends three ways:

- **Survived** — an attacker finds nothing blocking. The draft is published as the ready brief
  through intake's ordinary projection, with one line saying what the argument cost. This is the
  only place a brief reaches GitHub, so a plan that never survives is never something the
  maintainer had to read.
- **Contested out of rounds** — the cap is hit with objections still live. The draft is **never
  published**: a builder spent on a plan its attackers couldn't settle is the exact waste this
  design exists to prevent. The issue becomes a held issue carrying the newest draft and the
  surviving objections as the question; one maintainer reply restarts triage, and the next draft
  earns a fresh set of attackers.
- **A genuine fork** — a redraft may route to grilling itself when the objections expose a choice
  only the maintainer can settle. That ends the chain the ordinary held-issue way.

### Each attacker runs cold, on the newest draft only

An attacker reads the draft in front of it, the issue as filed, and the repository — never the
rounds behind the draft. What an earlier round settled must therefore live *inside* the draft:
fixed, or defended under an `## Answered objections` heading the next cold reader judges on its
written evidence like any other claim. This is the convergence mechanism, not a trace format —
a settlement that can't survive being written down hasn't actually been settled.

Rejected alternative: showing each attacker the prior rounds' objection log. It converges faster
on paper, but the attacker is then no longer cold — it inherits the previous rounds' framing,
and a wrong settlement two rounds back becomes progressively harder to re-open.

### The dial is the classifier

The complexity dial triage already stamps is the sizing signal for how much adversarial intensity
the ask deserves: a `standard` draft gets one attack round on the standard model tier, a `deep`
one up to three on the deep tier, through the same admission table every other stage uses. No
separate classifier session exists — rejected as a new stage whose whole job triage already does.

### The rounds are triage

The chain borrows Intake's `triaging` claim rather than taking one of its own — the issue is
still being decided, which is what that claim already means — and each round's record transfers
it to the next inside one transaction (the same claim-transfer openers Build→Review→Revise use,
ADR 0028), so the issue is never unowned mid-argument and a daemon crash resumes where it
stopped. Attack sessions run read-only in their own `{tool}-attack` worktree lane, share
triage's concurrency lane and permit shape, and never contend with the build queue.

### Failure is never confused with judgment

An **unreadable answer** spends its round — a durable fact that nobody attacked the draft — but
never reads as the draft surviving; the next round renews the attack on the *same* draft, since
there is nothing for a drafter to answer. An unreadable final round holds for the maintainer:
publishing an unattacked draft on our own spend cap is the one thing the design refuses to do.
An attack session that dies to **infrastructure** is retried under the ordinary attempt budget;
exhaustion hands the maintainer the un-argued draft through the grilling route rather than
losing the plan.

### What the by-hand paths do

`/agentflow scope` and the operator's own **Land it as ready** still publish directly: a human
who just settled the scope in conversation *is* the hardening. `build <N>` no longer has an
audit gate to refuse on — every ready brief from the daemon's own triage already survived its
attackers by construction.

## Consequences

- A brief the maintainer reads is already the survivor of its argument; disagreement between
  drafter and attackers reaches the maintainer only in the contested minority, as one held
  issue instead of a bounced round trip per objection.
- Ready briefs cost more sessions to produce (draft + N attacks + N−1 redrafts, N scaled by the
  dial), traded against builds, reviews, and revise rounds not spent on confidently wrong plans.
- The builder that picks up a hardened brief re-litigates nothing: `## Answered objections` in
  the brief is the settled record of what was argued.
- A replayed round is byte-identical to the one it replaces: every round's durable input carries
  the frozen snapshot, the grounding prompt, and the draft under attack.
