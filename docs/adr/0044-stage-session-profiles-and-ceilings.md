# 0044 — Stage session profiles and spend ceilings

- Status: accepted
- Date: 2026-07-19
- Ticket: [#231](https://github.com/ConnorGriffin/agentflow/issues/231) (wayfinder map [#226](https://github.com/ConnorGriffin/agentflow/issues/226))
- Evidence: [session profiles and ceilings research](../research/session-profiles-and-ceilings-draft.md)

## Ruling

Every daemon session is launched with a **per-stage profile** instead of today's
one-size surface and single two-hour timeout:

1. **Tool surface is an allowlist, per stage (Review amended by ADR 0047).** Read-only stages
   (Intake, Research) get read/search tools only — no edit tools. Code-writing stages
   (Build, Review, Respond, Revise, Mockup) keep the full edit/test surface. Review needs it to
   ship bounded fixes before recording its exact-final-head verdict. Withheld
   tools are **removed from the loaded surface** (that is where the cache-creation
   savings live), with a settings-level deny as backstop — not deny-only.
2. **MCP servers are pinned to strict mode** for every stage: only the servers the
   launcher hands the session attach, so the operator's personal claude.ai connectors
   never leak in ([#240](https://github.com/ConnorGriffin/agentflow/issues/240)).
   Codebase Memory is the sole operator-local server re-supplied so daemon sessions
   keep the code graph ([#244](https://github.com/ConnorGriffin/agentflow/issues/244)).
   Its command and arguments cross the launch seam; its environment does not. Every
   other MCP definition is discarded. A read-only stage's allowlist includes Codebase
   Memory so Intake and Research keep the same graph access Build and Review have. It
   is the withheld *edit* tools, not this read-only graph, that a read-only stage loses.
3. **Per-stage wall-clock and turn ceilings replace the shared two-hour timeout**,
   sized ~1.5–2× the observed maximum per (stage, complexity, effort) cell — the
   table in the research doc is the source of truth. The stages ratcheted in
   [#410](https://github.com/ConnorGriffin/agentflow/issues/410) (Intake, Attack, Review,
   Respond, Converse) are sized against the **p95 with headroom** instead: a ceiling
   censors its own maximum, and the tail past the p95 is unbounded (research §3b′).
   Thin-sample stages (Revise, Research, Mockup) ship the conservative drafted ceilings
   now and ratchet once per-attempt telemetry (#223) fills their cells; Respond's has
   since filled, and Converse — still with no recorded sessions of its own — carries
   Respond's ceiling by construction. Revise inherits the original builder's Build
   ceiling (consistent with ADR 0041).
4. **No dollar-denominated session cap.** The objective is prepaid headroom
   (ADR 0040); wall + turn ceilings are the only kill switches.
5. **Fail closed by construction.** A withheld capability is *removed from the
   session's loaded tool surface* (allowlist strip, with the settings deny as an
   independent second strip), verified against the CLI init event: the tool has no
   schema, so a read-only stage cannot emit a call for it at all. The fail-closed
   guarantee is that the capability is **unreachable** — not that a reach is caught
   and converted to a hold. (An earlier draft assumed a denied tool stayed callable
   and produced a "permission denied" event the coordinator could turn into a
   capability-naming human hold; the CLI does not work that way — a denied or
   un-allowlisted tool is absent, not present-and-refused — so no such event exists
   to key on.) Hitting a ceiling remains a recoverable timeout-class ending.

## Consequences

- Ceiling numbers are expected to ratchet: after #223's telemetry lands, the
  before/after comparison in the research doc (§4) must confirm cache-creation
  savings, earlier kills, and no capability starvation, holding ADR 0040's
  quality guardrails flat. The profile mechanism itself is settled; the numbers
  are calibration.
- Intake remains read-only. Review's original read-only profile was superseded by ADR 0047 after
  operational evidence showed that report-only findings created unnecessary follow-on work.
