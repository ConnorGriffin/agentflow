# Codex session-lead capability contract

Research for [#536](https://github.com/ConnorGriffin/agentflow/issues/536), captured
2026-08-07.

## Conclusion

The installed Codex runtime has the primitives to lead a Build or Revise session: it
can run a writable, sandboxed headless session; emit machine-readable events; constrain
a final response with a JSON Schema; resume a persisted non-interactive session; and
run native subagent workflows with model and reasoning configuration. It therefore is
not blocked by a missing provider feature.

Issue #509 implements the provider-neutral session-lead contract inside the existing
coordinator — the only coordinator; no separate Codex coordinator class or direct Codex build
path was added (ADR 538, ADR 541). Claude/Fable remains preferred when it can admit. A selected
Sol parent is launched non-ephemerally only for Build/Revise, so its native `spawn_agent` helpers
can attach to the parent thread; its Claude workers remain installed-CLI children. Durable
coordinator recovery, admission, lineage, and terminal proof are unchanged.

The native-helper mechanism itself is the 0.144.0 compatibility adapter documented in
[`codex-0.144-native-subagent-routing.md`](./codex-0.144-native-subagent-routing.md), not the
structured `model`/`reasoning_effort` spawn fields this note originally assumed: the installed
CLI's Sol-facing `spawn_agent` schema hides those fields, so `agentflow.codex_native_helpers`
generates a private per-launch custom role per reachable Codex worker and the parent selects it
through the hidden `agent_type` field instead. This note's own evidence — the opposite-provider
Codex→Claude CLI probe below — proves only that a Codex parent can shell out to the installed
Claude CLI; it does not and never did prove same-provider native `spawn_agent` delegation. That is
proven separately, from persisted Codex rollout metadata, by
[`codex-native-role-probe-2026-08-09.jsonl`](./evidence/codex-native-role-probe-2026-08-09.jsonl)
(captured via `scripts/codex-native-role-probe.py`): it shows the exact routed role, child model,
reasoning effort, and parent/child thread attribution the compatibility adapter produces — not
that every prompt reliably requests it (see that note's own reliability caveat).

## Required contract

| Capability | Required evidence | Current Codex / AgentFlow status |
| --- | --- | --- |
| Controlled parent launch | The provider adapter launches the parent in its assigned worktree with the Build/Revise profile, explicit model, sandbox, timeout, and durable launch token. | **Runtime yes; AgentFlow yes.** `CodexRunner.structured_argv` uses `codex exec`, `workspace-write`, `--cd`, `--json`, and the supplied profile. |
| Accountable routed delegation | The parent can delegate exploration, implementation, and repair through the one capability table; each worker has a pinned provider/model/reasoning rung, obeys bans, and follows the retry/escalate/stop rule. | **Integrated for the shared lead brief.** A Codex parent's native `spawn_agent` reaches a routed Codex worker through the 0.144.0 role adapter's hidden `agent_type` selector (private per-launch role files, `agentflow.codex_native_helpers`); an opposite-provider worker uses the installed Claude CLI. Version-gated and fails closed to the provider-failure rule on an unsupported build. No consumer-repository `.codex/agents/` file is needed. |
| Verification and stop rule | The lead inspects worker results, runs the repository gate, and stops with both failed attempts at the ladder top. | **Prompt-level contract only.** The shared lead brief carries this requirement; it must be retained verbatim for either parent. |
| Provider-independent terminal outcome | A clean parent exit is insufficient. Build completes only on the expected PR; Revise only on a verified pushed revision or required durable evidence. The coordinator returns only `completed` or `held`. | **Already provider-neutral.** The Build and Revise adapters verify GitHub/worktree facts outside provider output. Neither code-writing stage needs a model-produced result schema. |
| Gap handback and restart recovery | A missing outcome, provider interruption, or daemon restart produces one bounded fresh attempt or a stage-native human handoff. The fresh parent receives the durable recovery envelope; it never blindly repeats the same prompt. | **Coordinator-level support exists.** A Sol Build/Revise parent omits `--ephemeral` so native helpers can attach to its durable parent thread; coordinator-owned fresh-session recovery remains the authority and never relies on `exec resume`. All other Codex launches remain ephemeral. |
| Provider fact and spend accounting | The coordinator captures the parent family's events/usage and classifies only typed provider facts. Delegation must not silently evade the agreed spend/permit policy. | **Partially present.** `codex exec --json` supplies events and usage; capacity classification additionally needs the typed capacity helper. ADR 498 expressly accepts a temporary nested-Codex ledger bypass for the Claude parent, but no equivalent, measured policy exists for a Codex parent and its native children. |
| Independent review and merge safety | Review's tool must differ from the accountable parent tool; the parent tool—not a delegated worker—defines builder lineage. Same-tool review remains visibly tainted and cannot auto-merge. | **Provider-neutral implementation.** Build/Revise persist the selected parent lineage; ADR 538 preserves independent-review and same-tool-taint policy. |

## Evidence

### AgentFlow's current contract

- [ADR 498 — capability-routed session-led dispatch](../adr/adr-498-capability-routed-session-led-dispatch.md)
  makes one low-reasoning Claude/Fable parent accountable for planning, delegation,
  verification, and shipping, and explicitly defers a Sol/Codex parent to #509.
- [ADR 498 — headroom is a launch gate](../adr/adr-498-headroom-is-a-launch-gate.md)
  says Build and Revise launch only when Claude is clear; it allows the Claude parent to
  call nested Codex workers outside the permit ledger only as a temporary first adapter.
- [ADR 498 — parent-tool independent review](../adr/adr-498-tiered-parent-independent-review.md)
  makes the parent tool the accountable author and chooses the opposite tool for
  independent review.
- [`agentflow/model-routing.json`](../../agentflow/model-routing.json) declares Fable and Sol as
  session leads. [`routing.py`](../../agentflow/routing.py) renders the provider-aware native
  Codex/installed-Claude boundary and gets each launch identifier from that one table.
- [`balancer.py`](../../agentflow/balancer.py) selects Claude/Fable when it can admit and falls
  back to Codex/Sol when it cannot; the Build and Revise submission mappers preserve the selected
  accountable-parent lineage independently from retained branch/worktree lineage.
- [`profiles.py`](../../agentflow/coordinator/profiles.py) gives either Build or Revise
  parent the full write/test surface at low reasoning. [`build_stage.py`](../../agentflow/coordinator/build_stage.py),
  [`revise_stage.py`](../../agentflow/coordinator/revise_stage.py), and
  [`coordinator.py`](../../agentflow/coordinator/coordinator.py) prove completion and
  continuation from durable external facts rather than parent prose.

### Codex runtime and adapter facts

- The local `codex-cli 0.144.0` reports `multi_agent` as stable and enabled. Its
  `app-server generate-json-schema` output (inspected 2026-08-07) records child lifecycle
  states and a requested-model/reasoning shape, but that is the app-server surface, not what
  Sol's own `spawn_agent` tool call presents: the later 0.144.0 probe in
  [`codex-0.144-native-subagent-routing.md`](./codex-0.144-native-subagent-routing.md) found the
  live Sol-facing schema hides `agent_type`/`model`/`reasoning_effort` and exposes only
  `task_name`/`message`/`fork_turns`. Do not read the app-server schema as proof of what the
  model-facing tool call can express; the role adapter exists because of that gap.
- OpenAI documents that current Codex releases enable subagent workflows by default,
  supports custom agents with model and `model_reasoning_effort`, and warns that each
  subagent consumes additional tokens: [Subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents).
- OpenAI documents that `codex exec` supports explicit sandboxing, JSONL events,
  `--output-schema`, and `exec resume`: [Non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).
  `CodexRunner.structured_argv` retains `--ignore-user-config` for every launch and retains
  `--ephemeral` except for a Sol Build/Revise lead; that narrow exception keeps the parent thread
  addressable to native helpers without making CLI resumption part of recovery.
- Captured, sanitized acceptance evidence is
  [`codex-claude-child-probe-2026-08-09.jsonl`](./evidence/codex-claude-child-probe-2026-08-09.jsonl):
  a real workspace-write Codex parent completed the exact installed `claude --version` command
  with exit status zero. Synthetic negative cases remain in the hermetic probe tests.
- The Codex adapter intentionally treats a bare `codex exec --json` failure as unknown;
  it needs the typed account-limit companion to call a capacity interruption. See
  [`providers.py`](../../agentflow/coordinator/providers.py),
  [`runner.py`](../../agentflow/runner.py), and the earlier
  [provider interruption research](./provider-interruption-signals.md).

## Implications for the map

1. Codex can coordinate — this is settled, not an open eligibility question. #509 (ADR 538, ADR
   541) made native `spawn_agent` (through the 0.144.0 role adapter) the owned same-provider
   delegation mechanism, with the installed Claude CLI as the opposite-provider path; no second
   delegation mechanism competes with it.
2. Preserve the coordinator as the only owner of admission, attempts, recovery, and
   terminal handoffs. Native Codex thread state is useful implementation evidence, not a
   replacement persistence layer.
3. Keep parent-tool review independence. A Codex-led Build/Revise must choose Claude for
   independent review; a forced same-tool path must retain the existing human-merge taint.
4. Treat child spending as a first-class acceptance condition. Native parallelism is a
   capability, not a reason to make daemon activation less bounded.

## Implemented coordinator integration boundary

The existing coordinator is the only coordinator — #509 added no separate Codex coordinator
class. The following are its current invariants for a Codex-led Build/Revise:

1. **Lead identity and launch facts.** The provider adapter accepts the coordinator-provided record,
   model, profile, worktree, prompt, timeout, and durable launch token; report that its
   accountable parent tool is `codex`; and remain inadmissible when the Codex gate is
   not clear.
2. **Routed worker interface.** The shared brief gives worker requests a capability-table
   model/provider/reasoning rung plus bounded findings, not as an untyped shell string.
   The interface must enforce bans, the same-rung retry, one-rung escalation, and
   ladder-top handback. It must not invent a second routing table.
3. **Bounded child accounting.** Reported child lifecycle and usage facts remain necessary
   for AgentFlow to account for nested work without treating zero or a missing event as
   free (ADR 541: parent-stream usage is retained when reported, never synthesized as zero).
   A same-provider rung uses native `spawn_agent` through the role adapter; an
   opposite-provider rung uses the installed CLI. Neither is selected from personal
   configuration — both are the shared routing table's own provider field.
4. **Durable recovery boundary.** The provider adapter returns parent observation and final handback
   facts to the existing provider adapter/coordinator. It may not make CLI-native thread
   resumption the only recovery route: the stage-native external outcome remains authoritative.
5. **Parent-lineage review contract.** Its Build/Revise record must carry `codex` as the
   accountable builder lineage, preserve the owned branch/worktree across Revise, and
   make the reviewer selection choose Claude for independent review. A same-tool fallback
   must retain the human-merge taint.

After landing, verify through the public coordinator/pipeline surface:

- A ready issue with Claude unavailable and Codex clear admits exactly one Codex-led
  Build record, with the Codex lead model/profile and a Codex-owned branch lineage; a
  closed Codex gate admits no parent and consumes no attempt.
- The rendered brief states, for the shared routing table's assertions, retry with findings,
  then one-rung escalation, then a durable handback at the ladder top — a prompt-level contract
  only (see the "Verification and stop rule" row above). No executable test drives a live
  session lead through a real first-rung failure, escalation, and ladder-top handback; that
  would require a genuine lead-boundary integration test, which does not exist today.
- A parent whose process exits cleanly without an opened PR remains incomplete and takes
  the existing bounded recovery/hold path; an opened PR completes Build regardless of
  parent prose. The analogous Revise test requires a verified pushed revision or durable
  evidence.
- A blocking Claude review opens a Codex-led Revise on the retained Codex branch, and its
  next independent review is Claude. A forced Codex review is visibly tainted and cannot
  auto-merge.
- Captured Codex parent/child events prove nonzero usage is recorded when reported and
  missing usage remains unknown, not free. A daemon-restart fixture proves the existing
  restart-resume bound and one-handoff invariant still hold.

This checklist is implemented and verifiable through the public coordinator/pipeline surface
today, not a proposed future handoff.

## Limits

This research inspected the installed Codex runtime and AgentFlow's launch/configuration paths
and, together with
[`codex-0.144-native-subagent-routing.md`](./codex-0.144-native-subagent-routing.md), captured
sanitized evidence of both the opposite-provider CLI path and the same-provider native-role
adapter from real launches. It did not execute a live Codex-led Build inside the running daemon,
enable the daemon, or change capacity configuration. Per-helper spend persistence remains
deferred (ADR 541): native-helper usage is captured only when the parent stream reports it, and
that measured gap — not an undesigned coordinator — is the remaining open item.

The adapter has since been hardened past what this note originally described: the render-time
and launch-time capability checks are now one authoritative fact (a persisted, exact-match
`codex --version` marker on the record — a Codex upgrade or downgrade between submission and
launch fails the launch closed instead of silently omitting what the durable prompt already
promised); `codex_worker_roles` derives from every Codex model reachable in a worker rung of any
ladder, including Sol (plan/spec, prototype, and documentation route to it as a worker, not only
as a parent); and the owned role directory is removed by the launch supervisor's own
success/failure/spawn-failure `finally` path, with the 24h sweep kept only as the crash-recovery
backstop. The captured-evidence validator now also checks the routed reasoning rung and the
parent's non-ephemeral/strict-config/ignore-user-config/config-role-declared facts, not only the
routed role and model.
