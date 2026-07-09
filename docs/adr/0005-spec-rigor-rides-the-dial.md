# ADR 0005 — Spec rigor rides the dial: self-scoped brief vs frozen work order

- Status: Accepted
- Date: 2026-07-09

## Context

The superseded design froze a heavy, self-contained **work order** for every
issue routed to Codex — a hermeticity contract, file allow-list, spelled-out safe
defaults, named invariant tests. The reason was capability: Codex couldn't explore
the repo, couldn't ground against real data, couldn't stand up the app, so every
fact it needed had to be baked in.

Both tools can now explore and ground. So the frozen work order is no longer
*forced* by tool weakness. But the domain reason it existed at `ciq-autotune` has
not gone away: in a medical domain, a plausible-but-wrong guess about insulin math
or attribution is the expensive failure mode — and a smarter model that guesses
confidently is not safer, it's more convincing.

## Decision

**Spec rigor is set by the autonomy profile, not by the tool.**

- **`autonomous` / `reviewed`** — the **issue itself is the brief**: acceptance
  criteria plus file pointers. The builder **self-scopes** — reads the repo,
  grounds against real data where it helps, decides its own touch-set — and notes
  the decisions it made in the PR. No frozen contract.

- **`guarded`** — a **frozen hermetic work order** survives, reframed as the
  *grounding mechanism*: at scope time a human (or an Opus scope pass with
  real-data access) pre-decides the domain facts, freezes them as literals and
  test fixtures, and declares a file allow-list plus named invariant tests. The
  builder does **not** guess domain facts.

- **Gap protocol, retained at `guarded` only:** a builder that hits an unstated
  domain fact, threshold, or fixture **stops and posts a marker** rather than
  guessing. At `autonomous`/`reviewed` the builder is trusted to ground and decide,
  and records what it chose.

## Alternatives considered

- **Always a thin brief.** Rejected: the `guarded` domain punishes confident
  guesses; grounding must be pre-frozen there, not left to the builder.
- **Always a frozen work order.** Rejected: expensive to author, and it throws away
  the self-scoping capability that both tools now have — most work doesn't need it.

## Consequences

- **Triage/scoping effort is now profile-dependent.** A `guarded` issue needs a
  real-data grounding pass before it's buildable; an `autonomous`/`reviewed` issue
  just needs to be a well-formed issue. Who does the guarded grounding (human vs an
  Opus scope-runner with DB access) is a downstream question.
- The gap/marker protocol is preserved as a **per-level safety**, not a
  Codex-specific cage — any builder at `guarded` obeys it.
- The heavy work-order authoring cost is now paid only where domain risk earns it.
