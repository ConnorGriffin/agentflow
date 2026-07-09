# ADR 0001 — One pipeline, one dial: the per-repo autonomy profile

- Status: Accepted
- Date: 2026-07-09

## Context

We run an autonomous **issue → PR → review** pipeline across several repos with
wildly different risk. `ciq-autotune` is medical-adjacent — a plausible-but-wrong
merge can alter an insulin recommendation. A greenfield "vibe-code" project is
low-stakes: the worst case is a revert. Same owner, same machinery, opposite
tolerance for an unwatched agent.

As of 2026-07 both coding agents in play — Claude (Opus) and Codex (GPT-5.6 Sol)
— are full-loop autonomous agents: either can scope, ground, build, and review a
whole issue end to end. Tool identity no longer bounds what can be automated.

The prior design (`dotfiles/docs/adr/0001–0005`, dated 2026-07-08, **superseded
by this repo**) routed by *tool*: a `tool:codex` label confined Codex to a frozen
"hermetic middle" while Opus owned both pipe-ends. That conflated two different
things — *which tool runs the work* and *how hazardous the work is*. When both
tools reached full autonomy, the tool axis stopped carrying information.

## Decision

**One pipeline, parameterized by a per-repo _autonomy profile_ — a single dial.**

The dial runs from fully autonomous to fully human-gated:

- **Autonomous end** — the agent self-scopes, builds, gets a cross-tool review,
  and **merges on green CI + clean review**. The human audits *after* the fact.
- **Gated end** — the agent produces a mergeable, reviewed PR; a **human merges**.

**What sets a repo's position on the dial is _domain risk_** — the cost of a
plausible-wrong *merge* — not tool identity, not sandbox limits, not how
mechanical the change looks. Domain risk is the one constraint that a smarter
model does not erase.

**Tool identity is not on the dial.** Claude and Codex are interchangeable
runners (see a later ADR); either can occupy any stage at any profile level.

## Alternatives considered

- **Route by tool (the superseded design).** Rejected: conflates capability with
  hazard; obsoleted the moment both tools reached full-loop autonomy.
- **One uniform policy for every repo.** Rejected: medical caution would throttle
  vibe-code to a crawl, and vibe-code autonomy would be reckless in `ciq-autotune`.
- **Route by change size (mechanical → auto, big → human).** Rejected for the same
  reason the superseded design failed: a one-line change in a medical domain can
  be catastrophically wrong. Size is not risk.

## Consequences

- The named profile **levels**, and exactly what each one clamps (grounding rigor,
  review mode, merge policy), are the subject of the next ADR.
- **Review becomes the load-bearing safety gate** for the autonomous end of the
  dial — a later ADR.
- Per-repo config carries the profile and the repo's hazards; the engine stays
  generic and public-safe (no medical/PHI specifics leak into it).
