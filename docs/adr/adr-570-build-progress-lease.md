# ADR 570 — Build progress lease

- Status: accepted
- Date: 2026-08-12
- Ticket: [#570](https://github.com/ConnorGriffin/agentflow/issues/570)

## Context

Build attempts used one fixed child wall timeout for exploration, implementation, delegated
review, and the repository gate. Issue #531 twice reached its 2,700-second ceiling after making
durable progress, then spent a continuation reloading context and repeating verification. The
coordinator could observe that work, but it could not extend or supervise the already-detached
child that owns timeout enforcement.

A longer fixed wall would still choose between killing healthy long work and allowing a silent
runaway. Parent-side renewal would not survive a daemon restart and would split ownership of one
attempt between two processes.

## Decision

Build alone replaces its fixed child wall timeout with a child-local progress lease. The
detached supervisor renews its silent-inactivity deadline only on a new branch `HEAD`, a
completed `Edit`/`Write`/`NotebookEdit` action, or a completed successful recognized test.
The recognized test commands are `pytest`, `uv run pytest`, `npm test`, `npm run test`,
`pnpm test`, `yarn test`, `cargo test`, `go test`, and `make test`.

An in-flight recognized test may run through the silent deadline until its own test grace,
but neither test grace nor any later progress may cross the immutable attempt cap. Standard
Build uses 15m / 45m / 2h; deep medium/high uses 20m / 60m / 3h; deep extra uses 30m /
75m / 4h (silent / test / absolute).

Prose, chat, usage, reads, partial output, and repeated facts do not renew. Explicit
`session_timeout` and `AGENTFLOW_SESSION_TIMEOUT` remain fixed nonrenewable timeouts. Every
non-Build stage, including Revise, keeps its current fixed ceiling.

## Alternatives

- Raise Build's fixed wall ceiling. Rejected: it moves the false-timeout point without
  distinguishing progress from silence.
- Renew on any provider output or token activity. Rejected: prose, reads, retries, and usage can
  continue indefinitely without producing implementation progress.
- Let the coordinator renew the child. Rejected: the detached child is the crash-safe owner of
  timeout and process-group teardown, including while the daemon is down.

## Consequences

The supervisor retains existing timeout classification and TERM→KILL cleanup, so a lease or
cap expiry is the same recoverable timeout-class ending with the same retained worktree and
continuation accounting. No generic checkpoint protocol is introduced: the only facts are
already-durable output records and the worktree's `HEAD`.

Provider streams are decoded according to the record's pool. Codex facts come only from its
typed `item.started` / `item.completed` command and file-change records; Claude facts come only
from its paired `tool_use` / `tool_result` records. Malformed records, the other provider's
records, edits outside the worktree, and composed shell commands fail closed and do not renew or
gain test supervision. Composition includes chaining, pipes, redirects, command/process
substitution, variable/glob/brace/tilde expansion, comments, escaped shell syntax, and embedded
newlines. When recognized tests overlap, the earliest test deadline wins, so each test keeps its
own cap. `HEAD` is read from Git's durable ref files without starting a subprocess, and the
supervisor refreshes its monotonic clock after each observation before accepting progress.
Provider output is observed in bounded slices: at most 64 KiB and 128 JSONL records per poll,
with a cooperative 10ms parsing slice and a 1 MiB per-record ceiling. The supervisor checks the
current silent, test, and absolute deadline before and after each JSON decode; oversized records
and decoder failures, including pathological structural recursion, fail closed. Thus an output
burst cannot postpone teardown except for the bounded decode between two clock checks.
Durable reconstruction applies the same narrow decoder boundary to event and terminal-result
artifacts. A result is authoritative only when it has exactly the writer's three typed fields
(`exit_status: int|null`, `signal: int|null`, `timed_out: bool`); malformed, incomplete, or
unknown objects retain the pre-result `.exit` fallback rather than crashing recovery or inventing
an end fact. Duplicate result keys are malformed. Recovery reads only regular files and caps the
event artifact at 64 MiB, 100,000 records, 16 MiB per record and 16 MiB preserved partial output;
the result and legacy exit artifacts are capped at 4 KiB and 64 bytes respectively. These bounds
sit above the largest repository-evidenced legitimate session while keeping recovery finite.
Git ref observation is likewise regular-file-only, reads at most 8 MiB per metadata file, and runs
in a killable 25ms helper so a special or slow file cannot strand the provider-owning supervisor.
A natural provider exit retains its status and signal, but the supervisor refreshes its monotonic
clock after observing it and marks the result timed out when that observation is at or beyond the
silent, active-test, or absolute deadline that governed the iteration.
