# Methodology-session orchestration model

_Research date: 2026-07-16. Scope: issue [#127](https://github.com/ConnorGriffin/agentflow/issues/127), child of map [#123](https://github.com/ConnorGriffin/agentflow/issues/123). This is a recommended orchestration model, not an implementation design._

## Answer

**Recommendation (inference):** run an Ask or Chart Conversation as a **sequence of
coordinated Conversation-turn sessions** behind the *existing* session coordinator seam —
not a new bespoke agent loop and not one long-lived mega-prompt. Each turn is one bounded,
non-interactive provider session that loads the appropriate engineering-methodology skill
(grilling, domain-modeling, ui-mockups, research, codebase-design, …), reads the durable
Conversation context, works only in an isolated worktree, and produces one durable turn
outcome: either a reply/question back to the operator, or one or more **staged candidate
attachments**. A small new **Conversation stage adapter** — a sibling of the six existing
stage adapters — verifies that outcome; the **daemon-side workspace module** (ADR 0033) is
the only writer that adopts an accepted turn into an immutable Proposal version. Nothing the
skill or agent produces becomes durable truth until the operator explicitly approves one
exact Proposal hash and a Publication verifies the external effect.

This keeps agentflow's whole architecture intact — "persistent orchestrator, ephemeral
hands" ([ADR 0011](../adr/0011-persistent-orchestrator.md#L17-L34)), one session coordinator
that owns admission/continuation/crash-safety ([ADR 0030](../adr/0030-session-coordinator-seam.md#L33-L56)),
and the daemon-owned Project workspace that owns Conversation retention and Proposal adoption
([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L33-L74)). The
methodology skills are the deep leverage; the coordinator supplies the hard machinery once;
the workspace supplies durable memory and the promotion boundary. Agentflow becomes an
agentic development environment by composing many small seam-bounded skill invocations, not
by growing one giant agent prompt.

## The question, restated in the current architecture

An Ask/Chart Conversation is Wayfinder's interactive planning capability: it decides what
should become durable work, and it does not build or merge that work
([`CONTEXT.md`](../../CONTEXT.md#L25-L42)). It runs *upstream* of intake — planning artifacts
carry `wayfinder:*` labels and are excluded from the build pipeline until an approved
Publication files a standalone build issue
([ADR 0027](../adr/0027-wayfinder-planning-boundary.md#L16-L44)). So the four sub-questions
are: how a Conversation *invokes* the methodology skills against a repo; how it *retains and
resumes* context across turns and interruptions; how it *stages* candidate artifacts in
isolated working state; and how the *promotion/publication boundary* is enforced so
exploration never silently becomes truth. ADR 0033 deliberately left exactly this decision
open ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L132-L141)).

## Verified current seams (source facts, not recommendations)

| Seam | What it already does | Why it is the reuse point |
|---|---|---|
| **Session coordinator** | `submit_stage(Submission) -> identity` (idempotent), `cycle(pool) -> [StageOutcome]`, `park_completed(identity)` are the entire public surface; everything hard — the four-state record, waiting queue, attempt budget, admission matrix, permit ledger, crash-safe start handshake, outcome-first classification, reconciliation — lives behind it ([`coordinator.py`](../../agentflow/coordinator/coordinator.py#L84-L102), [`coordinator.py`](../../agentflow/coordinator/coordinator.py#L124-L218)). | A Conversation turn is one more logical stage; it needs precisely this machinery and should not reinvent it ([ADR 0030](../adr/0030-session-coordinator-seam.md#L33-L82)). |
| **Stage adapter (three-method surface)** | `prepare(record)` owns source/recovery and proves the claim before admission; `observe`/`capture`/`verify` reconstruct provider output and check the stage's *own* required outcome (a clean exit is never sufficient); `finalize_completed`/`finalize_hold` apply the durable boundary idempotently ([`build_stage.py`](../../agentflow/coordinator/build_stage.py#L22-L64), [`intake_stage.py`](../../agentflow/coordinator/intake_stage.py#L31-L77)). | A Conversation turn plugs in as a new adapter with the same shape; no coordinator change needed except registering the stage ([ADR 0030](../adr/0030-session-coordinator-seam.md#L148-L181)). |
| **Runner / provider launch** | Builds the structured `claude -p` / `codex exec` argv, pins the session to one worktree/branch, and sandboxes it there; provider commands may only be executed by the coordinator launcher ([`runner.py`](../../agentflow/runner.py#L76-L98), [`runner.py`](../../agentflow/runner.py#L252-L283)). The crash-safe child records `started` before spawning the provider ([`launcher.py`](../../agentflow/coordinator/launcher.py#L1-L16), [`launcher.py`](../../agentflow/coordinator/launcher.py#L78-L106)). | The isolation and no-direct-write property the promotion boundary depends on already exists; a skill session cannot escape its worktree. |
| **Dispatch** | Discovers each stage's input and submits its durable facts; it never starts a provider, counts a permit, or owns reconciliation ([`dispatch.py`](../../agentflow/dispatch.py#L1-L6), [`dispatch.py`](../../agentflow/dispatch.py#L42-L121)). | A Conversation turn is discovered and submitted the same way — one more `_submit_*` sibling. |
| **Daemon cycle** | Reconciles coordinator records, optionally submits cold work, and treats operator pause as reconcile-only ([`dispatch.py`](../../agentflow/dispatch.py#L124-L141), [`daemon.py`](../../agentflow/daemon.py#L115-L119)). | The daemon is already the only orchestrator and the only projection writer; it becomes the only Proposal adopter too. |
| **Daemon-owned Project workspace (ADR 0033)** | A per-Project SQLite store, separate from `coordinator/records.db`, owns Project identity, immutable Conversation turns, immutable Proposal versions/approvals, idempotent commands, and Publication intents/receipts; one daemon-side module is its only logical writer; skills/agents may produce staged attachments but only daemon-side orchestration adopts accepted outputs ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L43-L74), [ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L136-L141)). | This is the durable retention layer and the adoption/promotion authority the model hangs on. |
| **Continuation model (ADR 0028)** | One durable four-state record and bounded budget *per logical stage*; a completed stage hands the next stage a fresh record; a stage completes only when its required outcome exists; waiting is scheduler-owned; the durable record owns transient work while GitHub owns durable outcomes ([ADR 0028](../adr/0028-stage-scoped-continuations.md#L22-L128)). | A turn is a stage: interrupted turns continue on a fresh provider session against the same worktree with a bounded budget, and only a real turn outcome completes them. |

## Recommended model

### 1. Invoke — a Conversation turn is a coordinated logical stage

Add one logical stage (working name **`converse`**, parameterized by Ask/Chart intent and by
which methodology skill it runs) to the coordinator's enabled set
([`tracer.py`](../../agentflow/coordinator/tracer.py#L29-L38)). An operator turn is submitted
through the existing `submit_stage` surface, reusing the `Submission` facts already defined:
`repo` = the Project's enrolled repository, `subject` = the stable Conversation ID, `target`
= the immutable turn ordinal (so re-submitting the same turn is idempotent), `source` = the
Conversation's isolated worktree, `input_ptr` = a durable pointer the adapter rebuilds the
turn prompt from, `complexity`/`effort`/`pool` = the admission dials for that method
([`coordinator.py`](../../agentflow/coordinator/coordinator.py#L48-L71),
[`record.py`](../../agentflow/coordinator/record.py#L31-L51)).

The launcher runs the ordinary structured `claude -p` / `codex exec` command; the turn prompt
names the method to apply, and the skill triggers on it exactly as today's build session
triggers `/implement` — the methodology skills are consumed unchanged through their own
invocation contracts. Two invocation modes fall out of the skills' own designs and both fit
this one stage:

- **AFK method steps** (research is background-by-design and, as a spawned worker, performs
  the research directly rather than nesting another worker (`research` skill contract);
  codebase-design analysis) run exactly like today's coordinated non-interactive stages: one
  submission, one bounded session, one captured artifact.
- **HITL method steps** (grilling interviews one question at a time and waits for feedback
  (`grilling` skill contract); ui-mockups iterates in rounds to a *locked* spec (`ui-mockups`
  skill contract); domain-modeling challenges terms interactively) become a *sequence* of
  turns. Each operator message is one coordinated turn session that consumes the accumulated
  Conversation context plus the new input and returns the next question or the next staged
  artifact. The interactivity lives at the Conversation level (many turns), not inside one
  always-live provider process — which is exactly "persistent orchestrator, ephemeral hands"
  ([ADR 0011](../adr/0011-persistent-orchestrator.md#L17-L34)) applied to planning.

A Chart that fans out into parallel AFK research decision tickets (the sole exception
Wayfinder allows to one-decision-per-session — `wayfinder` skill contract) maps onto separate
submitted stages, each with its own admission and budget, not onto one session.

### 2. Retain / resume — two clean retention layers

Retention splits along the boundary ADR 0033 already drew:

- **Durable Conversation memory lives in the workspace.** Stable Conversation ID, append-only
  immutable turns, optimistic revisions, and immutable staged Proposal versions are owned by
  the daemon-side workspace module
  ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L43-L53)). This is the
  resumable history; a Conversation "closes only when that outcome is resolved or abandoned,
  and reopens only for the same outcome" ([`CONTEXT.md`](../../CONTEXT.md#L14-L17)).
- **Transient turn state lives in the coordinator record.** The current turn's provider
  attempt, worktree pointer, attempt budget, and process liveness are the coordinator's
  four-state record, keyed to the turn ([`record.py`](../../agentflow/coordinator/record.py#L27-L76)).

Resume across an interruption is the ADR 0028 path unchanged: an interrupted turn returns to
`waiting`, keeps its worktree, and continues on a fresh provider session that rebuilds its
prompt from `input_ptr` and the workspace Conversation context, within a bounded budget
([ADR 0028](../adr/0028-stage-scoped-continuations.md#L22-L47),
[`coordinator.py`](../../agentflow/coordinator/coordinator.py#L446-L497)). A turn completes
only when its real outcome (a durable turn appended, or attachments staged) exists — a clean
provider exit with nothing recorded is incomplete, exactly as for the six pipeline stages
([ADR 0028](../adr/0028-stage-scoped-continuations.md#L71-L97)).

One difference from a build stage is deliberate: a build stage owns a **GitHub claim label**,
but a Conversation is *not* a GitHub issue (planning stays upstream of intake —
[ADR 0027](../adr/0027-wayfinder-planning-boundary.md#L16-L26)). The "claim" for a turn is the
workspace's optimistic revision on the Conversation aggregate, so concurrent turns on one
Conversation are serialized by the workspace, not by a GitHub label. The completed-turn record
has no automated successor stage — the operator's next message submits the next turn — so a
completed turn is adopted and retired, and `park_completed` is the natural handoff when a turn
needs the operator and there is nothing more for the daemon to do
([`coordinator.py`](../../agentflow/coordinator/coordinator.py#L165-L188)).

### 3. Stage candidate artifacts — isolated working state, daemon adoption

A methodology session writes only into its **isolated working state**: its worktree plus, for
larger outputs, the content-addressed local blobs ADR 0033 places beside the workspace
database ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L56-L60)). The
skill/agent never writes the workspace, GitHub, coordinator records, projections, or
default-branch truth directly
([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L136-L141)).

The Conversation stage adapter's `capture`/`verify` extracts the candidate artifact from the
session's durable output — the same shape Intake already uses, where `capture` parses the
route from the final message and `verify` confirms a durable outcome exists
([`intake_stage.py`](../../agentflow/coordinator/intake_stage.py#L54-L60)). The adapter's
`finalize_completed` is where the **daemon-side workspace adopts** the accepted turn: it
appends the immutable turn and stages any candidate artifacts as **immutable Proposal
versions bound to an exact content hash**
([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L102-L107)). Editing a
staged artifact creates a new staged version; it never mutates an approved one. Multiple
candidate artifacts (for example ui-mockups variants) attach to the Conversation as
exploration, and the operator's selection promotes one into a Visual Specification Proposal —
the same "explore variants, lock one" pattern the Mockup stage already proves by committing
variant artifacts plus one round comment
([ADR 0028](../adr/0028-stage-scoped-continuations.md#L82-L84),
[`CONTEXT.md`](../../CONTEXT.md#L82-L89)).

### 4. Enforce the promotion / publication boundary — three gates the agent cannot bypass

The explicit boundary is the seam between *staged candidate artifacts in the workspace* and an
*approved Publication that creates durable truth*. It is enforced structurally at three points,
none of which a skill or agent output can cross on its own:

1. **A skill/agent cannot write durable truth — by construction.** The provider runs inside
   its assigned worktree with no workspace/GitHub write path, and provider commands are
   executable only by the coordinator launcher
   ([`runner.py`](../../agentflow/runner.py#L76-L98),
   [ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L136-L141)). Its output
   is staged attachments and nothing more.
2. **Adoption is daemon-only.** Only the daemon-side workspace finalizer turns an accepted
   session output into a Proposal version — the `finalize_completed` seam is the single place a
   completed session reaches a durable boundary
   ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L55-L56),
   [`build_stage.py`](../../agentflow/coordinator/build_stage.py#L46-L64)). The web/UI layer is
   not an adopter: GET reads projections and POST is command transport only
   ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L77-L100)).
3. **Approval → Publication is a separate, explicit operator step.** A staged Proposal version
   stays staged until the operator approves one exact content hash; Publication then follows
   the reconciled record-intent → external-effect → verify → receipt protocol, and only a
   verified effect becomes `published`
   ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L102-L129),
   [`CONTEXT.md`](../../CONTEXT.md#L19-L23)). Publishing a build issue *is* its build handoff;
   the published issue carries the resolved decisions and no `wayfinder:*` label, so normal
   intake grounds it and no exploration ever silently enters the pipeline
   ([ADR 0027](../adr/0027-wayfinder-planning-boundary.md#L18-L26)).

So a Conversation turn can only ever reach **staged**. `staged → approved → published` (or
`staged → discarded`) is operator-authorized, and a failed Publication stays approved for
retry rather than duplicating an effect
([`CONTEXT.md`](../../CONTEXT.md#L19-L23),
[ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L102-L123)).

## Mapping onto the current code (no new orchestrator)

| New/changed piece | Shape | Precedent it copies |
|---|---|---|
| `converse` logical stage | Registered in the coordinator's enabled stages; one stable identity per `(Project repo, Conversation ID, turn ordinal)` | The six existing stages ([`tracer.py`](../../agentflow/coordinator/tracer.py#L29-L38), [`coordinator.py`](../../agentflow/coordinator/coordinator.py#L557-L564)) |
| Conversation stage adapter | `prepare` reuses the Conversation worktree; `observe`/`capture` reconstruct provider output; `verify` checks a durable turn/attachment exists; `finalize_completed` adopts the turn into the workspace + stages Proposal versions; `finalize_hold` parks the turn for the operator | `BuildStageAdapter`, `IntakeStageAdapter` ([`build_stage.py`](../../agentflow/coordinator/build_stage.py#L22-L64), [`intake_stage.py`](../../agentflow/coordinator/intake_stage.py#L31-L77)) |
| Dispatch submission | A `_submit_conversation_turn` sibling that reads a pending operator turn (from a workspace command, not a GitHub scan) and calls `submit_stage` | `_submit_coordinated_build` / `_submit_coordinated_intake` ([`dispatch.py`](../../agentflow/dispatch.py#L42-L113)) |
| Turn intake | The operator's "advance Conversation" arrives as an authenticated POST command; the workspace records the turn immutably, then enqueues the coordinator submission | ADR 0033 command channel ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L90-L100)) |
| Runner / launcher / admission / reconciliation | Unchanged; reused wholesale | ADR 0030 seam ([ADR 0030](../adr/0030-session-coordinator-seam.md#L84-L127)) |

The only genuinely new modules are the small Conversation stage adapter and the workspace's
Conversation/Proposal adoption logic (which ADR 0033 already assigned to the workspace module).
Admission, continuation, crash-safety, and provider launch are reused, not rebuilt.

## Alternatives considered and rejected

- **One monolithic mega-prompt agent** that runs a whole Ask/Chart Conversation as a single
  long-lived interactive session with every methodology skill inlined. Rejected: it breaks
  "ephemeral hands" ([ADR 0011](../adr/0011-persistent-orchestrator.md#L17-L34)), cannot
  crash-recover mid-conversation, gets no per-turn admission/continuation/budget, and collapses
  four separately-contracted skills into one un-reviewable prompt — the exact "one giant agent
  prompt" the ticket rules out.
- **A new bespoke Conversation agent loop** parallel to the session coordinator. Rejected by the
  deletion test ([ADR 0030](../adr/0030-session-coordinator-seam.md#L183-L202)): it would
  re-implement admission, continuation, the crash-safe start handshake, and reconciliation a
  second time, and two schedulers would race on the same provider pools.
- **UI-/FastAPI-owned orchestration** — the browser or web server holds Conversation state and
  drives the sessions. Rejected: FastAPI GET is file-only and POST is command transport that
  never applies domain transitions or launches providers; browser count must not multiply
  provider work ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L77-L100),
  [ADR 0026](../adr/0026-daemon-owned-snapshot.md)).
- **Reusing the coordinator database as the Conversation workspace.** Already rejected by #126:
  long-lived human working history has different identity, lifetime, failure, and retention
  rules than fail-closed provider admission, and the coordinator store is an intentionally
  private seam ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L62-L65),
  [ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L157-L159)).
- **Storing Conversations/turns in GitHub or a working branch.** Rejected: it turns exploration
  into durable truth and erases the approval boundary
  ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L144-L153)), and planning
  is required to stay upstream of intake
  ([ADR 0027](../adr/0027-wayfinder-planning-boundary.md#L16-L26)).
- **Letting skills write Proposals or perform Publication directly.** Rejected: only daemon-side
  orchestration adopts accepted outputs, and Publication is separately approved and reconciled
  ([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L136-L141),
  [ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L102-L123)).

## What this grants downstream

- **[#128 prototype the existing-repo project workspace](https://github.com/ConnorGriffin/agentflow/issues/128)**
  (blocked only by tickets 1–2, so already unblocked; it explores the workspace UI/interaction
  with `/ui-mockups`) may now assume that a turn is a coordinated `converse` stage producing a
  durable turn plus staged attachments, that candidate artifacts and their `staged → approved →
  published` state are the workspace's job, and that the UI drives Conversations only through
  authenticated POST commands — it never orchestrates provider sessions.
- **[#129 lock the first validation slice](https://github.com/ConnorGriffin/agentflow/issues/129)**
  (blocked by tickets 3–4; this ticket clears blocker 3, so #129 remains blocked only on #128)
  may assume grilling runs as a sequence of coordinated Conversation turns, that the first slice
  can drive a single methodology skill end-to-end to a *staged* Proposal without Publication, and
  that failure/re-entry is the ADR 0028 continuation path.

## Open, left to implementation

Admission-matrix calibration for `converse` turns (method-specific model/complexity/demand
rows), the exact turn-prompt/context-rebuild format, and the workspace's Conversation/Proposal
schema are implementation choices, consistent with ADR 0030 leaving persistence and handshake
mechanisms private ([ADR 0030](../adr/0030-session-coordinator-seam.md#L119-L127)) and ADR 0033
leaving method execution to this decision and its prototype
([ADR 0033](../adr/0033-project-workspace-state-and-control-plane.md#L180-L182)).
