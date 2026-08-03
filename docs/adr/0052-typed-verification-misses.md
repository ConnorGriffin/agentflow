# ADR 0052 — Typed verification misses and prior-attempt push provenance

- Status: Accepted
- Date: 2026-07-31

## Context

Every stage completes only when its required outcome is durable (ADR 0028 outcome-first), and
every stage verifier answered that question with a bare bool. A verifier with eight ANDed
conditions therefore had eight silent ways to say no, and the coordinator recorded only "budget
exhausted" when the attempts ran out. Fleet history shows what that cost: of the 64 lifetime
holds, 50 exhausted their full attempt budget first, and in 26 of the 36 parked-PR cases the
session's own final message claimed the work was already done. The machine kept rejecting proof
of finished work and could not say why; each park was re-diagnosed by hand from transcripts and
patched point-wise (#330, #334–#336, #340, #341, #345, #347, #348, #361, #363, #370).

One contradiction was an outright park factory. The review prompt orders a continuation reviewer
to report `pushed_sha: ""` when it pushed nothing itself — the honest statement when an *earlier
attempt of the same logical review* pushed the fixes. Both verdict parsers rejected exactly that
statement ("changed final head has no push provenance"), so no honest continuation could ever
state a parseable verdict: it re-reviewed, said PASS, was scored incomplete, and the PR parked
after three attempts (the fix-axis parks of late July, e.g. PR #346).

## Decision

1. **Verification answers are typed.** A verifier returns a `Verification` — truthy on success,
   otherwise carrying the first failed check's stable id and one sentence of live values.
   Truthiness preserves the old contract, and legacy bool verifiers stay valid; the router and
   adapter pass the answer through unchanged. The review, revise, respond, and converse
   verifiers name every one of their conditions.

2. **The miss is named everywhere a human or a fresh session looks.** The coordinator persists
   the last miss on the record, stamps it into each attempt's telemetry entry, appends it to the
   exhaustion and no-new-recovery hold reasons, adds it to the recovery envelope handed to the
   next session ("satisfy that check; work behind it may already be durable"), and the park
   comment prints it as its first check line.

3. **A prior attempt's push is provable provenance.** Both verdict parsers accept a moved final
   head with empty `pushed_sha` when the caller proves that head durably (`owned_heads`). Review
   verification derives the proof from the retained detached checkout owning the stated final
   head, persists it on the record (`review_prior_push`), and settlement re-parses the captured
   verdict against that durable fact — never a checkout that may be gone. A head the checkout
   does not own (a third-party push) stays rejected, and in-payload fixes still require
   in-payload provenance.

## Consequences

- A parked PR now states which conjunct stopped it. New proof-recognition failures become a
  telemetry field to read, not a transcript archaeology session, and recurring miss ids point at
  the verifier condition to fix next.
- Honest fix-axis continuations complete instead of parking; the strictness that matters
  (exact-SHA anchoring, third-party pushes rejected, no-op pushes rejected) is unchanged.
- The repair envelope tells the next session exactly which proof to produce, so a repair can
  actually repair instead of re-verifying blindly and burning the budget.
- Verifiers not yet converted (build, intake, mockup, research) keep bool answers and simply
  carry no miss; they can adopt the type when their park classes warrant it.
- Admission-side refusals (#365, #399) were named here as the same disease in a different organ.
  **Issue #405 shipped that half against this same contract**, and it is worth stating what the
  preparation side needed that the verification side did not:
  - Every stage's `prepare` now answers with a `Verification`. The adapters and the router pass
    it through exactly as they pass a verify answer, and composed preparations (`rebuild the
    checkout` *and* `prove the claim`) rely on Python's `and` yielding the first falsy operand,
    so the half that refused survives instead of collapsing to a bare `False`.
  - `Verification` gained exactly one field, `expected`. A verification miss is always something
    to look at; a preparation refusal is not — a checkout held by a live sibling session is the
    fleet working as intended. The collaborator marks its own benign refusals, so the coordinator
    never keeps a list of blessed check ids. An expected refusal is still published; it only
    stops the repeat breadcrumb.
  - A miss is a fact about a finished attempt, so `verify_miss` is written once and read later.
    A refusal is a fact about *now*, so the record carries at most one and the coordinator clears
    it the instant preparation succeeds — before the capacity gate runs, which may then put its
    own reason in the cleared slot. It is written only when it changes, so a stage refusing
    identically every cycle costs one durable write, not one per tick.
  - Refusals publish on their own snapshot key. The live board is a projection of *running*
    records and the pool counts derive from it, so a waiting-and-refused record must never
    appear there.
