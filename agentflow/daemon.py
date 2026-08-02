"""The persistent orchestrator daemon — durable coordinator, ephemeral provider hands.

Two clocks (issue #80). A **fast tick** (~15s) asks a cheap cross-fleet change-probe one
question — a single API call for the whole fleet — and pays for a full dispatch pass only when
something actually moved, so newly-ready or newly-unblocked work reacts in ~15-30s instead of
up to ~5min, without running dozens of `gh` calls every 15s. A slow **heartbeat** (~5min) runs
a full pass unconditionally as the backstop for whatever the probe misses (GitHub's search
index lags a labelling/close). A full pass runs in a worker so a fast tick never blocks behind
a long build (dispatch-and-return), and a single-flight guard means an in-flight pass is never
doubled — so the serial reclaim/recheck bookends and the single-daemon claim dedup still hold.
Properties:

- **Dormant by default** — does nothing unless the enable flag exists, so it can be
  stopped instantly (`rm ~/.agentflow/enabled`). ADR 0011's kill switch. Dormant is genuinely
  free on the fast clock: the probe isn't even asked, so a paused daemon makes zero network calls
  between heartbeats.
- **Crash-tolerant** — each cycle is independent; an exception is logged and the loop
  continues. The coordinator store owns attempts, continuations, claims, and permits; GitHub owns
  issue/PR outcomes. A restart reconciles both before starting more work.
- **Sole snapshot producer** — it publishes the GitHub-backed fleet snapshot the console serves
  on the slow heartbeat clock (dormant included, so a paused board still refreshes) and after any
  full pass — never on the cheap fast tick, which must stay within its call budget. So watching
  the dashboard costs a bounded ~one production per heartbeat no matter how many tabs are open
  (ADR 0026). The web server (`agentflow-web`) only ever reads the published file.
- **Single instance** — a lock dir (stamped with the owner's pid) prevents overlapping
  runs; a background thread heartbeats the lock every ~60s so even a cycle longer than
  the stale threshold is never seen as stale, and only a genuinely stale lock is reclaimed.
  Shutdown removes the lock only if this process still owns it. The coordinator's SQLite
  transactions remain the cross-process authority for submissions and reservations.

Dispatch discovers repos concurrently, then the coordinator reconciles and admits every attempt
under the machine ceiling, stage caps, activity pacing, provider headroom, and immutable pool
permits. Outcome-first claim reconciliation runs only after that durable pass. Merges stay
serialized (ADR 0009). See `agentflow.dispatch`.

Requires a working `codex` (see AGENTFLOW_CODEX_BIN) and `claude`, `gh`, `git`, `uv`
on PATH, and — since it spawns tool sessions — an unsandboxed environment.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import os
import signal
import shutil
import threading
import time
from pathlib import Path

from agentflow import dispatch, live
from agentflow.config import ConfigurationError, RuntimeConfig, load_config
from agentflow.dashboard_data import snapshot
from agentflow.loop import RepoConfig, recheck_once
from agentflow.probe import ChangeProbe
from agentflow.runner import recover_stale_worktrees
from agentflow.state import state_dir

STATE_DIR = state_dir()
ENABLE_FLAG = STATE_DIR / "enabled"
LOCK = STATE_DIR / "daemon.lock"

# Two clocks (issue #80). The fast tick runs a cheap cross-fleet change-probe every
# FAST_TICK_SECONDS — one API call for the whole fleet — and only pays for a full dispatch
# pass when the probe reports change, so newly-ready work reacts in ~15-30s instead of ~5min
# without hammering the GraphQL budget. FULL_PASS_SECONDS is the heartbeat: a full pass runs
# unconditionally on that slower clock as the backstop for whatever the probe misses (search-
# index lag). AGENTFLOW_POLL_SECONDS is still honoured as the heartbeat default for back-compat.
FAST_TICK_SECONDS = int(os.environ.get("AGENTFLOW_FAST_TICK_SECONDS", "15"))
FULL_PASS_SECONDS = int(os.environ.get(
    "AGENTFLOW_HEARTBEAT_SECONDS", os.environ.get("AGENTFLOW_POLL_SECONDS", "300")))

# The lock is heartbeated every ~60s by a background thread, so a lock older than this
# means the owner is genuinely gone (crashed) — never merely mid-cycle. Keep the
# threshold well above the heartbeat interval so a missed beat or two is tolerated.
HEARTBEAT_SECONDS = 60
STALE_SECONDS = 3 * 3600

# How often one repository's worktrees are reclaimed. Kept per-repo and process-local inside
# `recover_worktrees`, so every caller shares one cadence and a restart always sweeps.
SWEEP_INTERVAL_SECONDS = int(os.environ.get("AGENTFLOW_SWEEP_SECONDS", "3600"))
_LAST_SWEEP: dict[str, float] = {}

def log(msg: str) -> None:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} agentflow: {msg}", flush=True)


def cycle(repos: list[RepoConfig], run, _log=log) -> None:
    """One serial pass over the repos, running `run` per repo. Each is isolated: an error in
    one never stops the rest. Used for the merge-time re-rebase pass that must remain serial
    (ADR 0009)."""
    for cfg in repos:
        try:
            _log(f"{cfg.repo}: {run(cfg, _log=_log)}")
        except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the daemon
            _log(f"{cfg.repo}: cycle error: {type(e).__name__}: {e}")


def _recheck(cfg: RepoConfig, _log=None) -> str:
    return f"recheck: {recheck_once(cfg)}"


def dispatch_cycle(repos: list[RepoConfig], _log=log, *, submit_new: bool = True) -> None:
    """Reclaim worktrees, reconcile coordinator records, optionally submit cold work, then
    serialize merge rechecks.

    Reclamation runs **first and inside this pass**, never beside it. It reads which sources the
    coordinator owns and then spends minutes confirming completion against GitHub; if a pass could
    admit work during that window, a resumed session's working directory could be archived out
    from under a live provider. Nothing else protects it — the on-disk activity marker is only
    written by the by-hand path, so for daemon-launched sessions the store snapshot *is* the
    protection. Running here puts sweep and admission under the same single-flight guard.

    A paused daemon does not reclaim. Pause is the operator's stop signal, and the only thing
    protecting a hand-worked checkout with no marker is its idle age — deleting directories right
    after a human said "stop" is the wrong surprise. The consequence is real and accepted: while
    dispatch is disabled the bound is not enforced, and a dormant daemon that still opens
    continuations and Ask turns slowly regrows registrations until it is re-enabled.
    """
    if submit_new:
        recover_worktrees(repos, _log=_log)
    dispatch.run_cycle(repos, submit_new=submit_new, _log=_log)
    if submit_new:
        cycle(repos, run=_recheck, _log=_log)


def workspace_cycle(repos: list[RepoConfig], _log=log) -> None:
    """Drive the Project workspace once: apply any transported commands (opening Asks, sending
    turns) through the coordinator as ``converse`` stages, reconcile those turns, and publish the
    bounded workspace projection (ADR 0033/0034). Isolated from the pipeline dispatch pass — it
    shares the coordinator's durable store, so interactive Ask turns and background pipeline work
    contend on the same permit ledger with the operator's turn ranked first."""
    from agentflow import coordinated_converse, dashboard_data, pipeline
    from agentflow.workspace import publish
    if not repos:
        return
    try:
        coord = pipeline.build_coordinator(_log=_log)
        workdir_for = {c.repo: c.workdir for c in repos}
        coordinated_converse.drain_commands(coord, workdir_for, _log=_log)
        for pool in ("claude", "codex"):
            coord.cycle(pool, now=int(time.time()))
        # Reconcile approved Proposals into real GitHub build issues — idempotent on the approved
        # hash, so a crash between "issue created" and "receipt recorded" never files a duplicate
        # (ADR 0033). Runs after the drain so an approval taken this cycle publishes this cycle.
        publish.reconcile_publications(repos, now=int(time.time()), _log=_log)
        # Mirror each published proposal's coarse pipeline state + landed evidence onto its card,
        # joined from the GitHub facts the daemon already reads — never from the web layer (ADR 0033).
        coordinated_converse.publish_projection(
            repos, pipeline_for=dashboard_data.workspace_pipeline)
    except Exception as e:  # noqa: BLE001 — a bad workspace cycle must not kill the daemon
        _log(f"workspace cycle error: {type(e).__name__}: {e}")


def publish_snapshot(repos: list[RepoConfig], produce=snapshot, _log=log) -> None:
    """Produce the GitHub-backed fleet snapshot and publish it for the console — the
    daemon is its only producer (ADR 0026), once per tick, dormant included (dormant is
    exactly when the operator watches). A `gh` outage skips one publish, never the loop;
    the console keeps serving the previous snapshot, honestly aged.

    Composes the existing v1 snapshot with the additive schema-v2 Decision Map projection
    (ADR 0036) — the map read runs every tick, including dormant, since it is not gated by
    dispatch. `previous_snapshot` lets the projection preserve last-verified per-repository
    map data across a failed or budget-skipped read rather than inventing an empty one."""
    try:
        from agentflow import operator_projection
        v1 = produce(repos, dispatch_enabled=ENABLE_FLAG.exists())
        previous = live.read_snapshot()
        v2 = operator_projection.project(
            repos, previous_snapshot=previous, heartbeat_seconds=FULL_PASS_SECONDS)
        merged = {**v1, **v2,
                  "fleet": operator_projection.fleet_recent_landed(v1.get("repos") or [])}
        live.write_snapshot(merged)
    except Exception as e:  # noqa: BLE001 — a bad publish must not kill the daemon
        _log(f"snapshot publish error: {type(e).__name__}: {e}")


def recover_worktrees(repos: list[RepoConfig], sweep=recover_stale_worktrees, _log=log) -> None:
    """Run fail-closed worktree recovery, at startup and then on a per-repository cadence.

    Durable coordinator records protect every live-state owned source. The live board is not
    consulted; it is regenerated from running records on the next reconciliation pass.

    The cadence lives here rather than at the call sites so a repository is swept at most once an
    hour however many passes ask. Startup stamps the clock, so the full pass that immediately
    follows it — and every ``--once`` run — does not pay a second back-to-back sweep. The clock is
    process-local and resets with the daemon, deliberately: a restart should sweep.

    Hourly, not per-pass: the reclamation floor is a day, and confirming completion costs one or
    two GitHub calls per owned registration against a loop designed to spend about one call per
    tick.
    """
    from agentflow import pipeline
    for cfg in repos:
        try:
            now = time.monotonic()
            swept_at = _LAST_SWEEP.get(cfg.repo)
            if swept_at is not None and (now - swept_at) < SWEEP_INTERVAL_SECONDS:
                continue
            _LAST_SWEEP[cfg.repo] = now
            protected = pipeline.owned_worktrees(cfg)
            report = sweep(cfg.repo, cfg.workdir, protected)
            if report.removed or report.retained or report.archived:
                line = (f"{cfg.repo}: worktree recovery removed {len(report.removed)} completed, "
                        f"archived {len(report.archived)} stranded, "
                        f"retained {len(report.retained)} for recovery")
                if report.archived:
                    # The stranded ref is the only remaining handle on that work, and the hold
                    # comment a maintainer reads still names the directory — so print both.
                    line += " — " + ", ".join(f"{path} -> {ref}" for path, ref in report.archived)
                _log(line)
        except Exception as e:  # noqa: BLE001 — one repo cannot block a pass
            _log(f"{cfg.repo}: worktree recovery error: {type(e).__name__}: {e}")


def _dead_family_running(_log=log) -> bool:
    """The fast tick's local-completion wake (issue #158): True when a ``running`` record's
    provider family is *proven* dead — its pid no longer exists — so its permits are stranded
    until reconciliation flips it off ``running``. Network-free: it is a local ``os.kill`` sweep
    of the durable ledger, so it keeps the fast tick's zero-network character (no `gh` call).

    Fail closed on uncertainty. Only a family that ``pid_family_alive`` proves gone counts; an
    unknown/permission-denied liveness (``pid_family_alive`` returns True) and a record that never
    recorded a family do not, so we never thrash on a family we cannot prove ended. A store-read
    error is swallowed like the other daemon cycle guards and reported as 'nothing to wake', never
    crashing the loop. Reuses the very liveness reconcile and worktree recovery already trust — not
    a second notion of completion. The wake converges: the full pass it triggers reconciles the
    dead family off ``running``, so the next tick finds nothing to wake."""
    from agentflow.coordinator import tracer
    from agentflow.coordinator.launcher import pid_family_alive
    from agentflow.coordinator.record import RUNNING
    try:
        records = tracer.load_records()
    except Exception as e:  # noqa: BLE001 — an unreadable ledger must not kill the fast tick
        _log(f"local-completion check skipped: {type(e).__name__}: {e}")
        return False
    return any(r.state == RUNNING and r.family and not pid_family_alive(r.family)
               for r in records)


class PollLoop:
    """The two-clock poll loop (issue #80). Each fast tick asks the change-probe one cheap
    question; a full dispatch pass runs only on change, or when the slow heartbeat clock is due.
    The full pass runs in a background worker so a fast tick never blocks behind a long build
    (dispatch-and-return) — and a single-flight guard means an in-flight pass is never doubled by
    a later tick, so the serial reclaim/recheck bookends and per-issue claim dedup still hold.

    Dormant (no enable flag) is free on the fast clock: the probe is not asked. On the slow
    heartbeat it runs a drain pass with cold submission disabled, so active durable records keep
    reconciling and the operator sees a fresh snapshot."""

    def __init__(self, repos, *, workspace_repos=(), probe=None, dispatch_pass=None, publish=None,
                 enabled=None, local_complete=None, clock=time.monotonic, spawn=None,
                 _log=log) -> None:
        self._repos = repos
        self._workspace_repos = workspace_repos
        self._probe = probe if probe is not None else ChangeProbe(repos)
        self._dispatch = dispatch_pass or dispatch_cycle
        self._publish = publish or publish_snapshot
        self._enabled = enabled or ENABLE_FLAG.exists
        self._local_complete = local_complete or _dead_family_running
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._log = _log
        self._running = threading.Lock()   # single-flight: at most one full pass at a time
        self._last_full = None             # monotonic start of the last full pass, or None

    def _heartbeat_due(self, now: float) -> bool:
        return self._last_full is None or (now - self._last_full) >= FULL_PASS_SECONDS

    def _start_full_pass(self, *, submit_new: bool) -> None:
        """Dispatch-and-return: run one full pass in a worker so the fast clock keeps ticking.
        If a pass from an earlier tick is still running, skip — it already covers this tick, and
        overlapping passes would race the serial bookends and the single-daemon claim dedup."""
        if not self._running.acquire(blocking=False):
            self._log("full pass still running from an earlier tick — skipping this one")
            return

        def work():
            try:
                self._dispatch(self._repos, submit_new=submit_new)
                workspace_cycle(self._workspace_repos)
                self._publish(self._repos)
            finally:
                self._running.release()

        self._spawn(work)

    def tick(self) -> None:
        """One fast tick. Enabled: probe (one call) and run a full pass on change, on a locally
        proven provider completion, or on the heartbeat. Dormant: never probe or sweep for local
        completion; reconcile owned records on the heartbeat without cold submission."""
        now = self._clock()
        heartbeat_due = self._heartbeat_due(now)
        if self._enabled():
            # Heartbeat is the unconditional clock; the GitHub probe and the local-completion sweep
            # are the opportunistic ones — so on a heartbeat tick we skip both (a full pass runs
            # anyway). The local sweep (issue #158) wakes a full pass when a provider family died
            # after its last GitHub bump, so stranded permits release within one fast tick instead
            # of waiting out the heartbeat; it is network-free, mirroring the probe's enabled-only
            # gating, and the pass it triggers reconciles the dead family off `running` so the next
            # tick converges back to quiet.
            if heartbeat_due or self._probe.changed() or self._local_complete():
                self._last_full = now
                self._start_full_pass(submit_new=True)
        elif heartbeat_due:
            self._last_full = now
            self._start_full_pass(submit_new=False)
        # Stamp liveness every tick (local, no network) so the console sees a live, fast-polling
        # daemon even while it's dormant or between full passes.
        live.mark_cycle(FAST_TICK_SECONDS)

    def run(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            self.tick()
            stop.wait(FAST_TICK_SECONDS)


def _try_claim() -> bool:
    """Atomically create the lock dir and stamp our pid inside. `mkdir` is the atomic
    step — exactly one racer can win it; the rest see FileExistsError and bow out."""
    try:
        LOCK.mkdir()
    except FileExistsError:
        return False
    (LOCK / "pid").write_text(str(os.getpid()))
    return True


def _acquire_lock() -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if _try_claim():
        return True

    # Serialize the inspect-and-reclaim decision. `flock` is tied to this open file
    # descriptor, so a crash releases it automatically without leaving another lock
    # artifact behind. The lock dir itself remains the public ownership record.
    guard = os.open(STATE_DIR, os.O_RDONLY)
    try:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if _try_claim():
            return True  # lock vanished before the guarded check — grab it fresh
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            return _try_claim()
        owner_is_dead = False
        try:
            owner_pid = int((LOCK / "pid").read_text().strip())
            if owner_pid > 0:
                try:
                    os.kill(owner_pid, 0)
                except ProcessLookupError:
                    owner_is_dead = True
                except OSError:
                    pass  # permission denied or unknown means the owner may still be alive
        except (OSError, ValueError):
            pass  # malformed ownership is reclaimed only by the existing age backstop
        if not owner_is_dead and age <= STALE_SECONDS:
            return False
        # Abandoned lock from a crashed run: take real ownership. Rename the old dir out
        # of the way first — the rename is atomic, and the guard ensures only the process
        # that inspected this lock can replace it.
        stale = LOCK.with_name(f"{LOCK.name}.stale.{os.getpid()}")
        try:
            os.rename(LOCK, stale)
        except OSError:
            return False
        log("reclaiming dead-owner lock" if owner_is_dead else "reclaiming stale lock")
        shutil.rmtree(stale, ignore_errors=True)
        return _try_claim()
    finally:
        os.close(guard)


def _owns_lock() -> bool:
    """True only if the live lock carries our pid — so we never remove another daemon's."""
    try:
        return (LOCK / "pid").read_text().strip() == str(os.getpid())
    except OSError:
        return False


def _release_lock() -> None:
    if not _owns_lock():
        return
    try:
        (LOCK / "pid").unlink()
        LOCK.rmdir()
    except OSError:
        pass


def _heartbeat(stop: threading.Event) -> None:
    """Touch the lock every ~60s so a *healthy* daemon is never seen as stale — even
    during a cycle longer than the stale threshold (one slow build+review+revise chain
    across repos is realistic). Runs in the background so a long cycle can't freeze the
    mtime; that frozen mtime is exactly what would let a second daemon reclaim a live
    lock, clear this daemon's build claim, and run a duplicate build."""
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            os.utime(LOCK, None)
        except OSError:
            return


def run(config: RuntimeConfig, *, once: bool = False) -> None:
    repos = list(config.repositories)
    workspace_repos = list(config.workspace_repositories)
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
    previous_handlers = {}
    previous_mask = None
    signals_blocked = False
    shutdown_requested = False
    acquired = False

    def request_shutdown(_signum, _frame):
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        raise SystemExit(0)

    try:
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[signum] = signal.signal(signum, request_shutdown)
            previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK, (signal.SIGTERM, signal.SIGINT)
            )
            signals_blocked = True
        if not _acquire_lock():
            log("another daemon is running; exiting")
            return
        acquired = True
        if signals_blocked:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
            signals_blocked = False
        beat.start()
        if not (
            os.environ.get("AGENTFLOW_CAPACITY_HELPER")
            or os.environ.get("AGENTFLOW_TRIAGE_GATE")
        ):
            log(
                "capacity helper not configured — Codex capacity and "
                "operator-activity detection are unavailable"
            )
        recover_worktrees(repos)
        if once:
            log(f"--once: running one cycle over repos={[c.repo for c in repos]}")
            dispatch_cycle(repos)
            workspace_cycle(workspace_repos)
            live.mark_cycle(FAST_TICK_SECONDS)
            publish_snapshot(repos)
            return
        log(
            f"daemon up — enable={ENABLE_FLAG}, fast={FAST_TICK_SECONDS}s, "
            f"heartbeat={FULL_PASS_SECONDS}s, repos={[c.repo for c in repos]}"
        )
        PollLoop(repos, workspace_repos=workspace_repos).run()
    finally:
        shutdown_requested = True
        stop.set()
        if acquired:
            _release_lock()
        if signals_blocked:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="agentflow daemon")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit (bypasses the enable flag; still respects the lock)",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigurationError as exc:
        parser.error(str(exc))
    run(config, once=args.once)


if __name__ == "__main__":
    main()
