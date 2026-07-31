# ADR 380 — A cold plan audit stands between "ready" and "building"

**Status:** accepted
**Originating issue:** #380

## Context

An issue reaches `ready-for-agent` by one of three routes — the daemon triaging a fresh issue, a
held issue promoted after the maintainer replies, or the maintainer scoping one by hand. In every
case the next dispatch cycle claimed it and started a builder.

Nothing in between ever read the brief and asked whether the *plan* was right. The one check that
existed — the durability projection — proves the labels, title, comment and composed body on
GitHub match the decision triage returned. It is a proof about paperwork, and deliberately so: it
never judges whether that decision was correct.

So a confidently wrong brief cost a full build, a review, and a revise round before anyone
noticed. Adversarial review of a plan is much cheaper than repairing the code written from it.

## Decision

Add one bounded adversarial step in front of build: a **plan audit**.

A fresh session with no memory of the triage that wrote the brief re-reads it against the actual
repository and returns exactly one of two answers:

- **countersign** — the brief survives. The issue is marked audited and dispatches to build on
  the same cycle it otherwise would have. Nothing else changes: no comment, no retitle, no body
  edit.
- **bounce** — the brief does not survive. The issue leaves the build queue, flips to
  needs-grilling, and the auditor's numbered objections become the question the maintainer
  answers.

Nothing is built on an un-countersigned brief — including by hand. `build <N>` skips the *queue*,
never the gate: it refuses an un-audited issue and tells the maintainer to run the audit inline,
in the session they are already in, before retrying. That is the pipeline's standing rule that a
by-hand verb adds convenience and never authority (ADR 0019).

### The rubric is the step

An auditor that only reads the brief has failed. Five axes, all of them judged:

1. **Grounding** — every load-bearing factual claim, especially anything in the brief's Verified
   section, is checked by opening the named files.
2. **Acceptance** — the criteria are observable from outside, and green genuinely means done,
   with no unstated work smuggled in.
3. **Interface shape** — the proposed front door judged against the engineering charter:
   interface far simpler than implementation, the deletion test on any new module, no seam built
   before its second caller. A brief that says nothing about interface shape is itself an
   objection.
4. **Scope and complexity budget** — out-of-scope is explicit; an edge case earns handling only
   if it's reachable from the inputs the acceptance criteria describe; speculative hardening in a
   plan is a defect.
5. **Cost** — the effort dial and the implied blast radius are proportionate to the ask.

Objections are numbered and each carries its evidence, why it breaks the build if unfixed, and
the cheapest fix. Taste is not an objection. An empty objection list is a successful audit, not a
lazy one.

### The shape of the session

The audit is intake-shaped, because it is the same kind of work: one bounded read-only pass over
one issue. It borrows intake's whole envelope — one permit on either pool, a 20-minute / 40-turn
ceiling, a read/search tool allowlist with no edit tools at all, and a throwaway checkout rebuilt
from `origin/main`.

What it does *not* share is its lane. The audit takes its own claim label and its own capped
admission lane beside triage, for two reasons. The claim, because the two lanes own an issue at
different moments — triage before it is settled, the audit after — and one shared label would let
either lane's live record shield the other's stale claim from reclamation, which is precisely what
the claim-lane table exists to prevent. The lane, because the audit is what stands between a
settled brief and a builder: a busy triage queue must never be the thing that stalls the whole
build queue behind it. Neither lane is the *build* lane, so an audit never occupies a build slot
and audits cannot starve building.

Same-tool is fine. The auditor must be *cold* — no context from the triage session — but it need
not be the other tool, so the audit takes whichever pool has headroom and never waits for a
particular one.

### The bounce is intake's own grilling route

Projecting a bounce through the existing triage projection is the economy of the whole design.
That projection already removes `ready-for-agent`, applies `agentflow:needs-grilling`, clears the
dials, and posts the body as a comment; the held-issue sweep already knows how to resume from it.
So the maintainer's reply re-runs triage exactly as it does for any other held issue, and the
second adversarial cycle is free. The bounce needs no new hold, no new label state, and no new
notification path.

The bounce comment therefore carries intake's disclaimer line. The held-issue sweep decides "the
maintainer answered" by looking for that marker; objections posted without it would read as a
maintainer reply and re-run triage instantly, in a loop with nobody in it.

### The countersign is a managed label

The marker is `agentflow:audit:countersigned`, and it joins the set of single-valued prefixed
labels the triage projection manages. That placement is the mechanism behind one requirement: a
re-route clears every managed label not in its new set, so an issue bounced back to grilling and
later promoted to ready again carries **no** stale countersign and its new brief is audited fresh.
The triage durability proof rejects an unexpected managed-prefix label, which is consistent — the
proof runs before the audit ever stamps its marker, so the marker is correctly absent at that
moment, and a projection that left a stale one behind genuinely is not durable.

### An audit belongs to a brief, not to an issue

The clearing rule above is only half of requirement 7. Clearing the label lets the issue back into
the audit queue, but the audit itself has to agree that a second audit is a *different* audit — a
step keyed on the issue alone would recognize the re-promoted issue as one it had already handled,
reuse the finished first audit, and stamp nothing. The issue would then sit `ready-for-agent`
forever: never countersigned, so never built, and never re-audited either.

So the audit's identity is the issue **and the brief currently on it**. Two audits of the same
brief are one audit and share a record, which is what makes a resubmission mid-cycle harmless;
an audit of a brief that has since been rewritten is a new audit and gets its own. Every route
back into the build queue runs through triage, which rewrites the brief — so the audit that
matters always runs.

### Failing safe

Every failure that produced a *verdict we cannot trust* bounces. The safe direction there is the
same one triage already fails toward:

- an unreadable, empty, or unknown verdict bounces, carrying an objection saying so, and never
  countersigns;
- a bounce whose objections went missing still bounces, and says that too;
- a session that never spoke at all — a dead shell, a killed process — is captured as nothing and
  retried silently on the attempt budget. Answering for it would send a good brief back to the
  maintainer over an infrastructure fault.

Exhausting that budget is where the audit deviates from triage, deliberately. A session that ran
out of room never read the plan, so it has no finding about it, and triage's own hold — which
routes the issue to the maintainer — would be a lie told with our spend cap. So an audit hold
**preserves the settlement**: the issue keeps `ready-for-agent` and its dials, no grilling route
is projected, and the only thing written is a comment diagnosing the *auditor*. Triage can
destroy nothing at hold time because nothing is settled yet; the audit holds against a settled
decision, and a spend-cap trip must not unwind it.

That leaves the issue ready with no held label, which the daemon's resume sweep never wakes on —
so the comment names `build <N>` / `pickup` as the resume in plain words. Nothing re-audits it in
the meantime either: the held record is terminal *for that brief*, so the next cycle's audit pass
reserves the issue and moves on rather than auditing it forever.

Exhaustion is not silence.

## Consequences

- Every ready issue costs one extra bounded read-only session before it is built. That is the
  price, paid deliberately: it is far below the cost of a build, review and revise round spent on
  a wrong plan.
- The build queue is now gated on a label the daemon stamps. An issue that is `ready-for-agent`
  but un-audited is simply not in the queue — visible to an operator as a ready issue that has
  not moved yet, and resolved by the next cycle's audit.
- Read-only work now has two capped lanes rather than one. Audits and triage no longer slow each
  other down, at the cost of a higher ceiling on concurrent read-only sessions than either lane
  alone implies.
- The audit draws nothing new on the console — it reports on the existing triage lane. Its one
  operator-visible surface is its claim label on the issue while it runs.

## Alternatives considered

**Dial-gating which issues get audited** — skipping the audit for low-effort or standard-
complexity work. Rejected for now: we have no measurement saying which briefs fail, and gating
before measuring would blind us to exactly the data that would justify the gate. Audit everything
first.

**Cross-tool routing** — requiring the auditor to be the *other* tool. Rejected: coldness is the
property that matters, and requiring a specific pool would make the audit block on that pool's
availability, holding ready work hostage to a tool-pairing rule this step does not need.

**A fix chain inside the audit** — letting the auditor rewrite the brief it objects to. Rejected:
one pass, two answers. The bounce lands in the grilling machinery, and triage re-running on the
maintainer's reply is the second adversarial cycle at no extra cost.

**Extracting the rubric into a shared standards file** — so the pipeline and the maintainer's own
interactive plan-review skill converge on one copy. Rejected until a second in-repo consumer
actually exists: the engineering charter is already loaded into every session and covers the
interface-shape axis, and that skill lives in a different, private repository which cannot read a
file from this one. No seam before its second caller.
