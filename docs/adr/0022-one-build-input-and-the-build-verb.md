# ADR 0022 — One build input (the Agent Brief), and `build <N>` as the by-hand trigger

- Status: Accepted
- Date: 2026-07-10

## Context

Two things named "work order" have coexisted, and the confusion had a cost:

1. **The personal `/go` + `/work-order` skills** (in private tooling, from
   the pre-agentflow two-tool era). `/work-order` wrote a self-contained copy/paste
   *implement prompt* — repo facts, model/effort assignment, worktree steps, the
   `/implement` (TDD) invocation, routing envelope — and `/go` fetched that comment and
   ran it in-session. Agentflow has since absorbed every part of that: the frame is
   `BUILD_PROMPT`, the model/effort assignment is the `complexity`/`effort` labels, and
   the frozen scope is the **Agent Brief** intake writes into the issue body. So on a
   `reviewed`/`autonomous` repo these skills are pure legacy — and worse, misleading:
   `/go` hunts for an `Open as:` comment that agentflow's normal issues don't have.

2. **Agentflow's guarded work order ([ADR 0005](0005-spec-rigor-rides-the-dial.md))** —
   a genuinely different artifact. Not a copy/paste frame: a *domain-grounding* document
   for a safety-critical repo, pre-freezing domain literals, test fixtures, a file
   allow-list, and named invariant tests so an unattended builder never *guesses*
   a domain fact. Its intent is sound and stays. But ADR 0005 delivered it as a
   **separate frozen comment** (`loop._work_order`, keyed off an `Open as:` line) — a
   second build-input format alongside the Brief, and the reason `run_once` refuses to
   build a guarded issue that has no such comment.

Two build-input formats is one too many. And the interactive surface ([ADR 0019](0019-human-re-entry.md))
had no by-hand "build this ready issue now" verb — that hole was being filled by the
legacy `/go`.

## Decision

**The Agent Brief is the single build input for every profile.** There is no separate
work-order comment.

- **`reviewed` / `autonomous`** — unchanged: build from the Brief in the issue body via
  `BUILD_PROMPT`.
- **`guarded`** — build from the Brief too. ADR 0005's *substance is retained* — a guarded
  issue's grounding is still pre-frozen before it's buildable — but it now **rides in the
  Brief**, not a separate comment. A guarded Brief carries the heavier grounding the domain
  demands: pre-decided literals and fixtures (the Brief's **Verified** section and grounded
  acceptance literals already model this), a file allow-list, and named invariant tests. The
  **gap-marker protocol** (builder stops and posts a marker on an unstated domain fact rather
  than guessing) is retained as the per-level safety it always was.
- **`BUILD_PROMPT` is hardened** to name the charter's test standard out loud — a test that
  **exercises the behavior through the public interface**, and, where it fits, **one that
  failed first for the right reason** — so the builder is told the bar up front, not only
  caught at cross-review. The process discipline the old `/implement` invocation carried now
  lives where it belongs: the charter (read by every builder) states the *outcome*, and
  cross-review enforces it as a blocking finding ([ADR 0018](0018-two-dials-review-by-evidence.md)).

**`/agentflow build <N>` is the by-hand trigger for a ready issue** — the arm the surface
was missing, and the replacement for `/go`.

- It **kicks agentflow's own build path** pinned to issue N — the same `run_once` /
  `_build_review_merge` the daemon runs: a separate builder (a *different model* than the
  reviewer, per [ADR 0006](0006-two-pool-runner-assignment.md)) implements it, opens the PR,
  cross-review runs, and it merges or parks per the repo's profile. There is **one** builder
  path, not a second one re-implemented in the skill; cross-review's different-model
  guarantee holds automatically; the skip invariant holds (build → review → merge still
  governed by the profile — `build` adds convenience, never authority).
- It **refuses and redirects** on anything not `ready-for-agent`: a held issue points at
  `pickup`; an un-triaged one at `triage` / `scope`. Only a ready issue builds.
- It **reuses the daemon's claim** (`agentflow:building`, [ADR 0021](0021-dispatch-dedup-build-claim.md))
  so a by-hand build can't collide with the daemon.

**The personal `/go` and `/work-order` skills are retired.** Every repo they were used on is
enrolled in the fleet, so `build <N>` is a clean swap.

## Alternatives considered

- **Keep the guarded work order as a separate frozen comment (retire only `/go`).** Rejected:
  keeps two build-input formats alive to serve one repo, and keeps a seed-the-comment tool
  (`/work-order`) on life support. The guarded grounding is content, not a format — it fits in
  the Brief.
- **Fold everything into one umbrella `pickup` that dispatches by label** (triage / grill /
  build from one verb). Rejected: `triage`, `scope`, `pickup`, `revise`, `build` are
  deliberately distinct operations a human chooses; collapsing them into one verb that guesses
  from a label trades clarity for a single word.
- **Name the verb `go`.** Rejected: resurrects the name being retired; `build` matches the
  pipeline's own stage vocabulary.

## Consequences

- **ADR 0005 is amended:** its intent (guarded pre-freezes domain grounding; the gap-marker
  protocol) stands; its *mechanism* changes — the grounding rides in the Agent Brief, and the
  separate frozen work-order comment is gone. `loop.run_once`'s guarded branch no longer
  requires `_work_order`; it builds from the Brief like every profile. Whoever authors a guarded
  Brief owes the heavier grounding.
- **`run_once` gains a way to target a specific issue** (today it grabs the next ready one), so
  `build <N>` can pin issue N.
- **The interactive surface is complete**: `triage` / `scope` (front), `pickup` (held issue),
  `build` (ready issue), `revise` (parked PR) — one verb per phase, all sharing the daemon's core.
- One less build-input format to keep in sync, and two fewer personal skills drifting out of
  the fleet's sight.
