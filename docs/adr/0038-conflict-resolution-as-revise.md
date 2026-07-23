# ADR 0038 — A survivor's merge conflict opens a Revise, not a park

- Status: Accepted
- Date: 2026-07-18

## Context

[ADR 0009](0009-collision-safety-without-allowlist.md)'s merge-time floor re-rebases
surviving sibling PRs each time `main` advances. When that rebase conflicts, the pass
posted a conflict notice and parked the PR for a human — deliberately: agentflow won't
force a conflicted merge. In practice the park became the *policy* rather than the
fallback: every multi-PR burst ended with the operator hand-rebasing survivors
(e.g. #194), even though resolving a rebase is exactly the kind of bounded code task
the pipeline's own Revise stage already performs, verifies, and re-reviews.

Three prerequisites landed before this decision and make it safe:

- Revise/Respond verification accepts a rebased (history-rewritten) head that the
  retained builder worktree provably owns (#199).
- A head move retires the stale review and opens a fresh review at the new head
  (#208), so a conflict resolution is never merged on the strength of a pre-conflict
  verdict.
- Reviews re-place across pools when their home pool loses launch capacity (#202),
  so the fresh review actually runs.

## Decision

**When the survivor re-rebase pass hits a conflict, open a conflict Revise instead of
parking.** The Revise runs on the builder's own lineage in the retained PR-branch
worktree with a single finding: rebase onto current `main`, resolve the conflicts,
keep the full test suite green. Preserve every compatible behavior from both sides.
Where the sides encode genuinely competing product intent, the resolver must not choose:
it records exactly two options, missing guidance, and a recommendation for one private
other-tool decision pass. If that pass still cannot decide, the PR parks for the maintainer.

Scope and bounds, as decided:

1. **Finished PRs only.** The conflict Revise exists for the merge-time survivor pass.
   A *build in progress* that hits an integration collision keeps its own handling
   (defer until `main` moves, then hand off — #209). Unreviewed work does not
   self-resolve conflicts.
2. **Normal merge rules afterward.** The resolved head gets a fresh cross-tool review
   (via #208's head-move reopening) and full CI; then the repo's ordinary merge
   policy applies as if the conflict never happened. No forced drop-to-human on
   autonomous repos, no auto-merge shortcut either. The fresh review's prompt gains
   one explicit lens: *verify the resolution did not silently discard `main`'s
   changes* (the `-X ours` hazard).
3. **Every new conflicting head gets a bounded Revise (amended by ADR 0047).** Conflict rounds
   remain separate from finding-driven revise rounds, but there is no PR-lifetime count. The
   coordinator's attempt budget bounds each logical conflict. Exhaustion or genuinely unsafe
   ambiguity parks with the existing conflict notice; routine repeated `main` movement does not.
4. **Conflict Revises jump the queue.** A conflict-blocked survivor is one merge from
   done; its Revise is admitted ahead of cold build submissions rather than queueing
   behind new work.

## Consequences

- Multi-PR bursts converge without operator rebasing; the operator sees conflicts
  only when the pipeline has genuinely failed twice.
- A conflict resolution is agent-authored merge arbitration: the safety relies on
  the fresh-review floor, not on trusting the resolution. The reviewer lens, compatible-behavior
  preservation, and private decision handoff are load-bearing; weakening any of them reopens the
  silent-drop hazard.
- Conflict rounds keep distinct identities without adding a lifetime cutoff. Sharing
  MAX_REVISES would let a busy review cycle starve conflict recovery (and vice versa) — the two
  failure modes remain unrelated.
- Queue-jumping trades a little new-build latency for faster drain of nearly-merged
  work, consistent with merge serialization already being the throughput bottleneck.

## Alternatives considered

- **Keep parking (status quo).** Rejected: the operator was the conflict-resolution
  path for every burst; ADR 0009's floor made conflicts *routine*, not exceptional.
- **Resolve during the build (pre-PR) too.** Rejected for now: a build's work is
  unreviewed; letting it also arbitrate against `main` compounds unreviewed risk.
  Revisit once conflict Revises have a track record.
- **Always drop conflict-resolved PRs to a human merge.** Rejected: it re-creates
  the operator-as-bottleneck this decision removes, and the fresh-review + CI floor
  is the same one every other merge trusts.
- **Share the existing revise budget.** Rejected: entangles two independent failure
  modes; exhaustion in one would silently disable the other.
