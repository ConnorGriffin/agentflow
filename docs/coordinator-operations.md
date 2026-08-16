# Coordinator operations

All provider work is coordinator-owned. There is no legacy execution mode or bypass switch.

## Activate

Complete [capacity calibration](getting-started.md#calibrate-capacity) before
activation; it is a prerequisite.

Validate the repository configuration, start the daemon under the process supervisor,
then inspect it while still paused:

```bash
uv run agentflow check
uv run agentflow service install
uv run agentflow status
```

When the startup log and configured repositories are correct, permit cold submissions:

```bash
uv run agentflow resume
uv run agentflow status
```

Activation permits cold submissions. It does not discard existing records; the first full pass
reconciles them before admission.

To keep one provider out of admission while the other continues, use the durable per-pool control:

```bash
uv run agentflow pool pause claude
uv run agentflow pool status claude
uv run agentflow pool resume claude
```

A per-pool pause survives service restarts and is not bypassed by operator dispatch or floodgates.
It does not replace the fleet-wide `pause`/`resume` drain boundary.

To keep a deliberately human-owned open issue (for example an operations ledger) out of
unattended intake and other issue dispatch, add the neutral `agentflow:ignore` label. It is an
operator opt-out, not a pipeline state or ownership claim; remove it only when the issue should
become eligible for unattended work.

## Observe

Use `uv run agentflow status` and `~/Library/Logs/agentflow.log`, captured by the
installed per-user LaunchAgent. The useful transient lines are exact and stage-specific:

- `attempt N/3 → <pool>` — one durable provider start consumed an attempt;
- `recovered running attempt N/3 pid <pid> — observing until <deadline>; claim retained`;
- `continuation N/2 (attempt N/3) → <pool>`;
- `attempt N/3 completed — <stage outcome>; claim transferred to <stage>`;
- `reclaimed orphaned <lane> claim — no live family or continuation record`;
- `claim reconciliation deferred — coordinator state unreadable`.

The console's `live-sessions.json` is a generated projection. Do not use or edit it to diagnose
ownership, attempts, permits, claims, or recovery; use the coordinator records and GitHub outcome.
An orphaned visible claim is held for a one-hour safety grace before removal so a short,
deterministic interactive scope operation cannot race the daemon.

For the read-only observational report over one repository and UTC window, see
[Learning pipeline: Run the report](learning-pipeline.md#run-the-report) for its
bounded facts, missingness, and non-action contract.

## Console service

`uv run agentflow service install` starts the daemon and the read-only operator console
(ADR 0035) as two separate per-user LaunchAgents from the same installed checkout — restart
after an unexpected exit is `KeepAlive`, one job per process. Pausing cold submission
(`uv run agentflow pause`) does not touch either service; the console stays up and readable
while dispatch is paused. Only `service install` (reload) and `service remove` change
which processes are running.

- **URL** — the console binds only to loopback: `http://127.0.0.1:8788`. It never queries
  GitHub directly; it serves the daemon's last published snapshot.
- **Logs** — `~/Library/Logs/agentflow.log` (daemon) and
  `~/Library/Logs/agentflow-console.log` (console), each the corresponding LaunchAgent's
  combined stdout/stderr.
- **Health check** — `uv run agentflow status` reports `daemon`, `console`, and `cold
  submission` as three independent facts (process failure and dispatch pause are not the
  same thing, and neither implies the other). The `console` line is a live `GET
  /api/snapshot` probe against `127.0.0.1:8788`; the same check can be run by hand:
  `curl -sf http://127.0.0.1:8788/api/snapshot`.
- **Restart** — `uv run agentflow service install` rewrites both LaunchAgents with the
  current executable and environment, then reloads both in place (`launchctl bootout` +
  `bootstrap`).
- **Removal** — `uv run agentflow service remove` stops and unloads both LaunchAgents and
  deletes only their generated plists. It does not touch coordinator state, configuration,
  or the cold-submission enabled flag.

### Wayfinder research awaiting disposition

Closed Wayfinder research always has a durable disposition; no hidden issues-to-file list
exists outside GitHub. A research ticket carrying `wayfinder:awaiting-disposition` has
finished its unattended investigation but still needs human judgment. Leave it open, do
not restore `wayfinder:resolving`, and disposition every candidate through Wayfinder:
each selected build is filed and indexed, while each unselected candidate is explicitly
recorded as no-build or deferred with a concrete trigger and verification condition.

### Wayfinder research parked for you

A ticket carrying `wayfinder:parked` is the opposite outcome: an unattended run ended without
recording a ruling the contract accepts, so it handed the ticket back (ADR 362). Read the
ticket's own comment before acting — it says which of two things happened, and they need
different responses:

- **The run answered, and the answer was refused.** The comment names the exact check that
  refused the ruling and carries whatever the run did write. Rewrite the question so a bounded
  session can answer it, or answer it in a Wayfinder session.
- **The coding agent never got to read the question** — a refused sign-in, a rejected request,
  or a spend ceiling stopped the session. The comment names that condition and its remediation.
  The question itself is untouched, so once the coding agent is healthy, file a fresh research
  ticket asking it again.

Either way, unattended research will never pick this ticket up again, and removing the label
does not restart it: the run's record is terminal, so a new question wants a new research
ticket. Do not restore `wayfinder:resolving` by hand.

## Maintenance

Inventory disposable residue without changing Git, worktrees, files, or Codebase Memory:

```bash
uv run agentflow maintenance
```

The command emits JSONL records with `action`, `eligible`, `reason`, and either `path` or
`project`. Review that complete inventory, then apply the same guarded reconciliation explicitly:

```bash
uv run agentflow maintenance --apply
```

Apply removes only missing Git registrations; inactive, clean worktrees carrying AgentFlow's
validated disposable ownership marker; exact known historical probes in such worktrees; and graph
projects whose recorded roots are missing or were removed in that run. It refuses dirty, live,
held, retained, unknown-owned, reachable, and unreachable entries. A second run is a no-op unless
new eligible residue appeared. Maintenance is operator-invoked and does not run in the dispatch
loop.

## Pause and drain

```bash
uv run agentflow pause
uv run agentflow status
```

Pause stops new cold submissions. The resident daemon still runs heartbeat passes that observe
running families, admit owned continuations, transfer claims, and finalize completion or a human
hold. Drain is complete when no non-retired coordinator record is waiting or running and every
completed record has transferred or settled its claim. Do not delete the coordinator database or
claim labels to accelerate a drain.

Stopping the process is not a drain. If it must be stopped, leave it paused and restart the same
coordinator-aware binary; reconciliation resumes from durable records.

## Upgrade

1. `uv run agentflow pause`.
2. Let active records drain, or keep the daemon running on the current coordinator-aware binary
   until they reach a durable boundary.
3. Update the clone to the new coordinator-aware revision and run `uv sync --group dev`.
4. Run `uv run agentflow check`, then `uv run agentflow service install`. The install
   command rewrites the LaunchAgent with the current executable, configuration, state,
   capacity-helper, and `PATH`, then reloads it in place.
5. Inspect the recovery lines, then run `uv run agentflow resume`.

Schema compatibility is a release requirement. If the new binary reports unreadable or newer
coordinator state, leave it paused; it must start nothing and clear no claim.

## Roll back

Rollback is bounded by durable coordinator ownership:

1. Pause cold submissions.
2. Drain active records with a coordinator-aware binary.
3. Check out only a revision that understands the existing coordinator store schema and preserves
   coordinator-only launching.
4. Run `uv sync --group dev` and `uv run agentflow service install` from that revision.

Never run a pre-coordinator or legacy-capable binary against active coordinator-owned work. There
is no safe conversion of a waiting/running record into a legacy retry. If no compatible rollback
revision exists, roll forward.
