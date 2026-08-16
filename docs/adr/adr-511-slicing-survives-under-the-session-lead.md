# ADR 511 — Slicing survives under the session lead

- Status: Accepted
- Date: 2026-08-16
- Ticket: [#511](https://github.com/ConnorGriffin/agentflow/issues/511)
- Supersedes: [ADR 466](adr-466-coordinated-build-routing-gate.md) (the separately-gated
  route and its fixed cheap/frontier slice-model pair), and the route-switch revert condition in
  [ADR 468](adr-468-slice-ledger-and-revert-condition.md)
- Constrains: [ADR 464](adr-464-slice-runs-in-session.md) (its in-session slice shape now
  converges with the session lead), [ADR 465](adr-465-work-order-is-the-non-self-scoping-brief.md)
  (the retained non-self-scoping brief), [ADR 468](adr-468-slice-ledger-and-revert-condition.md)
  (the retained commit-per-slice ledger), and
  [ADR 498](adr-498-capability-routed-session-led-dispatch.md) (the implemented session lead and
  provenance-stamped capability ladder)

## Context

Wayfinder map [#463](https://github.com/ConnorGriffin/agentflow/issues/463) proposed slicing a
slice-bearing work order beneath a coordinator. ADRs 464, 465, 466, and 468 respectively settled
the in-session shape, the non-self-scoping brief, a separate route gate with a fixed cheap/frontier
model pair, and the commit ledger with a switch-based re-review.

ADR 498 was accepted on 2026-08-04 and is implemented and running. Every Build and Revise launches
a session lead that delegates exploration, implementation, and fixes to workers, verifies the
result, and ships only verified work. `agentflow/coordinated_build.py:57` renders
`routing.session_lead_instructions`, and line 65 submits the stage with `session_lead=True`.

The previously proposed slice decomposition, commit-per-slice ledger, and slice-bearing work-order
gate are not implemented: a search across `agentflow/` finds none of them. This record settles
their direction; it does not describe existing slicing behavior.

The decisions now converge rather than compete. The session lead is the coordinator in ADR 464:
when a lead decomposes work into slices, those slices run as subagents inside that lead's own
session and land on one pull request. ADR 465 still gives those slices a work order that forbids
self-scoping. What no longer survives is the separately-gated route and its fixed model-choice
rule. ADR 498's provenance-stamped, benchmarked capability ladder is measured and running.

Map #463's audit remains useful as measurement: cost is linear in session length and concentrates
in deep builds over 60 turns — 40 attempts and $375.28 of a $1,464.02 window. That is an input to
routing recalibration in [#277](https://github.com/ConnorGriffin/agentflow/issues/277) and the
reason slicing remains worth having under the lead, not a justification for a second dispatch route.

## Decision

**Slicing survives and layers under the session lead.** ADR 464's in-session answer is the general
shape: the lead is the coordinator, and any slices it creates run as subagents in its own session,
landing on one pull request. ADR 465 is retained: a lead that slices uses a work order that does
not allow a slice to re-scope itself.

ADR 466 is superseded. Since ADR 498 every Build and Revise already runs under a session lead, a
separate gate deciding whether to delegate has nothing left to decide. ADR 466's fixed
cheap/frontier slice-model pair is also superseded: slices use ADR 498's provenance-stamped,
benchmarked capability ladder.

ADR 468 is partially retained. Commit-per-slice remains the ledger and measurement surface for
sliced work. Its revert condition tied to the gated route's switch is retired: there is no switch
left to flip.

The dated re-review in [#469](https://github.com/ConnorGriffin/agentflow/issues/469) is answered
as **TUNE**: slicing is kept and converged into the session lead; the separate gate and fixed
model-pair rule are dropped.

## Consequences

- ADR 498 remains the implemented Build/Revise dispatch decision. No behavior changes here.
- In-session slicing, the commit-per-slice ledger, and the slice-bearing work-order mechanism are
  not implemented today. Implementing in-session slicing under the lead remains open work; this
  record clears the way but does not deliver it.
- The map #463 deep-build measurement remains an input to #277's routing recalibration and explains
  why slicing is worth retaining under the lead.
