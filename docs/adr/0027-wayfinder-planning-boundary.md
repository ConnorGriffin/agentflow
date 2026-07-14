# ADR 0027 — Wayfinder planning artifacts stay upstream of intake

- Status: Accepted
- Date: 2026-07-13

## Context

Agentflow intake treats every open issue without a state label as a build request. It
grounds that request and rewrites the issue body into an Agent Brief before routing it.

Wayfinder uses GitHub issues for an earlier, interactive deciding stage: one map plus
child decision tickets. Those issues deliberately have `wayfinder:*` labels rather than
agentflow state labels. Letting intake see them would turn planning questions into build
briefs and destroy the map as the source of truth.

## Decision

The intake selector excludes every issue carrying a label whose name starts with
`wayfinder:`. The exclusion happens before agentflow claims, grounds, comments on, or
rewrites the issue.

Wayfinder hands work to agentflow by filing ordinary build issues after an independent
subtree has no open decision that could invalidate it. Those filed issues carry the
relevant resolved decisions in their bodies and intentionally do not carry a
`wayfinder:*` label, so normal intake grounds them and writes the Agent Brief and dials.

## Alternatives considered

- **Give planning issues an agentflow state label.** Rejected: a planning artifact is
  neither build-ready nor held build work, and mixing the state machines makes the
  tracker lie.
- **Teach intake to recognize only today's five wayfinder labels.** Rejected: the
  namespace is the boundary. Matching individual labels would silently admit a future
  decision-ticket type.
- **Stamp handed-off tickets `ready-for-agent`.** Rejected: that bypasses the grounding,
  Agent Brief, and complexity/effort decisions that downstream build and review consume.

## Consequences

- Wayfinder maps and decision tickets remain unchanged while the daemon sweeps issues.
- A mislabeled build issue stays outside agentflow until its `wayfinder:*` label is
  removed; this is fail-safe against accidental execution.
- The handoff issue body must stand alone. Intake grounds from the repository and does
  not need to chase the map or its closed decision tickets.
