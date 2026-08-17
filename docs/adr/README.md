# Architecture Decision Records — agentflow

Decisions about the tool-agnostic, autonomous **issue → PR → review** pipeline
that `agentflow` defines. Consuming repos carry only their own per-repo config
(profile + hazards); the design
lives here.

Supersedes earlier private-tooling ADRs 0001–0005 (the "two-tool
split / Opus-ends, Codex-middle" era), which were written the day before GPT-5.6
Sol shipped and are kept only for history.

New ADRs follow the fleet-wide issue-keyed convention:
`docs/adr/adr-<issue>-<slug>.md`, with heading `# ADR <issue> — Title`, then
Status + Date and Context / Decision / Alternatives / Consequences. The issue is
the GitHub issue that originated the decision. Multiple records from one issue
use distinct slugs.

The sequentially numbered records indexed below predate that convention. They
are legacy records: keep their filenames and links stable, but do not add new
ADRs in that format.

## Index

- [ADR 729](adr-729-receipt-repair-convergence.md) — Capability repair does every deterministic
  repair it can and then proves discovery: missing and verbatim-stale native-discovery receipts
  are re-proven in the same call, an intact installed runtime no longer blocks other repairs, and
  `ready` in a repair log line is earned by a re-probe. Supersedes the never-rewritten receipt
  clauses of ADR 582 and ADR 627.
- [ADR 649](adr-649-owned-disposable-maintenance.md) — Temporary provider probes, explicit
  per-worktree Git metadata, dry-run-first JSONL maintenance, and same-run-coupled graph pruning.
- [ADR 594](adr-594-builds-keep-origin-main-base.md) — Builds retain one hard-coded
  `origin/main` base; a native same-repository blocker edge orders lock-then-build work until its
  mockup contract merges.
- [ADR 694](adr-694-activate-promoted-review-methods.md) — Consecutive fleet-policy versions and
  query-only successor activation carry approved Review methods into production briefings.
- [ADR 635](adr-635-immutable-canary-reports.md) — One immutable, content-free report per
  canary stage/version is derived from Store-owned attribution and decoded attempt telemetry;
  retries return the committed final row rather than reinterpreting later telemetry.
- [ADR 627](adr-627-composed-operational-admission.md) — One Store-owned production admission
  transaction binds policy, capability, RouteCell, Safety, canary attribution, receipt, permit,
  and historically recoverable launch authority.
- [ADR 646](adr-646-immutable-route-selection.md) — Routing materializes one closed immutable
  launch artifact; OperationalSafety registers and decodes it for shared argv and supervision
  consumption without giving ordinary admission routing authority.
- [ADR 628](adr-628-read-only-effective-policy-briefings.md) — A read-only resolver folds pinned
  fleet policy, monotone repository overlays, and stage applicability into one bounded immutable
  briefing without acquiring promotion or persistence authority.
- [ADR 585](adr-585-bounded-operational-self-healing.md) — One OperationalSafety owner
  performs one deterministic rerun, exact-RouteCell quarantine/CAS reopen, and
  generation-bound approved-canary rollback in the coordinator Store.
- [ADR 584](adr-584-human-governed-promotion.md) — Exact, human-governed
  promotion through a read-only GitHub authority receipt and SQLite schema v4 binding marker.
- [ADR 626](adr-626-manifest-rooted-evaluation-semantic-bundle.md) — One versioned,
  manifest-rooted Evaluation bundle binds declarative candidate JSON to one pure semantic
  module; validation executes that authority without reimplementing its algorithms.
- [ADR 620](adr-620-evaluation-failure-classes.md) — Exactly six orthogonal evaluation failure
  classes; aliases, merged classes, and automatic policy mutation are rejected.
- [ADR 606](adr-606-explicit-missing-metrics-and-adjudication-lineage.md) — Missing-metric
  names exactly match null values, and adjudication binds the canonical case and answer key.
- [ADR 605](adr-605-canonical-evaluation-rulebook.md) — One versioned authority and no
  duplicated Evaluation policy; its one-data-file clause is superseded by ADR 626.
- [ADR 596](adr-596-typed-evidence-envelopes-and-lineage.md) — Typed failure/producer
  envelopes, bounded same-repository lineage, contextual briefing closure, the SQLite schema v3
  foundation extended by ADR 584, and suffix-routed JSON contract v2.
- [ADR 582](adr-582-capability-parity-and-environment-failure.md) — Methodology capability parity
  and named environment-failure recovery.

- [ADR 580](adr-580-evidence-module-interface-and-retention.md) — Evidence module interface,
  authority-verification seam, and bounded retention.

- [ADR 538](adr-538-automatic-codex-session-lead-fallback.md) — Automatic Codex session-lead
  fallback.
- [ADR 541](adr-541-native-session-lead-helpers.md) — Native session-lead helpers retain parent
  accounting.
- [ADR 570](adr-570-build-progress-lease.md) — Build alone uses a child-local progress lease,
  supervised test deadline, and immutable absolute cap; every other stage keeps its fixed wall.

- [0001](0001-per-repo-autonomy-profile.md) — One pipeline, one dial: the per-repo
  autonomy profile.
- [0002](0002-three-autonomy-levels.md) — Three autonomy levels: `autonomous`,
  `reviewed`, `guarded`.
- [0003](0003-cross-tool-review.md) — Cross-tool review is the independence gate.
- [0004](0004-auto-merge-gate.md) — The auto-merge gate: exact-head independent review, bounded
  reviewer-fix chain, and no same-tool taint.
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
- [0017](0017-guarded-auto-scope-human-merge.md) — Guarded project: auto-scope, human-merge
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
  stages continue behind a bounded recovery envelope; Review joins the latter in 0047.
- [0044](0044-stage-session-profiles-and-ceilings.md) — Every daemon session gets a
  per-stage tool allowlist (read-only for Intake/Research; Review becomes writable in 0047), an empty MCP set,
  and per-cell wall/turn ceilings replacing the shared two-hour timeout; fail closed
  on withheld capability.
- [0045](0045-intake-stays-all-deep.md) — Intake stays all-deep: a live pilot showed
  standard-first Intake doesn't cut headroom (grounding output dominates, not model
  tier), and the #228 corpus can't score a live run (grounding-SHA ≠ label-SHA), so the
  step-up is rejected.
- [0046](0046-production-routing-and-spend-policy.md) — The production routing and
  spend policy locks (map #226 terminal): ship on provider-default reasoning +
  placeholder ceilings and tune later; the `effort` dial drives the builder's
  reasoning effort (extra → Extra High, clamped; Max/Ultracode manual-only); intake
  effort coaching is an anchored rubric; recalibration is a monthly by-hand pass
  with manual guardrail rollback.
- [0047](0047-reviewers-ship-clear-fixes.md) — Depth-aware review uses four grounded actions;
  reviewer-authored heads ping-pong across tools until unchanged, outages and same-tool taint
  are explicit, and each new conflict gets bounded resolution plus one narrow decision handoff.
- [0048](0048-mockup-scope-and-locked-contract.md) — Intake classifies a UI mockup as `local`
  (inherit the shipping surface, vary only the addition) or `surface` (the open whole-surface
  tournament); each variant carries a ≤150-word `LOCKED` contract copied verbatim into the ready
  brief on pick, which the reviewer judges the implementation screenshots against.
- [0049](0049-reproducible-repository-capabilities.md) — Repository capabilities are
  pinned in one versioned manifest; doctor distinguishes missing from drifted, and
  safe local enrollment reproduces the Claude/Codex skill and UI runtime environment.
- [0050](0050-bounded-worktree-retention.md) — Retention is bounded: a stranded session's
  work is archived to a recovery ref and its checkout reclaimed, held sources lose their
  protection, and the daemon refuses to dispatch into a repository past a registration
  ceiling. Amends 0028 and 0043.
- [0051](0051-deploying-the-running-daemon.md) — Merged is not running: the detached
  runtime checkout is set to `origin/main` rather than fast-forwarded, divergence is
  named in the log, and the restart that completes a deploy yields to live sessions.
  Amends 0050.

Issue-keyed records:

- [ADR 362](adr-362-research-exhaustion-parks-visibly.md) — An unattended research run that
  ends with no usable ruling parks its ticket visibly (one comment naming the check that
  refused it, one `wayfinder:parked` label) instead of silently releasing the claim, and
  research dispatch stops claiming tickets for runs that will never start.
- [ADR 374](adr-374-graphql-heartbeat-budget.md) — The Decision Map heartbeat budget rises to
  63 GraphQL requests and 250 reported points per 300-second heartbeat, sized so all nine
  enrolled repositories refresh every heartbeat; GitHub charges requested maxima, not returned
  data, so ADR 0036's 60-point ceiling could never hold at fleet size. Amends 0036 (budget
  numbers only).
- [ADR 380](adr-380-pre-publish-hardening.md) — A triage draft is attacked cold, in rounds,
  before it is ever published: a cold session judges the draft on five axes and answers with
  objections or none, objections go to a redraft that is attacked again, and only a survivor is
  published as the ready brief. Amended by ADR 418.
- [ADR 386](adr-386-dead-shell-environment-fault.md) — A session whose shell cannot start is an
  environment fault, not a spent budget: the ending is named for the machine, refunds the attempt
  it could never have used, and holds for a human with a diagnosis the maintainer can act on
  instead of a misleading "ran out of tries". Amends 0028; extends 0030's provider seam. Amended
  by ADR 454.
- [ADR 401](adr-401-commit-time-signoff.md) — Preparing a session checkout installs the
  sign-off, so a commit carries it the moment it is made and no stranded pull request needs a
  human amendment. Confined to the session's own checkout, only for the commit's own author,
  fail-open with one line per repository. Supersedes #357's no-hooks scope line.
- [ADR 418](adr-418-remedied-objections-publish.md) — Publication stops requiring an empty
  objection list: the attacker types its own answer (`remedied`, `fork`), a final round whose
  objections all carry their own fix publishes with the fixes riding in the brief, and only a
  genuine fork reaches the maintainer — who sees the fork first. Amends ADR 380; the round cap
  is unchanged.
- [ADR 425](adr-425-retire-clean-summary-on-park.md) — A park retires the current clean review
  summary: every clean summary stamps its exact reviewed head, publishing for a new head
  supersedes the old one in place, and any park supersedes every current clean summary before
  the park comment is written, so a pull request needing attention never shows a clean verdict
  as current. Applies 0047; follows ADR 417.
- [ADR 442](adr-442-dispatch-ceiling-below-the-measured-argv-cliff.md) — The worktree dispatch
  ceiling is recalibrated to sit below the argv cliff measured from the transcripts of three
  dead sessions: the sandbox adds three filesystem deny paths per linked worktree to every
  command, so enough worktrees push the spawn past the OS argument limit and every shell
  command fails. Recalibrates 0050's number; the gate's design is unchanged.
- [ADR 454](adr-454-dispatched-environment-holds-keep-attempt.md) — A dispatched environment
  hold keeps its attempt: the fault still holds immediately and never auto-retries, but a
  session the launch handshake proved started stays charged through the pending handoff and the
  parked record, so replay after a restart cannot produce an attempt-zero park. Amends ADR 386.
- [ADR 464](adr-464-slice-runs-in-session.md) — A coordinated build's slice runs as an
  in-session subagent of the coordinator, not a separately launched runner session: one
  logical Build stage, one reservation, one worktree, one tool lineage. Spend is attributed
  per tier from the per-model breakdown, each finished slice is committed before the next
  starts, and the coordinator carries its own unmeasured ceiling cell. The route is a
  committed switch, off by default and set per cell; the coordinator picks each slice's
  model from a configured allowed set rather than being pinned to the cheap tier.
  Admission (ADR 0029) is unchanged.
- [ADR 465](adr-465-work-order-is-the-non-self-scoping-brief.md) — A work order is the form
  a brief takes when the builder that writes the code will not self-scope: `guarded` because
  it must not guess, a coordinated build because its workers cannot afford to look. Intake
  writes the durable grounding and the separability judgment at scope time; the slicer cuts
  the file-level slices at pickup against current `main`, because a file list rots and
  grounding does not. A slice is sealed for deciding and open for reading, a gap stops the
  worker and not the build, and self-scope is sharpened to a property of the session — which
  is what makes the route legal at `reviewed`. Constrains 0005 and 0022; extends ADR 464.
- [ADR 466](adr-466-coordinated-build-routing-gate.md) — The coordinated-build route is gated
  on the dials *plus* a slice-bearing work order: `deep` with `high` or `extra` effort
  pre-filters the cells, and intake's separability judgment is the actual gate, so an
  indivisible deep issue is never routed. Two cells switch on and agentflow dogfoods alone;
  the allowed slice-model set is `{sonnet, opus}` with `sonnet` the default; a coordinator
  that declines to decompose collapses to one slice and continues in place rather than
  re-dispatching; revise stays monolithic. Fills in ADR 464's switch; changes no cell of
  0041 or 0046.
- [ADR 468](adr-468-slice-ledger-and-revert-condition.md) — The per-slice commits *are* the
  slice ledger, so a coordinated build's durable state is its branch and no parallel record is
  kept; a slice returns only its summary, commit, invariant-test result and bounded concerns,
  and the coordinator never writes code itself. The dated re-review reads ADR 0040's existing
  guardrails at ADR 0040's existing bar: revert on a degraded guardrail or a saving at or below
  zero, tune on a saving under 20% with clean guardrails, and record "extend, do not judge" when
  the cell is too thin. Revert is the ADR 464 switch. Constrains 0043's recovery envelope.
- [ADR 498](adr-498-capability-routed-session-led-dispatch.md) — Build and Revise launch one
  Claude/Fable session lead at low session reasoning that delegates every piece of the work to
  workers chosen from the provenance-stamped capability table, verifies each result against the
  repository gate, and escalates one rung after a second failure. Complexity stops sizing the
  builder and effort becomes the worker reasoning instruction. Supersedes 0014, 0018's
  builder-model selection, and 0029's build/revise model validation.
- [ADR 498](adr-498-headroom-is-a-launch-gate.md) — Quota headroom gates the launch and nothing
  after it: Build and Revise wait for a clear Claude pool because that is the only pool with a
  parent implementation, and no worker delegation inside a running session consults the
  balancer. Nested `codex exec` workers deliberately bypass the Codex permit ledger. Narrows
  0020 until [ADR 538](adr-538-automatic-codex-session-lead-fallback.md)'s second parent restores
  partial-availability throughput.
- [ADR 498](adr-498-tiered-parent-independent-review.md) — Review stays one single-model session
  but its tier follows the builder's complexity dial — cheap for standard, frontier for deep —
  and independence is measured against the session lead's tool rather than whichever worker
  wrote the diff. Weakens 0003; supersedes 0018's always-deep reviewer.
- [ADR 511](adr-511-slicing-survives-under-the-session-lead.md)
  — Slicing survives under ADR 498's session lead: a lead that decomposes work runs slices as
  in-session subagents on one pull request, with ADR 465's non-self-scoping work order and ADR
  468's commit-per-slice ledger retained. It supersedes ADR 466's separately-gated route and fixed
  cheap/frontier pair, retires the switch-based revert condition, and records #469's **TUNE**
  verdict; in-session slicing shipped in `9fa005d`
  ([#722](https://github.com/ConnorGriffin/agentflow/pull/722)).
- [ADR 540](adr-540-bounded-review-follow-up-proposals.md) — A review carries one bounded
  follow-up proposal instead of creating a GitHub issue; historical URLs remain references, and
  public parks use a 2,000-character operator envelope. Amends ADR 0047.
- [ADR 516](adr-516-codex-spend-estimated-and-worker-capture.md) — Codex tokens are priced from the
  routing table's rate card at report time and every non-provider-billed figure is flagged
  estimated; a lead-run build/revise attempt whose Codex worker spend has not been captured is
  marked *delegate spend not counted*; worker spend is observed from the workers' own usage records
  and rolls into the one stage record that spawned them, never self-reported; stored telemetry is
  read, never rewritten. Extends 0040; reads ADR 498's capability table.
