# 0044 — Stage session profiles and spend ceilings

- Status: accepted
- Date: 2026-07-19
- Ticket: [#231](https://github.com/ConnorGriffin/agentflow/issues/231) (wayfinder map [#226](https://github.com/ConnorGriffin/agentflow/issues/226))
- Evidence: [session profiles and ceilings research](../research/session-profiles-and-ceilings-draft.md)

## Ruling

Every daemon session is launched with a **per-stage profile** instead of today's
one-size surface and single two-hour timeout:

1. **Tool surface is an allowlist, per stage.** Read-only stages (Intake, Review,
   Research) get read/search tools only — no edit tools. Code-writing stages
   (Build, Respond, Revise, Mockup) keep the full edit/test surface. Withheld
   tools are **removed from the loaded surface** (that is where the cache-creation
   savings live), with a settings-level deny as backstop — not deny-only.
2. **MCP servers are pinned to empty** for every stage. Personal connectors never
   attach to daemon sessions (tracked independently as
   [#240](https://github.com/ConnorGriffin/agentflow/issues/240)).
3. **Per-stage wall-clock and turn ceilings replace the shared two-hour timeout**,
   sized ~1.5–2× the observed maximum per (stage, complexity, effort) cell — the
   table in the research doc is the source of truth. Thin-sample stages (Respond,
   Revise, Converse, Research, Mockup) ship the conservative drafted ceilings now
   and ratchet once per-attempt telemetry (#223) fills their cells. Revise
   inherits the original builder's Build ceiling (consistent with ADR 0041).
4. **No dollar-denominated session cap.** The objective is prepaid headroom
   (ADR 0040); wall + turn ceilings are the only kill switches.
5. **Fail closed.** A stage that reaches for a capability its profile withholds
   ends in a human hold, never silent degradation. Hitting a ceiling remains a
   recoverable timeout-class ending.

## Consequences

- Ceiling numbers are expected to ratchet: after #223's telemetry lands, the
  before/after comparison in the research doc (§4) must confirm cache-creation
  savings, earlier kills, and no capability starvation, holding ADR 0040's
  quality guardrails flat. The profile mechanism itself is settled; the numbers
  are calibration.
- The read-only Intake/Review profile lands once, coordinated with the
  provider-native structured result surface (#224), not twice.
