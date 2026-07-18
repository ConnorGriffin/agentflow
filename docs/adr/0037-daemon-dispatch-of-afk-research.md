# ADR 0037 — Wayfinder plans, the daemon executes: AFK research dispatches through the coordinator

- Status: Accepted
- Date: 2026-07-18
- Supersedes: [ADR 0027](0027-wayfinder-planning-boundary.md)
- Amends: [ADR 0035](0035-workflow-engine-read-only-operator-console.md)

## Context

[ADR 0027](0027-wayfinder-planning-boundary.md) made the `wayfinder:*` namespace a
wall: intake never sees a planning ticket. That kept planning artifacts safe from
being rewritten into build briefs, but it also made them invisible to every
unattended process. A research ticket that comes unblocked while no wayfinder
session is live strands indefinitely — issue #180 sat exactly there. The operator
requirement is the opposite: *an investigation a machine can finish alone must
still resolve when no human gets to it.*

Meanwhile the wayfinder skill hand-rolled its own execution: charting launched AFK
research workers and supervised them itself (claim recovery, reconcile-on-replace)
— a second agent fleet beside the coordinator, with none of its guarantees. Those
sessions were also invisible to pool admission ([ADR 0029](0029-static-per-pool-admission.md))
and spend pacing ([ADR 0025](0025-activity-adaptive-spend-ceiling.md)): unaccounted
provider sessions racing the builds the balancer *does* know about.

[ADR 0035](0035-workflow-engine-read-only-operator-console.md) said planning and
research "run in the operator's chat tool." That phrase over-generalized what
dogfooding actually rejected — a bespoke turn-based web planning UI. It was never
meant to require a human at the keyboard for work an unattended agent session can
finish alone.

## Decision

### The boundary is judgment vs execution

Chat sessions own **judgment**: grilling, mockups, decisions, map reconciliation,
anything that needs a human in the loop. Agentflow owns **unattended execution and
its supervision** — builds, and now AFK research on planning tickets. Wayfinder is
a pure planner: it never executes. A prerequisite that must *change the world*
before a decision can be made is handed off as an ordinary Build Issue; its landed
result feeds back into the map as a decision input.

### Ticket types split by AFK-able vs needs-a-human

`wayfinder:research` is any investigation an unattended session can finish alone —
repo-internal audits as much as external documentation reads. The
inward-vs-outward distinction is retired as the axis; it was a proxy that
repo-internal audits like #180 break. `wayfinder:grilling`, `wayfinder:prototype`,
and `wayfinder:task` remain human-in-the-loop; `task` shrinks to prerequisites
that genuinely need the operator.

### Claim + type replaces the wall

The daemon sees wayfinder tickets. A shared claim label — `wayfinder:resolving` —
marks a ticket in progress by *any* session, human or daemon; it replaces
wayfinder's assignment-as-claim (the daemon runs under the operator's GitHub
identity, so assignment cannot distinguish the two). The daemon may dispatch an
unattended research session for an **open, unblocked, unclaimed
`wayfinder:research`** ticket, and for nothing else. Build intake still excludes
the entire `wayfinder:*` namespace — no planning ticket is ever ground into an
Agent Brief. That half of ADR 0027 survives; only the invisibility is gone.

### Research dispatch rides the coordinator

Unattended research sessions are coordinated sessions like any other: they take a
capacity permit from their pool ([ADR 0029](0029-static-per-pool-admission.md)),
respect the activity-adaptive ceiling ([ADR 0025](0025-activity-adaptive-spend-ceiling.md)),
and get continuation, bounded attempts, and crash recovery
([ADR 0028](0028-stage-scoped-continuations.md),
[ADR 0030](0030-session-coordinator-seam.md)) — a daemon restart releases any
`wayfinder:resolving` claim it holds for a dead run. There is **one dispatcher for
all unattended research**: wayfinder charting stops launching and supervising its
own workers. It files tickets; the coordinator runs the AFK-able ones whether the
strand happens at charting time or three days later.

### Unattended resolution is narrow

A daemon-dispatched session answers the bounded question and records it: findings
and the decision they support as a ticket comment, close the ticket, one titled
line appended to the map's "Decisions so far." It does **not** create newly
exposed tickets, graduate fog, judge handoff readiness, or write ADRs — the map's
graph is reconciled by the next human wayfinder session, which sees the closed
ticket. The daemon resolves questions; it never makes planning judgments.

### Nothing new is owned by the daemon

The claim is a GitHub label; the findings live on the ticket; the breadcrumb lives
on the map. No pre-issue workspace state returns. The console stays read-only and
simply shows research sessions in live sessions like any build. ADR 0035's phrase
"runs in the operator's chat tool" is amended to mean *an agent session, attended
or unattended*; its retirement of the web planning surface is untouched.

## Alternatives considered

- **A scheduled chat sweep (cron-launched research session).** Rejected: invisible
  to pool admission and spend pacing, no crash recovery for its claims, no attempt
  bounds, no dedup against a second fire — a third scheduler with the weakest
  guarantees, added right after diagnosing wayfinder's second one as the problem.
- **A machine user so the daemon can claim by assignment.** Rejected: a second
  GitHub account and token to manage, for parity a label already provides.
- **Fold `task` into `research`.** Rejected: AFK-able vs human-in-the-loop is a
  real behavioral fork — one you can walk away from, one you steer.
- **Let the daemon fully reconcile the map after resolving.** Rejected: that puts
  unattended planning judgment into the map. Answering a bounded question is
  execution; rewiring the decision graph is not.
- **Keep the wall and remind the operator about strands.** Rejected: a reminder is
  not a guarantee; the requirement is that AFK-able work resolves without a human.
- **Convert stranded research tickets into Build Issues.** Rejected: still needs an
  unattended trigger, and produces the wrong artifact — findings belong on the
  planning ticket, not in a PR.

## Consequences

- The strand guarantee holds from birth: every unclaimed AFK-able research ticket
  is eventually dispatched, with the coordinator's recovery discipline behind it.
- The wayfinder skill slims: worker launch/supervision is deleted from charting,
  and assignment-as-claim is replaced by the `wayfinder:resolving` label.
- ADR 0027's fail-safe narrows knowingly: a mislabeled ticket carrying
  `wayfinder:*` still can never become a build, but an unclaimed
  `wayfinder:research` ticket *will* be researched — the label now promises
  AFK-ability, so it must be applied honestly.
- #180 is AFK-able and becomes eligible for unattended pickup once relabeled
  `wayfinder:research`; whether to steer it by hand instead stays a per-run
  operator choice.
- The daemon grows a research stage (dispatch, claim, narrow resolution); that
  implementation lands through the normal pipeline as ordinary build issues.
