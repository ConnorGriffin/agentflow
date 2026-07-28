# Coordinator operations

All provider work is coordinator-owned. There is no legacy execution mode or bypass switch.

## Activate

Validate the repository configuration, start the daemon under the process supervisor,
then inspect it while still paused:

```bash
agentflow check
agentflow daemon
agentflow status
```

When the startup log and configured repositories are correct, permit cold submissions:

```bash
agentflow resume
agentflow status
```

Activation permits cold submissions. It does not discard existing records; the first full pass
reconciles them before admission.

## Observe

Use `agentflow status` and the daemon log captured by the process supervisor. The useful transient
lines are exact and stage-specific:

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

## Pause and drain

```bash
agentflow pause
agentflow status
```

Pause stops new cold submissions. The resident daemon still runs heartbeat passes that observe
running families, admit owned continuations, transfer claims, and finalize completion or a human
hold. Drain is complete when no non-retired coordinator record is waiting or running and every
completed record has transferred or settled its claim. Do not delete the coordinator database or
claim labels to accelerate a drain.

Stopping the process is not a drain. If it must be stopped, leave it paused and restart the same
coordinator-aware binary; reconciliation resumes from durable records.

## Upgrade

1. `agentflow pause`.
2. Let active records drain, or keep the daemon running on the current coordinator-aware binary
   until they reach a durable boundary.
3. Deploy the new coordinator-aware revision.
4. Restart `agentflow daemon` through its process supervisor, inspect the recovery lines, then
   run `agentflow resume`.

Schema compatibility is a release requirement. If the new binary reports unreadable or newer
coordinator state, leave it paused; it must start nothing and clear no claim.

## Roll back

Rollback is bounded by durable coordinator ownership:

1. Pause cold submissions.
2. Drain active records with a coordinator-aware binary.
3. Deploy only a revision that understands the existing coordinator store schema and preserves
   coordinator-only launching.

Never run a pre-coordinator or legacy-capable binary against active coordinator-owned work. There
is no safe conversion of a waiting/running record into a legacy retry. If no compatible rollback
revision exists, roll forward.
