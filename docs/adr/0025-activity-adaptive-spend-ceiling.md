# ADR 0025 — Activity-adaptive spend ceiling: the daemon yields to a live operator, it doesn't stop

- Status: Accepted
- Date: 2026-07-13
- Amended: 2026-07-16 — this ceiling paces autonomous **background** work only; interactive conversation turns (Ask/grilling) are exempt and admitted in real-time ([ADR 0034](0034-methodology-session-orchestration.md), [#161](https://github.com/ConnorGriffin/agentflow/issues/161))

## Context

The pool gate (`triage-gate.sh`) blocks unattended dispatch outright whenever the
operator has an interactive claude/codex session in active use (a transcript
write in the last `ACTIVE_WINDOW_MIN` = 10 minutes). The intent was sound — the
unattended daemon must not eat the 5-hour window out from under a live session —
but the mechanism is a **hard stop**: while the operator works, the fleet idles,
even at 5% spend with a deep queue. That's the same "idle while queued = wasted
prepaid capacity" bug [ADR 0006](0006-two-pool-runner-assignment.md) exists to
kill, and headroom-governed concurrency
([ADR 0023](0023-dashboard-replatform-control-plane.md)) makes the waste bigger.

A static reservation ("always keep 50% for the operator") is wrong too: on idle
days it strands half the window. The operator's need is dynamic — they might burn
50% of the window interactively during a burst, and none overnight.

## Decision

**The activity signal stops gating dispatch and starts selecting the ceiling.**
Per pool, per cycle:

- **Idle** (no operator transcript writes within the activity window): the daemon
  dispatches until the pool is ~**85%** spent (the existing `BURN_CEILING_PCT`).
- **Operator active on that tool**: the daemon's ceiling for that pool drops to
  ~**50%** spent, and dispatch **paces** — at most one new session per cycle on
  that pool. Already-running sessions finish; nothing is killed.
- Activity decays on the gate's existing window (~10 min after the last
  interactive write), so the ceiling ramps back up by itself — the rolling 5-hour
  window returns the burst's capacity gradually. Nothing is reserved while idle;
  the operator's headroom only materializes when a session is actually live.

The operator has **no ceiling** — it's their plan; only the daemon self-limits.

Codex weekly pacing from [ADR 0006](0006-two-pool-runner-assignment.md)'s 2026-07-13
amendment is an additional unattended-dispatch constraint. It follows a reported
10,080-minute window independently of the activity-adaptive short-window ceiling;
the 85% idle / 50% active decision here is unchanged.

Consequences that fall out for free:
- **Per-pool asymmetry**: driving Claude interactively throttles only the claude
  pool; the balancer (builder = more usable headroom) naturally shifts builds
  toward codex during the burst. The fleet re-routes rather than idles.
- The by-hand `TRIAGE_SKIP_ACTIVITY` escape hatch
  ([ADR 0022](0022-one-build-input-and-the-build-verb.md)'s `build <N>` path)
  becomes unnecessary for its original purpose but stays harmless.

**Facts move to the gate, policy moves to the balancer.** Today the gate script
(in private tooling) *decides* clear-vs-busy and agentflow obeys. Under this ADR the
gate reports **facts** — trailing-5h spend and active-or-idle — and **agentflow's
balancer applies the ceiling** (thresholds as named config: `IDLE_CEILING_PCT=85`,
`ACTIVE_CEILING_PCT=50`, `ACTIVE_PACE=1/cycle`). That makes the policy testable
in-repo, tunable without touching private tooling, and surfaceable: the dashboard's pool
strip ([ADR 0023](0023-dashboard-replatform-control-plane.md)) can honestly show
*"claude · yielding to operator · ceiling 50%"* instead of a mute "busy".

The gate's existing exclusion of agentflow's own sessions from the activity check
(`AGENTFLOW_WT_MARK`) is load-bearing and retained — the fleet must never read
*itself* as the operator.

## Alternatives considered

- **Keep the hard stop (status quo).** Rejected: idles the whole fleet for the
  duration of any interactive burst regardless of spend — the exact waste the
  two-pool design exists to prevent.
- **Drop the activity check entirely, keep only the spend ceiling.** Rejected:
  the daemon and the operator then race to the same wall, and a busy fleet can
  exhaust the window an hour before the operator needed it. No interactive
  priority at all.
- **Static reservation (always hold N% for the operator).** Rejected: strands
  capacity on idle days; the operator's need is bursty, not constant.
- **Model/effort downshift while active (build with smaller models instead of
  fewer sessions).** Rejected: complexity is a per-issue correctness dial
  ([ADR 0014](0014-cost-appropriate-model-tiers.md)/[0018](0018-two-dials-review-by-evidence.md)),
  not a pacing knob; quietly building correctness-sensitive work with a smaller
  model to save headroom is the wrong trade.

## Consequences

- The fleet works through interactive bursts instead of idling — bounded to the
  bottom half of the window, paced, and biased toward the other pool.
- The operator's worst case is bounded and known: at most `ACTIVE_CEILING_PCT`
  of a window spent by the daemon while they're active (plus sessions already
  in flight when the burst began).
- `triage-gate.sh` (private tooling, itself fleet-enrolled) needs a small change: expose
  activity as a reported fact (e.g. `activity` mode or a field alongside
  `spend:`), while `check` semantics remain for compatibility until agentflow
  switches over. Cross-repo, one seam.
- The balancer gains the ceiling policy + named config, and its dispatch
  preconditions become: `spend < ceiling(activity)` and `pace(activity)` — the
  same code the headroom-governed concurrency slice
  ([ADR 0023](0023-dashboard-replatform-control-plane.md), M6 slice 5) already
  touches, so this rides that slice rather than adding a new one.
- Supersedes the earlier informal intent to simply "drop the recent-activity
  guard": yield, don't stop.
