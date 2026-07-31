# Coordinator operations

All provider work is coordinator-owned. There is no legacy execution mode or bypass switch.

## Activate

Validate the repository configuration, start the daemon under the process supervisor,
then inspect it while still paused:

```bash
uv run agentflow check
uv run agentflow capacity calibrate
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
