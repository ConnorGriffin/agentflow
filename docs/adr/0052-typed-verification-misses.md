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
  carry no miss; they can adopt the type when their park classes warrant it. Admission-side
  refusals (#365, #399) are the same disease in a different organ and are not covered here.
