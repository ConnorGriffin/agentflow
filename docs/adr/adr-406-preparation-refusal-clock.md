# ADR 406 — A preparation refusal nobody can clear eventually calls a human

- Status: Accepted
- Date: 2026-08-03

## Context

Every bounded thing in the pipeline ends. An attempt that fails spends one of three; a stage that
spends all three parks the work and pings the maintainer ([ADR 0028](0028-stage-scoped-continuations.md)),
through the one crash-safe handoff envelope ([ADR 0042](0042-durable-handoff-envelope.md)). That
is the contract: the machine either finishes or says it cannot.

A stage refused *before* its session starts falls outside it. Preparation runs ahead of the
permit reservation and the attempt charge, so a refusal costs nothing and the record simply
retries next cycle — forever, at zero of three attempts, reserving nothing, notifying no one.
That is how one untracked scratch file stalled a review for half an hour with the operator none
the wiser (#399). Issue #405 made those refusals *legible* — each names the check that said no
and quotes the values it read ([ADR 0052](0052-typed-verification-misses.md)) — but legible is
not the same as ended. A refusal that will never clear on its own still ends nowhere.

The obvious rule — escalate any refusal that persists — is wrong, and expensively so. Most
refusals are the machine's own problem and retrying really is the right answer: an unreachable
remote, a dependency sync that fails, a sibling session still holding a checkout, a payload a
crash corrupted. Paging a human for those trains them to ignore the pages.

## Decision

1. **Only a locally proved, human-clearable refusal is clocked.** A preparation check may declare
   the `stall` disposition only where it has read evidence, off the network, that the state
   cannot be cleared by anything the fleet is allowed to do. Today exactly one state qualifies: a
   registered checkout a human pinned with `git worktree lock`, holding work that therefore
   cannot be archived out of the way ([ADR 0050](0050-bounded-worktree-retention.md) refuses
   to disturb a locked checkout, deliberately). Everything else — network, provisioning, busy
   contention, unreadable payloads, unclassified failure — stays undisposed and escalates to
   nobody, however long it lasts. Classification lives beside the predicate that reads the
   evidence, never in a coordinator-owned table of git conditions.

2. **The clock is durable and keyed on the refusal's identity.** The record carries the typed
   check id being clocked plus a start and a last-observed epoch. A different check id is a
   different problem and inherits none of the first one's age. Two bare timestamps could not
   express that, and would silently carry refusal A's elapsed time into refusal B after a
   restart.

3. **Unobserved time is not stuck time.** Head-of-line ordering can leave a record unoffered for
   an hour and a half. An observation more than `STALL_OBSERVATION_MAX_GAP` (10 minutes) after
   the last one starts a fresh clock *before* any bound is evaluated, so a queue backlog can
   never park anything.

4. **Two bounds, ten minutes and an hour.** At ten continuous minutes the stage is called
   stalled: a cadence-limited daemon line and a row in its own published snapshot key, naming
   the typed refusal and when the clock started. At sixty it enters the same crash-safe handoff
   every exhaustion uses — one park comment, the existing notification behavior. To a maintainer
   "the machine cannot start this and will not be able to" is the same event as a spent budget.

5. **Every observed refusal is recorded, with its consecutive count.** This supersedes #405's
   write-only-on-change rule (see ADR 0052): age is only observable if every observation is
   written. The count and the clock advance in one optimistic write, and the count replaces
   Review's process-local failure counter, so the periodic breadcrumb survives a restart instead
   of re-announcing an hours-old refusal as if it were the second one. A speculative pool-move
   probe is admitted *unobserved* — it is not the record's turn at anything.

6. **The park copy claims nothing.** Every existing handoff describes a session that happened and
   fell short. A stage that never started has no such story, so Intake, the attack round, and a
   fresh Review each grew copy that states no session ran, no attempt was used and no budget was
   drawn down; names what is pinned and where; and puts the resume command *after* the fix. The
   durable hold reason carries a distinct prefix so a crash-resumed handoff composes the same
   words and the once-only comment proof still holds.

7. **The gate side is excluded.** Weekly pacing, five-hour headroom, permits, and priority yields
   are waits with a reset time, not refusals a human can clear. No admission-gate branch produces
   a `stall` disposition, so none of them can start a clock — and no pre-launch gate metadata is
   made durable for an attempt that never began.

8. **Stalled records publish in their own key.** The live board is a projection of *running*
   records and every pool's running count derives from it, so a record that has started nothing
   and reserves nothing must never appear there — nor folded into the refusals, which is the
   ordinary condition half the fleet is in at any moment. Rendering it in the console is out of
   scope here and remains subject to `/ui-craft lock`.

## Consequences

- The one class of stall an operator can actually fix now ends in a park and a ping instead of
  silence. The pinned checkout itself is never touched: clearing it is the whole ask.
- Escalation coverage is deliberately narrow — one git condition. Widening it means a check
  proving, locally, that it has found another state no retry can clear; the disposition is opt-in
  precisely so that widening is a decision somebody makes rather than a default that drifts.
- A refused record costs one durable write per cycle it is observed, where #405 cost one per
  distinct refusal. That is a bounded cost on records that are already doing nothing, and it buys
  the only thing that makes escalation possible.
- A refusal whose subject does not resolve to an issue or PR number is never clocked. A park with
  nothing to post on proves nothing and would freeze the record pending forever — worse than the
  waiting it replaces — so those keep waiting, keep their claim, and keep retrying.
