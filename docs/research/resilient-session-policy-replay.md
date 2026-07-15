# Resilient-session continuation and admission replay

Research for [Replay continuation and admission policy against historical
sessions](https://github.com/ConnorGriffin/agentflow/issues/94), captured
2026-07-15.

## Conclusion

The settled policy passes every required replay scenario. Static per-pool permits stop
same-snapshot launch stampedes without giving up the supported short-stage overlap;
eligible continuations cannot be bypassed; every actual provider start is charged to a
bounded stage budget; and outcome-first, idempotent finalization prevents duplicate work,
lost claims, and repeated human handoffs. **No policy gap or new Wayfinder decision was
found.**

This is a replay of [ADR 0028](../adr/0028-stage-scoped-continuations.md),
[ADR 0029](../adr/0029-static-per-pool-admission.md), and
[ADR 0030](../adr/0030-session-coordinator-seam.md), not evidence that the current
scheduler already implements them. ADR 0030 explicitly leaves the coordinator and its
crash-recoverable provider-start handshake as implementation work.

## Reproducible model

The executable policy oracle is
[`tests/test_resilient_session_policy_replay.py`](../../tests/test_resilient_session_policy_replay.py).
It exposes the accepted coordinator surface—idempotent submission, scheduler cycles,
provider-start recovery, and outcome-first finish—without choosing production storage or
a handshake mechanism. Reproduce its 28 cases with:

```sh
env UV_CACHE_DIR=/tmp/agentflow-uv-cache AGENTFLOW_STATE=/tmp/agentflow-state \
  uv run pytest -q tests/test_resilient_session_policy_replay.py
```

The model uses only synthetic root and descendant identifiers. Representative roots carry
the accepted admission demand from aggregate completed-session cells; they contain no
prompt, transcript, repository content, credential, or historical subject identifier.
This follows the study's root-family accounting and aggregate-only method
([historical demand, “Reproducible method”](./historical-session-demand.md#reproducible-method)).

For each pool, the reducer applies these rules in order:

1. Derive permits in use from `running` root records; a root's descendants do not reserve
   again ([ADR 0030, “Running records are the permit ledger”](../adr/0030-session-coordinator-seam.md#running-records-are-the-permit-ledger)).
2. Consider eligible continuations by `eligible_at`, creation time, and stable identity,
   before cold roots. The first blocked continuation stops that pool's admission for the
   cycle; running work is not preempted
   ([ADR 0029, “Continuations have strict admission priority”](../adr/0029-static-per-pool-admission.md#continuations-have-strict-admission-priority-not-preemption)).
3. Require headroom/windows, operator pacing, machine ceiling, stage cap, and permit fit.
   Atomically reserve the entire matrix demand before starting
   ([ADR 0029, “Permits compose”](../adr/0029-static-per-pool-admission.md#permits-compose-with-rather-than-replace-existing-gates)).
4. Reconcile the provider-start fact as exactly `not_started` or `started`. Only `started`
   consumes one of the stage's three attempts
   ([ADR 0030, provider-start handshake](../adr/0030-session-coordinator-seam.md#running-records-are-the-permit-ledger)).
5. After the provider family ends and observations are durable, classify outcome first,
   then permanent/bail, then recoverable/incomplete/unknown. Missing outcomes wait while an
   attempt remains and otherwise produce one stage-native hold
   ([ADR 0028, “A stage completes only when its outcome exists”](../adr/0028-stage-scoped-continuations.md#a-stage-completes-only-when-its-outcome-exists)).

## Replay results

| Case | Synthetic replay and result | Executable evidence |
| --- | --- | --- |
| Reviewed matrix and fallback | Every accepted Build row is monotone by effort, every code-writing row demands at least three, an unknown row on a known pool reserves all five, and an unknown pool is inadmissible because it has no ledger to charge. Fallback therefore cannot leak demand into the other pool. **Pass.** | `test_reviewed_matrix_is_monotone_conservative_and_pool_scoped` |
| Same healthy snapshot | A four-permit Claude build, one-permit intake, and five-permit build see the same headroom fact. Atomic ordering admits `4 + 1` and leaves the five-permit root waiting with zero attempts; another ordering may admit `5` alone, but no ordering exceeds five. This directly replays the observed three-root burst that began within 1.2 seconds while the proxy still showed more than 70% free ([known limit episode](./historical-session-demand.md#claude-headroom-at-the-known-limit)). **Pass.** | `test_same_snapshot_historical_stampede_is_bounded_atomically` |
| Two code writers, one pool | Every writer demands at least three, so `3 + 3 > 5`: exactly one starts and the other retains zero attempts. **Pass.** | `test_two_writers_cannot_share_one_five_permit_pool` |
| Useful short-stage overlap | A four-permit Claude writer and one-permit Claude review fill the pool; a second writer cannot enter. Demand four can share only with a demand-one Intake or Claude Review; demand five remains exclusive, and a Codex Review demands two ([reviewed matrix](../adr/0029-static-per-pool-admission.md#the-reviewed-matrix)). **Pass.** | `test_near_exclusive_writer_keeps_useful_short_stage_concurrency` |
| Continuation priority and head-of-line blocking | A live one-permit root leaves four free. The oldest eligible five-permit continuation cannot fit, so a later continuation and cold intake do not bypass it. The live root is not preempted; when it ends, the old continuation starts first. Its waiting claim and attempt count are unchanged. **Pass.** | `test_continuation_head_of_line_blocks_bypass_without_preempting_live_work` |
| Tool lineage and movable read-only work | A Codex code-writing continuation waits when Codex is closed even if Claude is open. A read-only Review may move when its review-safety rules allow and is recalculated from Codex demand two to Claude demand one; a same-tool review may finish but cannot auto-merge ([ADR 0028, “Code-writing continuations preserve tool lineage”](../adr/0028-stage-scoped-continuations.md#code-writing-continuations-preserve-tool-lineage)). **Pass.** | `test_code_lineage_is_pinned_while_read_only_continuation_can_move` |
| Capacity deferral | Closing any one of headroom, machine capacity, stage capacity, or operator pace leaves the record `waiting` with no permit and no attempt. A permit-fit rejection does the same. Preparation failure is likewise before this boundary by policy. **Pass.** | `test_every_independent_gate_defers_without_permits_or_attempts`; `test_permit_deferral_also_consumes_neither_permit_nor_attempt` |
| Future reset eligibility | A typed capacity ending records its future reset. Before that instant the continuation is ineligible and cold work may proceed; at the reset it enters the continuation queue with its existing attempt and claim. No inline restart or hot loop occurs. **Pass.** | `test_future_capacity_reset_controls_eligibility_without_hot_looping` |
| Crash during provider start | After an atomic reservation, recovered `not_started` returns to `waiting`, releases the reservation, and preserves attempt zero. Recovered `started` consumes exactly one attempt even when reconciliation observes it repeatedly and retains demand while its process family is alive. **Pass.** | `test_crash_recovery_distinguishes_not_started_from_started_atomically` |
| Recovered provider families and descendants | A recovered live Codex Review root plus four synthetic descendants retains one two-permit root reservation, not five reservations. When the family is proven dead, classification releases both permits and returns the incomplete stage to `waiting` with attempt one and its claim intact. This matches the historical method, which charged 176 descendants across 84 of 149 observed Codex roots to their root families ([historical demand method](./historical-session-demand.md#reproducible-method)). **Pass.** | `test_live_recovered_family_keeps_one_root_reservation_and_dead_family_releases_it` |
| Recoverable, incomplete, unknown, and clean-without-outcome endings | Each started ending with no required outcome returns to scheduler-owned `waiting` while budget remains; it does not recurse from the failed stack. Codex's untyped `exec --json` failure therefore remains bounded `unknown` unless the adapter obtains typed companion facts ([provider signal conclusion](./provider-interruption-signals.md#conclusion)). **Pass.** | `test_non_terminal_endings_wait_when_the_outcome_is_missing` |
| Success and permanent outcome | A verified stage outcome completes even when provider facts also say permanent failure. Without an outcome, a typed permanent condition creates the stage-native hold immediately. **Pass.** | `test_outcome_precedence_permanent_hold_and_exhaustion_all_terminate_safely` |
| Exhaustion and exactly one handoff | Three `started` unknown endings consume attempts one through three. The third first enters non-eligible `waiting` with its claim intact. Only durable handoff proof permits `held` and claim release. A simulated daemon crash after proof reloads the persisted record, completes `held`, and produces neither a second handoff nor a second notification. All six logical stages use their native boundary: Intake/Build → `needs-grilling`, Mockup → `needs-mockup`, and Review/Revise/Respond → parked PR ([ADR 0028, “Exhaustion produces one stage-native handoff”](../adr/0028-stage-scoped-continuations.md#exhaustion-produces-one-stage-native-handoff)). **Pass.** | `test_outcome_precedence_permanent_hold_and_exhaustion_all_terminate_safely`; `test_exhaustion_creates_exactly_one_stage_native_handoff`; `test_handoff_proof_survives_crash_without_duplicate_handoff_or_notification` |
| Successful claim transfer | A completed stage keeps its claim until the next `waiting` record is durable. Finalization then transfers ownership and retires the completed record. At a durable external boundary, proof releases the claim without creating another stage. **Pass.** | `test_completed_stage_transfers_claim_before_retirement_or_releases_at_boundary` |
| Duplicate submission | Submitting the same logical stage identity twice returns one record. Combined with waiting claim retention and outcome-first startup reconciliation, concurrent cycles cannot create duplicate owned work. **Pass.** | `test_submission_is_idempotent_so_one_logical_stage_cannot_duplicate_work` |
| Bounded termination | One logical stage has at most three provider starts. Daemon restarts, elapsed time, capacity refresh, and source edits do not reset the count; a genuinely new stage target or explicit human re-entry does ([ADR 0028, “One continuation record and budget per logical stage”](../adr/0028-stage-scoped-continuations.md#one-continuation-record-and-budget-per-logical-stage)). Capacity can wait indefinitely without spinning or consuming resources; under eventual admission or a typed permanent fact, the stage completes or holds after at most three starts. **Pass.** | Attempt and terminal assertions across the crash, missing-outcome, and exhaustion cases |

The gate cases also confirm composition: free permits cannot open a closed headroom,
machine, stage, or pacing gate, while healthy headroom cannot bypass the atomic permit
budget. A cold root may use another normally eligible pool; a pinned continuation may not.
The by-hand path is subject to the same coordinator rules
([ADR 0030, coordinator interface](../adr/0030-session-coordinator-seam.md#the-seam-sits-at-one-logical-stage-session)).

## Evidence limits

The demand sample spans six changing days and 309 completed classified roots (164 Claude,
145 Codex). Several cells are deliberately sparse: current-model Claude deep-low and
deep-extra builds, Codex deep builds, both revise families, Claude Respond, and Codex
Mockup have fewer than five roots; there are no current-model Codex Respond, Claude Mockup,
or several high/extra build cells. The admission matrix handles these gaps conservatively
with monotone heavy bands and exclusive fallback rather than pretending the sample supports
fine-grained weights ([aggregate evidence](./historical-session-demand.md#aggregate-evidence)).

Only one provider-limit episode was observed. It proves that cumulative headroom is not an
atomic reservation, not the long-run precision of any particular demand value. Provider
crashes, daemon death during the start handshake, repeated reconciliation, and permanent or
unknown classifications are necessarily synthetic policy cases. The interruption study
establishes the typed provider facts but does not supply a frequency distribution
([reliable signals](./provider-interruption-signals.md#reliable-signals)).

## Current implementation boundary

The narrow scheduler paths confirm why this result validates policy rather than current
behavior:

- The current [`Governor`](../../agentflow/dispatch.py#L45-L99) holds one machine slot for
  a whole build/review/revise chain and does not account for per-pool demand.
- The [balancer](../../agentflow/balancer.py#L191-L201) chooses from cumulative headroom;
  it does not atomically reserve that fact.
- Provider adapters currently return only Boolean success plus final text
  ([runner launch interface](../../agentflow/runner.py#L324-L376)), not durable typed
  observations or a `started`/`not_started` fact.
- The live-session file fails soft and is reaped as a console projection
  ([`live.py`](../../agentflow/live.py#L54-L102)); it is not continuation ownership or a
  permit ledger.
- Build claims are currently released when the chain returns
  ([`loop.py`](../../agentflow/loop.py#L512-L540)), whereas a waiting continuation must keep
  its claim.

These are the already-settled ADR 0030 migration targets, not newly discovered policy
decisions. The replay therefore adds no dependency before the later implementation-slicing
work.
