# agentflow — build roadmap

A living plan. The *method* is fixed in [ADR 0012](docs/adr/0012-build-in-vertical-slices.md);
the *design* it builds toward is [ADR 0001–0011](docs/adr/). This file is the
sequence, and it will change as slices land.

## Method (ADR 0012)

- **Thin vertical slices**, each closing the loop end-to-end for a real repo before
  the next thickens it. Not horizontal layers.
- **One owner of the spine.** Subagents fan out only for independent leaves and an
  adversarial review pass per slice — never the integration core.
- **Dogfood on a live vibe-code repo** from slice one.
- **Two standing gates on every slice:**
  - **UI → `/ui-mockups`** to a *locked* visual spec before implementing (the M3
    dashboard + its controls).
  - **Interfaces → deep-module discipline** (`/improve-codebase-architecture`,
    `/codebase-design`): deep modules, deletion test, interface-as-test-surface,
    exact vocabulary; design-it-twice for non-obvious shapes. No shallow modules.

## Milestones

Each milestone is a working end-to-end slice, not a layer.

### M0 — Walking skeleton  *(= rollout step 1)* — ✅ DONE 2026-07-09
One vibe-code repo at `profile: autonomous`, one issue through the whole loop:
intake → build (tool A, worktree) → cross-review (tool B) → auto-merge on green +
clean. Proves session-spawn, the cross-tool handoff, and the merge gate on real
GitHub. May hardcode a single pool.

**Proven live** on `agentflow-sandbox` #1: Claude built `slugify`; Codex cross-reviewed
and caught a real spec ambiguity (ASCII vs Unicode); one revise round made it
Unicode-aware; Codex re-reviewed clean; the gate auto-squash-merged PR #3. Findings
folded in along the way: verdict-capture (not a model-written file), park-not-revise
on an unusable review, review anchors to acceptance ([ADR 0015](docs/adr/0015-review-anchors-to-acceptance.md)),
stale-review-worktree reset, and `AGENTFLOW_CODEX_BIN` (the Homebrew codex cask ships
no `codex-code-mode-host`, so PATH `codex` can't run commands — repair pending).
- **Reuse map:** [docs/reuse-map.md](docs/reuse-map.md). Leans on `codex-go`
  (generalized) + `spin-worktree.py` = spawner, `triage-gate.sh check` = balancer,
  the `triage-sweep` poller (filtered `ready-for-agent`, intake skipped), `close`'s
  `gh pr merge --squash` = merge, ntfy = notify. **New glue:** (1) a tiny persistent
  poll→build→review→merge loop holding both pools; (2) the cross-tool review call +
  a machine-readable `PASS`/`BLOCK` verdict — *the one genuinely new must-build*;
  (3) a ~15-line auto-merge decision wiring existing `revise`/`close`.

### M1 — Two pools + balancer + collision floor — ✅ DONE 2026-07-09
`balancer.py` picks builder = the pool with more rate-limit headroom (reusing
`triage-gate.sh` per agent), reviewer = the other tool; single-tool fallback returns
no reviewer so the loop can't auto-merge without independence. `daemon.py` is the
dormant-by-default, crash-isolated poll loop. Collision floor = rebase-once +
`INTEGRATION-COLLISION` marker in the build prompt + serialized (one-at-a-time)
merges. Balancer live-validated (correctly defers when both pools are busy).
*Concurrent dispatch across pools is a later refinement — M1 is serial.*

### M2 — The not-clean paths — ✅ DONE 2026-07-09
Auto-revise round + drop-to-reviewed proven live in M0; `notify.py` adds ntfy pings
for the needs-you set (parks, build bails), silent on autonomous merges
([ADR 0004](docs/adr/0004-auto-merge-gate.md)/[0010](docs/adr/0010-operator-dashboard.md)).

### M3 — Operator dashboard — ✅ read-only v0, 2026-07-09
`server.py` (zero-dependency stdlib http.server) serves `static/dashboard.html` +
`GET /api/snapshot` from `dashboard_data.py` — live two-pool headroom, per-repo
profile, in-flight PRs, recently-merged audit feed, and ratchet state, grounded in
the real payload. **Remaining:** the write *controls* (merge / ratchet / pause) and a
formal `/ui-mockups` pass *with the human* to lock the final visual (this v0 honors
the "mockups bind real data" rule but skipped the multi-variant exploration — the one
charter step still owed) ([ADR 0010](docs/adr/0010-operator-dashboard.md)).

### M4 — `guarded`  *(validates the load-bearing unknown)*
Grounding pass + frozen work order + gap protocol + named-invariant gate
([ADR 0005](docs/adr/0005-spec-rigor-rides-the-dial.md)). This is where we finally
test whether Codex can be configured to touch the read-only snapshot and stand up
the app — after everything else is proven. If it can't, `guarded` grounding pins to
Claude; the design holds.

### M5 — Trust ratchet
Per-repo correction-rate metric + the loosen/tighten controls
([ADR 0007](docs/adr/0007-decisive-intake-graduated-autonomy.md)).

### M5.5 — Dependency-aware dispatch ([ADR 0024](docs/adr/0024-dependency-aware-dispatch.md))
A `Blocked by #N` marker gates the ready set so an ordered chain of slices builds in
order and auto-advances. Small, independent — built first, because it's what lets M6
be filed as one batch instead of one issue at a time.

### M6 — Dashboard re-platform: interactive control plane ([ADR 0023](docs/adr/0023-dashboard-replatform-control-plane.md))
The read-only stdlib dashboard fell behind the pipeline (no held/parked in the inbox,
dead control buttons, no live-session view). Re-platform to a Svelte SPA + FastAPI
interactive control plane against the locked spec `mockups/dashboard-v2-combined.html`.
Vertical slices ([ADR 0012](docs/adr/0012-build-in-vertical-slices.md)), **filed as one
`blocked-by` chain** (M5.5) — the head builds, each later slice auto-advances as its
blocker merges:

1. **Walking skeleton** — FastAPI serves `/api/snapshot`; a Vite/Svelte shell renders
   the **Inbox** tab at parity with today's derivation. Proves the new stack end-to-end.
2. **Live sessions** — the daemon writes a live-session state file (`running[]`); the
   **Live** kanban reads it. *(blocked by 1)*
3. **Held + parked** — extend `snapshot()`; thicken the Inbox with held/parked rows;
   land the **Fleet** table + **History** feed. *(blocked by 1)*
4. **Controls** — POST + shared-secret auth, one verb per sub-slice (pause/resume →
   loosen → merge → pickup). The correctness/security-sensitive slice. *(blocked by 1)*
5. **Headroom-governed concurrency** — drop the serial dispatch cap (amends
   [ADR 0006](docs/adr/0006-two-pool-runner-assignment.md)); independent of the dashboard.

## Rollout coupling (signed off)

The build sequence *is* the repo rollout: **M0–M3 harden on a new vibe-code repo
(`autonomous`)** → add a `reviewed` repo → **port `ciq-autotune` last at `guarded`
(M4)**. The existing dotfiles `*-sweep` automation keeps running untouched until
agentflow's `guarded` path is proven, then retires per-repo.
