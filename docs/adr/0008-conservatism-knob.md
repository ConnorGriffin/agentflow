# ADR 0008 — "How conservative" is the autonomy profile, not a separate knob

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0007](0007-decisive-intake-graduated-autonomy.md) introduced the trust ratchet
and left one question open: is *decision-trust* (how much the triager's scope calls
are reviewed) dialable independently of *build-trust* (how much the diff is reviewed
and who merges)? [ADR 0002](0002-three-autonomy-levels.md) already set the house
rule — one coupled dial, promote a knob only on demonstrated need.

## Decision

**"How conservative" is the autonomy profile.** There is no separate scope-trust
knob. The ratchet moves the whole rung together — grounding rigor, decision review,
build review, and merge policy advance and retreat as one.

A separate knob is promoted **only** when a real repo demonstrably needs an
off-diagonal combination — and then only that one knob is exposed, not a full
matrix of independent settings.

## Alternatives considered

- **Expose a scope-trust knob now.** Rejected: speculative — no repo needs the
  off-diagonal yet, and every knob is config surface and a decision the owner must
  make per repo.
- **Free-for-all independent knobs.** Rejected: config sprawl; the realistic
  combinations lie on the diagonal (ADR 0002).

## Consequences

- Config stays at **one number per repo** (`profile: <level>`).
- The first repo that genuinely needs an off-diagonal split is the trigger to
  promote exactly one knob. The likeliest first case: a guarded project wanting
  **auto-scope but human-merge** — trust the model to route/scope, still hand the
  safety-critical merge to a human. Watch for it; don't pre-build it.
