# 0045 — Intake stays all-deep; standard-first Intake rejected

- Status: accepted
- Date: 2026-07-20
- Ticket: [#228](https://github.com/ConnorGriffin/agentflow/issues/228) (wayfinder map [#226](https://github.com/ConnorGriffin/agentflow/issues/226)); unblocks the Intake step-up question in [#232](https://github.com/ConnorGriffin/agentflow/issues/232)
- Evidence: [PR #252](https://github.com/ConnorGriffin/agentflow/pull/252) (closed, not merged) and the live pilot recorded in its [closing write-up](https://github.com/ConnorGriffin/agentflow/pull/252#issuecomment-5018263373)

## Context

Every Intake runs on the deep model (Opus/Sol) today — `intake_submission` hardcodes
`complexity="deep"`, so issue shape never changes the tier. Map #226 named all-deep Intake as
one of its two biggest spend levers (48 Opus Intake sessions at $42.23). Wayfinder #2 (#228)
asked whether **standard-first Intake** — start on the cheap model, step up to deep only on
typed triggers — could cut total Intake spend without degrading route/brief quality.

The prototype (PR #252) shipped the offline replay rig, a 47-issue pinned corpus, and the
reusable escalation design, but it had **no live responder**: no arm had ever actually run, so
its "finding" was that it couldn't produce numbers. A live responder was then built (it reuses
the production launch surface — `ClaudeRunner.structured_argv` plus the read-only Intake
profile — and parses the session's structured decision and normalized usage) and a small live
pilot was run. Two results decide the question.

## Decision

**Keep Intake all-deep. Do not adopt standard-first Intake.** The decision threshold from #228
was: adopt the step-up *only if* it materially reduces median total Intake spend with no
meaningful quality degradation. It fails on the spend axis alone.

1. **Standard-first does not cut headroom** — the prepaid resource ADR 0040 optimizes (dollars
   are the cross-tool comparison signal only, never the target). On the live pilot, all-standard
   ran ≈105% and standard-first + typed escalation ≈97% of all-deep headroom (the latter within
   noise). Dollars dropped ~30% on the cheap tier, but that is not the objective. The cause is
   structural: **Intake spend is dominated by grounding output (weighted 5× in the headroom
   formula), not model tier** — a cheaper model reads the same code and writes a
   similar-length brief, so swapping it barely moves the rationed resource.

2. **The quality axis cannot be measured with this corpus.** Every corpus entry pins the
   grounding checkout to one recent SHA, but the reference labels are the historical all-deep
   decisions made when each issue was *originally* triaged, against older per-issue code.
   Grounding-SHA ≠ label-SHA, so a live session grounds on today's code and is scored against a
   months-old decision. On the pilot every arm scored 0/N on route-match — the corpus failing to
   score itself, not the models failing.

Because the spend axis already fails the threshold, the unmeasurable quality axis does not need
resolving to reach a decision.

## Alternatives

- **Adopt standard-first on the ~30% dollar saving.** Rejected: dollars are explicitly not the
  optimization target (ADR 0040), and the resource that is — headroom — does not move.
- **Rebuild the corpus and re-run all arms before deciding.** Deferred, not done: the spend axis
  fails on its own, so there is no case to adopt even with a valid quality measurement. Revisit
  only if the spend picture changes (e.g. Intake grounding stops dominating the token mix).
- **Escalate on model self-signal only, drop deterministic direct-deep triggers.** Moot under
  this ruling; the direct-deep triggers were also over-firing on this repo's corpus (≈79% of
  issues), which would have collapsed the escalation arm into all-deep anyway.

## Consequences

- Intake stays on the deep model; `intake_submission`'s hardcoded `complexity="deep"` is
  correct and stays. #232 takes this as a **don't-adopt** for the Intake step-up lever and can
  lift it off its blocked list.
- Map #226's Intake-spend lever is closed as not worth pursuing on current evidence; the other
  lever it named (Opus Build share) is unaffected by this decision.
- The escalation **design** (the typed `EscalationReason` set and the bounded evidence
  carry-forward that refuses a blind rerun) and the **live responder** are shelved, not deleted.
  They are reusable if a future experiment first earns a **scoreable** corpus — ground-truth
  re-derived against a single pinned SHA (the blind-scorer pass #228 left open), or each issue
  grounded at its own triage-time SHA. Reviving the experiment is that corpus rebuild, not a
  flag flip.
- Lesson for future spend experiments: an offline replay rig is only as good as its ground
  truth. A corpus whose grounding target and scoring labels come from different code states
  cannot validate a live run, however byte-stable the replay is.
