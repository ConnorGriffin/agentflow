# ADR 0011 — Persistent orchestrator, ephemeral hands

- Status: Accepted
- Date: 2026-07-09

## Context

The two-pool balancer ([ADR 0006](0006-two-pool-runner-assignment.md)) and the
operator dashboard ([ADR 0010](0010-operator-dashboard.md)) both need a single,
coherent, always-on view. The superseded model — independent `triage-sweep` /
`codex-sweep` / `implement-sweep` LaunchAgents firing on a timer + gate —
structurally can't balance two pools against each other or serve a live console;
each sweep sees only its own slice.

But the build/review *work* is heavy, episodic, and best isolated per issue.

## Decision

agentflow is a **persistent orchestrator daemon** whose **hands are ephemeral**.

- **Persistent brain.** One always-on service owns the queue, the two-pool
  balancer, and the dashboard API; it reacts to events (GitHub webhooks, or polling
  as the safe default) and dispatches work.
- **Ephemeral hands.** It spawns **one worktree + one agent session per issue**
  (`claude -p` / `codex exec`, the same invocations used today), torn down after —
  identical isolation to the current flow, just dispatched by the daemon instead of
  a cron sweep.
- **Crash-recoverable.** On restart the daemon rebuilds working state from GitHub
  (issues/PRs/labels are the source of truth, [ADR 0010](0010-operator-dashboard.md))
  plus a small local store for its own state (pool calibration, ratchet metrics,
  in-flight session handles).
- **Dormant-by-default per pool.** The old `codex-sweep.enabled` flag generalizes to
  a per-pool pause/kill-switch; a pool with no enable flag dispatches nothing.

## Alternatives considered

- **Keep periodic sweeps.** Rejected: can't balance two pools or serve a live
  dashboard; state is scattered across independent runs.
- **Hybrid (persistent dashboard, sweep dispatch).** Rejected: splits the balancer's
  decision from its state — the balancer must own dispatch to load-balance at all.
- **Persistent workers too (long-lived agent processes).** Rejected: heavy and
  stateful; ephemeral worktree-per-issue is the proven isolation model.

## Consequences

- One supervised service (launchd keep-alive) replaces three LaunchAgent sweeps —
  net simpler operationally once built.
- The gate logic in `triage-gate.sh` (interactive-session detection, per-plan
  headroom) moves *into* the daemon's balancer.
- Webhooks vs polling is an implementation choice; polling is the safe default (no
  inbound network exposure), webhooks a latency optimization.
