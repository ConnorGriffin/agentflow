# agentflow reuse map (build reference)

Point-in-time inventory (2026-07-09) of which `dotfiles` logic ports into agentflow.
Not a decision (see ADRs) — a build aid. **The organizing flip:** everything today
routes by `tool:codex`; agentflow routes by a domain-risk `profile` — so most assets
need their tool-routing swapped for a `profile`/`pool` stamp.

## Component map

| Component | Verdict | Asset + port note |
|---|---|---|
| Orchestrator daemon [0011] | BUILD-NEW (loop reusable) | 3 stateless cron sweeps today. Reuse the sweep skeleton: `mkdir` lock + stale-reclaim, `next_candidate()` poll, `attempts.json` dedup, `run_one()`. Always-on supervisor holding both pools is new. |
| Two-pool balancer [0006] | **REUSE** (gate→comparator) | `scripts/triage-gate.sh` already reads both pools (Claude calibrated 5h spend; Codex `rate_limits.primary/secondary.used_percent`). New: turn the per-agent binary gate into a *comparator* (more-headroom → builder). tty + transcript-mtime interactive detection + reserve port directly. |
| Session spawner / worktree [0011] | **REUSE** (de-Codex it) | `scripts/codex-go` ≈ 80%: precheck → slug → worktree off FRESH `origin/main` → `uv sync` → launch → exit-code classify (0/2/3/4). Generalize launch line (`codex exec` vs `claude -p`) + namespace. `skills/spin-worktree/scripts/spin-worktree.py` = clean worktree primitive. |
| Intake [0007] | **REUSE** (reframe labels) | `skills/triage` auto mode IS the decide-then-review headless state machine. New: stamp `profile:`/`pool:` not `tool:codex`. work-order's hermeticity/insulin-math/exclusion gate = the "what makes it `guarded`" logic. |
| Build stage [0005] | **REUSE** | Spawner + `skills/work-order/orchestrator-prompt.md` (self-scoped brief) + `skills/go` builder entry + model/effort table. |
| Cross-tool review [0003] | **BUILD-NEW** (top gap) | **No runner exists** — the old `review-sweep` was never built; review is a manual `/close` rule. Must build: "other tool" dispatch, blocking-vs-nit severity contract [0004], reviewer sibling-PR check [0009], single-tool fallback. Riskiest must-build. |
| Auto-merge gate [0004] | BUILD-NEW (merge action reusable) | `skills/close` has the merge mechanics (`gh pr merge --squash --delete-branch`, conflict→stop, `pull --ff-only`) but is deliberately human-only. The green∧clean→merge / else revise-once / else drop-to-reviewed decision is net-new. |
| Revise + drop-to-reviewed [0004] | **REUSE** (strong) | `skills/revise` (`/revise --auto`) IS the one-pass PR-branch revision; `scripts/implement-sweep.sh` is its headless runner. `agent-followup` baton = the park state. New: one-round cap + auto-demotion. |
| Collision floor [0009] | **REUSE** (strong) | Rebase-once + `INTEGRATION-COLLISION` marker (orchestrator-prompt-codex + codex-go); serialized merge + sibling rebase (`close` steps 1-2, 8). |
| Dashboard backend [0010] | **BUILD-NEW** (top gap) | No web backend/UI. `scripts/ciq-queue.sh` + `skills/queue` = terminal status printer (the data-model seed: read GitHub + local state, surface the delta), but a CLI. |
| Per-repo config [0001/0008] | **REUSE** (repurpose) | `skills/work-order/repos/<repo>.md` already carries dir/repo/test-cmds/hazards. Add the `profile:` dial; make hazards machine-readable; fold in the hardcoded `REPOS=()` arrays. |
| Notifications [0010] | **REUSE** (unify) | `triage-sweep.sh notify()` = the ntfy path (reaches phone AFK) — port this one; the codex/implement sweeps' `terminal-notifier` variants diverge, unify to one. |
| Spend/headroom gating [0006/0011] | **REUSE** (strong) | Same `triage-gate.sh` (`check`/`calibrate`/`record-run` + burn mode). `codex-sweep.enabled` flag → per-pool kill-switch; `TRIAGE_FORCE=1` → dashboard force/jump. |

## Biggest gaps (net-new, no precedent)

1. **Cross-tool review stage + machine-readable verdict** — zero code precedent; the load-bearing safety gate. Highest-risk must-build.
2. **Persistent daemon** — cron sweeps can't balance two pools or serve a console; inner loop reusable, supervisor + crash-recovery new.
3. **Auto-merge decision** — `close` is human-only by design; "merge without a human" is entirely new.
4. **Unified two-tool runner** — `codex-go` is Codex-only; the Claude path lives separately in `implement-sweep.sh`. No shared runner interface.
5. **Dashboard backend + views + controls.**
6. **Crash-recovery / state store; trust-ratchet correction-rate metric** (only faint seeds in `attempts.json` / `state.json`).

## M0 recipe (walking skeleton)

One issue, one hazard-free vibe-code repo, `profile: autonomous`.

**Lean on:** `repos/<vibe>.md` config; `triage-sweep` `next_candidate()` + `attempts.json` poller (filter `ready-for-agent`, **skip intake for M0**); `triage-gate.sh check` for both agents (coin-flip + gate-check picks builder, real comparator is a fast-follow); `codex-go` generalized (param launch line + namespace) + `spin-worktree.py` + `orchestrator-prompt.md`; `close`'s `gh pr merge --squash` for the single PR; `triage-sweep` ntfy on merge/park.

**New glue (the M0 critical path):**
1. A tiny persistent loop tying poll→balance→build→review→merge in ONE process holding both pools (daemon seed; serial single-issue OK to start).
2. **The cross-tool review call + machine-readable `PASS`/`BLOCK` verdict** — dispatch tool B on the PR diff with the severity-line prompt. The one genuinely new must-build.
3. The ~15-line auto-merge decision: `checks green ∧ verdict PASS → close-merge; else one /revise --auto; else drop-to-reviewed + ntfy`.

Deferred past M0: dashboard, ratchet, intake profile-stamping, `guarded` work orders, overlap prediction, burn mode.
