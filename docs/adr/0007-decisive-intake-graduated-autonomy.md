# ADR 0007 — Decisive intake and graduated autonomy (decide-then-review + the trust ratchet)

- Status: Accepted
- Date: 2026-07-09

## Context

Two questions from the front of the pipe:

1. Now that builders self-scope ([ADR 0005](0005-spec-rigor-rides-the-dial.md)),
   does a triage stage still run on every issue?
2. How *conservative* should intake be when it judges an issue "build-ready"? The
   reflexive answer — punt anything ambiguous to the human (`needs-grilling`) —
   discards the model's work and, worse, never lets the model build the track
   record that would justify trusting it later.

## Decision

**1. Intake is a thin, decisive router — not a tollbooth.** Every new issue gets a
quick intake pass that stamps category, autonomy profile, pool, and a build-ready
judgment. Build-ready issues proceed straight to a builder. The heavy triage roles
— **ground** (guarded), **mockup** (visual/UX), **grill** (ambiguous) — fire only
as *exceptions*, on issues that aren't ready.

**2. Decide-then-review, don't punt.** Intake, and every later stage, makes its
best call and **stages it under a review gate** rather than asking the human up
front. It emits *an answer, not a question*. The gate — not a pre-emptive punt — is
what protects against a wrong call.

- **Carve-out:** genuinely *undecidable* ambiguity — where the missing input is the
  human's **intent**, not something groundable from the repo or data — still punts
  to `needs-grilling`. There's nothing to review there, only a question to ask.

**3. The gate's stringency *is* the autonomy profile.** How hard a staged decision
is scrutinized before it takes effect rides the same dial as everything else:
`guarded` → a human confirms it; `reviewed` → an independent (cross-tool) review
confirms it; `autonomous` → it takes effect and is audited after. Triage decisions
ride the dial exactly like build diffs do.

**4. The trust ratchet.** A repo starts conservative (gate on) and is loosened
toward autonomy as its staged decisions are **consistently confirmed without
correction**. Loosening is a deliberate, earned, per-repo act — never a default —
and it is reversible: if quality regresses, ratchet back.

## Alternatives considered

- **Mandatory full triage on every issue.** Rejected: a tollbooth that self-scoping
  builders make redundant for well-formed issues.
- **Punt on any ambiguity.** Rejected: discards the model's work and never produces
  the track record that earns autonomy.
- **Jump straight to full autonomy, fix it if it breaks.** Rejected for higher-risk
  repos: the first bad merge is too expensive. Earn the trust; don't assume it.

## Consequences

- **Staged decisions must be legible** — each records what it decided and why — so
  review is cheap and the ratchet has a signal to move on.
- **The ratchet needs a metric:** track the correction rate on staged decisions per
  repo; a sustained low rate is the cue to loosen, a spike the cue to tighten.
- Whether *scope-trust* can be dialed independently of *build-trust* (a separate
  knob vs. the single profile) is resolved in [ADR 0008](0008-conservatism-knob.md).
