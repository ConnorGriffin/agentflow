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
  headroom load balancer; Codex windows are classified by duration and weekly
  unattended use is paced to 80%.
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
- [0014](0014-cost-appropriate-model-tiers.md) — Cost-appropriate model tiers: intake
  sizes every issue (`light`/`standard`/`deep`).
- [0015](0015-review-anchors-to-acceptance.md) — Review anchors to the issue's
  acceptance criteria (beyond-scope correctness is a follow-up, not a blocker).
- [0016](0016-intake-stage.md) — Intake: the autonomous front of the pipe (ground →
  rewrite → route; native, drops the `/triage` skill).
- [0017](0017-ciq-auto-scope-human-merge.md) — ciq-autotune: auto-scope, human-merge
  (promotes ADR 0008's reserved off-diagonal knob).
- [0018](0018-two-dials-review-by-evidence.md) — Two dials (complexity + effort);
  review by evidence not demo; `tier:` retired.
- [0019](0019-human-re-entry.md) — Human re-entry: hold states, comment-resume, the
  `/agentflow` interactive surface, and the skip invariant.
- [0020](0020-build-review-under-partial-availability.md) — Running build/review under
  partial tool availability (prefer-don't-gate review; revise-until-clean with a bail).
- [0021](0021-dispatch-dedup-build-claim.md) — Dispatch dedup: claim an issue before
  building (`agentflow:building`) and before triaging (`agentflow:triaging`) it; lock
  heartbeat keeps single-instance sound.
- [0022](0022-one-build-input-and-the-build-verb.md) — One build input (the Agent Brief)
  for every profile; `build <N>` triggers a ready issue by hand; personal `/go` +
  `/work-order` retired (amends 0005's mechanism).
- [0023](0023-dashboard-replatform-control-plane.md) — Dashboard re-platform: an
  interactive control plane (Svelte + FastAPI, polling liveness, controls over the
  existing verbs); drop the serial dispatch cap for headroom-governed concurrency
  (amends 0010's mechanism and 0006's serialization).
- [0024](0024-dependency-aware-dispatch.md) — Dependency-aware dispatch: a
  `Blocked by #N` marker gates the ready set, so an ordered batch of slices builds
  in order and auto-advances (complements 0023's concurrency).
- [0025](0025-activity-adaptive-spend-ceiling.md) — Activity-adaptive spend
  ceiling: operator activity selects the daemon's ceiling (85% idle / 50% active,
  paced) instead of hard-stopping dispatch; gate reports facts, balancer owns
  policy (rides 0023's concurrency slice).
- [0027](0027-wayfinder-planning-boundary.md) — Wayfinder planning artifacts stay
  upstream of intake; only the build tickets wayfinder files enter agentflow.
- [0028](0028-stage-scoped-continuations.md) — Continuations are durable,
  stage-scoped fresh sessions with bounded attempts, retained claims and tool lineage,
  scheduler-owned waits, and stage-native human holds.
- [0029](0029-static-per-pool-admission.md) — Each provider pool has five static,
  review-controlled permits; conservative 1–5 demand bands preserve short-stage
  concurrency while preventing two code-writing sessions from sharing a pool.
- [0030](0030-session-coordinator-seam.md) — One session coordinator owns durable
  continuation, classification, and atomic admission; provider adapters extract facts
  while stage adapters retain completion and recovery locality.
