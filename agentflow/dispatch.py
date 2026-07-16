"""Headroom-governed concurrent dispatch (ADR 0023 M6 slice 5, amends ADR 0006).

M1 dispatched one issue per repo per cycle, serially. That stranded prepaid capacity:
a clear pool with a deep queue still built one thing and waited a full poll. This module
lifts that cap — the daemon dispatches ready work *concurrently*, bounded by three dials:

- **the machine ceiling** — the most agent sessions that may run at once, of any kind;
- **per-stage caps** — how many of each kind may run at once, with triage allowed more
  parallelism than builds (grounding sessions are short and cheap, and a deep intake queue
  should drain fast while builds stay capacity-bound);
- **per-cycle pacing** — when the operator is active on a pool, at most `ACTIVE_PACE` new
  sessions start on it per cycle (ADR 0025; the *ceiling* itself lives in `balancer`).

The `Governor` is the one place those dials are enforced. It is the dispatch test surface:
admission is a pure decision over its counters, so the concurrency/pacing behaviour is
exercised without spawning anything. The per-issue dedup claims (ADR 0021) still stop two
sessions from grabbing the same issue; the governor only bounds *how many* run at once.

Merges are deliberately NOT governed here — they stay serialized behind `gate`'s merge
lock (ADR 0009's collision floor): concurrency multiplies builds, never overlapping merges.
"""

from __future__ import annotations

import os
import threading
from collections import Counter

from agentflow import balancer, coordinated_build, live, loop
from agentflow.balancer import pick_pair
from agentflow.coordinator import DRAINING, Phase, Rollout

# Named config (env-overridable). The machine ceiling caps total live sessions; the
# per-stage caps cap each kind. Triage > build on purpose (see the module docstring).
MACHINE_CEILING = int(os.environ.get("AGENTFLOW_MAX_SESSIONS", "4"))
TRIAGE_CONCURRENCY = int(os.environ.get("AGENTFLOW_TRIAGE_CONCURRENCY", "3"))
BUILD_CONCURRENCY = int(os.environ.get("AGENTFLOW_BUILD_CONCURRENCY", "2"))
STAGE_CAPS = {
    "triage": TRIAGE_CONCURRENCY,
    "build": BUILD_CONCURRENCY,
    "mockup": 1,
    "respond": 1,
}


class Governor:
    """The dispatch admission gate — bounds how many sessions run at once, per kind and in
    total, and paces new sessions on an operator-active pool. One session = one machine slot
    for its whole life (a build's nested review rides the build's slot, not a second one).

    Thread-safe: the daemon dispatches concurrent chains, each calling `admit` before it
    starts real work and `release` when it finishes. `begin_cycle` resets the per-cycle
    pacing budget. `admit`'s decision is the concurrency policy — the test surface."""

    def __init__(self, *, machine_ceiling: int | None = None,
                 stage_caps: dict[str, int] | None = None, pace: int | None = None) -> None:
        self.machine_ceiling = MACHINE_CEILING if machine_ceiling is None else machine_ceiling
        self.stage_caps = dict(STAGE_CAPS if stage_caps is None else stage_caps)
        self.pace = balancer.ACTIVE_PACE if pace is None else pace
        self._lock = threading.Lock()
        self._live = 0                 # sessions admitted and not yet released
        self._per_stage: Counter = Counter()
        self._paced: Counter = Counter()   # new sessions started per active pool this cycle

    def begin_cycle(self) -> None:
        """Reset the per-cycle pace budget — call once at the top of each dispatch cycle."""
        with self._lock:
            self._paced.clear()

    def admit(self, stage: str, tool: str, *, active: bool = False) -> bool:
        """Try to claim a machine slot for a `stage` session on `tool`'s pool. Returns True
        and reserves the slot when there is room under the machine ceiling, the stage's cap,
        and — when the operator is `active` on that pool — the per-cycle pace. Otherwise the
        dispatch waits for a later cycle."""
        cap = self.stage_caps.get(stage, 1)
        with self._lock:
            if self._live >= self.machine_ceiling:
                return False
            if self._per_stage[stage] >= cap:
                return False
            if active and self._paced[tool] >= self.pace:
                return False
            self._live += 1
            self._per_stage[stage] += 1
            if active:
                self._paced[tool] += 1
            return True

    def release(self, stage: str) -> None:
        """Free the machine slot a finished `stage` session held. Pacing is per-cycle, so a
        release never returns pace budget — only concurrency slots."""
        with self._lock:
            self._live = max(0, self._live - 1)
            if self._per_stage[stage] > 0:
                self._per_stage[stage] -= 1

    @property
    def live(self) -> int:
        with self._lock:
            return self._live


class _Slot:
    """Binds the governor to this cycle's per-pool activity so a dispatched stage only names
    its kind and chosen tool — the operator-active fact (and thus pacing) is supplied here."""

    def __init__(self, governor: Governor, active_by_tool: dict[str, bool]) -> None:
        self._gov = governor
        self._active = active_by_tool

    def admit(self, stage: str, tool: str) -> bool:
        return self._gov.admit(stage, tool, active=self._active.get(tool, False))

    def release(self, stage: str) -> None:
        self._gov.release(stage)


def _spawn(fn) -> threading.Thread:
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


def _pool_activity(_log) -> dict[str, bool]:
    """Read each pool's operator-active fact once per cycle (ADR 0025) and log the yield so
    the decision is observable before the dashboard surfaces it. Unknown reads as idle."""
    active: dict[str, bool] = {}
    for tool in ("claude", "codex"):
        try:
            status = balancer._query_pool(tool)
        except Exception:  # noqa: BLE001 — a gate blip must not stop the whole cycle
            status = None
        is_active = bool(status and status.active)
        active[tool] = is_active
        if is_active:
            _log(f"{tool} yielding to operator · ceiling "
                 f"{balancer.ACTIVE_CEILING_PCT:.0f}% · pacing to {balancer.ACTIVE_PACE}/cycle")
    return active


def _run_and_log(cfg, label: str, fn, _log) -> None:
    try:
        _log(f"{cfg.repo}: {label}: {fn()}")
    except Exception as e:  # noqa: BLE001 — one stage's failure can't sink the repo's cycle
        _log(f"{cfg.repo}: {label} error: {type(e).__name__}: {e}")


def _triage_fanout(cfg, slot: _Slot, _log) -> list[threading.Thread]:
    """Start several grounding sessions for one repo at once when its intake queue is deep.
    Selection and the per-issue triaging claim are placed serially here — each claim is set
    (and the issue reserved in memory) before the next candidate is chosen — so the fan-out
    never grabs the same issue twice even before GitHub shows the label. Each session then
    runs concurrently, bounded by the governor's triage cap and the machine ceiling."""
    threads: list[threading.Thread] = []
    reserved: set[int] = set()
    while True:
        picked = loop._next_intake_candidate(cfg, reserved)
        if picked is None:
            break
        issue, extra = picked
        n = issue["number"]
        builder, _, block_msg = pick_pair()   # intake needs one available tool, not a pair
        if builder is None:
            _log(f"{cfg.repo}: intake: #{n}: no pool has headroom ({block_msg}) — deferring")
            break
        if not slot.admit("triage", builder.tool):
            break   # machine at capacity or this pool's pace spent — try next cycle
        loop._claim_triage(cfg.repo, n)
        reserved.add(n)
        _log(f"{cfg.repo}: #{n}: routing → {builder.tool} (intake)")

        def run(issue=issue, extra=extra, builder=builder):
            try:
                _log(f"{cfg.repo}: intake: {loop._run_intake_session(cfg, issue, extra, builder)}")
            finally:
                slot.release("triage")
        threads.append(_spawn(run))
    return threads


def _dispatch_repo(cfg, slot: _Slot, _log, phase: Phase, coordinator=None) -> None:
    """Dispatch one repo's ready work concurrently: fan out triage across its intake queue,
    and start at most one build, one mockup draw, and one PR reply. Build concurrency across
    the fleet comes from running every repo's `_dispatch_repo` at once. Merges are handled
    serially by the caller after these settle (ADR 0009).

    The rollout gates every provider launch (issues #103, #104, #105): `legacy` keeps today's
    paths; `coordinated` submits one durable Build stage (a completed Build opens its Review, a
    blocking Review opens its Revise, and a completed Revise opens its next Review, all behind the
    coordinator), while Mockup and Respond remain queued; `draining` launches nothing new while
    existing work finishes. No legacy stage may bypass that dormant gate."""
    threads: list[threading.Thread] = []
    if phase.launch_legacy:
        threads = _triage_fanout(cfg, slot, _log)
        threads.append(_spawn(lambda: _run_and_log(
            cfg, "build", lambda: loop.run_once(cfg, _log=_log, slot=slot), _log)))
        threads.append(_spawn(lambda: _run_and_log(
            cfg, "mockup", lambda: loop.produce_once(cfg, _log=_log, slot=slot), _log)))
        threads.append(_spawn(lambda: _run_and_log(
            cfg, "respond", lambda: loop.respond_once(cfg, _log=_log, slot=slot), _log)))
    elif phase.submit_coordinated and coordinator is not None:
        _run_and_log(cfg, "intake",
                     lambda: _submit_coordinated_intake(cfg, coordinator, _log), _log)
        _run_and_log(cfg, "build",
                     lambda: _submit_coordinated_build(cfg, coordinator, _log), _log)
    for thread in threads:
        thread.join()


def _submit_coordinated_build(cfg, coordinator, _log) -> str:
    """Submit this repo's next ready issue as one durable Build stage. Submission is idempotent
    on the stage identity, so a repeat or restart never opens a second Build — the coordinator
    owns admission, continuation, and completion from here (issue #103)."""
    issue = loop._next_ready_issue(cfg, _log=_log)
    if not issue:
        return "no ready-for-agent issues"
    builder, _reviewer, block_msg = pick_pair()
    if builder is None:
        return f"#{issue['number']}: no pool has headroom ({block_msg}) — deferring"
    submission = coordinated_build.build_submission(cfg, issue, builder.tool)
    if submission is None:
        return f"#{issue['number']}: skipped — no agentflow:complexity:* label (ADR 0018 gate)"
    if not loop._claim(cfg.repo, issue["number"]):
        return f"#{issue['number']}: could not claim Build — refusing coordinator submission"
    coordinator.submit_stage(submission)
    return f"#{issue['number']}: submitted to coordinator → {builder.tool} (build)"


def _submit_coordinated_intake(cfg, coordinator, _log) -> str:
    """Submit all currently admissible Intake candidates as durable read-only stages."""
    from agentflow import coordinated_intake
    reserved: set[int] = set()
    submitted = []
    while True:
        picked = loop._next_intake_candidate(cfg, reserved)
        if picked is None:
            break
        issue, extra = picked
        builder, _reviewer, block_msg = pick_pair()
        if builder is None:
            return ("; ".join(submitted) if submitted else
                    f"#{issue['number']}: no pool has headroom ({block_msg}) — deferring")
        submission = coordinated_intake.intake_submission(cfg, issue, extra, builder.tool)
        if submission is None:
            return f"#{issue['number']}: Intake source unreadable — deferring"
        # Persist ownership first, then project its GitHub claim. A crash can therefore leave
        # either no claim or an idempotently resubmittable record, never an unowned claim that
        # looks like ambiguous legacy work and holds the rollout drain forever.
        coordinator.submit_stage(submission)
        if not loop._claim_triage(cfg.repo, issue["number"]):
            return f"#{issue['number']}: Intake record saved; claim pending — deferring admission"
        reserved.add(issue["number"])
        submitted.append(f"#{issue['number']} → {builder.tool}")
    return "; ".join(submitted) if submitted else "no un-triaged issues"


def _resolve_phase(rollout, repos, _log, requested_mode=None) -> Phase:
    """This cycle's Build rollout phase. Any ambiguous rollout, live-session, coordinator,
    claim, or worktree read fails closed into a named drain; it can never re-enable legacy
    launching or activate coordinated Build by guessing."""
    try:
        sessions = live.running_strict()
        return coordinated_build.resolve_phase(
            rollout or Rollout(log=_log), repos, sessions, requested_mode=requested_mode)
    except Exception as e:  # noqa: BLE001 — ambiguity drains Build, not the whole cycle
        reason = f"rollout state unreadable ({type(e).__name__}: {e})"
        _log(f"rollout: {reason} — draining")
        return Phase(DRAINING, (reason,))


def run_cycle(repos, governor: Governor | None = None, *, rollout=None,
              rollout_mode=None, coordinator=None, _log=None) -> None:
    """One concurrent dispatch pass over the fleet (ADR 0023 M6 slice 5). Reads each pool's
    activity and the Build rollout phase, then dispatches every repo's ready work at once —
    governed by the machine ceiling, per-stage caps, and per-pool pacing. When Build is behind
    the coordinator (coordinated or draining), one shared coordinator reconciles the Build pools
    and republishes the live board as a projection of its running records after the repos
    settle. Merges are NOT here: they stay serialized in the caller's re-rebase pass (ADR 0009)."""
    _log = _log or (lambda _m: None)
    gov = governor if governor is not None else Governor()
    gov.begin_cycle()
    slot = _Slot(gov, _pool_activity(_log))
    phase = _resolve_phase(rollout, repos, _log, rollout_mode)
    coord = None
    if not phase.launch_legacy:  # coordinated or draining — the coordinator owns Build now
        coord = coordinator if coordinator is not None else coordinated_build.build_coordinator(_log)
    repo_threads = [_spawn(lambda cfg=cfg: _dispatch_repo(cfg, slot, _log, phase, coord))
                    for cfg in repos]
    for thread in repo_threads:
        thread.join()
    if coord is not None:
        coordinated_build.reconcile_and_project(coord, phase, _log=_log)
