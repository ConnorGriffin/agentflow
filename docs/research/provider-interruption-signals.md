# Provider interruption signals

Research for [Research reliable Claude and Codex interruption signals](https://github.com/ConnorGriffin/agentflow/issues/92),
against Claude Code 2.1.181 and Codex CLI 0.144.0 on 2026-07-15.

## Conclusion

Agentflow can classify Claude capacity, authentication, and server failures from the
CLI's structured stream. Codex has the same typed facts in its app-server protocol,
but `codex exec --json` currently discards the error type and retains only the error
message. Exit status alone is therefore a terminal signal, not a failure-kind signal.

The runner should preserve four independent observations for every launch: structured
events, process exit/signal, supervisor timeout, and the required stage outcome. It
should never infer the cause from exit status alone or from arbitrary model text.

## Reliable signals

| Observation | Classification | Resume time |
| --- | --- | --- |
| Claude `rate_limit_event` with `status: "rejected"` | recoverable capacity interruption | event `resetsAt`, when present |
| Claude assistant `error: "rate_limit"`, or error result with `api_error_status: 429` | recoverable capacity interruption; use the rate-limit event to distinguish the window | event reset; otherwise bounded backoff |
| Claude `authentication_failed` / HTTP 401, `billing_error` / HTTP 402, or permission/config error | human hold | none |
| Claude `server_error` with HTTP 500/504/529, terminal transport loss, or runner timeout/signal | recoverable execution interruption | bounded backoff; immediate for a fresh-session context/timeout continuation |
| Codex app-server `UsageLimitExceeded` plus a current reached window from `account/rateLimits/read` | recoverable capacity interruption | matching window `resetsAt` |
| Codex app-server connection/stream/failed-attempt/internal-server error | recoverable execution interruption | bounded backoff |
| Codex app-server `Unauthorized`, `BadRequest`, or `SandboxError` | human hold | none |
| Codex `turn.failed`/top-level `error` or non-zero exit without a typed cause | incomplete interruption, not a diagnosis | bounded continuation; query typed account/rate-limit state before deciding capacity versus hold |
| Either CLI reaches runner timeout, is killed by a signal, crashes, or closes without a terminal event | recoverable execution interruption if the local stage state is safe | bounded backoff |
| Agentflow's explicit bail marker | deliberate bail, never a recoverable interruption | none |

An incomplete clean exit belongs to the same bounded continuation path. A successful
process is not a successful stage until the stage's required outcome exists.

## Claude Code evidence

Claude print mode supports `json` and `stream-json`; the stream is the useful adapter
because it preserves events emitted before an abrupt exit. [Claude Code documents the
machine-readable output modes](https://code.claude.com/docs/en/headless#stream-responses),
and Anthropic's Agent SDK provides the concrete wire vocabulary:

- `RateLimitEvent.rate_limit_info.status` is `allowed`, `allowed_warning`, or
  `rejected`; it also carries `resets_at`, `rate_limit_type`, and utilization. The
  current wire type carries `resetsAt`; the Python SDK additionally models named
  windows and utilization. Anthropic's own test names `status == "rejected"` as a hard
  limit and verifies the Unix reset timestamp. The event requires Claude Code 2.1.181
  or later. ([current SDK reference](https://code.claude.com/docs/en/agent-sdk/typescript#sdkratelimitevent),
  [parser test](https://github.com/anthropics/claude-agent-sdk-python/blob/main/tests/test_rate_limit_event_repro.py))
- `AssistantMessage.error` includes `authentication_failed`, `oauth_org_not_allowed`,
  `billing_error`, `rate_limit`, `overloaded`, `invalid_request`, `model_not_found`,
  `server_error`, and `unknown`.
  A terminal result separately supplies `is_error` and, for API failures, a safe-to-log
  `api_error_status`. ([assistant errors](https://code.claude.com/docs/en/agent-sdk/typescript#sdkassistantmessage),
  [result fields](https://code.claude.com/docs/en/agent-sdk/typescript#sdkresultmessage))
- `system/api_retry` reports an attempt and delay *before* Claude retries a transient
  API failure. It is progress telemetry, not a terminal interruption. Classification
  waits for the final result or process end. ([streaming reference](https://code.claude.com/docs/en/headless#stream-responses))
- Anthropic defines HTTP 401 as authentication, 402 as billing, 429 as rate limit,
  and 500/504/529 as server, timeout, and overload failures. It calls connection,
  rate-limit, and 5xx failures transient and notes that SDKs retry them before returning
  control. ([Anthropic error reference](https://platform.claude.com/docs/en/api/errors),
  [Claude Code error reference](https://code.claude.com/docs/en/errors))

The local Claude 2.1.181 transcript for the interrupted `ciq-autotune` build corroborates
the structured contract: the terminal assistant event had `error: "rate_limit"`,
`isApiErrorMessage: true`, `apiErrorStatus: 429`, and a synthetic message reporting the
local reset time. `rate_limit_event.resetsAt` is authoritative when emitted. If it is
absent, match only Claude's documented session/weekly/Opus-limit terminal messages and
their reset suffix; do not search arbitrary tool or model output for rate-limit words.

## Codex evidence

`codex exec --json` emits JSONL events including `turn.completed`, `turn.failed`, and
top-level `error`. OpenAI documents this as the automation surface, and Codex's own
test requires a server failure to produce exit code 1. ([non-interactive mode](https://developers.openai.com/codex/noninteractive),
[exit test](https://github.com/openai/codex/blob/main/codex-rs/exec/tests/suite/server_error_exit.rs))

Those signals reliably say that the turn ended, but not why:

- The exec JSON schema's `ThreadErrorEvent` contains only `message`.
  ([exec event schema](https://github.com/openai/codex/blob/main/codex-rs/exec/src/exec_events.rs))
- The JSONL adapter receives the typed app-server error and deliberately maps only its
  message into `ThreadErrorEvent`; a failed terminal turn then becomes `turn.failed`.
  ([JSONL adapter](https://github.com/openai/codex/blob/main/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L408-L445))
- Non-terminal `item.completed` entries of `type: "error"` are warnings and must not
  override a later `turn.completed`. Terminal turn state plus process exit is the
  boundary.

The app-server surface retains the missing facts. Its `error` and failed-turn payloads
carry `codexErrorInfo`, including `UsageLimitExceeded`, connection/response-stream
failures with optional HTTP status, `Unauthorized`, `BadRequest`, `SandboxError`, and
`InternalServerError`. Its documented `account/rateLimits/read` method returns current
primary/secondary windows, `usedPercent`, `windowDurationMins`, `resetsAt`, and a
backend-classified `rateLimitReachedType`. ([app-server errors and auth API](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#errors),
[rate-limit API](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md#7-rate-limits-chatgpt))

One caution is load-bearing: Codex maps subscription exhaustion, billing quota, and
"usage not included" to the same protocol value, `UsageLimitExceeded`.
([core mapping](https://github.com/openai/codex/blob/main/codex-rs/protocol/src/error.rs#L177-L203))
Therefore that enum alone must not schedule a continuation. Treat it as a capacity pause
only when a fresh `account/rateLimits/read` response supplies a reached window and future
reset; otherwise hold it as a plan/billing problem.

## Design consequence

The shared runner classification can be tool-neutral, but signal extraction remains in
two adapters:

- Claude should be launched with structured streaming and classified from typed events.
- Codex needs either the typed app-server event surface or a companion app-server
  account/rate-limit query. Keeping only `codex exec --json` requires an explicit
  `unknown interruption` fallback and cannot meet exact failure-kind classification by
  itself.

No new ADR is warranted for these provider facts. The later decision about whether the
Codex adapter adopts app-server is architectural and belongs in the runner-seam decision,
where alternatives and consequences can be judged together.

Both adapters should fixture-test the supported CLI versions and retain unknown fields.
Current documentation includes behavior newer than the installed Claude 2.1.181, so a
future implementation must feature-detect or pin the event contract instead of assuming
the latest documentation describes every installed binary.
