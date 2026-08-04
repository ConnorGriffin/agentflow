# ADR 516 — Codex spend is estimated from the rate card, and worker spend is daemon-observed

- Status: Accepted
- Date: 2026-08-04
- Ticket: [#516](https://github.com/ConnorGriffin/agentflow/issues/516)
- Related: [ADR 0040](0040-spend-per-success-measurement-contract.md) (the spend report this
  extends), [ADR 498](adr-498-capability-routed-session-led-dispatch.md) (`CapabilityRouting`,
  the single read path this rate card is exposed through)

## Context

The date-window spend report (`agentflow/coordinator/telemetry.py`) showed provider-billed
dollars only. Codex reports tokens but no dollar cost, so every recorded Codex attempt —
244 of them at the time of this ticket — read as `$0.00`, indistinguishable from an attempt
that genuinely cost nothing. Separately, a session lead (a Claude `fable` build/revise session)
shells out to `codex exec` workers whose spend landed nowhere at all: neither on the lead's own
usage nor anywhere else.

Fixing the display without fixing the accounting would have made the report look complete while
still being wrong. Four decisions were frozen before implementation, to stop the natural
temptation to re-litigate them slice by slice:

## Decision

1. **Unknown is distinguishable from zero, everywhere.** Until worker capture lands for an
   attempt, a lead-run build/revise attempt is marked *delegate spend not counted* in the spend
   report — in the rendered text and in the structured report, so no aggregate reading it can
   silently treat it as fully measured.
2. **Worker spend is observed, never self-reported.** A session lead's Codex workers are priced
   from usage the daemon reads off the worker's own rollout files, and that usage rolls up into
   the ONE Build/Revise stage identity that spawned it — never a second telemetry record. The
   lead is never trusted to report its own delegate spend.
3. **Estimated dollars are flagged, always.** Codex token counts are priced from the routing
   table's rate card (`agentflow/model-routing.json`, exposed only through
   `CapabilityRouting` — ADR 498's single read path) and every non-provider-billed figure is
   flagged *estimated*. A total that mixes billed and estimated dollars is itself estimated —
   there is no way to report a "partially estimated" total as if it were a fact.
4. **History is read, never rewritten.** The 244 historical Codex attempts are priced from the
   same rate card at read/report time. The stored telemetry entry files stay byte-unchanged —
   pricing is a projection over durable facts, not a migration of them.

## Consequences

- The rate card lives once, in `model-routing.json`'s structured `rate_card`, validated at load
  time alongside the rest of the table (a priced model must exist; a malformed rate is a
  `RoutingConfigError`, not a silent zero). No cached-read rate is carried — cached reads are
  excluded from every estimate.
- `ModelSpendRow` grew `estimated` and `delegate_uncaptured_attempts`; `format_spend_report`
  renders both, and a cell with no dollar fact at all still renders `—`, never `0.000000`.
- Worker capture is read-side only: `ClaudeProviderAdapter.observe` scans the Codex sessions
  root (default `~/.codex/sessions`, overridable via `AGENTFLOW_CODEX_SESSIONS`) for rollouts
  whose `cwd` realpath-matches the lead's workspace, and merges their last cumulative
  `token_count` totals into the observation's `usage.model_costs` before the coordinator ever
  persists it. A scan failure of any kind degrades to no worker entries rather than failing the
  observation it rides on.
- Once a lead-run attempt's usage carries a Codex-priced model-cost entry (worker capture
  merged it in), the *delegate spend not counted* mark disappears for that attempt on its own —
  there is no separate flag to flip.
