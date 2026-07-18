# ADR 0024 — Dependency-aware dispatch: `blocked-by`, so an ordered batch builds in order

- Status: Accepted
- Date: 2026-07-13
- Revised: 2026-07-17 — native GitHub `blocked-by` edges added as a second
  recognized source, unioned with the body prose (see "Native `blocked-by`
  edges" below).

## Context

The dispatcher is dependency-blind. It picks the **oldest `ready-for-agent`
issue** and builds it ([ADR 0006](0006-two-pool-runner-assignment.md) chooses the
pool; [ADR 0021](0021-dispatch-dedup-build-claim.md) claims it), with no notion
that one issue must land before another.

But a big change — a refactor, a re-platform ([ADR 0023](0023-dashboard-replatform-control-plane.md)),
a migration — decomposes into an **ordered chain of vertical slices**
([ADR 0012](0012-build-in-vertical-slices.md)), where slice B builds on slice A's
*merged* foundation. Today the only way to enforce that order is to **file one
issue at a time and hand-advance** — babysitting the queue. And
[ADR 0023](0023-dashboard-replatform-control-plane.md)'s headroom-governed
concurrency makes it worse: more sessions dispatch in parallel, so an unordered
batch is *more* likely to grab slice C before slice A exists.

The dispatcher needs to respect a declared dependency.

## Decision

Model one relationship — **"blocked by"** — and gate dispatch on it.

- **Two recognized sources, unioned.** A blocker can be declared either in the
  issue body as a `Blocked by #N` line (multiple allowed) **or** as a native
  GitHub `blocked-by` relationship on the issue. The dispatcher's blocker set is
  the **union** of both, deduped; an issue is free only when *every* blocker in
  that union is closed.
  - *Body prose* keeps GitHub the source of truth and is parsed the way agentflow
    already parses body markers (`MISSING-CONTEXT:`, the historical `Open as:`, the
    `agentflow:` labels) — no new GitHub feature required. It was the original
    (2026-07-13) sole source.
  - *Native `blocked-by` edges* (added 2026-07-17) are read over the GitHub
    dependencies API. Planning tools (wayfinder) already express dependencies as
    native GitHub relationships, so honoring them directly removes the seam where a
    native dependency had to be hand-translated into a prose line that was silently
    ignored if mistyped. Only same-repo edges join the gate; cross-repo edges are
    out of scope and ignored (see [#156](https://github.com/ConnorGriffin/agentflow/issues/156)).
- **Filtered only at build dispatch**: a ready issue is not selected for a build
  while any blocker (from either source) is open or its state cannot be verified.
  Intake and grounding do not apply this gate; they still prepare the whole batch up
  front. That build-eligibility rule is the entire behavioral change.
- **Stateless, no release event.** Eligibility is recomputed every cycle, so the
  moment a blocker closes (its PR merges), its dependents become dispatchable on
  the next pass. Nothing to trigger, nothing to get stuck — the same
  "GitHub is state of record, recompute each cycle" property the daemon already
  relies on ([ADR 0011](0011-persistent-orchestrator.md)). Crash-safe by
  construction.
- **Orthogonal to readiness.** Intake still grounds and marks the whole batch
  `ready-for-agent`; only build dispatch holds the *dependent* ones until their
  turn. A blocked issue is not a held issue ([ADR 0019](0019-human-re-entry.md)) —
  no human input is pending, just an upstream merge.

Together with [ADR 0023](0023-dashboard-replatform-control-plane.md)'s concurrency,
the dispatcher becomes **parallel where work is independent, ordered where it's
dependent** — the behavior you actually want from a fleet.

## Alternatives considered

- **File one issue at a time (status quo).** Rejected: it's manual queue-babysitting,
  and concurrency makes it more error-prone, not less.
- **A first-class epic / parent-child construct with an explicit release step.**
  Rejected: heavier machinery for the same result. A `blocked-by` chain *is* an
  ordered epic, with a stateless filter instead of a release event.
- **GitHub sub-issues / task-list relations as the dependency source.** Reasonable
  later, but it leans on a newer GitHub feature; the body marker needs nothing new
  and matches existing parsing. Native `blocked-by` edges were subsequently added as
  that second recognized source (2026-07-17), unioned with the prose rather than
  replacing it.
- **One giant issue built across many commits/PRs.** Rejected: breaks
  one-issue-one-PR and, more importantly, the **per-slice review gate** — the whole
  point of vertical slices is that each closes the loop through review + merge.

## Consequences

- Build dispatch gains a small, deep dependency filter (one predicate over the
  ready set; a cheap per-candidate blocker-state check, batchable). Intake and
  grounding remain unchanged.
- **A dependency chain can be filed once** — the head builds, and each later slice
  unblocks automatically as its blocker merges. No babysitting. Generalizes past
  refactors to any dependent work.
- **[ADR 0012](0012-build-in-vertical-slices.md) still governs the *scoping*.** The
  gate removes the *mechanical* cost of ordering; it does not license hard-freezing
  downstream slice scopes. File the chain, but keep later issue bodies loose and
  revise them before they unblock — slice 1 usually teaches you something. The gate
  lets you *choose* how much to pre-commit.
- A misdeclared cycle, or a blocker that never closes, **fails safe** (the
  dependents simply never dispatch) and is visible/loggable — never a wrong build.
- The dashboard ([ADR 0023](0023-dashboard-replatform-control-plane.md)) can later
  surface a **blocked** chip in the queue so the ordering is legible.
- **Prose is now the legacy source, kept for backward compatibility.** The native
  edge read is additive: no existing `Blocked by #N` issue changes behavior, and
  the same every-blocker-closed / fail-closed rules apply to both sources. The
  intended future posture is to **deprecate the `Blocked by #N` prose** once native
  `blocked-by` is proven reliable across the fleet's headless (daemon) runs — at
  which point native becomes the single source and prose is retired. That
  deprecation is deliberately out of scope here.
