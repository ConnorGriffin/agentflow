# ADR 0023 — Dashboard re-platform: an interactive control plane (Svelte + FastAPI, polling liveness)

- Status: Accepted — write/control-plane direction superseded by
  [ADR 0035](0035-workflow-engine-read-only-operator-console.md); Svelte/FastAPI and
  headroom-governed concurrency remain
- Date: 2026-07-13

## Context

The operator dashboard ([ADR 0010](0010-operator-dashboard.md)) shipped as a
read-only, zero-dependency stdlib page (`server.py` + one `static/dashboard.html`,
`GET /api/snapshot`). The pipeline has since grown a whole front half and a
live-execution layer the console has no window into:

- The **needs-you inbox is materially incomplete.** It derives only from in-flight
  PRs + ratchet-ready, so **held issues** (`needs-grilling` / `needs-mockup`,
  [ADR 0016](0016-intake-stage.md)/[0019](0019-human-re-entry.md)) and **parked
  PRs** (drop-to-reviewed / failed-merge / open-question) never appear — though
  [ADR 0010](0010-operator-dashboard.md) itself named them as inbox members.
- **The controls promised by ADR 0010 were never built.** Every inbox card renders
  a `Merge`/`Loosen` button, but the page is 100% read-only and the buttons are
  dead. The console *looks* like a control plane and isn't one.
- **No live view.** The daemon now tracks live agent process ownership (PID +
  worktree markers, [ADR 0011](0011-persistent-orchestrator.md)), but `in_flight`
  only means "a PR is open," not "an agent is executing right now."
- The fleet went from 1 repo to 6; the single glance-screen layout no longer fits.

The information model, not the tech, is what fell behind. It was designed before
intake, held issues, parks, and live sessions existed.

## Decision

Re-platform the dashboard as an **interactive control plane**, still read-*over*-
GitHub ([ADR 0010](0010-operator-dashboard.md)'s core stance holds), with a locked
visual spec (`mockups/dashboard-v2-combined.html`, via `/ui-mockups`).

- **Stack: Svelte SPA (Vite) + FastAPI.** A deliberate break from the stdlib/
  read-only properties of [ADR 0010](0010-operator-dashboard.md) — the surface is
  now a multi-view app with write actions, and the stdlib `http.server` is the
  wrong tool for SSE-free polling + POST controls + auth. It stays **terminal-
  native in look** (dark, mono, dense — `PRODUCT.md`/`DESIGN.md` unchanged); the
  framework is invisible to the operator.
- **Multi-view IA (the new decision):** a console shell with tabs — **Inbox**
  (the fuller needs-you worklist: merges + **held** + **parked** + ratchet-ready),
  **Live** (running sessions as a triaging→building→reviewing board), **Fleet**
  (repos × signals, expand-in-place), **History** (merged/audit) — plus a shared
  drill-down. Inbox is the landing tab; watching is not the job.
- **Liveness is polling, not webhooks.** The **daemon writes a live-session state
  file** (the running sessions only it knows); the server reads it and **caches
  `gh` on a ~15s TTL**; the browser polls `/api/snapshot` every ~3–5s. Two clocks:
  a cheap fast browser refresh over a rate-limit-aware `gh` refresh. Webhooks were
  rejected for now — they need a public endpoint/tunnel for a localhost tool; a
  ~15s reconciliation poll is near-instant with zero infra, and can be revisited.
- **Controls are POST endpoints over the verbs that already exist** — the
  `/agentflow` surface's `pickup` / `build` and `gate` merge / `ratchet`
  ([ADR 0019](0019-human-re-entry.md)/[0022](0022-one-build-input-and-the-build-verb.md)) —
  behind a shared-secret token + same-origin check. The server is a **thin adapter
  over deep modules**, not a re-implementation of pipeline logic. This is the
  correctness/security-sensitive surface: it mutates GitHub and spawns agents, so
  it ships one verb at a time, safest first (pause/resume → loosen → merge →
  pickup).
- **`snapshot()` gains a v2 contract:** top-level `daemon` status + `running[]`,
  and per-repo `held[]` / `parked[]` + `effort` on ready issues. The daemon owns
  the live-session portion; the server merges it with cached `gh` reads. (SQLite is
  deferred — a JSON state file suffices until a durable history/trends view earns
  it.)

**Coupled decision — drop the serial dispatch cap.** M1's one-issue-per-repo-per-
cycle serialization ([ADR 0006](0006-two-pool-runner-assignment.md)) becomes
**headroom-governed concurrency**: the fleet runs as many sessions as pool
headroom (and a physical machine ceiling — worktrees/PIDs/CPU) allows, not one at
a time. This is what makes the Live board load-bearing (real WIP across lanes and
pools) rather than near-always-empty theater, and it's the honest reading of
[ADR 0006](0006-two-pool-runner-assignment.md)'s own "idle pool while work is
queued = bug." Dispatch dedup ([ADR 0021](0021-dispatch-dedup-build-claim.md))
already claims per-issue, so concurrent dispatch is safe; serialized **merges**
stay serial (the collision floor, [ADR 0009](0009-collision-safety.md)).

## Alternatives considered

- **Extend the stdlib page in place** (add sections + wire the dead buttons).
  Rejected: keeps a one-screen layout built for a 1-repo fleet, and hand-rolling
  SSE-free polling + POST + auth on `BaseHTTPRequestHandler` is masochism once a
  build step exists anyway.
- **Keep it read-only, drop the dead buttons.** Rejected: abandons ADR 0010's
  stated control-action goal; the operator's job is acting on exceptions, not just
  seeing them.
- **Webhooks + event-driven pipeline now.** Rejected *for now*, not on merit — it
  needs standing tunnel/GitHub-App infra for a localhost tool. Recorded as the
  future upgrade that would also make the daemon react to new issues instantly.
- **Re-platform without dropping the concurrency cap.** Rejected: the Live view is
  the forcing function; a serial fleet leaves its lanes empty and the whole
  live-ops surface unjustified.

## Consequences

- **[ADR 0010](0010-operator-dashboard.md) is amended:** its *stance* (one console,
  read-over-GitHub, the needs-you set = ntfy set) stands; its *mechanism* changes —
  read-only → interactive, stdlib → Svelte + FastAPI, single screen → multi-view.
- **[ADR 0006](0006-two-pool-runner-assignment.md) is amended:** dispatch is no
  longer serial per repo; concurrency is headroom-governed. Merges stay serialized.
- The daemon gains a new responsibility: **persist live-session state** for the
  console (it already tracks the PIDs/worktrees; this writes them out).
- agentflow takes on a **build step and a JS framework** — a real maintenance cost,
  accepted because the surface is now genuinely multi-view and interactive.
- Built in vertical slices ([ADR 0012](0012-build-in-vertical-slices.md)), each
  closing the loop before the next: walking-skeleton stack → live-session file +
  Live → held/parked + Fleet/History → controls (one verb at a time) →
  concurrency. Dogfooded through the fleet on this repo (`reviewed`: a human
  merges changes to the merge machinery).
