# Pitfalls and future work

*Honest costs of the current design, credible next steps, and where to read more.*

## Pitfalls and sharp edges

An honest list of the places where the design's costs are real.

**Bounded recovery is a hard stop, by design.** Three attempts and the work is durably
held for a human. That is the intended behavior, but it has a price: a red check on a
pull request can cost up to two builder sessions before it reaches the operator.
`RESTART_RESUME_CAP` at 5 and `REPAIR_BUDGET` at 1 are deliberately small for the same
reason — bounded churn beats unbounded churn — and the cost is that a genuinely transient
problem sometimes consumes the whole budget.

**Environment faults can still be misclassified as budget exhaustion.** In the worktree
outage described in [Building and reviewing](building-and-reviewing.md), every session
lost its shell for an environmental reason and the failed attempts were all recorded as
"continuation budget exhausted" — technically accurate and diagnostically useless.
[ADR 386](https://github.com/ConnorGriffin/agentflow/blob/main/docs/adr/adr-386-dead-shell-environment-fault.md)
fixed this for Claude by detecting a dead shell through tool-use and tool-result
correlation and refunding the attempt as an environment fault. It explicitly did *not*
fix it for Codex: the Codex exec JSON surface carries no typed tool-result fact to
correlate a refusal back to a shell call. That is a known, documented, unaddressed gap.

**The lock-retry asymmetry.** `Store.upsert` was given a bounded retry on
"database is locked" — two delays before failing closed. The `_reserve` permit-reservation
path was not patched at the same time. A transient busy writer can therefore still fail a
reservation that a short retry would have won.

**The permit-default flip-flop.** The permit budget default was changed from 5 to 25,
which broke capacity-sensitive tests, then reverted to 5 with a runtime override. It was
a config-versus-code boundary bug, and it produced no ADR — the entire record is in commit
messages. A number that behaves like policy should be documented like policy.

**ADR-before-code drift.** In-session slicing, the commit-per-slice ledger, and
slice-bearing work orders were each described by an ADR before any implementation existed;
all three landed the same day, much later. The ADRs read as if the system already worked
that way. Reading the decisions without reading the git log can mislead you about what is
actually running.

**A cosmetic inconsistency in park comments.** A head that was green, then moved, then
came back red can show a stale "Outcome: clean." above a later park. The verdict is
correct; the leftover line is not.

**Cost misattribution across subagents.** The provider rolls subagent cost into the parent
session's total, and telemetry keyed on the routing dial once read a coordinated build as
fully deep while most of its turns ran on a cheap tier — silently wrong rather than
visibly missing, which is the worse failure mode. A per-model breakdown corrected it.

**The progress lease is Build-only.** Every non-Build stage, Revise included, still uses
the older fixed ceiling. A long but genuinely progressing Revise session can still be
killed by a coarse timeout that a lease would have renewed.

!!! note "An accepted failure, stated plainly"
    Universal per-issue file allow-lists for collision safety were rejected as "false
    safety." Instead the engine relies on merge-time rebase, CI, cross-tool overlap
    review, and serialized merges — which means it accepts an occasional doomed parallel
    build by design rather than pretending a static allow-list would have prevented it.

## Where it could go next

These are candidates that follow from the material above, not commitments on a roadmap.

- **Extend dead-shell detection to Codex.** The Claude path already refunds an
  environment fault instead of charging it against the continuation budget. Codex sessions
  keep the older classification because the exec JSON surface offers nothing to correlate.
  If that surface gains a typed tool-result fact, parity becomes straightforward.
- **Retry parity on the reservation path.** Giving `_reserve` the same bounded
  database-is-locked retry that `upsert` received would close the asymmetry, and the
  fail-closed behavior beyond the retry window would be unchanged.
- **A byte-based worktree ceiling.** The current gate counts registrations as a proxy for
  a byte limit whose slope moves with CLI version and path length. Measuring the actual
  spawn argv, or at least the per-registration slope on the running machine, would stop
  the number from rotting between recalibrations.
- **Progress leases beyond Build.** Revise is the obvious next candidate: it is a
  code-writing stage whose genuine work time varies as much as Build's, and it currently
  gets a fixed wall.
- **The deferred learning-pipeline stages.** Paired or synthetic provider evaluation,
  causal claims, automatic mutation, and slice-level attribution are all explicitly out of
  scope today and fail closed by omission. Any of them would need a human-governed
  promotion path before it could touch the engine.

## Further reading

- [Get started](../getting-started.md) — requirements, install, enrollment, calibration,
  first run, and recovery.
- [Understand the pipeline](../pipeline.md) — stages, authority, review, revise, merge, and
  recovery.
- [Repository capabilities](../capabilities.md) — generated contracts, pins, and readiness.
- [Operate AgentFlow](../coordinator-operations.md) — pause, drain, upgrade, diagnosis, and
  rollback.
- [Learning pipeline](../learning-pipeline.md) — observed outcomes, human methodology review,
  and deferred evaluation.
