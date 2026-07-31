# ADR 417 — A red check on the reviewed head is decided from GitHub, not from the verdict

- Status: Accepted
- Date: 2026-07-31
- Amends: [0004](0004-auto-merge-gate.md) (a third unwaivable gate joins cross-tool review and
  screenshot evidence, and it applies to every clean exit, not only the auto-merge arm);
  extends [0020](0020-build-review-under-partial-availability.md) (the revise round now has a
  finding the reviewer never wrote)

## Context

On 2026-07-31 a review posted `Outcome: clean.` on PR #412 at head `8bb3d30`, 23 minutes after
that head's `python` check had completed **red**. The redness was real and the change caused it:
the branch edited the bundled operator skill without re-recording its pinned digest, so
enrollment read every repository as drifted and exited non-zero — 14 tests, fixed by re-pinning
the digest and nothing else (`94788e7`, one line of `agentflow/capabilities.toml`).

Two conditions produced the miss. The review's local run had 97 failures unrelated to the change
— comparable reviews the same week ran at 6, so the noise floor swings by more than an order of
magnitude. And the technique used to see past that noise, differencing the failure set against
`main` in the same sandbox, reported "identical failure set". Those 14 failures were
branch-only; a differential run against genuinely-`main` sources would have surfaced them. So the
baseline run was not running the baseline, and the reviewer drew a confident conclusion from a
comparison that could not support it.

The authoritative answer was on the pull request the whole time, free to read, produced in a
clean environment. Nothing in the pipeline read it: agentflow's own profile is `reviewed`, and
settlement returns before its CI wait for any non-`autonomous` repository. Reading checks was
already normal practice for *some* reviews (#398 lists `gh pr checks` among its evidence; #414's
blocking finding **was** a red check) — an inconsistency in what a review consults, not a
capability it lacks.

## Decision

**A review may not finish clean while the exact reviewed commit has a red check.** The gate is
decided from GitHub at settlement, like the screenshot-evidence gate, so a reviewer cannot clear
it by not looking. It fires only on a clean verdict; a reviewer that reads the checks itself and
finds red records it blocking, and the ordinary revise path owns it.

**A caught red check opens a revise round, not a park.** A red build is the machine-fixable
failure the revise budget exists for, and #412 would have been fixed on the first round. The
round draws from the same two-round auto-revise cap as review findings, so a pull request that
has already spent both parks immediately. The revise is handed the failing check name and the
head SHA and nothing else — the builder has the repository and can rediscover the cause locally;
fetching CI logs is a second failure mode for a fact it can obtain itself. The backstop is
otherwise silent: no comment announces the overruled clean verdict, because the pull request is
about to heal itself and the park at the end of a spent budget is where the operator's attention
belongs.

**A check whose outcome is asking for a human parks immediately.** `action_required` means the
check itself wants an operator; sending a builder at it twice is guaranteed spend for a
guaranteed park.

**A round that cannot reproduce the failure pushes nothing and lets the re-read decide.** A
flaky check that has since gone green is the one case where "carry on" is correct, and re-reading
the check on the next settlement is free.

**The state mapping is named, across both vocabularies GitHub uses in one context list.**
Not-completed / `pending` / `queued` / `in_progress` / `expected` → pending. Check-run `failure`,
`timed_out` and status `failure`, `error` → red, revisable. `action_required` → red, park
immediately. `success`, `neutral`, `skipped`, `cancelled`, `stale` → not red. `cancelled` is
deliberately not red: a cancelled run recorded no verdict, and parking on one is a false human
interrupt. The console's coarse pipeline verdict calls `CANCELLED` failing; that divergence is
intentional and the console is not changed to match.

**The typed read carries the failing check names and the commit it was read on**, not a bare
four-valued verdict — both the revise finding and the park body must name the check, and a
second read to obtain names would defeat the point of the first.

**Pending and unreadable get opposite dispositions.** Pending must not block, because settlement
retries free and forever and consumes no attempt: a required check that never runs would strand
the record silently — no clean summary, no park, no ping. An unreadable answer defers, matching
what settlement's adjacent GitHub reads already do — but only on the exits that would otherwise
finish clean. The check read reaches GitHub by a route the neighbouring reads do not share, so a
malformed query is an independent failure mode; consulting it on every path would let one bad
query silently freeze every park in the fleet.

**The reviewer is also told to read the checks**, so the mechanical gate is a backstop rather
than the only reader, and told that a differential local run counts as evidence only if the
baseline is demonstrably the baseline — the rule the incident actually calls for. A failure the
change could plausibly reach is dismissed only with an individually named cause, and a run too
noisy to read is declared unusable rather than reasoned around.

## Consequences

- A red check now costs up to two builder sessions before it reaches the operator. That is the
  accepted price of not waking a human for a one-line digest re-pin.
- Persistent unreadability of the check status leaves clean pull requests silently unsettled —
  no comment, no ping — for as long as it persists. Accepted, and identical to what settlement
  already does when the comment thread or the pull request's facts are unreadable.
- The gate reads any red check on the head rather than consulting branch protection for which
  checks are *required*; that read needs admin rights the fleet will not have everywhere, and
  the superset needs no special permission.
- A clean summary published for an earlier, genuinely green head is edited in place and nothing
  withdraws it, so a head that was green, moved, and came back red can show a stale
  `Outcome: clean.` above a later park. That contradiction predates this change and is left to a
  follow-up.
- The sandbox that manufactures the local noise (#396, #386) is untouched. The gate must not
  wait on it.
