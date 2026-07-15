# ADR 0028 — Stage-scoped continuations retain ownership across fresh sessions

- Status: Accepted
- Date: 2026-07-15

## Context

Agentflow currently collapses a provider launch to success or failure and treats a
session with no live process as abandoned. A timeout, rate limit, CLI crash, or clean
exit without the stage's required outcome therefore either loses the captured failure
facts or becomes a human hold immediately. For worktree-owning stages, that also leaves
local progress with no automatic path forward.

The pipeline needs same-machine continuity for every agent session without turning a
temporary provider condition into GitHub noise, losing dispatch ownership, weakening
cross-tool review, or retrying forever. [Provider research](../research/provider-interruption-signals.md)
also established that process exit alone is not diagnostic: classification must combine
structured provider events, process exit/signal, supervisor timeout, and the required
stage outcome.

## Decision

### One continuation record and budget per logical stage

Agentflow persists a continuation record for one logical stage outcome, separate from
the ephemeral live-session file. Intake, build, review, revise, mockup, and respond each
receive their own record. A completed stage hands the claim to the next automated stage
with a fresh record and budget; it does not make the whole issue-to-PR chain one large
retry unit.

Every stage gets one initial provider attempt and at most **two automatic continuation
attempts**. An attempt is consumed atomically immediately before the provider process is
spawned. Worktree preparation, provisioning, admission, and other failures before spawn
do not consume it. Once spawned, the attempt counts even if the daemon crashes or the
provider process never returns a usable result.

The budget never resets while the same logical stage remains unresolved. Restarts,
elapsed time, refreshed capacity, and ordinary source edits do not create free attempts.
A genuinely new stage target does:

- a review is bound to one PR head SHA, so a new SHA starts a new review stage;
- a response is bound to one unanswered maintainer comment, so a later question starts
  another response stage;
- human re-entry after a durable hold starts a new stage; and
- every normal stage transition (build to review, review to revise, revise to review)
  starts the next stage with its own budget.

### Four persisted states

| State | Meaning |
| --- | --- |
| `waiting` | The stage owns its claim but no provider process. `eligible_at` controls when it may be admitted. A cold stage starts here with zero attempts used. |
| `running` | The attempt number and supervisor deadline were persisted before spawn. |
| `completed` | The required stage outcome and the facts needed by the next pipeline step are durable. |
| `held` | A bail, permanent condition, or exhausted continuation budget has produced the stage-native human handoff. |

`completed` and `held` are reconciliation states, not long-lived history. Agentflow
retires the record after it has either transferred ownership to the next `waiting` stage
or confirmed the durable external boundary and released the claim. Exhaustion is a
reason for `held`, not a fifth state. Eligibility is a property of `waiting`, not a
separate state.

The active record retains the facts required to make that state meaningful: repository
and subject, stage and immutable target where applicable, required outcome, attempt
count, eligibility time, provider/tool assignment, tool lineage, worktree or durable
source pointer, supervisor deadline and process identity, claim, and the provider,
process, timeout, and outcome observations from the latest attempt. The later runner
seam decision chooses the storage interface and adapter shape; this decision defines the
facts and behavior they must preserve.

### A stage completes only when its outcome exists

A clean provider exit is neither necessary nor sufficient. Agentflow verifies the
stage's required outcome independently:

| Stage | Required outcome |
| --- | --- |
| Intake | A parsed route is persisted and can be applied idempotently to the issue. |
| Build | The expected PR exists for the owned branch. |
| Review | A parsed verdict is persisted for the exact reviewed head SHA. |
| Revise | The same PR branch contains the verified pushed revision, or the required non-code evidence/comment is durably attached. |
| Mockup | The variant artifacts are committed and the single variant-round issue comment exists. |
| Respond | The marked reply exists and any requested branch change is pushed. |

Local worktree changes are durable continuation state, but they are not a completed
stage. If the provider exits cleanly with only local changes, the stage follows the same
bounded continuation path as any other incomplete attempt.

Classification uses this precedence:

1. If the required outcome exists, mark the stage `completed`, regardless of process
   exit status.
2. If the runner emitted an explicit bail, or typed facts show authentication, billing,
   permission, configuration, or another permanent condition, create the stage-native
   `held` handoff immediately.
3. If facts show a recoverable interruption, or the attempt is incomplete or unknown,
   return to `waiting` when budget remains; otherwise create the `held` handoff.

Exit code alone never supplies the cause. Unknown Codex failures remain bounded
incomplete interruptions unless a typed companion query establishes a capacity reset or
a permanent account/configuration problem.

### Waiting is scheduler-owned, never an inline retry

Every recoverable or incomplete ending first persists `waiting`, releases its capacity
permit, retains its dispatch claim, and returns control to the scheduler. Agentflow never
recursively launches a continuation from the failed call stack.

- A typed capacity interruption with a future reset uses that reset as `eligible_at`.
- A timeout, signal, crash, network/server failure, unknown incomplete ending, or clean
  exit without outcome becomes eligible on the next daemon cycle.
- Eligible continuations are considered before cold starts on their eligible pool, but
  they still pass normal pool admission, machine, and stage caps.
- Eligible continuations on one pool are ordered by `eligible_at`, then record creation
  time and stable identity; no stage-specific priority is added here.
- Waiting does not consume another attempt or a capacity permit. A provider start does.

Exact admission demands and pool permit budgets belong to the separate admission-matrix
decision. A provider process that is still alive after daemon restart remains `running`
and continues to count its admission demand.

### The durable record owns transient work; GitHub owns durable outcomes

The continuation record is authoritative for same-machine transient stage ownership.
GitHub remains authoritative for durable pipeline outcomes. The live-session file and
PID marker answer only whether a process is executing now. A GitHub claim is the visible
dedup guard mirrored from the active record; it is not evidence that a provider process
is alive.

The originating issue's existing claims cover the automated work:

- intake uses `agentflow:triaging`;
- mockup uses `agentflow:drawing-mockup`; and
- build, review, revise, later response, and autonomous re-review use
  `agentflow:building`, whose meaning is broadened to “agentflow currently owns this
  change.”

When another automated stage follows, agentflow creates the next `waiting` record and
transfers the claim before retiring the completed record. Build to review and the review
to revise loop therefore have no ownership gap. The claim is released only at a durable
external boundary: a routed or held issue, a variant round awaiting a human pick, a
replied or parked PR, or a merged PR.

On daemon startup, reconciliation is outcome-first:

1. A required outcome advances or completes the record idempotently.
2. A `running` record whose process is alive is observed without duplication until its
   persisted supervisor deadline. At that deadline the process is killed and classified.
3. A `running` record whose process is dead is classified from its persisted observations
   and required outcome, with the already-consumed attempt preserved.
4. A `waiting` record keeps or restores its GitHub claim and returns to the priority queue
   when eligible.
5. Only a claim with no required outcome, no live process, and no continuation record is
   stale and reclaimable.

An unreadable continuation store fails closed: do not clear claims or start possible
duplicates. Log the store failure for human repair. This amends ADR 0021's stale-claim
assumption: “no live process” is no longer enough to prove abandonment.

### Code-writing continuations preserve tool lineage

Build, revise, mockup, and respond remain pinned to their original Claude or Codex tool
lineage across automatic continuations. A capacity pause waits for that pool; a permanent
tool failure becomes a human hold. The daemon never silently rescues code-writing work
with the other tool, because doing so would consume the independent reviewer.

A human may explicitly authorize a cross-tool rescue. That ends the original stage and
starts a new human-directed stage with a fresh budget, records mixed lineage, and makes
the resulting PR human-merge-only. Read-only stages may move to another available tool
when their safety constraints allow it; a same-tool review may finish but cannot
auto-merge.

### Exhaustion produces one stage-native handoff

After the third provider attempt (initial plus two continuations) ends without the
required outcome, agentflow creates exactly one durable handoff and one notification:

- intake and build move the issue to `agentflow:needs-grilling`;
- mockup remains `agentflow:needs-mockup`; and
- review, revise, and respond park the existing PR for human action.

Code-writing worktrees remain exactly as they are. Existing commits may use the normal
draft-PR preservation path, but uncommitted work is neither discarded nor force-committed.
Read-only worktrees may be recreated. A later human re-entry starts a fresh stage and
adopts the retained code-writing worktree and original lineage unless the human explicitly
chooses discard or cross-tool rescue; missing or unsafe local state holds again rather
than silently starting over.

Transient waiting is local and quiet: no GitHub comment and no notification. Bail and
permanent conditions may hold immediately because they are not transient.

### Logs are the transient operational interface

The daemon emits stable, single-line events with repository, subject, stage, attempt,
cause/outcome, timing, and claim disposition. The required shapes are:

```text
{repo}: {subject}: {stage}: attempt 1/3 → {tool}
{repo}: {subject}: {stage}: attempt 1/3 interrupted ({cause}) — continuation 1/2 eligible at {time}; claim retained
{repo}: {subject}: {stage}: continuation 1/2 (attempt 2/3) → {tool}
{repo}: {subject}: {stage}: recovered running attempt 2/3 pid {pid} — observing until {deadline}; claim retained
{repo}: {subject}: {stage}: attempt 2/3 completed — {outcome}; claim transferred to {next_stage}
{repo}: {subject}: {stage}: attempt 3/3 interrupted ({cause}) — continuation budget exhausted; held for human; claim released
```

When eligibility is “next cycle,” the wait log says that instead of inventing a
timestamp. Permanent holds and terminal outcomes use the same final shape with their
actual reason and boundary. Provider messages may be summarized into safe typed causes;
arbitrary model text and secrets never enter these lines.

## Alternatives considered

- **One budget for the whole issue-to-PR chain.** Rejected because an interrupted build
  would consume the independent reviewer's allowance, and a successful stage is the
  natural reset boundary.
- **Retry inline inside the runner.** Rejected because it bypasses scheduler priority and
  admission, creates hot loops, and cannot recover cleanly across daemon death.
- **Use the live-session file as continuation state.** Rejected because liveness entries
  intentionally disappear when sessions finish and do not contain stage ownership,
  attempts, lineage, or outcomes.
- **Release the claim while waiting.** Rejected because the next cycle or a manual path
  could dispatch duplicate work into the same issue or branch.
- **Automatically switch tools for code-writing work.** Rejected because it destroys the
  cross-tool independence gate without an explicit human trade-off.
- **Require remote recovery branches for every pause.** Rejected because same-machine
  durability is sufficient; forced WIP commits would alter and publish unfinished work.

## Consequences

- Temporary interruptions stop being issue/PR events; they become durable local scheduler
  state with exact logs.
- A claim can legitimately outlive a provider process. Claim reclamation and manual entry
  paths must consult continuation ownership before acting.
- Provider adapters must preserve structured events, partial output, process exit/signal,
  supervisor timeout, and required-outcome observations instead of returning only
  `(ok, final_message)`.
- The orchestration path must resume from the recorded stage rather than replaying the
  whole build chain. The deep runner/orchestrator seam is intentionally left to its own
  Wayfinder decision.
- Paused work releases capacity and later competes through normal admission with priority
  over cold starts. The static weights and permit budget remain a separate decision.
- Remote machine/worktree-loss recovery, dashboard paused-state UI, and dynamic admission
  weights remain out of scope.
