# ADR 0034 — A Conversation turn is a coordinated logical stage; the daemon adopts, the operator promotes

- Status: Superseded by [ADR 0035](0035-workflow-engine-read-only-operator-console.md)
- Date: 2026-07-16
- Amended: 2026-07-16 — the operator's conversation outranks background work at admission
- Amended: 2026-07-16 — an interactive conversation turn is real-time: it is exempt from headroom pacing, and only true-zero capacity may defer it ([#161](https://github.com/ConnorGriffin/agentflow/issues/161))
- Amended: 2026-07-17 — shelf-first: both interactive carve-outs above are retracted; interactive methodology work lives in the operator's chat tool and enters the workspace only as staged Proposals; the workspace is the artifact surface, not a chat client

## Context

The Project lifecycle now has settled vocabulary ([#125](https://github.com/ConnorGriffin/agentflow/issues/125),
`CONTEXT.md`) and a settled durable-state and control-plane boundary
([ADR 0033](0033-project-workspace-state-and-control-plane.md)). ADR 0033 deliberately left one
question open: how an Ask or Chart Conversation actually invokes the engineering-methodology
skills, resumes context, stages candidate artifacts, and returns a Proposal payload
([ADR 0033](0033-project-workspace-state-and-control-plane.md#L132-L141)).

Agentflow already runs autonomous work as one persistent orchestrator with ephemeral provider
hands ([ADR 0011](0011-persistent-orchestrator.md)). Every provider attempt for the six pipeline
stages crosses one deep session coordinator that owns admission, bounded stage-scoped
continuations, a crash-safe start handshake, and outcome-first classification
([ADR 0028](0028-stage-scoped-continuations.md), [ADR 0030](0030-session-coordinator-seam.md)).
Wayfinder planning stays upstream of intake; only an approved, published build issue enters the
pipeline ([ADR 0027](0027-wayfinder-planning-boundary.md)).

The risk this decision guards against is turning agentflow into one giant interactive agent
prompt: a single long-lived session that inlines grilling, domain-modeling, ui-mockups,
research, and codebase-design, holds the whole Conversation in memory, and writes results
wherever it likes. That would abandon ephemeral hands, lose crash recovery and per-turn
admission, and erase the approval boundary.

The recommended model and its source grounding are recorded in
[Methodology-session orchestration model](../research/methodology-session-orchestration-model.md).

## Decision

### A Conversation turn is one coordinated logical stage

An Ask or Chart Conversation runs as a **sequence of Conversation-turn sessions**, each one
bounded and non-interactive, submitted through the *existing* session coordinator
(`submit_stage` / `cycle` / `park_completed`) as a new logical stage. A turn's stable identity
is `(Project repository, Conversation ID, turn ordinal)`, so submission stays idempotent and
interrupted turns continue rather than duplicate. There is no second scheduler and no bespoke
Conversation agent loop: admission, the attempt budget, the crash-safe launcher, and
reconciliation are reused unchanged.

### Skills are invoked per turn, not inlined into one prompt

Each turn runs the ordinary structured provider command; the turn prompt names which
methodology skill to apply, and the skill triggers exactly as build sessions trigger their
skills today. AFK method steps (research, codebase-design analysis) are one submission and one
captured artifact. Interactive method steps (grilling, ui-mockups rounds, domain-modeling) are a
*sequence of turns* — interactivity lives at the Conversation level, one coordinated session per
operator turn, never one always-live process. A Chart that fans out into parallel AFK research
tickets maps onto separate submitted stages, each with its own admission and budget.
*(Amended 2026-07-17: interactive method steps no longer run as workspace turns at all — they run
in the operator's chat tool; only bounded, answer-and-walk-away turns and AFK steps run through
the coordinator. See the shelf-first amendment below.)*

### Shelf-first: interactive methodology work lives in the operator's chat tool

*(This section replaces, and retracts, the two 2026-07-16 amendments: "the operator's
conversation outranks background work" and "an interactive turn is real-time".)*

Those amendments were right that a grilling exchange is hard-earned, deliberate, steerable
human-and-AI work whose value latency destroys. They were wrong about where that work should
run. One day of dogfooding the real-time posture produced its refutation: an admission
carve-out so interactive turns bypass headroom pacing
([#161](https://github.com/ConnorGriffin/agentflow/issues/161)), permit-release latency a
waiting human immediately notices
([#158](https://github.com/ConnorGriffin/agentflow/issues/158),
[#164](https://github.com/ConnorGriffin/agentflow/pull/164)), and a workspace transcript that
needed its own rendering repairs — the leading edge of rebuilding an interactive chat client
feature-by-feature inside the workspace, while the operator's chat tool (Claude Code, Codex)
already *is* that client, polished. The deletion test applies: a workspace chat that must be
made as responsive as Claude Code merely relocates Claude Code.

So the interactive tier moves out of the workspace:

- **Interactive method steps — grilling above all, and any actively steered exchange — run in
  the operator's own chat tool** as ordinary skill sessions. They are the operator's sessions,
  outside coordinator admission; agentflow neither schedules, paces, nor renders them.
- **The workspace Conversation surface is bounded, not real-time.** Answer-and-walk-away Ask
  turns and AFK method steps (research fan-outs, codebase-design analysis, mockup rounds)
  remain coordinated **background** turns at ordinary pipeline priority — the recent-session
  cooldown, activity pacing, and the [ADR 0025](0025-activity-adaptive-spend-ceiling.md)
  ceiling all apply; no converse turn is exempt. The workspace makes no further chat-client
  investment: no streaming, no real-time admission, no rendering parity.
- **A chat-tool session reaches the workspace through the same one-way door.** An interactive
  session's resolved output enters as a **staged Proposal** — skills stage, the daemon adopts,
  the operator promotes — so the promotion boundary below is unchanged. The chat tool never
  writes durable truth directly; approval and Publication stay hash-bound, operator-only
  workspace acts.

Under this posture the workspace's identity is the **artifact surface**: staged Proposals
awaiting decision, hash-bound approval, provenanced Publication and build handoff, the pipeline
mirror, and landed Acceptance Evidence — the things a chat tool cannot durably be.

### Two retention layers, cleanly split

Durable Conversation memory — the stable Conversation ID, append-only immutable turns, optimistic
revisions, and immutable staged Proposal versions — lives in the daemon-owned per-Project
workspace ([ADR 0033](0033-project-workspace-state-and-control-plane.md#L43-L53)). The current
turn's transient state — provider attempt, worktree, budget, liveness — lives in the coordinator
record ([ADR 0028](0028-stage-scoped-continuations.md#L49-L69)). A turn completes only when its
real outcome exists (a durable turn appended, or attachments staged); a clean provider exit with
nothing recorded is incomplete and continues within its bounded budget. A Conversation is not a
GitHub issue, so a turn's "claim" is the workspace's optimistic revision on the Conversation
aggregate, not a GitHub label.

### Skills stage; the daemon adopts; the operator promotes

A methodology session writes only into its isolated working state (its worktree and
content-addressed local blobs). It never writes the workspace, GitHub, coordinator records,
projections, or default-branch truth directly
([ADR 0033](0033-project-workspace-state-and-control-plane.md#L136-L141)). The Conversation stage
adapter — a sibling of the six existing stage adapters — verifies the turn outcome, and its
`finalize_completed` is the single place the **daemon-side workspace adopts** the accepted turn
into immutable Proposal versions bound to an exact content hash. Editing stages a new version; it
never mutates an approved one.

### The promotion boundary is enforced structurally

Exploration can only ever reach **staged**. It becomes durable truth only when the operator
explicitly approves one exact Proposal hash and a Publication verifies the external effect
(`staged → approved → published`, or `staged → discarded`). Three gates make this
non-bypassable: the provider is sandboxed to its worktree with no durable write path; only the
daemon-side finalizer adopts a session output into a Proposal; and approval → Publication is a
separate operator step following ADR 0033's reconciled protocol. Publishing a build issue is its
build handoff, and the published issue carries the resolved decisions with no `wayfinder:*`
label, so intake grounds it and no conversation silently enters the pipeline
([ADR 0027](0027-wayfinder-planning-boundary.md#L16-L26)).

## Alternatives considered

- **One monolithic mega-prompt agent** running the whole Conversation with every skill inlined.
  Rejected: breaks ephemeral hands, cannot crash-recover mid-conversation, gets no per-turn
  admission or continuation, and collapses four separately-contracted skills into one
  un-reviewable prompt.
- **A new bespoke Conversation agent loop** beside the coordinator. Rejected by the deletion
  test: it re-implements admission, continuation, the start handshake, and reconciliation a
  second time, and two schedulers race on the same pools.
- **UI-/FastAPI-owned orchestration.** Rejected: the web layer reads projections and transports
  commands only; it never applies domain transitions or launches providers
  ([ADR 0033](0033-project-workspace-state-and-control-plane.md#L77-L100)).
- **Reusing the coordinator database as the Conversation workspace.** Already rejected by #126:
  long-lived working history has different identity, lifetime, failure, and retention rules than
  fail-closed provider admission.
- **Storing Conversations in GitHub or a working branch, or letting skills write Proposals /
  publish directly.** Rejected: it turns exploration into durable truth and erases the approval
  boundary that ADR 0027 and ADR 0033 exist to protect.

## Consequences

- Agentflow gains interactive planning without a second orchestrator: the only new pieces are a
  small Conversation stage adapter and the workspace's Conversation/Proposal adoption logic that
  ADR 0033 already assigned to the workspace module.
- Every Conversation turn, like every pipeline stage, enters through the one scheduler-owned
  admission and continuation path, and is crash-recoverable and independently budgeted.
- A completed turn has no automated successor stage — the operator's next message submits the
  next turn — so a completed turn is adopted and retired, and `park_completed` handles a turn
  that needs the operator.
- Admission-matrix calibration for `converse` turns — all background, per the shelf-first
  amendment — the turn-prompt/context-rebuild format, and the workspace Conversation/Proposal
  schema remain implementation choices for the prototype.
- [#128](https://github.com/ConnorGriffin/agentflow/issues/128) (workspace prototype) may assume
  the coordinated-turn model, workspace-owned candidate artifacts and their staged/approved/
  published state, and UI-drives-only-via-commands. [#129](https://github.com/ConnorGriffin/agentflow/issues/129)
  (first validation slice) may assume grilling as a sequence of coordinated turns to a staged
  Proposal without Publication. *(Amended 2026-07-17: grilling runs in the operator's chat
  tool; its output enters the workspace as a staged Proposal. The slice-1 tracers are otherwise
  unaffected — staging, approval, Publication, and evidence are exactly the artifact surface
  this amendment keeps.)*
