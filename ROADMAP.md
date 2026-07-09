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

### M0 — Walking skeleton  *(= rollout step 1)*
One vibe-code repo at `profile: autonomous`, one issue through the whole loop:
intake → build (tool A, worktree) → cross-review (tool B) → auto-merge on green +
clean. Proves session-spawn, the cross-tool handoff, and the merge gate on real
GitHub. May hardcode a single pool.
- **Reuse map:** [docs/reuse-map.md](docs/reuse-map.md). Leans on `codex-go`
  (generalized) + `spin-worktree.py` = spawner, `triage-gate.sh check` = balancer,
  the `triage-sweep` poller (filtered `ready-for-agent`, intake skipped), `close`'s
  `gh pr merge --squash` = merge, ntfy = notify. **New glue:** (1) a tiny persistent
  poll→build→review→merge loop holding both pools; (2) the cross-tool review call +
  a machine-readable `PASS`/`BLOCK` verdict — *the one genuinely new must-build*;
  (3) a ~15-line auto-merge decision wiring existing `revise`/`close`.

### M1 — Two pools + balancer + collision floor
Port the per-plan headroom logic into the two-pool load balancer
([ADR 0006](docs/adr/0006-two-pool-runner-assignment.md)); run issues concurrently
across pools; add the universal collision floor — rebase-once + serialized merge
([ADR 0009](docs/adr/0009-collision-safety.md)).

### M2 — The not-clean paths
One auto-revise round + drop-to-reviewed + ntfy pings
([ADR 0004](docs/adr/0004-auto-merge-gate.md)). Now `autonomous` is provably never
less safe than `reviewed`.

### M3 — Operator dashboard  *(UI gate applies)*
`/ui-mockups` → locked spec first. Read-only first (fleet overview, two-pool
headroom, needs-you inbox, recently-merged audit feed), then the controls (merge,
ratchet, pause) ([ADR 0010](docs/adr/0010-operator-dashboard.md)).

### M4 — `guarded`  *(validates the load-bearing unknown)*
Grounding pass + frozen work order + gap protocol + named-invariant gate
([ADR 0005](docs/adr/0005-spec-rigor-rides-the-dial.md)). This is where we finally
test whether Codex can be configured to touch the read-only snapshot and stand up
the app — after everything else is proven. If it can't, `guarded` grounding pins to
Claude; the design holds.

### M5 — Trust ratchet
Per-repo correction-rate metric + the loosen/tighten controls
([ADR 0007](docs/adr/0007-decisive-intake-graduated-autonomy.md)).

## Rollout coupling (signed off)

The build sequence *is* the repo rollout: **M0–M3 harden on a new vibe-code repo
(`autonomous`)** → add a `reviewed` repo → **port `ciq-autotune` last at `guarded`
(M4)**. The existing dotfiles `*-sweep` automation keeps running untouched until
agentflow's `guarded` path is proven, then retires per-repo.
