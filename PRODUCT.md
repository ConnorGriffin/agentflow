# Product

## Register

product

## Users

Connor, solo, operating a fleet of his own GitHub repos where autonomous agents
(Claude + Codex) build, cross-review, and auto-merge PRs. Context: a glance —
often AFK-adjacent — to answer one question and move on. Not a team; not a
customer-facing surface. The operator's real job is **exception-handling**: most
work self-merges, so the console exists to surface the few things that need a
human (a `guarded`/`reviewed` merge, a parked PR, a repo whose trust ratchet is
ready to loosen) and to confirm the rest is healthy.

## Product Purpose

The operator console for **agentflow** — the tool-agnostic autonomous
issue → PR → review pipeline. It reads GitHub + scheduler state and answers, at a
glance: *is the fleet healthy, and what needs me?* It shows two-pool rate-limit
headroom (idle-while-queued = wasted prepaid capacity), each repo's autonomy
profile and in-flight work, what's awaiting a human merge, an audit feed of what
landed, and the trust-ratchet state. Success = the operator sees fleet health
and their next action in one sweep, trusts what merged without watching it, and
is pinged only for what genuinely needs them.

## Brand Personality

Terminal-native, dense, honest. It reads as an evolution of the CLI it grew from
— a developer/operator instrument, not a product pitch. Voice is plain and exact
(the same voice as the pipeline's own commit messages). It shows the real state
truthfully, including the unflattering one (both pools busy, a thin history, a
parked PR) — no vanity metrics, no fake-live gloss.

## Anti-references

- **SaaS gradient-hero-metric dashboards** — big number + gradient accent +
  supporting stats. The cliché this must not become.
- **Generic Grafana / observability walls** — telemetry for telemetry's sake.
  This surfaces *decisions and health*, not every metric.
- **Decorative glassmorphism / glass cards.** Rare and purposeful, or nothing.
- **Side-stripe / left-bar accent cards** — `border-left` color accents on cards,
  rows, or callouts. Never. Full borders, background tints, or leading glyphs.
- **Card overuse.** Cards are the lazy default container, and nested cards are
  always wrong. Prefer rows / tables / lists where they're the better affordance;
  a card must earn its place.

## Design Principles

- **Surface exceptions, not telemetry.** An autonomous fleet mostly runs itself,
  so the console's primary job is the human's queue — what needs you — with health
  as fast context, not a metrics wall.
- **Honest over impressive.** Show the real state, including the unflattering one.
  No invented data, no vanity numbers, no fake liveness.
- **Dense but legible.** Operator-grade information density: every signal earns
  its pixels, glanceable in one sweep, one screen, no horizontal scroll.
- **The dial is visible.** Autonomy profile and the trust ratchet are first-class
  — the whole product is graduated, earned trust, so the UI makes it legible.
- **An evolution of the CLI.** Reads as the same tool as the pipeline underneath,
  not a foreign web app bolted on top.

## Accessibility & Inclusion

WCAG AA: body text ≥ 4.5:1, large/UI text ≥ 3:1 — verified against the dark
palette (muted gray on tinted panel is the failure mode to watch). Never encode
state by color alone (profile, tool, headroom carry a label or glyph too, for
color-blind readers). Full `prefers-reduced-motion` alternative for any motion.
