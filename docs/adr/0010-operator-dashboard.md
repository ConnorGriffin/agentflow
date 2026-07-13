# ADR 0010 — The operator dashboard: one console over GitHub-as-source-of-truth

- Status: Accepted — mechanism amended by [ADR 0023](0023-dashboard-replatform-control-plane.md)
  (read-only → interactive, stdlib → Svelte + FastAPI, single screen → multi-view;
  the read-over-GitHub stance and the needs-you set stand)
- Date: 2026-07-09

## Context

With two pools building concurrently across *multiple* repos, GitHub's per-repo
issue/PR views stop giving an operator picture of the fleet. And agentflow now
holds state GitHub structurally can't: live pool headroom, trust-ratchet
correction-rate, the cross-repo drop-to-reviewed backlog, utilization.

The paradox of autonomy: the *less* the human is in the per-PR loop, the *more* a
strong at-a-glance surface matters — "is the fleet healthy, what shipped while I
was away, what needs me, is a prepaid pool idle while work is queued." That is an
operator console, not a task queue.

## Decision

agentflow ships a **dedicated dashboard** — the operator console for the fleet —
that **sits on top of GitHub as the source of truth**. It does not replace GitHub:
issues, PRs, CI, and merges stay there, where the agents actually operate. The
dashboard *reads* GitHub + the scheduler's own state and offers the *control*
actions the human needs.

**Shows:**
- Fleet overview — per-repo profile/rung; in-flight issues by stage and tool.
- **Two-pool headroom + utilization** — the [ADR 0006](0006-two-pool-runner-assignment.md)
  "idle pool while work is queued = bug" signal, made visible.
- **Needs-you inbox** — `guarded` merges awaiting, drop-to-reviewed parks, intent-gap
  grillings. The same set ntfy pings.
- **Recently-merged audit feed** — what the fleet shipped, ranked for spot-check
  (the "audit after" surface the autonomous rung promises).
- **Ratchet state** — per-repo correction-rate trend and a "ready to loosen?" cue.

**Controls:**
- Merge a `guarded`/parked PR (the human's merge click).
- Ratchet a repo up/down.
- Pause/resume a pool or a repo (kill switch — echoes the old
  `codex-sweep.enabled` dormant-by-default flag).
- Jump to a grilling gap / the underlying GitHub issue.

## Alternatives considered

- **GitHub-native + ntfy + a thin status view.** Rejected: can't unify
  cross-repo/cross-pool, and has nowhere to surface agentflow's own state. The
  fleet would have no operator view.
- **Dashboard as source of truth (own datastore for issues/PRs).** Rejected: that
  rebuilds GitHub; agents, CI, and merges already live there. Read-over-GitHub, not
  replace.

## Consequences

- agentflow now owns a UI it must maintain — a deliberate cost, justified by the
  fleet being multi-repo and multi-pool.
- It implies a **persistent backend** (the two-pool scheduler already is one) — the
  orchestration shape is the next ADR.
- **ntfy still fires** for away-from-desk; the dashboard is the at-desk console.
  Both point at the same needs-you set, so they never disagree.
