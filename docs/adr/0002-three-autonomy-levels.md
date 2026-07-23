# ADR 0002 — Three autonomy levels: `autonomous`, `reviewed`, `guarded`

- Status: Accepted
- Date: 2026-07-09

## Context

[ADR 0001](0001-per-repo-autonomy-profile.md) established a single per-repo
autonomy dial set by domain risk. This ADR names its rungs and fixes what each
one clamps.

The dial bundles three sub-policies that empirically move together:

- **Grounding rigor** — may the agent trust the work order, or must it verify
  against real data / a running app before it's allowed to be right?
- **Review mode** — how much independent scrutiny a diff gets before it can land.
- **Merge policy** — who lands it: the machine on green, or a human.

A repo that needs real-data grounding to be *correct* is exactly a repo you don't
let merge itself — so coupling these into one dial (rather than three free knobs)
matches how real repos actually distribute.

## Decision

Three named levels. A repo declares exactly one (`profile: <level>`).

| Level | Grounding | Review | Merge |
|---|---|---|---|
| **`autonomous`** | agent self-grounds as needed | cross-tool exact-head review, including any reviewer fixes | **auto-merge** on green CI + clean untainted review |
| **`reviewed`** | agent self-grounds as needed | cross-tool review when available; same-tool fallback is explicit | human glances, then merges |
| **`guarded`** | **mandatory** real-data / running-app grounding, frozen at scope time | dual (both tools) or human review | human merges after full review |

- `autonomous` — vibe-code / greenfield / low domain risk. The worst case is a
  revert. The machine closes the loop.
- `reviewed` — the sensible default for most repos. The machine does everything up
  to a reviewed, mergeable PR; the human's only act is a glance and a merge click.
- `guarded` — `ciq-autotune` and anything medical-adjacent. A plausible-wrong
  merge is expensive, so grounding is mandatory and a human always merges.

**Single coupled dial.** Grounding/review/merge are not independently settable.
Off-diagonal combinations (e.g. auto-merge but mandatory grounding) are deferred
until a real repo demonstrably needs one — at which point the specific knob gets
promoted, not the whole matrix.

## Alternatives considered

- **Three independent knobs.** Rejected for now: more config, and the realistic
  combinations lie on the diagonal. Promote a single knob on real need.
- **Two levels (auto vs human).** Rejected: loses the common middle — "arrives
  fully reviewed, I just merge" — which is where most repos live.

## Consequences

- The **review mode** column is the load-bearing safety control for the top two
  rungs; "cross-tool review" and what makes a review *clean enough to auto-merge*
  are the next ADRs.
- `guarded`'s "mandatory grounding, frozen at scope time" is the one place the old
  hermetic-work-order discipline survives — as a per-level requirement, not a
  per-tool cage.
- Default for a new repo is `reviewed` unless its owner dials it either way.
- ADR 0047 later made review a depth-aware fix/review chain. The profile still controls who may
  merge; it no longer implies exactly one report-only review pass.
