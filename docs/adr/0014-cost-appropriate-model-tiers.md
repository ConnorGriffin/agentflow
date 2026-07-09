# ADR 0014 — Cost-appropriate model tiers: intake sizes every issue

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0006](0006-two-pool-runner-assignment.md) took dollar-cost off the table
(both plans are prepaid flat-rate) and made rate-limit **headroom** the scarce
resource. That made it tempting to ignore *which tier within a tool* runs an issue
— and the first M0 `Runner` hardcoded the deep tier (Opus) for everything.

Running Opus 4.8 or GPT-5.6 Sol on a routine CSS/config change is exactly the waste
ADR 0006 exists to prevent: the deep tier burns the 5h and weekly windows far
faster, so mis-sizing a trivial issue *starves the headroom that real work needs*.
Tier selection is the **per-issue complement** of pool balancing — same scarce
resource, finer grain. (It also maps straight to dollars if a pool ever runs on
pay-per-token.)

Each tool ships three tiers: Claude haiku/sonnet/opus; Codex Luna/Terra/Sol.

## Decision

**Every issue is assigned a tool-agnostic cost tier by intake — a hard gate. No
build runs without one.**

| Tier | Work shape | Claude | Codex |
|---|---|---|---|
| `light` | routine/mechanical: CSS, config threading, doc/test sweeps, default flips | haiku | Luna |
| `standard` | ordinary features, moderate logic | sonnet | Terra |
| `deep` | correctness-sensitive, design-heavy, multi-surface | opus | Sol |

- **The tier resolves to each tool's concrete model at launch.** The `Runner` takes
  a tier, never a hardcoded model; each adapter owns its tier→model map
  (`Runner.model_for(tier)`, unit-tested).
- **Intake assigns the tier by work-shape** — the same judgment the old work-order
  `model:*`/`effort:*` table encoded, now required and auditable rather than
  self-selected. It rides the trust ratchet like every other intake decision
  ([ADR 0007](0007-decisive-intake-graduated-autonomy.md)).
- **Tier is orthogonal to pool.** ADR 0006 picks the *pool* by headroom; this ADR
  picks the *tier* within it by complexity. A `light` issue runs haiku on the Claude
  pool, Luna on the Codex pool.

### Reviewer tier — OPEN (pending confirmation)

The cross-tool reviewer needs a tier too. Options: (a) track the issue tier; (b)
track it but **floor at `standard`** (never review with the light tier, since review
is the safety gate that permits unattended merge); (c) always `deep`. **Rec: (b)** —
size review to the work, but a light-tier reviewer risks rubber-stamping and defeats
the cross-tool safety argument ([ADR 0003](0003-cross-tool-review.md)/[0004](0004-auto-merge-gate.md)).
To be finalized before the reviewer stage is built.

## Alternatives considered

- **Builder self-selects its tier.** Rejected: it was the deferred default and is not
  a guarantee — a model asked to size itself trends toward "use the big one." Intake
  sizing is explicit and auditable.
- **One tier everywhere ("always deep, to be safe").** Rejected: the exact waste this
  ADR exists to stop.

## Consequences

- A wrong tier is a cheap miss (re-run higher); *systematic* mis-sizing shows up as
  headroom burn on the dashboard ([ADR 0010](0010-operator-dashboard.md)) — the
  signal to retune intake's rules.
- Exact Codex model IDs (`gpt-5.6-terra`, `gpt-5.6-luna`) still to confirm;
  `gpt-5.6-sol` is verified working.
