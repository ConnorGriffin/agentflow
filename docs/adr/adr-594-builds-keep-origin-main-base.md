# ADR 594 — Builds keep `origin/main` as their sole base

- Status: Accepted
- Date: 2026-08-16
- Ticket: [#594](https://github.com/ConnorGriffin/agentflow/issues/594)
- Constrains: [ADR 0024](0024-dependency-aware-dispatch.md) (a declared dependency gates Build
  dispatch until its same-repository blocker is closed), and [ADR 0009](0009-collision-safety.md)
  (the merge-time `main` rebase and CI floor)

## Context

The lock-then-build lifecycle can place a mockup pull request ahead of its implementation pull
request. The mockup pull request lands the lock manifest, frozen behaviour ledger, and mock
modules; until it merges, those contract artifacts are not on `main`. A Build round must not port
against that incomplete base.

Build worktree preparation has one hard-coded base: `origin/main`. A per-build base ref could
point a Build at the mockup branch before its pull request merged, but that would make AgentFlow
support stacked pull requests, bases that move while review is in progress, and reviews against
work that might never merge.

The existing dependency gate already models the actual ordering. A ready issue is free only when
no agent owns it and every declared blocker is closed. It reads native same-repository GitHub
`blocked_by` edges, ignores cross-repository edges, and fails closed when the dependency graph
cannot be read; an unreadable graph is never interpreted as having no blockers.

## Decision

**Builds keep their single hard-coded base of `origin/main`. AgentFlow will not add a per-build
base ref.**

For lock-then-build work, the execution issue declares a native GitHub `blocked_by` edge on the
mockup issue. Under ADR 0024, the daemon does not dispatch the Build until the mockup issue is
closed. Its pull request has then merged, so the next Build starts from `origin/main` containing
the lock manifest, frozen behaviour ledger, and mock modules.

The dependency is therefore a machine-honored tracker edge, rather than prose in a process
document. This is the existing mechanism serving the first caller; the charter's rule against
building a seam before a second caller genuinely needs it applies here.

## Alternatives

- **Add a per-build base ref.** Rejected. It would add stacked-pull-request behavior, moving
  review bases, and review of work that may never merge, without serving a need that the existing
  dependency gate does not already meet.
- **Withhold the ready label and drive the Build manually.** Rejected. It hides a real dependency
  from the daemon and makes an automated ordering rule into repository-local prose and
  coordinator work.

## Consequences

- A mockup issue and its build issue form an ordinary native `blocked_by` chain. The build may be
  ready, but it is not dispatchable until the mockup issue closes; it then builds from current
  `origin/main`.
- The `Daemon guard` paragraph in ciq-autotune's process document should be replaced with an
  instruction to declare the mockup issue as a blocker of the execution issue. That repository is
  outside this change.
- No Python behavior changes. ADR 0024 remains the build-dispatch mechanism and ADR 0009 remains
  the merge-time `main` safety floor.
