# ADR 0021 — Dispatch dedup: claim an issue before building (and before triaging) it

- Status: Accepted
- Date: 2026-07-10

## Context

[ADR 0009](0009-collision-safety.md) makes *merges* collision-safe (rebase + serialize +
sibling review). It does **not** stop a *duplicate dispatch*: an issue stays
`ready-for-agent` and open while its PR is in review, so the loop re-picks it every cycle
— re-running a build on the live branch, or opening a second PR on a different tool. An
open PR is only a *lagging* signal: between dispatch and PR-open (minutes of build) the
issue has no PR at all, so a concurrent dispatch (the two-pool fan-out, or a manual agent
racing the daemon) fires a duplicate.

## Decision

**Claim the issue before the build runs, and skip anything claimed or already in flight.**

- `run_once` applies the label **`agentflow:building`** to the issue *before*
  `builder.build`, and releases it in a `finally` (whatever the outcome).
- `_next_ready_issue` skips an issue that carries the claim **or** has an open
  `agentflow/<tool>/issue-N-*` PR. The **claim covers the no-PR-yet build window**; the
  **open-PR check covers the parked-in-review window**, which outlives the claim (the
  claim is released when `run_once` returns, but a `reviewed` PR stays open for days).
- **`reclaim_claims`** drops a claim with no open PR every cycle — self-healing a claim
  stranded by a crash or a swallowed `gh` release. A stale claim is fail-safe (the issue
  is *skipped*, never duplicated).
- **The daemon heartbeats its lock** each cycle so a healthy daemon is never seen as
  stale. Single-instance is load-bearing: without it a second daemon's reclaim would clear
  a live claim and duplicate the build.

**The same claim guards intake ([ADR 0016](0016-intake-stage.md)).** Intake has the
*identical* no-signal-yet window: `intake_once` picks the oldest issue carrying none of
intake's state labels, grounds it in a **multi-minute** session, and only stamps the state
label (`ready-for-agent` / `needs-grilling` / `needs-mockup`) at the *end* via
`apply_intake`. Between selection and that stamp the issue still reads as un-triaged, so a
concurrent dispatch — a manual `/agentflow` triage racing the daemon — would fire a second
grounding session and post a duplicate intake summary. (This window is latent under the
serial single-instance daemon; the *observed* double-summary on an issue was a distinct bug
— the grounding session ran `gh` to post the body itself while `apply_intake` also posted it
— fixed separately by making the session's only output its JSON decision.)

- `intake_once` applies **`agentflow:triaging`** *before* the grounding session and releases
  it in a `finally`. `_untriaged` (the pure selector predicate) skips upstream
  `wayfinder:*` planning artifacts, the state labels, **and** the claim;
  `_next_resumable_issue` skips a held issue already being re-triaged.
- **One asymmetry from the build claim: no `reclaim_claims` equivalent.** A build proves
  liveness with its open PR, so a claim without one is safely stale. **Intake opens no PR** —
  no liveness signal — and the case it guards *is* a manual triage racing the daemon, so a
  blind cycle-top reclaim would clear that live claim and reopen the race. The `finally`
  covers normal completion and in-process errors; only a hard crash (SIGKILL / machine death)
  mid-intake strands the claim, which is fail-safe (the issue is *skipped*, never
  double-triaged) and cleared by hand.

## Alternatives considered

- **Open-PR check alone.** Rejected: lagging — it can't see a build that hasn't opened a
  PR yet, which is exactly the concurrent-dispatch window.
- **A local lock/DB of in-progress issues.** Rejected: violates GitHub-as-source-of-truth
  (ADR 0010) and doesn't survive a restart; a label does both.

## Consequences

- Dedup is **prediction-free** and visible (you can see `agentflow:building` /
  `agentflow:triaging` in the UI).
- **Known limitations** (surfaced by an adversarial verification pass, left for when they
  become reachable):
  - **Concurrent fan-out (future, [ADR 0006](0006-two-pool-runner-assignment.md)):** the
    claim is a flag, not a lock — check-then-`--add-label` is a TOCTOU, and `--remove-label`
    is not ref-counted. Safe under the *current serial* single-instance daemon; when
    parallel builds land, the claim must be set in the serial dispatch step before
    fan-out (or moved to an atomic reservation). Applies to both claims.
  - **A crash mid-intake strands `agentflow:triaging`** — deliberately not auto-reclaimed
    (see the asymmetry above), so that one issue is skipped until the label is cleared by
    hand. If this becomes reachable, the safe reclaim is age- or daemon-startup-based, not
    the build's open-PR check.
  - **A manual agent that opens no PR and sets no claim is invisible** to the daemon — it
    leaves no source-of-truth signal. Manual paths should claim (or draft-PR) early.
- ADR 0009's merge-time floor is unchanged; this is a *distinct* layer (stop the duplicate
  from firing, vs. resolve conflicts at merge).
