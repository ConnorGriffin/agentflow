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
- [0026](0026-daemon-owned-snapshot.md) — The daemon is the sole producer of the
  snapshot; web reads local published state and never queries GitHub.
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
- [0031](0031-build-tracer-rollout.md) — Historical staged rollout of all six logical stages;
  issue #109 removes the legacy mode after the drain and leaves coordinator-only pause/drain.
- [0032](0032-shared-global-agent-instructions.md) — Claude and Codex share one
  machine-global instruction file; the engineering charter remains canonical and
  referenced by both.
- [0033](0033-project-workspace-state-and-control-plane.md) — The daemon alone owns
  local Project/Conversation/Proposal state and projections; GitHub and default-branch
  git remain authoritative for published artifacts.
- [0034](0034-methodology-session-orchestration.md) — A Conversation turn is a coordinated
  logical stage behind the existing session coordinator; skills stage candidate artifacts in
  isolated working state, the daemon adopts accepted turns into immutable Proposals, and only
  an explicit operator approval + Publication crosses the promotion boundary.
- [0035](0035-workflow-engine-read-only-operator-console.md) — Agentflow is a headless
  workflow engine with one unified read-only operator console; chat owns planning,
  GitHub/repositories own durable truth, the Project workspace is retired, and the
  map viewer survives as an information model for a fully reopened UI design.
- [0036](0036-bounded-repository-map-projection.md) — The daemon projects bounded GitHub-native
  Decision Maps, verified handoffs, pipeline state, landed evidence, and contextual ADR links
  under a fixed heartbeat/API budget; the browser remains a read-only file consumer.
- [0037](0037-daemon-dispatch-of-afk-research.md) — Wayfinder plans, the daemon
  executes: the boundary is judgment vs execution; claim + type replaces the
  `wayfinder:*` wall, and unclaimed AFK-able research tickets dispatch through the
  coordinator under permits and recovery (supersedes 0027, amends 0035).
- [0038](0038-conflict-resolution-as-revise.md) — A survivor's re-rebase conflict opens
  a conflict Revise on the owned PR branch instead of parking or force-resolving.
- [0039](0039-open-prs-drain-first.md) — Admission ranks PR-bound stages (review,
  revise, respond) ahead of issue-bound stages: open PRs drain before new work starts.
- [0040](0040-spend-per-success-measurement-contract.md) — Spend experiments measure
  headroom-denominated cost per verified stage and per merged issue, gated by quality
  guardrails and cohort cells; dollars are only the cross-tool comparison signal.
- [0041](0041-stage-model-reasoning-matrix.md) — Stage model/complexity cells stay
  mostly unchanged (Opus Build and all-deep Intake are complexity decisions owned by
  #228; deep cross-tool Review is safety, not a savings target); Respond/conflict-Revise
  carry-complexity is a directional parity note; every reasoning-effort cell is unset
  pending #223 telemetry.
- [0042](0042-codegraph-okf-complementary-layer.md) — The curated operational
  knowledge (OKF) layer complements, never replaces, the slim code graph: retrieval
  gated by task shape and capped at a few concepts, kept as a derived projection of
  CONTEXT.md and the ADRs.
- [0043](0043-recovery-state-before-replay.md) — A retry needs new recovery state: a
  clean read-only exit with no outcome earns one targeted repair then parks; worktree
  stages continue behind a bounded recovery envelope; identical stateless replays stop.
