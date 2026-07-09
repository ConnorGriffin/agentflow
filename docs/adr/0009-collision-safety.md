# ADR 0009 — Collision safety without a universal allow-list

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0006](0006-two-pool-runner-assignment.md) turns on **concurrency** — both
pools build in parallel — while [ADR 0005](0005-spec-rigor-rides-the-dial.md)
removed the universal per-issue file allow-list (only `guarded` still declares
one). The superseded design's four parallelism gates all leaned on that declared
allow-list. We need collision safety — both file conflicts and *semantic*
collisions with **no file overlap** — without a universal allow-list to check.

## Decision

**Universal merge-time floor (every repo, every rung):**

1. Each PR **rebases once onto current `main` and reruns full CI** before it is
   mergeable.
2. **Merges serialize** — one PR lands at a time; each surviving sibling then
   re-rebases and reruns CI against the new `main` before it's eligible.
3. The **cross-tool reviewer checks the diff against currently-open sibling PRs**
   for semantic overlap, and flags a conflict as a blocking finding.

**Upfront overlap-check is an optimization, off by default.** Intake *may* predict
an issue's file set from its pointers and serialize overlapping issues while
fanning out disjoint ones — but this only avoids *wasting* parallel builds that
would conflict. It is not a safety mechanism (a wrong prediction gives false
safety), and it is enabled only for repos that run hot enough for the waste to
matter.

**No-file-overlap semantic collisions** are owned by cross-tool review + the
second-merger's rebase-and-rerun-CI, and at `guarded` specifically by the **named
invariant tests** — which exist for exactly this cross-cutting case.

## Alternatives considered

- **Universal per-issue allow-list reservation (old design).** Rejected:
  reintroduces the heavy work-order authoring we deliberately scoped to `guarded`
  only, and a *predicted* allow-list can be wrong — false safety.
- **No serialization; resolve conflicts ad hoc at merge.** Rejected: blind to
  semantic collisions and produces merge pile-ups.

## Consequences

- The floor is **robust and prediction-free**: the worst case is a doomed parallel
  build caught at rebase/CI and re-run — wasted tokens (~free under flat-rate
  plans, [ADR 0006](0006-two-pool-runner-assignment.md)), never a merged conflict.
- `guarded`'s named invariant tests remain the only mechanism that auto-catches a
  no-file-overlap semantic break — reinforcing why `guarded` requires them.
- Merge serialization is a per-repo lock: merge throughput is bounded even though
  builds fan out. Acceptable — merges are fast; builds are the long pole.
