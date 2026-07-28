# ADR 0017 — Guarded project: auto-scope, human-merge

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0008](0008-conservatism-knob.md) kept "how conservative" on one coupled dial and
named the likely first exception: a guarded project wanting **auto-scope but
human-merge** — trust the model to route/scope, still hand a safety-critical merge
to a human. It said to watch for it and promote exactly that one knob when a real repo
needed it. Building intake ([ADR 0016](0016-intake-stage.md)) is that moment.

ciq's own closed-issue history already worked this way: auto-triage grounded against a
read-only snapshot, wrote a **self-scoping** brief (*"explore fresh; these are pointers,
not line numbers"*), and a human merged every PR. It never used ADR 0005's frozen
hermetic work orders despite carrying the `guarded` label.

## Decision

A guarded project runs the **auto-scope + human-merge** off-diagonal — the one
knob ADR 0008 reserved for demonstrated need:

- **Scope/build trust = `reviewed`:** intake self-scopes with **mandatory** real-data
  grounding (the `ciq-pull-db` snapshot), and the builder self-scopes from the brief.
  No frozen work order.
- **Merge policy = human:** every PR parks for a human merge, regardless of review
  verdict. Nothing reaches `main` unattended. This half of the dial does not ratchet.

## Alternatives considered

- **Keep ciq `guarded` with frozen work orders** (ADR 0005). Rejected: heavier than
  ciq's actual practice. The *grounding rigor* — not a frozen contract — is what the
  medical domain needs; the human merge is the gate that matters.
- **Move ciq to plain `reviewed`** (human glances, could auto-merge later). Rejected:
  the medical merge stays a deliberate human act, permanently.

## Consequences

- `profile: reviewed` on ciq's `AGENTS.md` is read literally (per PR #336). The
  grounding fetch (ADR 0016) supplies the rigor a bare `reviewed` repo would lack.
- The frozen work order (ADR 0005) stays *defined* for a future repo that needs it, but
  is **unused** in the fleet today — no dead machinery; build the seam at the second
  caller, not before.
