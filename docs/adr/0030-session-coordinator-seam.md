# ADR 0030 — One session coordinator owns continuation and admission

- Status: Accepted
- Date: 2026-07-15

## Context

Agent sessions currently cross two shallow, disconnected interfaces. Stage orchestration
chooses a runner, takes an in-memory governor slot, prepares a worktree, calls
`runner.launch`, and then interprets a Boolean plus final text. Six paths repeat that
shape: intake, build, review, revise, mockup, and respond. The governor holds one slot for
an orchestration chain, so a build's later review and revise do not receive independent
provider admission decisions. The live-session file and worktree PID marker disappear or
are reaped after a process ends and cannot own continuation attempts or waiting claims.

[ADR 0028](0028-stage-scoped-continuations.md) instead requires one durable four-state
record per logical stage, outcome-first classification, bounded fresh-session
continuations, and scheduler-owned waits. [ADR 0029](0029-static-per-pool-admission.md)
requires every provider attempt to reserve its full demand atomically, after preparation
but before attempt consumption and spawn, with eligible continuations ahead of cold work.
Provider research also requires structured events, process exit or signal, supervisor
timeout, and the stage outcome to remain independent observations.

Putting continuation policy in every stage would duplicate the difficult part six times.
Putting only permits in a second governor would leave queue priority, attempt accounting,
process recovery, and permit lifetime split across modules. Moving stage completion into a
generic runner would instead flatten the behavior that is intentionally different: a
build proves a PR, a review proves a verdict for one head SHA, and a mockup proves both
committed artifacts and one issue comment.

## Decision

### The seam sits at one logical stage session

Introduce one deep **session coordinator** between stage orchestration and the two provider
adapters. Stage orchestration submits the facts for one logical stage and later consumes
its completed stage outcome or human hold. The coordinator hides the continuation record,
waiting queue, attempt budget, provider lifecycle, failure classification, admission
matrix, atomic permit accounting, reconciliation, and exact transient logs behind that
interface.

Conceptually, its small external interface is:

```text
submit(stage, subject, target, source, claim, sizing, tool rules) -> record identity
cycle() -> completed stage outcomes or human holds
```

`submit` is idempotent for the stage identity. `cycle` first reconciles existing records,
then considers eligible continuations in ADR 0028 order before newly submitted cold stages,
admits and supervises provider attempts, and returns only terminal facts that stage
orchestration can consume. The first eligible continuation that cannot pass normal
admission stops later work from bypassing it on that pool, as ADR 0029 requires. The
by-hand path submits through the same interface; operator mode may retain its existing
activity-gate treatment but cannot bypass permits, attempt accounting, lineage, or
continuation priority.

The interface accepts the logical stage enum (`intake`, `build`, `review`, `revise`,
`mockup`, or `respond`), never a live-board lane or GitHub label. One normalization
function at the seam handles legacy orchestration aliases. Callers that know they are
revising or drawing a mockup state that logical stage directly, so the ambiguous display
lane `building` can never turn Revise into Build and `triaging` can never turn Mockup into
Intake.

### The coordinator owns the common policy once

The coordinator alone applies ADR 0028's classification precedence:

1. a verified stage outcome completes the stage;
2. an explicit bail or typed permanent provider condition holds it; and
3. a recoverable, incomplete, or unknown ending waits when budget remains and holds when
   the budget is exhausted.

It also owns attempt consumption, eligibility, continuation ordering, tool-lineage rules,
and the stable logs. Provider adapters do not decide whether to continue or hold. Stage
adapters do not count attempts or turn an unknown exit into a policy decision.

The existing dispatch governor is deepened into this coordinator rather than retained as a
parallel admission layer. Machine ceiling, per-stage caps, operator pacing, headroom gates,
and the pool permit budget are evaluated together for each provider attempt. A build,
review, and revise therefore each acquire and release their own admission, even when stage
orchestration advances through them without waiting for another daemon cycle.

### Running records are the permit ledger

Atomic pool admission and continuation state share one implementation and one critical
section. The permits in use are derived from durable `running` records; there is no second
permit counter to reconcile.

After stage-specific preparation succeeds, the coordinator checks every independent gate
and atomically does all of the following before spawn:

- verifies that the selected pool can fit the matrix demand;
- changes the record from `waiting` to `running`;
- records the pending attempt identity, selected tool, model, demand, and deadline; and
- makes the reservation visible to every concurrent dispatcher.

The provider adapter's start interface has one crash-recoverable, atomic result keyed by
the pending attempt: either `not_started`, proving that no provider process existed, or
`started`, carrying the durable process-family identity. The coordinator consumes the
attempt if and only if the result is `started`. The adapter may not let a provider process
escape without making that result recoverable, even if the process exits immediately or
the coordinator dies before reading it. Reconciliation queries the same start result: a
committed reservation with `not_started` returns to `waiting` without consuming an attempt,
while `started` always counts. This distinguishes admission from provider spawn without
adding a fifth continuation state; the later implementation decision chooses the handshake
mechanism.

If admission fails, the record stays `waiting` and no attempt is consumed. When a provider
family has ended and its final observations are durable, one atomic transition releases the
derived reservation: a recoverable ending returns to eligible `waiting`, a completed
outcome enters `completed`, and a hold decision returns to non-eligible `waiting` with its
typed reason. The stage adapter then idempotently produces the human handoff; only proof of
that handoff permits the transition from `waiting` to `held`. If the coordinator dies
between handoff creation and that transition, reconciliation finds the same proof and
completes the transition without duplicating the handoff. A recovered live process keeps
its recorded demand. An unreadable or ambiguous running record therefore fails closed
without a separate permit-repair path.

One running record reserves for the root provider family: descendants and subagents inherit
its reservation and never submit or reserve independently. This ADR deliberately does not
choose a persistence technology or start-handshake mechanism. The coordinator's local
state implementation remains private: one production representation is not a reason to
expose a storage adapter seam. Tests exercise the coordinator interface with isolated state;
a second real storage implementation would be the point at which a storage seam becomes
real.

### Two provider adapters extract facts; they do not classify policy

Claude and Codex are the two real adapters at the provider seam. They receive only the
launch facts they need: tool and model, prompt, working directory, timeout, environment,
and the durable attempt identity used by the start handshake. They preserve and return:

- process identity and family liveness;
- structured provider events and an opaque copy of unrecognized event fields;
- exit status or signal and whether the supervisor timed out;
- typed provider facts such as capacity with a reset, authentication, billing,
  permission, configuration, server or transport failure, or unknown; and
- partial output plus the captured final message when one exists.

Claude extracts these facts from its structured stream. Codex must use the typed app-server
surface or a typed companion account/rate-limit query when it wants to distinguish capacity
from a permanent plan problem; `codex exec --json` alone remains an unknown interruption.
That is an adapter capability constraint, not permission for Codex-specific continuation
policy.

### Stage adapters preserve completion and recovery locality

Each logical stage supplies a small adapter with three responsibilities:

```text
prepare(record) -> provider launch facts
observe(record, provider facts) -> outcome evidence, bail, or incomplete
finalize(record, completion-or-hold decision) -> durable external proof
```

`prepare` owns the stage's source and recovery behavior and proves its claim before
admission: reuse the retained branch/worktree for code-writing stages, or recreate read-only
state from the durable source. `observe` checks and serializes the stage's required outcome:
parsed intake route, expected PR, head-bound review verdict, verified revision, committed
mockup plus comment, or marked reply plus any pushed change. Provider success cannot replace
this check. `finalize` idempotently applies a completed outcome or creates the stage-native
human handoff for a non-eligible waiting record, then transfers or releases the GitHub
claim. A completed record remains until the next-stage transfer or external-boundary proof
is durable. A record enters `held` only after the human handoff proof exists, then remains
until claim release or park is confirmed. A daemon restart can therefore repeat
finalization safely without losing ownership or creating a second handoff.

Only these minimal facts cross when stage orchestration submits a logical stage:

- repository, subject, logical stage, and immutable target needed for the stable identity;
- required claim and durable source/worktree pointer;
- complexity and build effort needed by the reviewed admission matrix;
- allowed pool or pinned tool lineage; and
- the stage-specific input pointer needed to reconstruct the prompt.

The submission does not carry live-board aliases, raw provider messages, admission demand,
an attempt number, or a continuation decision. Those are derived and owned behind the seam.
Stage-specific outcome payloads return as opaque, typed evidence to their own adapter; the
coordinator only needs to know whether the required outcome exists and whether finalization
is proven.

## Alternatives considered

- **Add a permit manager beneath the current governor and launch calls.** Rejected by the
  deletion test: removing it would merely scatter atomicity, permit lifetime, and priority
  back across the governor, balancer, and six callers. Two counters could also disagree
  after a crash.
- **Make `runner.launch` own continuation and admission.** Rejected because provider
  adapters do not know the stage outcome, claim transfer, cold-routing choices, or global
  continuation queue. It would either misclassify clean-but-incomplete exits or grow a
  shallow callback interface containing the whole pipeline.
- **Put the full stage state machine in each orchestration path.** Rejected because the
  classification precedence, attempt budget, capacity waiting, reconciliation, and logs
  would be repeated six times and drift.
- **Move all stage completion and handoff behavior into the coordinator.** Rejected because
  the interface would expose every stage's GitHub and worktree rules and erase locality.
  The three-method stage adapter keeps the common lifecycle deep while leaving each stage's
  completion behavior beside its existing orchestration.
- **Expose a storage repository interface now.** Rejected because there is one production
  implementation to choose later. One adapter is a hypothetical seam; atomic state and
  admission should stay private until a second real representation exists.

## Consequences

- Every provider attempt, including nested review and revise work, enters through one
  scheduler-owned admission and continuation path.
- The current direct `launch` calls, chain-long governor reservations, in-memory intake
  failure streak, and Boolean launch classification become migration targets; the later
  implementation-slice decision chooses their order, not this ADR.
- `live.py` remains a console projection of running records rather than an ownership or
  recovery source. Worktree PID markers remain provider-liveness evidence, not continuation
  state.
- `balancer.py` continues to supply headroom and pool-choice facts, but the coordinator is
  the only module allowed to turn those facts plus the admission matrix into an atomic
  start.
- Historical replay remains #94. This decision neither validates the policy against
  history nor chooses implementation slices or persistence technology.
