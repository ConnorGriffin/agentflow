# ADR 0018 — Two dials, and review by evidence not demo

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0014](0014-cost-appropriate-model-tiers.md) sized every issue with one `tier:`
label (`light`/`standard`/`deep`). But ciq's real practice used three *separate* dials
— `model:` (sonnet/opus), `effort:` (low/med/high), `review:` (diff/explainer/demo) —
and never used `tier:` at all. And the `review:demo` mode needs a human watching a live
server, which breaks unattended autonomy.

## Decision

Intake stamps **two dials**, tool-agnostic:

- **complexity**: `standard | deep` → sonnet/Terra or opus/Sol for the builder.
  (Drops the unused `light`/haiku — ciq's builder floor was always sonnet.)
- **effort**: `low | medium | high | extra`.

Every reviewer runs at the `deep` tier, independently of builder complexity. The
complexity dial continues to size builders and revisers only.

The **`review:` dial is dropped.** Its intent is met by two *always-on* rules:

- **Every PR is written for the human who merges** — plain language: what changed, why,
  what to check. No jargon. (This is what `diff`/`explainer` were really for, and the PR
  already *is* the diff.)
- **UI changes attach headless Playwright screenshots** to the PR — proof-of-match to
  the locked mockup — replacing the live `demo` with no human in the loop. A live
  demo-on-a-port (`8100 + issue#`) stays available for *interactive* use, never required
  autonomously.

## Alternatives considered

- **Keep the single `tier:` dial** (ADR 0014). Rejected: too thin — ciq needed model
  size and effort independently.
- **Keep `review:demo` for UI.** Rejected: it nukes autonomy — someone has to watch.
  Screenshots carry the same evidence unattended.
- **A per-issue review-mode dial (diff/explainer).** Rejected: redundant — a
  human-framed PR is the explainer, the PR is the diff.

## Consequences

- `tier:` is retired; the loop's hard gate reads **complexity + effort**. This
  supersedes ADR 0014 on the *taxonomy*; its principle — intake sizes every issue as a
  hard gate — stands.
- **"PR framed for a human, not jargon"** becomes a charter-level standard — the
  merge-glance depends on it.
- The **dials and hold-state labels** are namespaced **`agentflow:*`**
  (`agentflow:complexity:*`, `agentflow:effort:*`, `agentflow:needs-grilling`,
  `agentflow:needs-mockup`) so they never collide with a repo's own triage labels.
  `ready-for-agent` stays bare — it's the established queue label the loop already reads
  across the fleet, so renaming it buys churn without value.
