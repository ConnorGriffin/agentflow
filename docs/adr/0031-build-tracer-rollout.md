# ADR 0031 — Build moves behind the coordinator through a drain-safe rollout

- Status: Accepted
- Date: 2026-07-15

## Context

[ADR 0028](0028-stage-scoped-continuations.md), [0029](0029-static-per-pool-admission.md),
and [0030](0030-session-coordinator-seam.md) accepted a durable session coordinator but left
it dormant: no production stage submits to it. Turning it on for Build is the first
user-visible resilient-session tracer, and it cannot be a flag flip. When the switch happens
the machine may still hold work that predates the coordinator — a live legacy build process, an
`agentflow:building` claim, a worktree PID marker, or an ambiguous worktree. Any of these could
be mistaken for coordinator-owned continuation state, and clearing or guessing at them would
either duplicate a build or abandon one. The old live-board lanes (`building`, `triaging`) are
display aliases, not logical stages, so a stage must never be inferred from them.

The pipeline therefore needs a rollout that starts in today's legacy behavior, stops starting
new legacy provider work while the running work finishes, and only enters coordinated Build
once nothing current-format is left to confuse. Rollback needs the same care in reverse.

## Decision

### One durable intent, a phase re-derived each cycle

The operator's desired mode — `legacy` or `coordinated` — is the only durable rollout state; it
survives daemon restart in one small file under the state directory. Every cycle derives a
**phase** from that intent plus the observed world:

| Intent | World | Phase | A cycle may… |
| --- | --- | --- | --- |
| coordinated | current-format sessions/claims/worktrees remain | `draining` | launch nothing new |
| coordinated | nothing current-format remains | `coordinated` | submit and admit Build |
| legacy | a coordinator record still owns in-flight work | `draining` | launch nothing new |
| legacy | no coordinator record owns work | `legacy` | launch the legacy build |

Because the phase is derived, a restart mid-drain simply re-evaluates and keeps draining. The
safety property falls out of the table: in every combination, at most one of legacy launching
and coordinated submission is enabled, so no cycle can launch both. Forward activation waits for
current-format sessions to finish and **refuses** — naming the evidence for human repair —
rather than clearing an ambiguous live entry, PID marker, claim, or dirty worktree. If
current-format state cannot be resolved safely, activation does not proceed.

### Coordinated Build, and only Build

In the coordinated phase a ready issue becomes exactly one durable Build stage submitted to the
coordinator. Build keeps `agentflow:building` while it waits, reuses its owned branch and
worktree on a continuation, stays in its original tool lineage, and completes only when the
expected PR exists (ADR 0028 outcome-first). Preparation of that retained worktree happens
before admission, so a preparation miss consumes neither a permit nor an attempt. Build is the
only enabled logical stage: the coordinator's admission gate admits Build and refuses Review,
Revise, Intake, Respond, and Mockup, so they may be submitted but sit visibly `waiting`,
consuming no permit and no attempt until their own slices land.

The live board's building lane becomes a projection of the coordinator's running records rather
than a per-session write; waiting records reserve nothing and do not appear. Legacy claim
reclamation must consult coordinator ownership and never strip a claim a record still owns — a
claim can now legitimately outlive its provider process.

### Rollback is a drain, not an abandonment

Disabling coordinated behavior stops new submissions but keeps reconciling existing records
until each reaches a durable external boundary (its PR) or a stage-native hold. Legacy launching
cannot resume while any coordinator record still owns in-flight work, and a rollback request
never abandons or converts an active record into a legacy retry.

## Alternatives considered

- **Flip Build to the coordinator directly.** Rejected: a live legacy build, claim, or worktree
  present at the flip would be indistinguishable from coordinator state, risking a duplicate or
  an orphaned build.
- **Infer the logical stage from the live-board lane.** Rejected: `building` and `triaging` are
  ambiguous display aliases; a Revise or a Mockup would be miscounted as Build or Intake.
- **Store the phase durably and mutate it on transitions.** Rejected: a crash mid-transition
  could persist a phase the world contradicts. Deriving it from durable intent plus live
  evidence is self-correcting across restarts.
- **Clear ambiguous claims/worktrees to activate faster.** Rejected: fail-closed refusal that
  names the evidence is the whole point; clearing it is exactly the duplicate/abandon risk.

## Consequences

- Enabling and rolling back Build are both drains with an explicit, restart-surviving phase; the
  daemon consults it once per cycle and routes Build accordingly.
- Only Build is wired behind the coordinator; the other five stages and the legacy provider
  surface are unchanged, so their behavior cannot regress from this slice.
- Claim reclamation, the live board, and dispatch now treat coordinator ownership as
  authoritative for Build. Enabling further stages, adopting already-running legacy sessions,
  remote recovery, and dynamic admission tuning remain out of scope.
