# ADR 468 — The commits are the slice ledger, and the switch is the revert

- Status: Accepted
- Date: 2026-08-02
- Ticket: [#468](https://github.com/ConnorGriffin/agentflow/issues/468)
  (wayfinder map [#463](https://github.com/ConnorGriffin/agentflow/issues/463))
- Constrains: [ADR 464](adr-464-slice-runs-in-session.md) (bounded return, commit-per-slice),
  [ADR 465](adr-465-work-order-is-the-non-self-scoping-brief.md) (the gap rule, the one-line
  summary), [ADR 0043](0043-recovery-state-before-replay.md) (recovery envelope),
  [ADR 0040](0040-spend-per-success-measurement-contract.md) (the guardrails the re-review reads)

## Context

ADR 464 settled that a slice's return to the coordinator must be **bounded** — a result, never a
transcript — and left what it contains to this ticket. ADR 465 added one required element: a
one-line summary each finished slice contributes to the next slice's context. The rest is open,
and it is load-bearing, because the premise of the whole shape is that the coordinator stays
thin. The coordinator's window is the one context that grows across a coordinated build.

The audit corrects a tempting misreading of *why* it must stay thin. Cost is **linear** in turns
(`$ = 0.063 × turns^0.99`, flat from 20 to 160 turns), so a long coordinator is not itself
expensive and there is no context-size cost cliff to stay under. Keeping it thin is about
**judgment quality and turn inflation** — a coordinator reasoning over an ever-widening window
makes worse slicing decisions, and re-grounding inflation is the parameter map #463 identified
as able to zero the saving at 1.6×.

The second half is the revert condition, and ship-first is only defensible if it is written down
before shipping rather than argued afterwards. Two facts constrain it. The modelled saving is
**23–50%** on the gated cohort against ADR 0040's "materially reduces spend" bar of **≥20%** —
so the floor of the model sits barely above the bar, and a result between the two is the likely
case, not an edge case. And the cohort may not fill: ADR 466 switches the route on for two cells
in one repository, and the equivalent historical cell produced **11 verified stages across 13
days**, only just clear of ADR 0040's ≥10 quantitative minimum.

## Decision

### What a slice hands back

**A finished slice returns exactly four things:** the one-line summary ADR 465 requires, the
commit it left on the branch, whether its named invariant tests passed, and a bounded list of
unresolved concerns. Nothing else — no transcript, and **not the diff**, because the commit
already holds it and re-importing it into the coordinator's window is the context growth this
rule exists to prevent.

**The coordinator keeps the work order's durable half, the slice list, and one line per finished
predecessor.** It re-reads nothing per slice; reading the repository is the worker's job, done
in the worker's own fresh window.

**The coordinator orchestrates and never writes code.** It may merge, split or reorder the slices
it was given (ADR 465), but every edit to the repository goes through a worker — including a
one-line fix it can see the answer to. This is the invariant the whole shape rests on: a
coordinator that edits accumulates file contents, diffs and test output in its window and becomes
exactly the monolithic deep builder it was introduced to replace. The temptation is strongest for
trivial fixes, which is why the rule admits no size exception.

**When a slice reports a fact that contradicts the plan**, the coordinator follows ADR 465's gap
rule at build scope: if the contradiction is a repository fact it can verify, it re-slices within
what the work order already named and continues; if it is a domain or intent fact, or if it
invalidates the work order's grounding, it **parks the build**. It never invents work the order
did not name in order to route around the contradiction — that is re-authoring, which ADR 465
forbids.

### The slice ledger is the commit history

**The per-slice commits are the ledger. No parallel record is kept.** ADR 464 already requires
each finished slice to be committed before the next starts; each commit names its slice in the
message, so the ledger is reconstructible from `git log` in the retained worktree.

ADR 0043's recovery envelope for an interrupted coordinated build therefore carries what it
already carries — attempt number, missing outcome, retained worktree — and the slice ledger comes
free from the worktree itself. A continuation reads the commits to learn which slices landed and
resumes at the first one that did not. Nothing about the coordinator's in-session state needs to
survive, which is what makes a coordinated build survivable at all: the session is disposable and
the branch is the durable record.

A coordinator-authored ledger was the alternative and is rejected below.

### The revert condition

Read on **2026-08-16** by the dated re-review ([#469](https://github.com/ConnorGriffin/agentflow/issues/469)),
against the switched-on cells only:

- **The guardrails are ADR 0040's, unchanged:** merge rate, hold/park rate, review BLOCK rate and
  blocking findings, revise rounds, retries per completed stage, and time-to-merge. **ADR 0040's
  bar is the bar** — at most one adverse guardrail event beyond control per 10 trials, and median
  time-to-merge within +25%. No new threshold is invented here.
- **Success is ADR 0040's definition:** at least 20% lower median headroom spend per verified
  stage, with no increase in spend per merged issue.
- **A saving below 20% with clean guardrails tunes, it does not revert.** This is the explicit
  answer to "what if it lands at 15%". ADR 464 made context narrowing a **co-equal** motivation
  with cost, and that effect shows up in the guardrails — fewer blocking findings, fewer revise
  rounds — not in the spend line. Reverting a route that is delivering better first-pass work
  because it saved 15% instead of 20% would be reading half the instrument. Tuning means
  narrowing or widening the switched-on cells and adjusting the allowed slice-model set, both
  reviewed configuration edits.
- **Revert is triggered by exactly two conditions:** any guardrail degrading past ADR 0040's bar,
  or a saving at or below zero. Either one reverts; a saving between zero and 20% with clean
  guardrails does not.
- **Revert is mechanically the switch.** ADR 464 already settled this: turning the cells off in
  committed fleet configuration stops coordinated builds happening, with no code removed and no
  migration. It is a reviewed pull request like any other configuration change.
- **"Extend, do not judge" is an allowed and expected verdict, declared in advance.** If the
  switched-on cells hold fewer than ADR 0040's ten verified coordinated stages on 2026-08-16, the
  re-review records that the cell is too thin to judge and extends to the next monthly
  recalibration pass rather than ruling on a small sample. Given the cohort's audited rate this is
  a likely outcome, not a fallback — and a re-review that judged a five-stage cell would be
  manufacturing a verdict.

## Alternatives considered

- **A coordinator-authored slice ledger persisted beside the record.** Rejected. It is a second
  source of truth for something the commit history already states, it can drift from the branch
  whenever a slice half-lands, and it must itself survive a crash — reconstructing durable state
  the worktree already holds. The charter's deletion test settles it: removing the ledger moves no
  complexity, because `git log` answers the same question.
- **Returning each slice's diff to the coordinator** so it can review the work before continuing.
  Rejected: it re-imports the exact context slicing exists to shrink, and the diff is reviewed
  once, properly, by the cross-tool reviewer on the single pull request.
- **Returning the worker's transcript on failure only.** Rejected. The failure path is where the
  coordinator's window is already largest, and a failed slice fails the build (ADR 465), so
  nothing downstream needs the transcript. The retained worktree and the provider's own session
  log are where a human looks.
- **Letting the coordinator make trivial edits itself.** Rejected. There is no principled size
  boundary, every exception is locally reasonable, and the accumulated effect is the monolithic
  builder this shape replaces. A one-line slice is cheap; a thick coordinator is not.
- **Inventing a coordinated-build-specific guardrail set.** Rejected. ADR 0040's guardrails were
  chosen to detect quality degradation from any cause, and a bespoke set would let the route be
  judged on criteria it was designed to pass.
- **Reverting on any saving below the 20% bar.** Rejected as reading half the instrument, per the
  co-equal-motivation finding above.
- **Extending the window automatically until the cohort fills.** Rejected: it turns a dated
  re-review into an open-ended one and removes the forcing function that makes ship-first
  accountable. The extension is a recorded verdict a human reads, not an automatic reschedule.

## Consequences

- **A coordinated build's durable state is its branch.** The session holds nothing that must
  survive it, so continuation after a wall-clock kill, a crash or a redeploy reduces to reading
  the worktree — the same path ADR 0043 already implements.
- **The commit message format becomes load-bearing.** A slice commit that does not name its slice
  makes the ledger unreadable to a continuation. This is a real coupling, accepted because the
  alternative is a parallel record that can disagree with the branch.
- **The re-review has a defined readout and a defined non-answer.** The runnable readout attached
  to [#467](https://github.com/ConnorGriffin/agentflow/issues/467) reproduces the map's audit to
  the cent and already reports intake spend beside build spend, which ADR 465 requires so a
  build-stage saving cannot be an intake-stage cost in disguise.
- **The per-model breakdown ([#472](https://github.com/ConnorGriffin/agentflow/issues/472)) must
  land before any cell switches on.** Without it a coordinated build files its whole attempt under
  the coordinator's dial and reads as an all-deep build — silently wrong rather than visibly
  missing — and the tier-premium question, which is the map's central one, has no answer.
- **A likely first verdict is "extend".** Declaring that in advance is the point: it stops a thin
  cell from being read as either a success or a failure, and it keeps the route switched on
  through a second window without anyone having to argue for an extension after the fact.
