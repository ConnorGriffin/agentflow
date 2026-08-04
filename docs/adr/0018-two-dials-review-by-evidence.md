# ADR 0018 — Two dials, and review by evidence not demo

- Status: Accepted — complexity sizing the builder superseded by
  [ADR 498](adr-498-capability-routed-session-led-dispatch.md), and always-`deep` review by
  [ADR 498](adr-498-tiered-parent-independent-review.md); the two dials themselves, the dropped
  `review:` dial, and review-by-evidence remain
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

## Amendment — 2026-07-28: declaring UI surfaces fleet-wide (issue #337)

The UI-evidence gate reads a per-repo `ui-surfaces:` declaration in AGENTS.md. Only
agentflow ever wrote one, so the gate was inert in other enrolled repos with real
frontends. The declaration stays
per-repo (a repo knows its own surfaces; a central list would drift), with three
amendments:

- **`ui-surfaces: none` is the explicit headless value.** A repo that means "no user
  facing surface" now says so. The gate stays inert for it, and builder/reviewer prompts
  stop asking it for screenshots of a UI it does not have.
- **Silence is a third state, reported but never fail-closed.** An undeclared repo
  behaves exactly as it did — gate inert — but `python -m agentflow.enroll audit`
  names it. Failing closed on silence would park unrelated private-tooling and
  sandbox PRs for no reason; the fix for silence is to answer it, not to stop the
  fleet.
- **Enrolment seeds the line provisionally, and a backfill command settles it.**
  `enroll-standards.sh` seeds `ui-surfaces: none` so a new repo starts declared;
  `python -m agentflow.enroll surfaces <dir> [--apply]` inspects a checkout, proposes the
  right value, and prints which of that repo's open PRs the declaration would newly park
  before it writes anything. The seed is written without looking at the repo, so a `none`
  the checkout contradicts is **not** treated as a settled answer: the backfill rewrites
  that line and the audit names the repo, so a repo with a real UI can never go quiet
  behind the seed. A hand-written surface list is never rewritten. Detection is
  enrolment-only — the merge path still reads a written declaration and never guesses.

The retroactive turn-on was **measured before it took effect**: across the fleet's
open PRs at the time of the audit, exactly one would newly have needed screenshots,
in a `reviewed` repo where nothing auto-merges anyway. It has since landed; the
impact preview is the durable way to re-measure before each backfill.
