# Architecture Decision Records — agentflow

Decisions about the tool-agnostic, autonomous **issue → PR → review** pipeline
that `agentflow` defines. Consuming repos (`ciq-autotune`, `work-kit`, vibe-code
projects) carry only their own per-repo config (profile + hazards); the design
lives here.

Supersedes the tooling ADRs in `dotfiles/docs/adr/0001–0005` (the "two-tool
split / Opus-ends, Codex-middle" era), which were written the day before GPT-5.6
Sol shipped and are kept only for history.

Format follows the ciq-autotune convention: `# ADR NNNN — Title`, Status + Date,
then Context / Decision / Alternatives / Consequences.

## Index

- [0001](0001-per-repo-autonomy-profile.md) — One pipeline, one dial: the per-repo
  autonomy profile.
- [0002](0002-three-autonomy-levels.md) — Three autonomy levels: `autonomous`,
  `reviewed`, `guarded`.
- [0003](0003-cross-tool-review.md) — Cross-tool review is the independence gate.
- [0004](0004-auto-merge-gate.md) — The auto-merge gate: severity bar, one revise
  round, drop-to-reviewed.
- [0005](0005-spec-rigor-rides-the-dial.md) — Spec rigor rides the dial:
  self-scoped brief vs frozen work order.
- [0006](0006-two-pool-runner-assignment.md) — Runner assignment: a two-pool
  headroom load balancer.
- [0007](0007-decisive-intake-graduated-autonomy.md) — Decisive intake and
  graduated autonomy (decide-then-review + the trust ratchet).
- [0008](0008-conservatism-knob.md) — "How conservative" is the autonomy profile,
  not a separate knob.
- [0009](0009-collision-safety.md) — Collision safety without a universal
  allow-list.
- [0010](0010-operator-dashboard.md) — The operator dashboard: one console over
  GitHub-as-source-of-truth.
- [0011](0011-persistent-orchestrator.md) — Persistent orchestrator, ephemeral
  hands.
- [0012](0012-build-in-vertical-slices.md) — Build in vertical slices, dogfooded on
  a live repo (method + the `/ui-mockups` and deep-module gates).
- [0013](0013-engineering-charter.md) — Engineering standards: one canonical charter,
  both tools, enforced at review.
