# Claude-only session-lead assumptions

Research for [#537](https://github.com/ConnorGriffin/agentflow/issues/537), captured 2026-08-07.

## Conclusion

The durable coordinator is already provider-aware: records have a pool, provider adapters
launch and observe either pool, admission derives permits from the selected pool, and the
merge gate judges reviewer independence against the recorded builder tool. The Claude-only
restriction is therefore not a reason to add a Codex coordinator or a direct Codex build
path. It is a set of fixed choices at the **session-lead boundary**.

Assume the separately-developed Codex coordinator class lands. Its integration is correct
only if it becomes another eligible *parent* selected by the existing coordinator, while
the coordinator remains the sole owner of admission, attempts, recovery, claims, and
terminal handoffs. A Codex parent must use the same routing table and stage outcome checks
as the Claude/Fable parent; it must not turn a clean provider exit, a native child result,
or an unavailable Claude pool into completion or an unbounded retry.

## Current fixed assumptions

| Boundary | Current Claude-only fact | Post-landing integration requirement |
| --- | --- | --- |
| Lead selection | `choose_session_lead` discards Codex status and returns Claude only; `pick_session_lead` deliberately does not query Codex capacity. | Select from both eligible parent pools using their typed launch gates. Claude unavailable + Codex clear may admit one Codex parent; neither clear admits none. A lead must never bypass the normal cold-submission/admission path. |
| Routing | `CapabilityRouting.LEAD_POOL` is `"claude"`; config validation requires Fable to be the sole `session-lead`, and `resolve` admits session-led stages only on that pool. | Represent lead eligibility in the one routing table and render a provider-neutral lead brief. The parent receives worker requests from that table, including bans, reasoning rung, retry-with-findings, one-rung escalation, and ladder-top handback. It does not carry a second routing table. |
| Build/Revise submission | Build and all Revise constructors stamp `pool="claude"`, build a Claude lane worktree ref, and stamp `builder_lineage="claude"` for Revise. | Submission must pick the selected parent pool/model once and use it consistently for `pool`, parent launch profile, source/worktree lane, and accountable builder lineage. Branch lineage remains the tool owning the retained branch and can differ from the current Revise parent. |
| Admission and attempt ownership | The admission matrix has Build/Revise parent rows only for `("claude", "fable", ...)`. The coordinator commits an attempt only after the durable `started` fact. | Add Codex parent rows through the existing immutable matrix and provider adapter. Do not add a child-specific permit manager, direct provider spawn, or attempt counter. One root record reserves the parent family; descendants remain charged to that root reservation. |
| Recovery and retained worktree | Record state, launch tokens, recovery envelopes, and source pointers are generic; `worktree_ready` selects the runner by `record.pool` and reuses an existing branch/worktree. | Codex-led recovery must retain the exact attempt budget, launch-token idempotency, and stage-native outcome proof. A retry uses the same retained Codex branch when present; a reclaimed checkout is recovered only by the existing branch/stranded-ref rules. A missing outcome still takes the bounded continuation-or-hold path, not a fresh untracked session. |
| Review attribution and independence | Review chooses the opposite tool when possible. The gate compares `reviewer_tool` with `builder_tool`; same-tool review is tainted and cannot auto-merge. | A Codex parent is the accountable builder tool even when it delegates. A Codex-led Build/Revise therefore prefers Claude independent review. If only Codex is available, the existing visible taint/human-merge path remains; no "parent used a Claude worker" exception may make it independent. |
| Telemetry and spend | Attempt telemetry is keyed by launch token and records the selected pool/model plus normalized usage. Codex usage preserves missing values as unknown and has no provider-dollar value. | Persist parent and delegated-child spend without treating missing usage as zero or free. The root attempt/verified-stage linkage, restart idempotency, stage × pool × model attribution, and failed-attempt numerator all remain intact. A Codex coordinator must return enough durable child facts for that accounting rather than relying on personal CLI configuration or parent prose. |

## Durable path audit

### 1. Selection and routing are the narrow Claude gate

`agentflow/balancer.py` makes the restriction explicit: `choose_session_lead` deletes the
Codex status and returns a Claude runner only, while `pick_session_lead` makes Codex a
synthetic closed pool. `agentflow/routing.py` repeats the rule in `LEAD_POOL`, routing
validation, and session-led resolution. Those are policy seams to generalize after the
incoming class exists; they are not evidence that the coordinator itself is Claude-only.

The shared lead instructions already contain the desired accountability and stop rule:
plan/delegate/verify, use the capability table, retry once with findings, escalate once,
then hand back at the ladder top. They are Claude-shaped in their worker mechanics
(Claude native subagents plus `codex exec`), so provider-neutral selection must come with a
provider-neutral delegated-work interface. The class must consume the table; it must not
independently choose providers or models.

Sources: [`balancer.py`](../../agentflow/balancer.py#L354-L361),
[`balancer.py`](../../agentflow/balancer.py#L543-L559),
[`routing.py`](../../agentflow/routing.py#L50-L50),
[`routing.py`](../../agentflow/routing.py#L149-L167), and
[`routing.py`](../../agentflow/routing.py#L190-L247).

### 2. Admission and attempts must stay coordinator-owned

ADR 0030 assigns the coordinator alone the waiting queue, continuation order, admission,
attempt accounting, provider lifecycle, and handoff state. It requires a durable
`started`/`not_started` handshake, with an attempt consumed only for `started`. The
implementation follows that rule: `Record` holds launch identity, family, source, lineage,
attempt count, and recovery state; `_consume_attempt` is idempotent against the durable
start fact. The matrix is the one remaining hard-coded parent admission boundary.

The Codex class can be a provider launch/observation implementation. It cannot own an
alternate lifecycle, independently reserve capacity for nested work, or classify an
untyped provider exit as permission to launch another parent.

Sources: [ADR 0030](../adr/0030-session-coordinator-seam.md),
[`record.py`](../../agentflow/coordinator/record.py#L47-L176),
[`coordinator.py`](../../agentflow/coordinator/coordinator.py#L975-L981), and
[`admission.py`](../../agentflow/coordinator/admission.py#L89-L185).

### 3. Lineage and recovery are provider-neutral data with Claude-shaped writers

The record distinguishes `builder_lineage` (accountable original builder) from
`branch_lineage` (owner of the retained checkout). That distinction is necessary for a
Codex parent: Revise may run under either eligible parent, but must operate on the branch
that already owns the PR. `worktree_ready` already derives its runner from `record.pool`
and preserves an existing worktree unchanged; its recovery semantics should need no
provider-specific replacement.

The direct writers in `coordinated_build.py` and `coordinated_revise.py` are the risk:
they currently stamp Claude for pool, source, and parent lineage. Generalizing only the
balancer would therefore create records whose admission, worktree path, and merge
attribution disagree.

Sources: [`record.py`](../../agentflow/coordinator/record.py#L123-L176),
[`coordinated_build.py`](../../agentflow/coordinated_build.py#L33-L64),
[`coordinated_revise.py`](../../agentflow/coordinated_revise.py#L59-L152),
[`stage_worktree.py`](../../agentflow/stage_worktree.py#L21-L90), and
[ADR 0050](../adr/0050-bounded-worktree-retention.md).

### 4. Review independence follows the parent, not a worker

The review selection path already accepts a builder-tool input and prefers the opposite
pool. The merge gate requires a different reviewer tool from builder tool; a clean verdict
cannot overcome missing independence. A Codex-led parent must therefore write `codex` as
the accountable builder lineage. Delegating a slice to Claude does not convert a Codex
parent into a Claude-built PR, and a Codex-only review must stay tainted for human merge.

Sources: [`balancer.py`](../../agentflow/balancer.py#L364-L388),
[`gate.py`](../../agentflow/gate.py#L280-L314), and
[ADR 498 — parent-tool independent review](../adr/adr-498-tiered-parent-independent-review.md).

### 5. Spend remains an outcome-linked, provider-attributed fact

The coordinator records every ended attempt under its launch token, allowing restart
reconciliation to overwrite the same entry rather than double-count it. The Codex parser
keeps missing usage explicit and gives no unearned dollar value; Claude and Codex normalize
different provider fields into the same attempt usage shape. ADR 0040 requires failed and
superseded spend to remain in the verified-stage numerator. Those rules are especially
important for a Codex parent with children: child output cannot vanish into an unmeasured
native-agent transcript, and unavailable usage cannot be priced as zero.

Sources: [`coordinator.py`](../../agentflow/coordinator/coordinator.py#L1217-L1249),
[`telemetry.py`](../../agentflow/coordinator/telemetry.py#L139-L226),
[`telemetry.py`](../../agentflow/coordinator/telemetry.py#L284-L308), and
[ADR 0040](../adr/0040-spend-per-success-measurement-contract.md).

## Post-landing acceptance evidence

The integration should be accepted only when public coordinator/pipeline tests demonstrate:

1. Claude unavailable + Codex clear admits exactly one Codex-led Build or Revise through the
   normal coordinator; a closed Codex gate launches nothing and consumes no attempt.
2. The parent obeys the shipped routing table for delegation, including banned-model refusal,
   same-rung retry with findings, one-rung escalation, and a bounded handback at the ladder top.
3. A clean Codex parent exit without the required PR/revision stays incomplete and follows the
   existing recovery/hold path; daemon restart preserves the single attempt and single
   handoff invariants.
4. A Codex-owned Build branch survives into Revise. A Codex parent has Claude independent
   review when available; a forced same-tool review is tainted and cannot auto-merge.
5. Parent and child Codex usage is persisted once per durable launch identity; reported usage
   is nonzero where observed, while absent usage stays unknown rather than zero/free.

## Candidate supported by this research

The separately landing Codex coordinator class is an incoming dependency. After it lands,
one independent vertical integration remains: wire it into the existing selector, routing,
submission, admission, lineage, recovery, review, and telemetry seams under the acceptance
evidence above. This research does not authorize daemon activation; the map's activation
policy decision remains separate.

## Limits

This was a source and ADR audit. It did not modify the incoming class, change daemon state,
or launch a live Build/Revise session.
