# ADR 538 — Automatic Codex session-lead fallback

- Status: Accepted
- Date: 2026-08-07
- Ticket: [#538](https://github.com/ConnorGriffin/agentflow/issues/538)
- Restores: [ADR 0020](0020-build-review-under-partial-availability.md) for Build and Revise
- Amends: [ADR 498](adr-498-headroom-is-a-launch-gate.md)

## Context

ADR 498 made Build and Revise Claude-gated while the only session-lead adapter was Fable. Codex
can now meet the session-lead contract, but its parent and delegated Codex work must not bypass the
coordinator's durable five-permit ledger.

## Decision

When the daemon is already enabled, Build and Revise prefer Claude/Fable whenever Claude can
admit them. If Claude cannot admit and Codex can, they launch under Sol/Codex through the existing
session coordinator. A Codex-led parent reserves all five Codex permits for its full lifetime;
its descendants inherit that reservation and never reserve independently. This deliberately pauses
other Codex work while the parent runs, rather than adding nested-worker accounting.

Every fresh Build or Revise submission selects its parent pool before creating the durable record.
The existing submission mappers accept that selected pool, stamp it as the record pool and
builder lineage, and retain the branch/worktree lineage independently. This applies to cold and
manual Build, normal and CI-driven review Revise, and conflict Revise; it does not add a new
coordinator seam.

Review keeps the existing autonomy-profile policy. Autonomous work waits for Claude's independent
review. Reviewed work may receive a Codex same-tool review while Claude is unavailable, remains
visibly tainted, and requires a maintainer merge. No Codex-specific enablement switch or pilot
workflow is introduced.

## Alternatives

Keep Claude as the only lead and leave Codex capacity idle during a Claude outage. Rejected because
it preserves the throughput regression ADR 538 restores without adding safety evidence.

## Consequences

The lead-selection seam remains one provider-neutral coordinator interface, giving callers the
existing Build/Revise submission path and preserving locality for admission, recovery, and
parent-tool review independence. Codex fallback restores partial-availability progress, but a
running Codex-led session consumes the full Codex pool by design.

No live completed-session evidence exists yet for the new Sol parent row. Its full-five-permit
admission is a conservative policy decision, exercised by the synthetic historical admission replay
in the repository; it is not represented as an observed calibration.
