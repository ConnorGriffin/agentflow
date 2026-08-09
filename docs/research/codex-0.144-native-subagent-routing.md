# Codex CLI 0.144.0 native subagent routing for AgentFlow #509

Research for AgentFlow issue #509, captured 2026-08-09. This note uses only
installed-tool evidence, persisted rollouts from harmless probes, OpenAI's tagged
`rust-v0.144.0` source, the cached official Codex manual, and this checkout's
runtime/routing source.

## Answer

Codex CLI 0.144.0 **can execute an exact same-provider native child route today**, but
the usable interface is a hidden compatibility path rather than the schema it exposes to
Sol. MultiAgentV2's runtime accepts `agent_type`, `model`, and `reasoning_effort`, while
the Sol-facing `spawn_agent` schema removes those fields and shows only `task_name`,
`message`, and `fork_turns`.

The most defensible 0.144.0 adapter is:

1. AgentFlow creates a private temporary role file for every allowed Codex
   model/reasoning pair in the already validated routing table.
2. It launches the Sol parent with the existing `--ignore-user-config --strict-config`
   boundary and injects each role with CLI overrides:
   `-c agents.<role>.description=... -c agents.<role>.config_file=<absolute-temp-path>`.
3. The parent calls native `spawn_agent` with the hidden runtime field
   `agent_type:<role>` and **`fork_turns:"none"`**. The role file pins `model` and
   `model_reasoning_effort`; the child task message carries the bounded worktree task.
4. AgentFlow enables this adapter only for an explicitly supported CLI build after a
   startup acceptance probe proves the role, model, and effort from persisted child
   metadata. Any absent field, inherited model, unknown role, missing rollout, or probe
   failure closes native Codex delegation and holds/falls back through the existing
   provider policy. It must never silently run the wrong rung.

This is executable on the installed 0.144.0 build, and the probes below prove it. It is
not a stable documented contract: the role selector is absent from the exposed schema,
and one control run showed Sol refusing hidden model/effort overrides when the prompt did
not explicitly explain the compatibility path. AgentFlow should therefore isolate this
behind a versioned capability adapter and fail closed. Upgrade support can replace the
adapter once the installed CLI exposes and honors the documented explicit spawn fields.

This note narrows two claims in
[`codex-session-lead-capability-contract.md`](./codex-session-lead-capability-contract.md):
0.144.0 does run exact routed native helpers, but its Sol-facing schema does **not**
expose the structured model/reasoning fields. The earlier non-ephemeral requirement is
retained because the controlled `--ephemeral` probe failed before child creation while
the otherwise identical durable probe succeeded.

## Installed runtime facts

The binary is the Homebrew first-party release artifact:

```text
$ codex --version
codex-cli 0.144.0

$ command -v codex
/opt/homebrew/bin/codex

$ ls -l /opt/homebrew/bin/codex
/opt/homebrew/bin/codex -> .../Caskroom/codex/0.144.0/codex-aarch64-apple-darwin
```

`codex exec --help` establishes the launch controls relevant here:

- `-c key=value` applies dotted TOML overrides;
- `--strict-config` rejects unrecognized config;
- `--ignore-user-config` skips `$CODEX_HOME/config.toml` while authentication still uses
  `CODEX_HOME`;
- `--cd`, `--sandbox`, and `--json` provide the worktree, permission, and event boundaries;
- `--ephemeral` suppresses persisted session files.

`codex features list` reports `multi_agent` as stable and enabled. It lists
`multi_agent_v2` as under development and false, but the actual rollout
`turn_context.multi_agent_version` is `"v2"`; feature-list prose is therefore not a
sufficient capability test for this model/runtime combination.

The installed binary's first-party source-path strings include
`core/src/tools/handlers/multi_agents_v2/spawn.rs`, the exact Sol-facing tool description
for `task_name`/`message`/`fork_turns`, and the `MultiAgentV2ConfigToml` field
`hide_spawn_agent_metadata`. The authoritative matching source is OpenAI's tagged
[`rust-v0.144.0` V2 spawn handler](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs#L36-L80):

- `SpawnAgentArgs` is `deny_unknown_fields` but explicitly includes `agent_type`,
  `model`, `reasoning_effort`, `service_tier`, `fork_turns`, and legacy
  `fork_context` ([lines 170–180](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs#L170-L180)).
- Full-history forks reject role/model/effort overrides. Non-full-history spawns apply
  direct model/effort overrides and then `apply_role_to_config` ([lines 62–80](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs#L62-L80)).
- `fork_turns:"none"` is the no-fork mode; omitted `fork_turns` defaults to `all`, which
  is the incompatible full-history mode ([lines 182–212](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs#L182-L212)).

The schema/runtime mismatch is deliberate in this build. The tagged tool-spec source
first constructs V2 properties for `agent_type`, `fork_turns`, `model`,
`reasoning_effort`, and `service_tier`, then
`hide_spawn_agent_metadata_options` removes every selector except `fork_turns`
([`multi_agents_spec.rs` lines 556–602](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/core/src/tools/handlers/multi_agents_spec.rs#L556-L602)).
The live schema presented in this session and recorded developer/tool context therefore
contains exactly:

```text
task_name: string
message: string
fork_turns?: string
```

It contains no `agent_type`, `model`, or `reasoning_effort`. `task_name` is the child
instance/path name, not a role selector.

## Controlled executable probes

All probes ran in a temporary directory, used `--ignore-user-config`,
`--skip-git-repo-check`, `--sandbox read-only`, and instructed both parent and child not
to inspect files or invoke tools. No consumer repository content or private transcript
content is quoted here. Persisted messages are encrypted by Codex; only sanitized launch
fields, turn settings, and literal probe markers follow.

### Persistence is required in 0.144.0

The ephemeral command was:

```sh
codex exec --ignore-user-config --skip-git-repo-check --sandbox read-only \
  --ephemeral --json '<harmless spawn-one-child-and-wait probe>'
```

It started the parent, then emitted on stderr:

```text
ERROR codex_core::tools::router: collab spawn failed: no thread with id: <parent-id>
```

No receiver existed, and the parent reported that the child result was unavailable. The
otherwise identical command without `--ephemeral` completed:

```json
{"type":"item.completed","item":{"type":"agent_message","text":"PARENT_OK CHILD_OK"}}
```

This is stronger evidence than the help text alone: in this installed build, ephemeral
parent state is not registered where native spawn expects it. AgentFlow's current
`CodexRunner.structured_argv` behavior—omit `--ephemeral` only for Sol native-helper
parents—is required unless a future version-specific probe proves otherwise.

### Hidden direct model/reasoning fields execute exactly

A non-ephemeral Sol parent was explicitly told to issue a V2 tool call with
`fork_turns:"none"`, `model:"gpt-5.6-terra"`, and
`reasoning_effort:"medium"`. The sanitized persisted parent call is:

```json
{"name":"spawn_agent","arguments":{"task_name":"hidden_override_probe","fork_turns":"none","model":"gpt-5.6-terra","reasoning_effort":"medium"}}
```

The child rollout independently records:

```json
{"model":"gpt-5.6-terra","reasoning_effort":"medium","multi_agent_version":"v2"}
{"model_provider":"openai","agent_path":"/root/hidden_override_probe","agent_role":null}
```

The run completed `PARENT_OVERRIDE_OK CHILD_OVERRIDE_OK`. Thus the fields are not
rejected as unknown by the runtime; they are hidden from the model-facing schema.

A control attempted `-c multi_agent_v2.hide_spawn_agent_metadata=false`. Sol still said
the available interface did not expose model/effort, emitted a call without them, and the
child inherited `gpt-5.6-sol`. A local config toggle is not an adequate way to restore a
public schema on this installed/model combination.

### Injected custom roles execute exact model and effort

The temporary role layer was:

```toml
model = "gpt-5.6-luna"
model_reasoning_effort = "low"
developer_instructions = "Return exactly CHILD_ROLE_OK and do not use any tools."
```

The strict-config parent launch added:

```text
-c agents.routed_probe.description="Issue 509 harmless routed-role probe"
-c agents.routed_probe.config_file="<absolute-temporary-path>/routed-role.toml"
```

and the parent was explicitly told to issue `agent_type:"routed_probe"` with
`fork_turns:"none"`. Sanitized persisted evidence is:

```json
{"name":"spawn_agent","arguments":{"task_name":"custom_role_probe","fork_turns":"none","agent_type":"routed_probe"}}
{"model":"gpt-5.6-luna","reasoning_effort":"low","multi_agent_version":"v2"}
{"agent_path":"/root/custom_role_probe","agent_role":"routed_probe"}
{"type":"agent_message","message":"CHILD_ROLE_OK","phase":"final_answer"}
```

The matching 0.144.0 config schema declares flattened `[agents.<role>]` entries with
`description` and `config_file`
([`config_toml.rs` lines 621–662](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/config/src/config_toml.rs#L621-L662)).
The tagged role loader states that roles are selected at spawn time, loads the role as a
high-precedence config layer, and returns `unknown agent_type '<name>'` for an unresolved
role ([`role.rs` lines 25–75](https://github.com/openai/codex/blob/rust-v0.144.0/codex-rs/core/src/agent/role.rs#L25-L75)).

A negative control configured the same role but supplied only
`task_name:"routed_probe"`, with no `agent_type`. Its child ran on inherited
`gpt-5.6-sol`, not Luna. Therefore **`agent_type` selects the role in 0.144.0;
`task_name` only names an instance**.

### What was not proved

The model-facing tool generator, rather than an external API, authored the successful
hidden-field calls. The runtime accepts them, but the schema does not promise the model
will emit them on every prompt/model build. This is why successful one-off execution is
not enough to enable the feature fleet-wide without an installed-version capability
probe and fail-closed verification.

## Current documentation versus 0.144.0

The cached official manual at
`.../openai-docs-cache/codex-manual.md`, section **Multi-agent operations → Custom
agents**, links to OpenAI's current [Subagents documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents.md).
It describes standalone personal/project files under `~/.codex/agents/` and
`.codex/agents/`, explicit spawn values, and global
`agents.default_subagent_model` / `agents.default_subagent_reasoning_effort`. The cached
configuration reference likewise lists those default keys and says explicit spawn values
take precedence.

Those are current/newer documentation facts, not an accurate exposed-tool contract for
the installed Sol + CLI 0.144.0 combination:

- the installed tagged schema uses `[agents.<role>]` plus `config_file` and does not
  declare the two newer `default_subagent_*` fields;
- the installed V2 runtime can consume explicit spawn values, but its live Sol-facing
  schema hides them;
- standalone role-file discovery is unnecessary for AgentFlow because strict CLI `-c`
  declarations plus an absolute temporary `config_file` were executable and avoid
  modifying the consumer repository.

Do not implement against the cached manual without a minimum-version and live-capability
gate. The honest upgrade boundary is the first installed release/model combination whose
actual `spawn_agent` schema exposes a role or model and reasoning selector and whose
persisted child metadata passes the acceptance test below. Version alone is necessary
but not sufficient because the selected model can affect the reserved tool schema.

## Fit with AgentFlow's current routing and runner

Primary checkout sources already provide most of the required boundary:

- [`agentflow/model-routing.json`](../../agentflow/model-routing.json) is the one routing
  table. Its Codex models are Sol (`gpt-5.6-sol`, lead), Terra
  (`gpt-5.6-terra`, worker), and Luna (`gpt-5.6-luna`, worker); its worker reasoning map
  is `low`, `medium`, `high`, `xhigh`. Area ladders and bans decide which pairs exist.
- [`agentflow/routing.py`](../../agentflow/routing.py) validates every model/provider/CLI
  id, owns the stage parent and review selection, renders the worker ladder, and maps the
  issue effort to the worker reasoning rung. No compatibility adapter should copy this
  table.
- [`agentflow/runner.py`](../../agentflow/runner.py) launches Codex with the routed parent
  id, assigned worktree, sandbox, JSONL, `--ignore-user-config`, narrow MCP overrides,
  and parent reasoning. It already omits `--ephemeral` only when `native_helpers` is true.
  It currently does not inject roles or enforce a child-capability probe.

The role set should be generated from the validated same-provider route pairs reachable
for the current session, not maintained as another static matrix. Suggested opaque role
names such as `af_codex_luna_medium` are implementation identifiers; the role file owns
the exact CLI id and `model_reasoning_effort`.

## Security, isolation, and recovery

Use a config overlay, not a replacement `CODEX_HOME`:

- Keep `--ignore-user-config` so personal MCP servers, skills, and user roles do not enter
  AgentFlow. The successful strict-config role probe proves CLI `-c` declarations still
  work across that boundary.
- Create a mode-0700 per-launch temporary directory outside the consumer worktree and
  mode-0600 role files containing only allowlisted model/effort/instructions. Use absolute
  `config_file` paths. Never write `.codex/` into a consumer repository.
- Keep the user's real `CODEX_HOME` only for its existing authentication and native
  session store. `codex exec --help` explicitly says `--ignore-user-config` ignores
  config while auth still uses `CODEX_HOME`.
- A fresh generated `CODEX_HOME` is not self-contained: it has no login material. Copying
  or symlinking `auth.json` would duplicate or broaden access to a credential, and a
  keyring-backed installation may not be representable that way. It also moves rollout
  and SQLite state, weakening AgentFlow's ability to verify child metadata and recover
  stale probe artifacts. Do not use this mechanism unless Codex gains a supported
  separate config-home flag or AgentFlow is explicitly provisioned with session-scoped
  auth.
- Non-ephemeral parents and children persist rollouts in the existing Codex home. That is
  required by 0.144.0 native spawn but has privacy/disk-retention consequences. AgentFlow
  should retain only sanitized child attribution/usage facts and never copy transcripts
  into its records.
- Remove the temporary role directory after the parent exits. A crash can leave
  non-secret config files; use a per-launch owner-only directory and bounded stale-file
  cleanup. Persisted Codex rollouts remain Codex-owned recovery evidence, not the
  coordinator's resume authority.

## Rejected alternatives

### Treat the visible schema as the full runtime

Rejected. Tagged source and a live child prove hidden direct overrides execute. Failing
closed solely because the fields are absent would discard an available compatibility
path, though absence still requires gating it.

### Prompt-steer a generic child

Rejected. A generic child inherits the parent settings; prose asking it to “act as Luna
medium” does not change its model. Persisted metadata, not self-report, is the proof.

### Use `task_name` as a role name

Rejected by the negative control. It names `/root/<task>` and does not apply the matching
configured role.

### Pass hidden `model` and `reasoning_effort` on every spawn

Executable, but less defensible than generated roles. It asks Sol to reproduce two hidden
free-form values correctly on every call. A generated role reduces this to one allowlisted
hidden selector and makes the model/effort an immutable config layer that can be verified.

### Set one global default child model/effort

Rejected for arbitrary routing. Even newer documented defaults supply only one pair per
parent, while one Sol lead can need Luna, Terra, and Sol workers at different reasoning
rungs. The installed 0.144.0 tagged `AgentsToml` does not declare those newer default keys
anyway.

### Restore metadata with `multi_agent_v2.hide_spawn_agent_metadata=false`

Rejected on this build/model. The live control still exposed the restricted interface and
spawned an inherited Sol child. It is not an AgentFlow portability contract.

### Generate a temporary `CODEX_HOME`

Rejected as the normal path because config isolation already exists through
`--ignore-user-config` plus `-c`, while a new home also replaces the auth and session-state
location. Seeding it would mishandle credentials or require new operator provisioning.

### Launch separate nested `codex exec` processes

Rejected for issue #509's same-provider **native-helper** requirement. It can pin a model
and effort through public CLI flags/config, but it is a provider subprocess, not a native
child thread, and loses native parent/child lifecycle and attribution.

### Claim broad support from one successful hidden call

Rejected. The schema omission makes this a compatibility behavior. Ship it only behind an
exact installed-capability test; otherwise mark native helpers unavailable and use the
existing provider fallback/hold behavior.

## Minimal executable acceptance test

Run this before enabling the adapter for an installed CLI/model combination. The test is
harmless but intentionally non-ephemeral so native spawn can register and persist the
evidence.

1. Assert `codex --version` belongs to the adapter's explicit compatibility allowlist.
2. Create an owner-only temporary role file containing a non-parent routed pair, for
   example Luna/medium, and launch a read-only Sol parent with `--strict-config`,
   `--ignore-user-config`, and CLI role declarations.
3. Instruct the parent to make exactly one tool call with
   `task_name:"af_acceptance"`, `agent_type:"af_luna_medium"`, and
   `fork_turns:"none"`; the child returns one literal marker and uses no tools.
4. Bound the parent wall time. On completion, locate the parent and child rollouts by the
   returned parent thread id and assert all of the following from JSON—not model prose:

```json
parent function_call arguments:
{"task_name":"af_acceptance","fork_turns":"none","agent_type":"af_luna_medium"}

child session_meta source:
{"agent_path":"/root/af_acceptance","agent_role":"af_luna_medium"}

child turn_context:
{"model":"gpt-5.6-luna","reasoning_effort":"medium","multi_agent_version":"v2"}

child final marker:
"AF_CHILD_OK"
```

5. Run the same parent command with `--ephemeral` and require the native spawn to be
   classified unavailable if it still reports `no thread with id`. Do not accept a parent
   final answer that merely paraphrases failure.
6. Delete the temporary role directory. Record only version, requested role/model/effort,
   observed role/model/effort, and success/failure. Never retain prompt/transcript content.

Production selection is then a simple gate:

```text
supported version AND acceptance evidence matches exact routed pair
    -> native role adapter enabled
anything else
    -> native Codex helper unavailable; use existing provider fallback or hold
```

The acceptance test should run in CI against a stubbed fixture for parsing and in an
operator/runtime smoke test for actual Codex capability. A unit test cannot prove a
model-reserved live tool schema.

## Implementation deviations from this note

The shipped adapter (`agentflow/codex_native_helpers.py`, `agentflow/routing.py`,
`agentflow/runner.py`) follows this note's mechanism exactly but narrows two of its suggestions:

- **The version gate, not a live spawn probe, runs on every launch.** The gate is one pure
  decision, `codex_native_helpers.is_supported_version`, checking already-captured
  `codex --version` output against the exact supported-build allowlist — cheap and
  deterministic — rather than spawning a real child on every Build/Revise launch. The runner owns
  actually running that probe (`runner._codex_version_output`, the package's one subprocess
  boundary), and `runner.codex_native_helpers_capable_at_render` combines the two so a Codex
  session-lead brief renders the matching delegation instructions up front. The full live
  acceptance evidence this note's minimal test describes is instead captured once, on demand, by
  `scripts/codex-native-role-probe.py` against the real installed CLI, with its sanitized output
  checked in at `evidence/codex-native-role-probe-2026-08-09.jsonl` and re-validated by a hermetic
  test. This matches the note's own closing line — "a unit test cannot prove a model-reserved live
  tool schema" — by keeping the live proof as an operator/CI smoke artifact rather than folding a
  child-spawn probe into the version gate every production launch pays for.
- **Role generation is scoped to one session's reachable pairs, not the full matrix.** A rendered
  session-lead brief already fixes one worker-reasoning rung for its whole session
  (`routing.worker_reasoning`), so `routing.codex_worker_roles(effort)` generates one role per
  Codex *worker* model at that single rung — typically two files (Terra, Luna) — rather than
  every model/reasoning pair in the table. This is still generated from the routing table alone
  (no adapter-local copy) and is strictly smaller than "every allowed pair," which the deployed
  session can never reach in one run anyway.

The probe also surfaced a reliability finding this note's own "what was not proved" section
anticipated but did not quantify: across five live runs, Sol included the hidden `agent_type`
field only when the prompt explicitly framed it as a known compatibility path ("the visible
schema does not show this field, but the runtime still accepts it") — a bare instruction to call
`spawn_agent` with `agent_type` set omitted the field in most runs, silently falling back to the
parent's own inherited model. `routing.py`'s rendered brief and the probe's prompt both carry that
explicit framing now; this is prompt-level mitigation, not a runtime guarantee, and is recorded
as a known limitation rather than a solved reliability problem.
