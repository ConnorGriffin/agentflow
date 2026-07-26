# ADR 0048 — Mockup scope (local vs surface) and a LOCKED design contract

- Status: Accepted
- Date: 2026-07-26

## Context

Every non-minor UI issue ran the same mockup pipeline: a 3-4 "wildly-different" whole-surface
concept tournament, regardless of whether the change was a whole-surface replacement or a small
addition inside an already-shipping surface. Reopening the entire visual world for a contained
addition wastes the round and invites drift from the shipping look.

The charter requires screenshots as proof-of-match to the locked visual specification, but the
pipeline only proved that screenshots *exist* (ADR 0018's mechanical gate). After a maintainer
picked a variant, the ready brief recorded the variant's file path; the independent reviewer was
handed no compact, durable contract to judge the implementation against, and mockup files later
move to the archive — so the spec the review depended on could vanish.

The repo's own visual-authority docs compounded the ambiguity: `mockups/INDEX.md` had no current
`locked` row and its grounding line lifted tokens from `agentflow/static/dashboard.html`, a file
deleted by ADR 0026; `DESIGN.md` said the same. So a "local" inheritance had no honest incumbent
to resolve to.

A joint review against Impeccable v4.0.2 surfaced both gaps. This decision amends the mockup
portion of ADR 0016 (intake) and ADR 0018 (charter gates); the mechanical UI-evidence gate is
untouched.

## Decision

### Intake classifies mockup scope

Intake's structured decision carries a `mockup_scope ∈ {local, surface}` on a `mockup` route and
states the scope in the kickoff comment. `local` is the fail-safe default: an unknown, missing, or
invalid value never reopens the whole surface. The scope is stamped as a managed
`agentflow:mockup:<scope>` label so the later produce phase recovers it durably, and it is cleared
on re-route exactly like the complexity/effort dials.

- **local** — an addition inside a shipping surface. The round inherits that surface's identity
  (theme, layout, components, data) and varies only the addition: purpose, hierarchy, interaction,
  states, and fit. It does not reopen the whole visual world. The incumbent it inherits is the
  shipping web UI at `agentflow/webui/`.
- **surface** — a whole-surface replacement or a brand-new surface. Unchanged: the 3-4
  genuinely-different whole-surface concept tournament, retaining ADR 0035's open visual questions.

### Every variant carries a LOCKED contract

Each drawn variant emits a `LOCKED` contract of at most 150 words: thesis, user path, first
viewport, visual rules, required interactions/states, the states that must be screenshotted, and
explicit out-of-scope boundaries. The contract lives inline in the marked mockup issue comment
(the durable channel intake reliably reads on a pick), beside its variant.

### The contract is copied verbatim into the ready brief

On a maintainer pick/resume, intake copies the chosen variant's `LOCKED` contract **verbatim**
into the ready build brief (under a `LOCKED visual contract` heading) and records the exact
committed mockup path and branch. Review stays self-contained after the mockups are archived.

### Build produces the stated states; review judges against the contract

The build produces a screenshot of every state the contract names must be screenshotted and
satisfies its stated visual rules and interactions. The independent reviewer opens the
implementation screenshots and compares them to the contract. A mismatch is `fix_before_completion`
**only** when it violates a stated contract line; unsupported visual taste the contract never
claimed remains `discard_preference`. The four-action taxonomy is preserved.

### The mechanical gate is unchanged

The existence-only UI-evidence gate (ADR 0018) is left byte-for-byte alone. Contract fidelity is
reviewer judgment, not a new unwaivable mechanical matcher — no pixel-diff, no automated
contract-match gate.

## Consequences

- A contained UI addition no longer triggers a whole-surface tournament, and stays visually
  consistent with the shipping app by inheriting it.
- The reviewer has a durable, compact spec to judge against that survives mockup archival.
- The visual-authority docs resolve `local` inheritance to the shipping web UI with honest
  provenance, ending the dangling reference to a deleted file.
