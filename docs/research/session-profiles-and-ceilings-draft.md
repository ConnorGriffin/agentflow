# Draft: stage session profiles and spend ceilings

Research draft for [Wayfinder #4: prototype stage-specific tool profiles and spend
ceilings](https://github.com/ConnorGriffin/agentflow/issues/231) (map
[#226](https://github.com/ConnorGriffin/agentflow/issues/226)), captured 2026-07-19.

The question: **how much spend comes from oversized session surfaces and ceilings
rather than the selected model?**

> **This is a design draft, not a ruling.** The measured-savings half of #231 is
> deliberately deferred: it needs [#223](https://github.com/ConnorGriffin/agentflow/issues/223)'s
> per-attempt telemetry to prove a before/after. What is settled here is (1) what
> history *can already show* about wasted surface and dead ceilings, and (2) the
> smallest safe per-stage profile and ceiling policy those numbers justify. No ADR
> ships until the operator reviews and the post-#223 comparison runs. This applies
> the [spend-per-success measurement contract](spend-per-success-measurement-contract.md)
> ([ADR 0040](../adr/0040-spend-per-success-measurement-contract.md), on PR #233)
> as written.

All numbers are read-only from `~/.agentflow/coordinator` (215 Claude sessions with
a result event, 2026-07-16 → 07-19), joined to their stage record by launch token,
with stage inferred from the worktree path for the 60 unjoined streams. No new
sessions were run.

## 1. What every session gets today: one surface, one ceiling, for every stage

The daemon builds *one* Claude command for every stage — Intake, Review, Build,
Respond, Revise, Converse, Research, Mockup all share it
(`agentflow/runner.py`, `ClaudeRunner.structured_argv`):

```
claude -p <prompt> --model <model> --output-format stream-json --verbose
       --permission-mode acceptEdits --setting-sources project --settings <sandbox>
```

There is **no `--allowed-tools`, no `--disallowed-tools`, no `--max-turns`, no
per-session budget flag, and no per-stage variation.** The only lever that changes
between stages is `--model` (the complexity dial, owned by
[#230](https://github.com/ConnorGriffin/agentflow/issues/230)/ADR 0041). Every
stage is handed the full editing surface and the full skill catalogue whether it is
a read-only Review or a code-writing Build.

The single time ceiling is likewise stage-blind: `_SESSION_TIMEOUT_S` and
`SUPERVISOR_WINDOW` are both a hardcoded **2 hours** (`agentflow/coordinator/launcher.py`,
`coordinator.py`), the same for a 90-second Review and a 42-minute Build.

## 2. Historical waste, quantified

### 2a. Surface: 90–95% of the loaded tool surface is never touched

Each Claude session loads a **median of 29 tools** (max 59). Grouping by stage and
counting which of those tools the session actually *invokes*:

| Stage | n | distinct tools used (median) | **unused surface** | of the ~29 always-loaded tools, # never used by *any* session of the stage |
|-------|---|------------------------------|--------------------|-----------------------------------------------------------------------------|
| Intake | 54 | 2 | **93%** | 24 / 29 |
| Review | 70 | 2 | **93%** | 26 / 29 |
| Build | 77 | 3 | **90%** | 22 / 29 |
| Respond | 4 | 2 | 95% | 25 / 29 |
| Revise | 5 | 3 | 90% | 25 / 29 |
| Converse | 3 | 3 | 90% | 26 / 29 |

The read-only stages are the sharpest: across **54 Intakes and 70 Reviews**, the
*only* tools ever used are `Bash`, `Read`, and a handful of strays. Everything else
— `AskUserQuestion`, `Cron*`, `DesignSync`, `EnterPlanMode`, `Monitor`,
`NotebookEdit`, `RemoteTrigger`, `ScheduleWakeup`, `Task*`, `PushNotification`,
`Web*` — is loaded into every one of those sessions and never called.

**A correctness note, not just a cost note:** the read-only stages *did* reach for
edit tools they should never have — **3 `Write` calls across the 54 Intakes and 2
`Edit` calls across the 70 Reviews**. A read-only profile would have fail-closed on
these instead of letting a review mutate its checkout. That is the charter's
fail-closed argument arriving for free.

### 2b. Personal MCP servers leak into daemon sessions

**82 of 216 Claude sessions loaded three personal MCP servers** — `claude.ai Google
Drive`, `claude.ai Google Calendar`, `claude.ai Gmail` — and 12 sessions had their
tool schemas fully expanded into the surface (that is where the 59-tool maximum
comes from). None of these are relevant to any agentflow stage. `--setting-sources
project` was meant to exclude user config, yet the MCP servers still attach;
launching under strict MCP mode and re-supplying only the operator's local dev
servers (the code-graph tool) closes this leak outright while keeping code-graph
available to daemon sessions (#244).

### 2c. Ceiling: the 2-hour timeout is dead — it never fires, and it can't kill early

Session wall-clock against the 2-hour (7200s) ceiling:

| | p50 | p90 | p95 | p99 | max |
|---|---|---|---|---|---|
| duration | 130 s | 706 s | 1 314 s | 2 340 s | **2 520 s** |

- **The median session uses 1.8% of the ceiling; p95 uses 18.3%.**
- **Zero of 215 sessions reached even half the ceiling.** The longest session ever
  observed (a `deep/extra` Build, 42 min) used 35% of it.
- The 2-hour timeout therefore contributes **no savings mechanism today**: nothing
  ever approaches it, so it neither paces spend nor kills a runaway before it has
  already burned far more than a healthy session would. A stage that genuinely
  hangs can burn up to two hours before the deadline notices.

Per-stage duration and turn distributions (the basis for §3's ceilings):

| Stage (complexity/effort) | n | dur p50 | dur p95 | dur max | turns p95 | turns max |
|---------------------------|---|---------|---------|---------|-----------|-----------|
| Intake (deep) | 54 | 139 s | 308 s | 338 s | 28 | — |
| Review (deep) | 70 | 94 s | 217 s | 292 s | 22 | — |
| Respond (deep) | 4 | 62 s | 356 s | 401 s | 24 | — |
| Converse (deep) | 3 | 147 s | 211 s | 218 s | 21 | — |
| Revise (deep) | 5 | 404 s | 842 s | 934 s | 75 | — |
| Build deep/extra | 3 | 2 346 s | 2 503 s | 2 520 s | 224 | 229 |
| Build deep/high | 11 | 1 092 s | 1 789 s | 2 057 s | 144 | 146 |
| Build deep/medium | 15 | 522 s | 1 775 s | 2 306 s | 133 | 197 |
| Build standard/low | 12 | 251 s | 781 s | 875 s | 46 | 49 |
| Build standard/medium | 2 | 280 s | 286 s | 286 s | 35 | 36 |

### 2d. How the surface cost shows up in headroom

Prepaid headroom is the optimization target (ADR 0040). Weighting all 215 sessions'
tokens with the Claude gate formula (`input + 1.25×cache_creation + 5×output`):

| component | weighted headroom | share |
|-----------|-------------------|-------|
| output | 16.15 M | **53%** |
| **cache_creation** | 13.95 M | **45%** |
| input | 0.60 M | 2% |
| cache_read (412.8 M raw tokens) | 0 (weight 0) | — |

**Reading this honestly, against the ticket's exact question:**

- Just over half of headroom (**53%, output**) is driven by *how much the model
  writes* — a function of model and work effort, i.e. #230/#228's territory, **not**
  surface or ceiling.
- Just under half (**45%, cache_creation**) is the cost of *loading context and
  surface into cache each session*. The tool-schema + skill-catalogue prefix is a
  fixed, stage-invariant slice of this, re-created on every session. Per-stage
  cache_creation medians: Build 54.3k, Revise 40.6k, Intake 38.7k, Review 34.4k,
  Respond 9.6k.

So the surface is a **real, roughly-half-of-headroom lever**, but the draft is
careful not to overclaim: history cannot yet decompose the 45% into "tool schemas"
vs "repo files the session `Read`" vs "prompt/skills." That decomposition is
exactly what #223 must measure. What history *can* say: the surface is oversized
(§2a–2b), and the ceiling is inert (§2c).

## 3. The proposal: smallest safe per-stage profile + real ceilings

The seam is `provider_command(record)` → `ClaudeRunner.structured_argv(...)`
(`agentflow/coordinator/providers.py`, `runner.py`). The record already carries
`stage`, `complexity`, and `effort` at launch, so a profile table keyed on
`(stage, complexity, effort)` attaches with no new plumbing. The launcher's uniform
`session_timeout` becomes a per-record value the same table supplies.

### 3a. Tool profiles

| Stage | Tool surface | MCP | Skills / slash-commands |
|-------|--------------|-----|-------------------------|
| **Intake** | read-only: `Read`, `Bash`, `Grep`, `Glob`, `WebFetch`, `ToolSearch`; **no `Edit`/`Write`/`NotebookEdit`** | none | disable all except a structured-verdict skill if one is used |
| **Review** | read-only: `Read`, `Bash`, `Grep`, `Glob`; no edit tools | none | disable all |
| **Research** | read-only + only the external tool its question needs (`WebSearch`/`WebFetch`); no edit tools | none | research skill only |
| **Build / Respond / Revise** | full edit/test surface (`Read`/`Edit`/`Write`/`Bash`/`Grep`/`Glob`/…) | none | keep repo-relevant skills; drop personal/scheduling ones |

Rules for every profile: **MCP set pinned to strict mode, re-supplying only the
operator's local servers** — the code-graph tool, allowlisted for read-only stages
too (closes §2b, keeps code-graph; #244) — and
`Cron*`/`Schedule*`/`RemoteTrigger`/`PushNotification`/`Monitor`/`Task*`/plan-mode
tools dropped everywhere — history shows them loaded in every stage and used in
none.

The Intake/Review read-only + structured-result shape is the same surface
[#224](https://github.com/ConnorGriffin/agentflow/issues/224) (provider-native
structured result schemas for Intake and Review, currently open, no build PR yet)
will enforce; this draft and #224 should land the read-only profile once, together,
not twice.

### 3b. Per-stage / model / effort ceilings (replacing the single 2-hour timeout)

Each ceiling is set well **above the observed max** for headroom against natural
variance, while still killing a genuine runaway **2–6× sooner** than the current
2-hour wall. Turn ceilings come from the harness's `--max-turns`; the wall-clock
ceiling replaces `_SESSION_TIMEOUT_S` per record. A per-session USD budget is also
available (`error_max_budget_usd` already surfaces in the result stream) as a
belt-and-braces cap.

> **The Intake, Review, Respond and Converse turn ceilings below are superseded — see
> §3b′ (#410).** They were drawn from the sample in §2c and drifted under the work once
> the sample grew. They are kept here as the record of what the first calibration was.
> **The Build standard rows, and the Revise row that inherits them, are likewise superseded —
> see §3b″ (#416).** The Research, Mockup and Build deep rows still stand.

| Stage (complexity/effort) | observed max | **wall ceiling** | **turn ceiling** | vs 2 h today |
|---------------------------|--------------|------------------|------------------|--------------|
| Intake (deep) | 338 s | 20 min | 40 | 6× tighter |
| Review (deep) | 292 s | 15 min | 40 | 8× |
| Respond (deep) | 401 s | 15 min | 40 | 8× |
| Converse (deep) | 218 s | 15 min | 40 | 8× |
| Research (deep) | 682 s (n=1) | 30 min | 80 | 4× |
| Mockup (deep) | 2 429 s (n=1) | 60 min | 200 | 2× |
| Revise (carries builder complexity) | 934 s | = builder's Build ceiling | = builder's | — |
| Build standard/low | 875 s | 25 min | 80 | 5× |
| Build standard/medium | 286 s | 25 min | 80 | 5× |
| Build deep/medium | 2 306 s | 45 min | 200 | 2.7× |
| Build deep/high | 2 057 s | 45 min | 200 | 2.7× |
| Build deep/extra | 2 520 s | 60 min | 300 | 2× |

Revise inherits the original builder's Build ceiling, mirroring ADR 0041's ruling
that finding-driven Revise carries the builder's complexity.

### 3b′. First ratchet against live telemetry (#410, 2026-07-31)

§3b promised these numbers would ratchet "once per-attempt telemetry (#223) fills the
thin cells". They have, and the first reading showed the rule in §3b had been violated
by drift rather than by choice.

**Measure the work, not the counter the ceiling censors.** The provider's reported turn
counter *is* the quantity `--max-turns` bounds: every session the ceiling actually killed
reports exactly the cap plus one — the kills under a cap of 40 all at 41 turns, the kills
under a cap of 80 all at 81, without exception. That is exactly why its recorded
distribution cannot calibrate the ceiling: it is truncated by the ceiling being
calibrated. Nor is the truncation uniform — the cap did not fire on every session that
reported past it, and dozens of sessions in the 40-capped stages report 42 to 87 turns and
end cleanly — so a capped stage's turn distribution mixes cut-off sessions with survivors
and describes neither.

The table below therefore counts **tool calls** — the calls a session issues, read off its
own recorded stream, a number nothing in the launch truncates. Tool calls are not turns,
but they track them tightly: turns are tool calls plus one in about nine of every ten
sessions that recorded both, and across the whole sample a session's turns never exceed its
tool calls by more than two. The tie is broken the other way by a turn that issues several
calls at once (in the extreme, one review stopped at a cap of 40 had issued 196). A turn
ceiling set clear of a tool-call p95 is therefore clear of the turn p95 to within two turns
— slack nowhere near deciding this pin, whose tightest margin is 80 against 51 and whose
widest is 120 against 66. A drift big enough to matter moves these numbers by tens.

Measured over 516 recorded sessions — a snapshot of a store that grows as the fleet
runs, taken 2026-07-31 before the three builds §3b″'s later pass caught (which is why
its build cells sum to 134 against the 131 here; reconciled at the top of §3b″, #422):

| Stage | n | tool calls p50 | p90 | p95 | max | dur p95 | dur max | cap |
|-------|---|-----------|-----|-----|-----|---------|---------|-----|
| Review | 210 | 26 | **55** | **66** | 335 | 469 s | **899 s** | **40** |
| Build | 131 | 48 | 113 | 138 | 164 | — | — | 80–300 |
| Intake | 87 | 19 | 39 | **45** | 52 | 335 s | 559 s | **40** |
| Respond | 55 | 14 | 40 | **51** | 57 | 477 s | 594 s | **40** |
| Revise | 23 | 41 | 95 | 99 | 100 | — | — | inherited |
| Research | 6 | 33 | 39 | 39 | 39 | 608 s | 608 s | 80 |
| Attack | 2 | 44 | 44 | **44** | 44 | 109 s | 109 s | **40** |
| Mockup | 2 | 66 | 66 | **66** | 66 | 1 013 s | 1 013 s | 200 |

**Every stage capped at 40 has its p95 above the cap**, review worst at 66 against 40 —
and review is simultaneously pinned against its wall, its longest session running 899 s
against a 900 s ceiling.

The direct evidence is stronger than the percentiles. **42 sessions were terminated by
the ceiling**, the provider naming it outright ("Reached maximum number of turns (40)"),
31 of them reviews. The work each had already finished before it was cut off, in tool calls:

- review — 40, 44, 46, 46, 47, 47, 47, 48, 48, 48, 48, 49, 50, 50, 50, 50, 50, 51, 51,
  52, 52, 53, 53, 55, 55, 57, 60, 60, 70, 70, 196
- respond — 40, 42, 46, 55 · intake — 48 · build — 80, 89, 92 · revise — 95, 99, 100

Each had read the diff and run the suite and was stopped before it could state a verdict.
$104.18 spent for no verdict, no fix and no decision.

Applying §3b's own rule to that sample:

| Stage | wall ceiling | turn ceiling | grounding (p95) |
|-------|--------------|--------------|-----------------|
| Intake | 20 min (unchanged) | 40 → **80** | 45 tool calls / 335 s |
| Attack | 20 min (unchanged) | 40 → **80** | 44 tool calls / 109 s (n=2); mirrors intake (ADR 380) |
| Review | 15 → **30 min** | 40 → **120** | 66 tool calls / 469 s |
| Respond | 15 → **20 min** | 40 → **80** | 51 tool calls / 477 s |
| Converse | 15 → **20 min** | 40 → **80** | shares respond's shape; no recorded sessions |
| Research | unchanged (30 min / 80) | unchanged | 39 tool calls / 608 s — already clear |
| Mockup | unchanged (60 min / 200) | unchanged | 66 tool calls / 1 013 s (n=2) — already clear |
| Build, Revise | unchanged | unchanged | ~~p95 138 / 99 tool calls against 80–300 — already clear~~ **wrong; superseded by §3b″ (#416)** |

> **The Build/Revise row above is withdrawn.** It compares one figure pooled across all
> build cells against the whole range of build ceilings, which are set per cell. The pooled
> p95 of 138 is the deep tier's number and the ceiling it clears is the deep tier's ceiling;
> the standard tier, sitting on its own ceiling, averaged out of view. The kill list ten
> lines above contradicts the row directly — three builds and three revisions are on it.
> §3b″ breaks the same sample out per cell.

Every cell the code pins carries its sample here, and only here: `_OBSERVED_P95` in the
profile table is this row set (plus §3b″'s), so a number pinned by a test always has a
stated source.

**p95 with headroom, not the maximum.** Two reasons. A ceiling censors its own
distribution — review's 899 s maximum is the wall stopping it, not the work finishing,
and every count sitting on a cap is a session cut off rather than done — so the
observed numbers are lower bounds. And the tail is genuinely unbounded: one review ran
335 tool calls, which no ceiling that still kills a runaway could clear. Setting the bar at
p95 and then leaving headroom above it is what makes an ordinary long session survive
while a runaway still dies.

The observed distribution now lives beside the ceiling table in code, so the next drift
fails a test rather than parking a pull request.

### 3b″. Per-cell reading for Build and Revise (#416, 2026-07-31)

The same session store and the same quantity (tool calls read off the session
stream), broken out per `(stage, complexity, effort)` rather than pooled — but **not
the same snapshot** (#422). This pass re-read the store later the same morning as
§3b′'s, by which point it had grown to 529 recorded sessions, so the build cells
below sum to 134 where §3b′'s pooled row counts 131. Recounting the store at each
read point reproduces both figures exactly: three builds finalized between the reads
— one deep/high at 69 tool calls, two standard/low at 18 and 40 — and none of them
moves any reading in this table, so no ceiling turns on the difference. Where the
two counts disagree, this per-cell sample is the authoritative one: it is the row
set `_OBSERVED_P95` carries and the one the build ceilings are set from.

| Cell | n | p50 | p90 | **p95** | max | **ceiling** | killed at it |
|------|---|-----|-----|---------|-----|-------------|--------------|
| Build standard/low | 33 | 30 | 65 | **80** | 92 | 80 → **160** | 1 |
| Build standard/medium | 17 | 23 | 82 | **89** | 89 | 80 → **160** | 2 |
| Revise (standard, inherits the standard Build tier) | 12 | 45 | 99 | **100** | 100 | 80 → **160** | 3 |
| Build deep/medium | 35 | 40 | 94 | **138** | 144 | 200 (unchanged) | 0 |
| Build deep/high | 34 | 78 | 138 | **157** | 164 | 200 (unchanged) | 0 |
| Build deep/extra | 6 | 113 | 146 | **146** | 146 | 300 (unchanged) | 0 |

**The standard tier was sitting on its own work.** Standard/low's p95 lands exactly on
its ceiling; standard/medium's is 9 above it; a revision inheriting the tier reaches 100
against 80. All six of the ceiling kills in §3b′'s build and revise lists are in this
tier — 80, 89 and 92 for builds, 95, 99 and 100 for revisions — with the provider naming
the cause outright ("Reached maximum number of turns (80)"). Each had done most of the
job and lost it.

**The deep tier is clear against its own readings** — 138, 157 and 146 against 200, 200
and 300, with no kills — so those cells do not move.

**Raising Build raises Revise.** A revision carries the original builder's cell (ADR
0041) rather than a ceiling of its own, so the standard tier's rise covers it and no
separate Revise ceiling is introduced.

**Two cells are unmeasured and are given no number of their own.** Build deep/low (n=8,
p95 86) and Build standard/high (n=1, 63 tool calls) are not named in the ceiling table
and fall back to the per-complexity default — 200 and 160 respectively. Deep/low is
comfortably clear at that fallback; standard/high's single session is too thin a sample
to rule on either way, and is recorded here as unmeasured rather than dressed up as a
reading.

**Only tool calls were read per cell.** The wall-clock ceilings are not in evidence in
this pass and are unchanged; no build or revise session in the sample was stopped by its
wall. Raising a turn ceiling does raise what a genuine runaway can burn before it dies,
and the wall ceiling is the backstop that bounds that — 25 minutes for the standard tier,
which is what makes 160 turns a safe number to allow.

### 3c. Fail-closed when a narrow profile is missing a capability

The narrow profile must **fail closed** on a withheld capability (e.g. a Review
reaching for `Edit`, as 2 historically did) — never silently degrade and never
silently "succeed."

*Correction from implementation (ADR 0044 pt 5):* the draft assumed the withheld
tool would remain callable and produce a "permission denied" event the coordinator
could turn into a capability-naming human hold. Verifying against the CLI init
event showed that both the `--tools` allowlist and a `permissions.deny` block
*remove the tool from the loaded surface entirely* — it has no schema, so the model
cannot emit a call for it. Fail-closed is therefore delivered by **unreachability**:
the read-only stage physically cannot exercise the capability. There is no denied
event to catch, so there is no capability-naming hold on this path (and none is
needed — nothing degrades because nothing can be edited). A ceiling hit is separate:
the wall/turn ceiling is a `TIMEOUT`-class recoverable end (existing behavior), and
the tightened numbers make that kill *useful* instead of theatrical.

## 4. What the post-#223 comparison must confirm

This draft is grounded in *what sessions did*, not *what a narrowed session would
cost* — that requires the instrumented before/after #223 unlocks. The comparison
must measure, per stage, holding the [ADR 0040](../adr/0040-spend-per-success-measurement-contract.md)
quality guardrails (merge rate, review BLOCK rate, revise success, human-hold rate)
flat:

1. **Smaller initial context / fewer cache-creation tokens.** Does pinning the tool
   set and MCP to a narrow profile measurably drop per-session `cache_creation`
   headroom? History says cache_creation is 45% of headroom but cannot isolate the
   schema slice; #223's per-attempt token capture at launch can.
2. **Earlier, cheaper kills.** Under the new ceilings, do the tail sessions (the
   handful above p95) terminate sooner *without* raising the human-hold or
   unfinished-issue rate? The mechanism is a real deadline replacing an inert one.
3. **No capability starvation.** Does the read-only Intake/Review profile ever
   fail-close on a *legitimately needed* tool? History suggests no (only stray
   edits), but a controlled cohort must confirm before the profile is locked.
4. **Cost per verified stage and cost per merged issue** (the contract's two
   metrics) must not worsen for any stage cell — a narrower surface that hurts
   delivery would push wasted spend into the merged-issue numerator.

## 5. Open questions for the operator

1. **Read-only enforcement mechanism** — CLI `--disallowed-tools` vs a settings
   `permissions.deny` block? Coordinate with #224's structured-schema work so the
   read-only profile lands once.
2. **Ceiling headroom multiplier** — the table sets wall ceilings ~1.5–2× observed
   max. Comfortable, or tighter to bite sooner (accepting rare false kills)?
3. **Thin-sample stages** — Respond (4), Revise (5), Converse (3), Research (1),
   Mockup (1) are below the contract's ≥10 bar. Ship conservative ceilings now and
   ratchet once #223 fills the cells, or leave these on the 2-hour default until
   they have data?
4. **MCP leak** — is the personal-MCP attachment (§2b) a config bug worth a
   separate fix regardless of profiles?
5. **USD budget cap** — adopt the per-session `error_max_budget_usd` belt as a
   third ceiling, or rely on wall + turn alone?
