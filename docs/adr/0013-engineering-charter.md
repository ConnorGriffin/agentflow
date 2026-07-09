# ADR 0013 — Engineering standards: one canonical charter, both tools, enforced at review

- Status: Accepted
- Date: 2026-07-09

## Context

The flow must guarantee every app it builds is well-architected and maintainable —
*regardless of which tool built it*. Both tools read a hierarchical instruction
file (Claude: `~/.claude/CLAUDE.md` global + repo `CLAUDE.md`; Codex:
`~/.codex/AGENTS.md` global + repo `AGENTS.md`), but the setup is fragmented and
already drifting:

- Claude's global is versioned (`dotfiles/claude/CLAUDE.md`) but **Claude-only**.
- Codex's global (`~/.codex/AGENTS.md`) is **empty** — Codex builds inherit *no*
  standards.
- Per-repo copies are **hand-duplicated** (`ciq-autotune` has twin `CLAUDE.md` /
  `AGENTS.md` that will drift) or **missing** (`retirement-planner` is Codex-blind).

So today, whether an app is well-architected depends on which tool happened to build
it and whether two files stayed in sync. For a flow that builds on either tool, that
is not a guarantee.

## Decision

**One canonical charter; both tools read the same bytes.** The engineering charter
is a single file, [`standards/CHARTER.md`](../../standards/CHARTER.md).

- **Machine-global scope.** Claude's global instructions `@import` the charter;
  Codex's global `~/.codex/AGENTS.md` is a **symlink** to it. Applies to all coding
  on this machine — the bar is good everywhere, and it fixes the empty Codex global
  immediately. (Chosen over flow-scoped: less wiring, no repo left blind.)
- **Per-repo:** `AGENTS.md` is the canonical repo file; `CLAUDE.md` is a **symlink**
  to it. Never two copies. The repo file carries facts + `profile` + hazards, not the
  charter (that's inherited from global).
- **Enforced at cross-review — this is the actual guarantee.** An instruction file is
  a *suggestion* a builder can under-apply. The cross-tool reviewer
  ([ADR 0003](0003-cross-tool-review.md)) is handed the charter as an explicit
  rubric, and a violation — a shallow module, an unmocked UI surface, an interface you
  can't test through — is a **blocking finding** ([ADR 0004](0004-auto-merge-gate.md)
  severity line). Injection makes compliance likely; the review gate makes it real.
- **An enroll step wires it** ([`scripts/enroll-standards.sh`](../../scripts/enroll-standards.sh)),
  non-destructively: dry-run by default, backs up before touching, skip-and-warn
  (the `install.sh` pattern). It populates the empty Codex global, adds the Claude
  `@import`, and normalizes a repo's twin files into `AGENTS.md` + a `CLAUDE.md`
  symlink.

## Alternatives considered

- **Rely on each tool's native file separately.** Rejected: that *is* today's drift —
  empty Codex global, hand-synced twins.
- **Flow-scoped charter (enrolled repos only).** Rejected for the universal bar: more
  wiring and a non-flow repo gets nothing; deep-module/testing discipline is good
  everywhere.
- **Inject at build only, skip review enforcement.** Rejected: an instruction is not a
  gate; without the reviewer rubric, "well-architected" stays aspirational.

## Consequences

- The charter's **content** is a living doc worth its own refinement pass; this ADR
  fixes the *mechanism*, not the wording.
- Claude keeps its personal-prefs global; the charter is **additive** (via `@import`),
  not a replacement.
- The **reviewer prompt must carry the charter rubric** — the cross-tool review stage
  (a top must-build, see [reuse-map](../reuse-map.md)) owns that.
- Enrolling a new repo or a new tool becomes a **symlink, not a copy** — drift is
  structurally impossible.
