# ADR 464 — A coordinated slice runs in the coordinator's own session

- Status: Accepted
- Date: 2026-08-02
- Ticket: [#464](https://github.com/ConnorGriffin/agentflow/issues/464)
  (wayfinder map [#463](https://github.com/ConnorGriffin/agentflow/issues/463))
- Constrains: [ADR 0029](0029-static-per-pool-admission.md) (admission is unchanged),
  [ADR 0040](0040-spend-per-success-measurement-contract.md) (attribution),
  [ADR 0043](0043-recovery-state-before-replay.md) (recovery),
  [ADR 0044](0044-stage-session-profiles-and-ceilings.md) (profiles and ceilings)

## Context

A coordinated build decomposes one issue into slices that a deep coordinator delegates to
cheap workers, landing them all on a single pull request. The root shape question is
whether a slice runs inside the coordinator's own provider session or as a separately
launched runner session the coordinator dispatches and tracks.

Map #463's audit fixes the economics: cost is linear in session length
(`$ = 0.063 × turns^0.99`, flat at ~$0.060/turn from 20 to 160 turns), and the lever is the
**tier premium** — at an equal 25 turns, standard costs $0.81 and deep costs $2.35. A
coordinated build wins by moving mechanical turns onto the cheap tier, not by running
slices at the same time. Concurrency is not the mechanism, so the one thing launched
sessions are uniquely good at buys nothing here.

Cost is not the only motivation, and it may not be the larger one. A model reasons worse as
its context window fills, so a monolithic deep build spends its last turns — the ones that
finish the work — on the widest, most polluted context it will ever hold. Slicing attacks
that directly: each slice reasons over a small, purpose-built context. If that produces
better first-pass work, the saving compounds beyond the tier premium into fewer blocking
review findings, fewer revise rounds, and fewer follow-up bug fixes. That effect is a
hypothesis this map cannot measure in advance — history has no coordinated builds in it —
but it points the same way as the cost argument, and it is why the shape is worth shipping
rather than merely worth pricing.

It also decides the shape, because the two candidates handle context very differently. An
in-session subagent is not a shared window: it gets its own fresh context, returning only
its result to the parent. So the quality argument is fully available in-session, and only
the coordinator's own window grows. A launched session buys the same fresh context at the
cost of an engine change.

Launched slices also cannot fit the admission budget they would have to run under. A Claude
deep build already reserves four or five of a pool's five permits, and ADR 0029 sets the
minimum code-writing demand at three precisely so two writers can never share a pool. A
coordinator plus one launched slice needs seven permits on a five-permit pool. Admitting
that shape means either retuning the matrix — which map #463 rules out, it adds a route
rather than changing one — or inventing a sub-three writer row that dissolves the
invariant the budget exists to enforce.

The strongest objection to in-session slices was spend attribution: a provider rolls
subagent cost into the parent session's `cost_usd`, so a coordinated build reports one
blended number. Grounding weakens it. The coordinator already charges descendants to the
root reservation by design — that is why Codex review is priced at demand two — and
Claude's terminal `result` event carries `modelUsage` keyed by the model that ran, so
coordinator dollars and slice dollars are separable per attempt today. What is missing is
only that `agentflow/coordinator/telemetry.py` takes the model label when `modelUsage` has
exactly one key, so a mixed-tier session records no model at all.

## Decision

**A slice runs as an in-session subagent of the coordinator.** One logical Build stage, one
durable record, one worktree, one provider attempt, one tool lineage.

- **Admission is untouched.** A coordinated build reserves its ordinary Build cell and its
  slices reserve nothing, exactly as ADR 0029's root-reservation rule already states for
  descendants. The scheduler sees one build; the fleet's headroom accounting is unchanged.
- **Attribution is per tier, not per slice.** Telemetry keeps the per-model breakdown
  instead of collapsing it to a single label, so a coordinated build reports deep-tier and
  standard-tier spend separately against one stage identity. That answers the map's
  question — did the tier premium shrink — and preserves ADR 0040's cohort cells, which are
  keyed by model. Per-slice numbers are not billed and are not pursued here; a
  coordinator-reported slice ledger is [#468](https://github.com/ConnorGriffin/agentflow/issues/468)'s
  question, not this one.
- **A finished slice is committed to the branch before the next one starts.** Durable
  progress, not a ceiling number, is what makes the shape survivable: a coordinator killed
  at its wall leaves committed slices behind, and ADR 0043's worktree-carrying continuation
  resumes from that work rather than replaying the coordinated build.
- **The coordinator gets its own ceiling cell, named unmeasured.** It carries more work in
  one session than a monolithic deep build, and no sample exists yet. Per ADR 0044 an
  unmeasured cell falls back to its complexity's default and is never given a number that
  reads as measured; it ratchets once telemetry fills it. The coordinator additionally
  holds each slice to an internal turn budget so a single runaway slice cannot consume the
  whole wall.
- **A slice's return to the coordinator is bounded.** The quality argument only holds while
  contexts stay narrow, and the one window that does grow across a coordinated build is the
  coordinator's. A slice returns a result, never its transcript. What exactly it returns is
  [#468](https://github.com/ConnorGriffin/agentflow/issues/468)'s question; that it must be
  bounded is settled here.
- **A slice needs a defined session profile** under ADR 0044. It is code-writing work, so
  it inherits the Build allowlist with MCP pinned strict; a slice never widens the surface
  its coordinator was launched with.

## Alternatives considered

- **Separately launched runner sessions per slice.** Rejected. It buys concurrency the
  economics do not need, cannot be admitted without breaking ADR 0029's writer invariant,
  and fails the charter's deletion test: dispatch, the admission matrix, permit budgets,
  worktree ownership, and per-slice continuation records do not concentrate complexity,
  they smear it across the engine. The coordinator would become a second session
  coordinator nested inside the real one, duplicating ADR 0030's seam.
- **In-session slices with a new per-slice billing record.** Rejected. The provider does
  not bill per subagent, so the record would be reconstructed rather than observed, and
  ADR 0040's contract is per logical stage — a slice is not one.
- **A hard per-slice wall-clock kill.** Rejected in favour of the internal turn budget plus
  commit-per-slice. Killing the session is the provider's lever, not the coordinator's;
  durable committed progress is what the recovery path actually needs.

## Consequences

- Coordinated build is a change to the Build stage adapter and its prompt, not an engine
  change. Dispatch, admission, permits, and continuation stay as they are.
- Mixed-tier sessions must stop recording a null model. Until telemetry keeps the per-model
  breakdown, a coordinated build is invisible to ADR 0040's model-keyed cohort cells and
  the re-review has no readout.
- The whole coordinated build shares one wall clock, so a runaway burns to a single kill.
  Commit-per-slice bounds the loss to the slice in flight.
- The re-review reads two effects, not one. Tier-split spend answers whether the premium
  shrank; ADR 0040's existing guardrails — review BLOCK rate and blocking findings, revise
  rounds, merge rate — are what would show the context-narrowing effect, and they must be
  read as a result here rather than only as a floor not to fall through.
- The coordinated pull request stays in one tool lineage, so the cross-tool reviewer
  remains genuinely independent and review is unchanged: one pull request, one review.
