# ADR 0043 — A retry needs new recovery state, or it is not a retry

- Status: Accepted
- Date: 2026-07-19
- Amends: [ADR 0028](0028-stage-scoped-continuations.md) (stage-scoped
  continuations and the attempt budget), [ADR 0030](0030-session-coordinator-seam.md)
  (the coordinator seam and stage adapters)
- Evidence: [spend-per-success research](../research/spend-per-success-measurement-contract.md)
  (7.8% of Claude spend went to superseded retry attempts),
  issue [#225](https://github.com/ConnorGriffin/agentflow/issues/225)

## Context

Every logical stage gets one initial provider attempt plus two automatic
continuations. Before this ADR, *any* non-permanent ending with no verified
outcome — including a clean exit that simply produced nothing — requeued, and
the requeue rebuilt the identical durable prompt for a fresh, stateless provider
session. A clean Intake or Review exit with no route or verdict therefore burned
two more full sessions that had nothing new to work from and produced the same
empty result. Measurement found this class of superseded replay to be 7.8% of
all Claude spend. A daemon restart could additionally refund and replay the same
attempt several times.

A retry is only resilience when the fresh attempt has *something new to act on*:
new evidence, retained partial work, or a changed input. Replaying an identical
prompt in a stateless session is waste, not resilience.

## Decision

The coordinator classifies every non-permanent, non-verified ending by whether a
fresh attempt would have new recovery state, and only then decides to continue,
repair once, or park:

- **A genuine capacity / server / timeout interruption always continues** within
  the attempt budget. The limit lifts or the transport recovers, so the next
  session is not an identical replay. This is unchanged.
- **A worktree-owning stage** (build, revise, respond, mockup, research,
  converse) **continues** within the budget, because its continuation carries the
  retained worktree forward — genuinely new state. Each continuation now also
  carries a **recovery envelope**: bounded durable facts (the attempt number, the
  missing outcome, and the retained worktree path) appended to the prompt so the
  fresh session resumes from that work rather than restarting.
- **A read-only stage** (intake, review) **owns no partial work**, so a clean
  exit with a missing outcome would replay identically. It earns **at most one
  targeted repair** — a continuation whose envelope names the exact missing proof
  — and then **parks for a human** instead of spending a third identical session.
- **A stage that reports no new state at all** parks at once, with no replay.
- **A daemon restart** that kills a family leaving no provider end fact still
  resumes the same attempt in place, uncharged and bounded by the restart cap; it
  is never counted as a repair.

The classification is the stage adapter's job (it owns the worktree and the
notion of a required outcome); the coordinator stays stage-agnostic and turns the
classification into a continuation, a single repair, or a park. A stage adapter
that provides no classifier keeps the historical continue-within-budget behavior,
so the seam is opt-in.

## Consequences

- Read-only stages that produce nothing durable now park after two attempts
  instead of three, eliminating the identical third replay. Worktree stages keep
  their full budget but resume rather than restart.
- The recovery envelope is deliberately bounded — a few durable facts, never the
  prior event stream — so a fresh session is grounded without re-ingesting a whole
  transcript.
- Park reasons gain a distinct "no new recovery state to act on" case, separate
  from budget exhaustion, so the operator log tells replay-waste from genuine
  exhaustion.
