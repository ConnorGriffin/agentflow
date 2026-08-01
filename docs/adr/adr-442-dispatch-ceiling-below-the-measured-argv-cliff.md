# ADR 442 — The worktree dispatch ceiling sits below the measured argv cliff

## Status

Accepted (2026-07-31). Recalibrates the number behind ADR 0050's dispatch gate; the gate's
design — count-only preflight, refuse cold work, fail open on unreadable git — is unchanged.

## Context

Three sessions died on 2026-07-31 the way #386/#415 taught the pipeline to name: every shell
command failed to spawn and the stage parked as `permanent provider condition (environment)`.
Issue #442 asked for the cause to be established from evidence, because the one recorded
hypothesis (the sandbox deny list over the OS argument limit) came from a failing session's own
assertion and its numbers did not fit ADR 0050's measurement.

The provider transcripts of the two dead builds (#421, #422) carry the primary evidence — the
CLI's own spawn diagnostic on every failed Bash call:

> Could not start /bin/zsh: the command line plus environment exceed the OS exec argument limit
> (E2BIG). At spawn: command line 1.1MB across 3 args (largest single arg 1.1MB) … The Bash
> sandbox profile adds 210 filesystem deny paths to every command, 156 of them for registered
> git worktrees …

Measurements taken against that incident (all on this machine, Claude CLI 2.1.212,
`kern.argmax` = 1,048,576):

- The CLI adds **three** deny paths per linked worktree of the session's repository
  (`.git/worktrees/<name>/{config.worktree,config.worktree.lock,commondir}`): 210/207 deny
  paths at the two deaths ⇒ **52/51 linked worktrees** (53/52 as `git worktree list` counts,
  including the main checkout).
- Controlled reproduction in a synthetic repo: 60 linked worktrees ⇒ E2BIG at **1.8 MB**
  spawn argv (231 deny paths); 120 ⇒ **3.2 MB** (411 paths). Slope ≈ **24 KB of profile per
  registration** over a ≈ 0.4 MB base; the cliff on this machine sits at ≈ 50 registrations.
- The daemon log brackets the incident: agentflow's registry grew from ~41 listed at 15:01 to
  53 at 16:13 as five concurrent intake/attack sessions launched (each registering worktrees);
  the two builds launched at the peak and died; the 16:45 recovery sweep removed 11
  registrations, after which the same launch shape fit under the limit again.

So the deny-list hypothesis is **ruled in mechanically** but ADR 0050's calibration is ruled
out: the cliff was measured at ~246 registrations / ~1.6 MB when the ceiling was set at 175,
and the current CLI reaches the limit at ~50. The per-registration cost moves with CLI version
and path length; the count is a proxy that rots.

## Decision

`WORKTREE_DISPATCH_CEILING` drops from 175 to **40** — ~12 listed registrations below the
measured death point, margin sized to the intra-hour growth the incident actually showed
(~12 registrations between the last sweep and the deaths). Cold work over the ceiling defers
and retries after sweeps shrink the registry, instead of launching sessions that die on their
first command and park for a human.

The `environment` disposition (one-strike park, attempt refunded — #386) is deliberately
unchanged. The incident does show the condition lifting on its own, but every observed
occurrence is this argv cliff, and the recalibrated gate now refuses those launches before a
session exists to die; a retry loop layered behind a correctly calibrated gate would only fire
when the calibration is wrong, which is precisely when a human should look.

## Consequences

- A repository near the cliff loses dispatch throughput (defers) rather than sessions. On the
  day of the incident this ceiling would have deferred the two builds until the 16:45 sweep.
- The number still does not port: another machine, repository path depth, or CLI version moves
  the cliff. Re-measure before trusting it elsewhere — the reproduction is one synthetic repo,
  `git worktree add` in a loop, and one sandboxed `claude -p` probe whose E2BIG diagnostic
  reports the exact bytes and deny-path counts.
- If a future CLI stops pricing registrations into spawn argv, the ceiling can rise again;
  that change should arrive with new measurements, as this one did.
