# ADR 0019 — Human re-entry: hold states, comment-resume, and the interactive surface

- Status: Accepted
- Date: 2026-07-09

## Context

Intake ([ADR 0016](0016-intake-stage.md)) can route an issue to a hold state
(`needs-grilling`, `needs-mockup`). Something has to define how a held issue moves
again, and how the maintainer works *with* the pipeline by hand at any phase — the
counterpart to the autonomous daemon.

## Decision

- **Held issues are inert to agents.** No builder touches `needs-grilling` /
  `needs-mockup`. They move only when the human re-enters.
- **A comment reply auto-advances.** When the maintainer replies on a held issue, the
  daemon re-checks it: promote to `ready-for-agent` if the fork is settled, else quietly
  sharpen the leftover questions. It re-posts only if something genuinely remains open —
  **no spam.**
- **`/agentflow <verb> N` is the interactive surface** — one skill whose verbs
  (`pickup`, and the per-phase entries) run the **same** logic the daemon runs. It
  resumes a held issue live, enters the mockup phase, or scopes an issue start-to-finish
  in conversation to skip the front steps.
- **The skip invariant.** Driving by hand only ever skips the **front** steps (triage /
  grilling / mockup). However an issue gets scoped, it still runs build → review →
  merge, and the repo's profile governs those gates. **The safety gates are never
  skippable** — manual entry adds convenience, never authority.

Grilling questions — posted by intake or `/agentflow` — are in the **maintainer's
voice**: the app's behavior and their real numbers, never code symbols / ADR numbers /
file paths, with a symptom, concrete options, and a recommendation.

## Alternatives considered

- **Held issues wait only for an explicit `/agentflow pickup`.** Rejected: a plain
  GitHub reply is the low-friction path; the daemon can re-check on comment activity.
- **Let manual runs bypass review/merge when the human is present.** Rejected: the gates
  are the whole safety story; presence is not authority.

## Consequences

- The daemon watches held issues for maintainer replies since its last pass (the same
  activity-discovery it already does for the ready queue).
- Daemon and skill share one intake/scope/grill **core** (a deep module, two entry
  points) — no second implementation to drift.
