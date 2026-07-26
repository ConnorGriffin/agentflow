---
# Authoritative design tokens (impeccable reads these). GitHub-dark-derived, dark-only.
# The shipping surface — the v2 console at agentflow/webui/ — is the live source of truth
# a `local` mockup round inherits (ADR 0026/0048); harvest current tokens from it, not from
# the retired stdlib dashboard these values were originally lifted from.
colors:
  bg: "#0d1117"        # app background
  surface: "#161b22"   # panels / rows
  border: "#30363d"    # hairlines
  ink: "#e6edf3"       # primary text
  muted: "#8b949e"     # secondary text (AA: use only ≥ large or with care on surface)
  accent: "#58a6ff"    # primary interactive · also codex / reviewed
  success: "#3fb950"   # autonomous · merged · healthy · headroom-ok
  warning: "#d29922"   # guarded · claude · caution · headroom-low
  danger: "#f85149"    # busy / blocked / error
typography:
  primary: "ui-monospace, SFMono-Regular, Menlo, monospace"
  scale: "compact"     # 12–18px; data-dense, not marketing-large
rounded: "8px"
spacing: "compact"     # operator-grade density; vary for rhythm, don't pad
theme: "dark-only"
---

# Design

## Theme

Dark-only, GitHub-dark-derived — the scene is an operator glancing at a terminal-
adjacent console, often in a dim room, mid-flow. Monospace-primary: this is a
developer instrument, an evolution of the CLI underneath, not a web product.

## Color roles

Semantic, not decorative. Every color means one thing:

- **Autonomy profile:** `autonomous` = success/green, `reviewed` = accent/blue,
  `guarded` = warning/amber. Always paired with a text label (never color-only).
- **Pool / tool:** `claude` = amber, `codex` = blue. Also labelled.
- **Headroom / health:** > 40% green, 15–40% amber, < 15% red.
- One accent (blue) for interactive/primary; state colors used sparingly and only
  where they carry meaning. No gradients; emphasis via weight/size, not hue-blends.

## Typography

One family (the mono stack) in multiple weights — no second font. Contrast comes
from weight and size, not from pairing. Numbers and identifiers (PR #, repo,
percentages) are the content; treat them as first-class. Cap any prose at 65–75ch.

## Components

- **Rows and tables over cards.** The fleet is tabular by nature (repos × signals);
  prefer aligned rows. A card must earn its place; nested cards are banned.
- **No side-stripe / left-bar accents.** State goes in full borders, background
  tints, badges, or leading glyphs — never a `border-left` color bar.
- Badges/pills for profile + tool (color + label). Thin SVG bars/dials for headroom
  and ratchet — hand-drawn, no chart lib.

## Layout

One screen, no horizontal body scroll; internal regions scroll within themselves.
Density with rhythm — vary spacing to group, don't uniformly pad. Semantic z-index
scale (sticky header → dropdown → modal → toast), never arbitrary values.

## Motion

Minimal and intentional: a live-pulse on in-flight work, a subtle enter on new feed
items. Ease-out (quart/expo), no bounce. Full `prefers-reduced-motion` alternative
(crossfade or instant) on everything.
