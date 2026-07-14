# ADR 0016 — Intake: the autonomous front of the pipe

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0007](0007-decisive-intake-graduated-autonomy.md) designed a decisive intake
stage, but the loop never grew one: `loop.run_once` reads `ready-for-agent` + a tier
label directly and trusts that a labeled issue is well-formed. So a vague issue
labeled ready gets scoped broadly by whatever the builder guesses — and
[ADR 0015](0015-review-anchors-to-acceptance.md) (acceptance criteria *are* the merge
bar) means a vague issue yields a mushy review that can't save you. The front of the
pipe is missing. This is the biggest real gap in what's built.

## Decision

**Intake is a real, autonomous pipeline stage** that fires on every open issue with
**no state label**, except upstream `wayfinder:*` planning artifacts as established by
[ADR 0027](0027-wayfinder-planning-boundary.md). It:

1. **Grounds** — reads the code deeply, and if the repo declares a read-only
   data-fetch (the *grounding fetch*, e.g. ciq's `ciq-pull-db` → `ciq.readonly.db`),
   pulls a fresh snapshot and checks real numbers. On-demand: a crisp issue skips it.
2. **Rewrites** the title and description into something specific (records
   `> Retitled from: "…"`).
3. **Routes** to exactly one outcome:
   - **`ready-for-agent`** — nothing left to decide; writes the brief and proceeds.
   - **`needs-mockup`** — a user-facing surface beyond a minor bugfix; holds for a
     `/ui-mockups` pass.
   - **`needs-grilling`** — a real choice remains that changes the result and can't be
     settled from code/data, **or** the request contradicts a recorded ADR; holds and
     posts questions ([ADR 0019](0019-human-re-entry.md)).

**The ask bar** (tightening ADR 0007's decide-then-review): intake scopes anything it
can pin down confidently and only holds when a genuine, *outcome-changing* fork
survives grounding. It never punts small uncertainties it can reasonably decide, and
never silently overrides an ADR.

**The brief** (at `ready-for-agent`): scoped fix; a `Verified:` section (the claim
re-derived against named code on the snapshot); dos/don'ts; the decision tree from any
grilling; acceptance criteria (grounded numeric literals where they apply); and the two
dials ([ADR 0018](0018-two-dials-review-by-evidence.md)).

**Intake is native to agentflow** — tool-agnostic, owning its prompt/logic in-repo. It
does **not** shell out to the external `/triage` skill; that dependency is dropped.

## Alternatives considered

- **Keep trusting `ready-for-agent` = well-formed** (status quo). Rejected: vague
  filing → broad build → weak review. The exact gap this closes.
- **Reuse the `/triage --auto` skill as the stage.** Rejected: an external, Claude-only
  skill in the runtime; the engine should own its core stage, tool-agnostically — one
  source of truth. (Dropping it also removes the second copy that would drift.)
- **Decide-then-review on *any* ambiguity** (ADR 0007, literal). Kept in spirit but
  tightened: an outcome-changing, ungroundable fork holds for the human rather than
  staging a guess a vague review can't catch.

## Consequences

- The daemon grows a second scan: the **intake queue** (unlabeled build issues, excluding
  `wayfinder:*` planning artifacts) runs before the build queue (`ready-for-agent`).
- Staged decisions stay legible (retitle note + `Verified:` + brief) so the merge gate
  can catch a mis-scope and the trust ratchet (ADR 0007) has a signal.
- Grilling questions post in the maintainer's voice (ADR 0019).
- The frozen `guarded` work order (ADR 0005) is no longer intake's default output; see
  [ADR 0017](0017-ciq-auto-scope-human-merge.md) for how ciq is grounded instead.
