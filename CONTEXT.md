# agentflow — glossary

The ubiquitous language for the autonomous issue → PR → review pipeline. Glossary
only — no implementation details, no decisions (those are in `docs/adr/`).

## Terms

- **Pipeline** — the end-to-end path a unit of work travels: issue → triage/scope
  → build → PR → review → merge. One pipeline serves every repo; behavior varies
  only by the repo's autonomy profile.

- **Autonomy profile** — a per-repo dial governing how much an unwatched agent is
  trusted. It sets grounding rigor, review mode, and merge policy together. One of
  three levels:
  - **`autonomous`** — agent self-scopes, builds, gets one cross-tool review, and
    auto-merges on green CI + clean review. (Vibe-code / low domain risk.)
  - **`reviewed`** — agent builds and gets a cross-tool review; a human glances and
    merges. The default. (Most repos.)
  - **`guarded`** — mandatory real-data grounding, dual/human review, human merges.
    (`ciq-autotune` / medical-adjacent.)

- **Domain risk** — the cost of a *plausible-but-wrong merge* in a given repo. The
  durable constraint that a smarter model does not erase; it, not tool identity,
  sets a repo's position on the autonomy dial. High in `ciq-autotune` (medical),
  low in a vibe-code project.

- **Complexity** — the per-issue builder model size intake stamps as a hard gate:
  `standard` (sonnet/Terra) or `deep` (correctness-sensitive — opus/Sol).
  Tool-agnostic; each runner resolves it to its tool's model. Orthogonal to the pool:
  pool = *which plan* (by headroom), complexity = *how big a builder model within
  it*. Reviewers do not inherit this dial; every review uses the deep tier.
  (Supersedes the earlier single `tier` dial; `light`/haiku dropped — ADR 0018.)

- **Effort** — the second dial intake stamps alongside complexity: `low | medium |
  high | extra` — how much work the issue warrants, independent of model size.

- **Runner** — the interchangeable executor that performs a pipeline stage:
  Claude (Opus) or Codex (GPT-5.6 Sol). Chosen per stage by cost / availability /
  preference, **not** by a capability ceiling — both are full-loop capable.

- **Builder** — the runner that implements an issue and opens the PR. Self-reviews
  and flags uncertainties, but its own sign-off never gates a merge.

- **Reviewer** — the runner that reviews the PR at the deep tier (opus/Sol),
  independently of builder complexity. Must be a *different model* than the builder
  (see cross-tool review); its verdict is the one that counts.

- **Cross-tool review** — a review performed by a different model than the one that
  built the diff (Codex→Claude or Claude→Codex). Independence from the builder is
  the point; it targets "green CI but confidently wrong." Degrades to same-tool
  review only when one tool is unavailable, and that never auto-merges.

- **Blocking finding** — a reviewer finding at or above the correctness/security
  severity line; the only kind that blocks a merge. Lesser findings post as
  non-blocking nits.

- **Auto-revise round** — the single builder pass that addresses review findings
  before the pipeline re-reviews. Capped at one, to avoid revise/re-review loops.

- **Drop-to-reviewed** — the escape valve: an `autonomous` PR that can't clear
  review after its one revise round is demoted to `reviewed` for that issue
  (findings posted, human pinged, PR waits). Autonomy parks doubt, never forces a
  merge — so `autonomous` is never less safe than `reviewed`.

- **Brief** — at `autonomous`/`reviewed`, the spec a builder starts from: the issue
  itself (acceptance criteria + file pointers). The builder self-scopes from it.

- **Self-scope** — a builder reading the repo and grounding against real data to
  decide its own touch-set and approach, instead of being handed a frozen spec.
  Trusted at `autonomous`/`reviewed`; disallowed for *domain facts* at `guarded`.

- **Work order** — a *frozen hermetic spec* used only at `guarded`: grounding
  pre-done at scope time (real-data facts as literals + fixtures), a file allow-list,
  and named invariant tests, so the builder never guesses a domain fact. Not a
  per-tool cage — the grounding mechanism for high domain risk.

- **Gap protocol** — at `guarded`, a builder that hits an unstated domain fact,
  threshold, or fixture stops and posts a marker rather than guessing (a
  plausible-wrong guess is the expensive failure). Retained as a per-level safety.

- **Pool / headroom** — each prepaid plan (Claude, Codex) is a *pool* of rate-limit
  capacity. A pool can report multiple windows, such as a 300-minute window and a
  weekly window; reported windows may appear or disappear as plan limits change.
  *Headroom* is the unspent remainder. The scarce resource the scheduler optimizes
  — cost is not, since both plans are flat-rate. Idle headroom while work is queued
  is wasted sunk cost.

- **Two-pool load balancer** — the scheduler that assigns builds to keep both pools
  maximally utilized in parallel: builder → the pool with more headroom, reviewer →
  the other tool/pool. Never leaves a prepaid plan idle while work is queued.

- **Intake** — the autonomous stage every new issue passes through (fires on any open
  issue with **no state label**): it grounds the request (reads code + a read-only data
  pull if the repo declares one), rewrites the title/description, stamps the dials, and
  routes to one outcome — `ready-for-agent`, `needs-mockup`, or `needs-grilling`. Not a
  tollbooth: it scopes anything it can pin down confidently and holds only an
  *outcome-changing* fork it can't settle from code/data (ADR 0016).

- **Held issue** — an issue parked at `needs-grilling` or `needs-mockup`: inert to
  agents until the maintainer replies in a comment (which auto-advances it) or drives it
  with `/agentflow pickup` (ADR 0019). No builder touches a held issue.

- **Grounding fetch** — a per-repo, **read-only** pull of real data intake runs to check
  facts before scoping (ciq: `ciq-pull-db` → `ciq.readonly.db`). Declared once in the
  repo's config; run on-demand, skipped for issues already crisp (ADR 0016).

- **Decide-then-review** — the pipeline's default posture: a stage makes its best
  decision and *stages it under a review gate* instead of asking the human up front.
  Emits an answer, not a question. Only undecidable *intent*-gaps punt to grilling.

- **Trust ratchet (graduated autonomy)** — a repo starts conservative (gates on,
  decisions reviewed) and is loosened toward autonomy as its staged decisions are
  consistently confirmed without correction. Earned, deliberate, per-repo,
  reversible. The autonomy profile is the *current* setting; the ratchet moves it.

- **Operator dashboard** — agentflow's console for the fleet, sitting *over* GitHub
  (the source of truth), not replacing it. Reads GitHub + scheduler state; shows
  fleet overview, two-pool headroom, the needs-you inbox, a recently-merged audit
  feed, and ratchet state; offers control actions (merge, ratchet, pause, jump).

- **Needs-you inbox** — the operator's action list: `guarded` merges awaiting,
  drop-to-reviewed parks, and intent-gap grillings. The same set ntfy pings.

- **Charter** — the canonical engineering-standards file (`standards/CHARTER.md`)
  every app in the flow must meet: deep-module architecture, UI→`/ui-mockups`,
  test-through-the-interface, maintainability. Both tools read the *same bytes*
  (Claude `@import`s it; Codex's global `AGENTS.md` symlinks to it), and it's
  enforced at cross-review as blocking findings. Machine-global scope; per-repo files
  carry facts + `profile`, not the charter.

- **Hazard** — an *environmental* obstacle to autonomous work: PHI/real data,
  live credentials, a demo that needs a running app. Historically fenced work off
  to a specific tool; now treated as agent-handleable and captured in per-repo
  config, not in routing.
