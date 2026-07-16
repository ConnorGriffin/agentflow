"""The persistent orchestrator daemon (ADR 0011) — brain persistent, hands ephemeral.

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
  continues. State of record is GitHub, so a restart loses nothing.
- **Sole snapshot producer** — it publishes the GitHub-backed fleet snapshot the console serves
  on the slow heartbeat clock (dormant included, so a paused board still refreshes) and after any
  full pass — never on the cheap fast tick, which must stay within its call budget. So watching
  the dashboard costs a bounded ~one production per heartbeat no matter how many tabs are open
  (ADR 0026). The web server (`agentflow-web`) only ever reads the published file.
- **Single instance** — a lock dir (stamped with the owner's pid) prevents overlapping
  runs; a background thread heartbeats the lock every ~60s so even a cycle longer than
  the stale threshold is never seen as stale, and only a genuinely stale lock (from a
  crashed run) is reclaimed — atomically, taking real ownership. Shutdown removes the
  lock only if this process still owns it. Single-instance is load-bearing:
  dispatch dedup (the `agentflow:building` and `agentflow:triaging` claims) assumes one
  daemon — each is check-then-claim, not atomic. Concurrent dispatch keeps that safe by
  selecting-and-claiming serially (builds are one-per-repo; the triage fan-out reserves each
  issue in memory before choosing the next), so two sessions never grab the same issue.

Dispatch is now concurrent (ADR 0023 M6 slice 5): each cycle reclaims stale claims, then
dispatches every repo's ready work at once — multiple builds across repos, several triages
within a deep queue — bounded by the machine ceiling, per-stage caps, and the activity-
adaptive ceiling/pacing (ADR 0025). Merges stay serialized (ADR 0009). See `agentflow.dispatch`.

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
from agentflow.dashboard_data import snapshot
from agentflow.loop import (RepoConfig, pipeline_once, reclaim_claims,
                            reclaim_triage_claims, recheck_once)
from agentflow.probe import ChangeProbe
from agentflow.runner import _worktree_is_active, recover_stale_worktrees

STATE_DIR = Path(os.environ.get("AGENTFLOW_STATE", os.path.expanduser("~/.agentflow")))
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

# One repo per entry; extend as repos are enrolled (each needs an AGENTS.md
# `profile:` line, ready-for-agent + tier:* labels, and PR CI).
REPOS = [
    RepoConfig("ConnorGriffin/agentflow-sandbox",
               os.path.expanduser("~/Code/ConnorGriffin/agentflow-sandbox")),
    RepoConfig("ConnorGriffin/home-depot-location-probe",
               os.path.expanduser("~/Code/ConnorGriffin/home-depot-location-probe")),
    RepoConfig("ConnorGriffin/ciq-autotune",  # guarded: medical/PHI, human merges
               os.path.expanduser("~/Code/ConnorGriffin/ciq-autotune")),
    RepoConfig("ConnorGriffin/agentflow",  # dogfood: the engine in its own fleet
               os.path.expanduser("~/Code/ConnorGriffin/agentflow")),
    RepoConfig("ConnorGriffin/homelab",  # reviewed: manual deploy, live DNS/tailnet
               os.path.expanduser("~/Code/ConnorGriffin/homelab")),
    RepoConfig("ConnorGriffin/dotfiles",  # reviewed: install.sh mutates the live machine
               os.path.expanduser("~/Code/ConnorGriffin/dotfiles")),
]


def log(msg: str) -> None:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} agentflow: {msg}", flush=True)


def cycle(repos: list[RepoConfig], run=pipeline_once, _log=log) -> None:
    """One serial pass over the repos, running `run` per repo. Each is isolated: an error in
    one never stops the rest. Used for the passes that MUST stay serial — reclaim and the
    merge-time re-rebase (ADR 0009) — while concurrent build/triage dispatch runs in between."""
    for cfg in repos:
        try:
            _log(f"{cfg.repo}: {run(cfg, _log=_log)}")
        except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the daemon
            _log(f"{cfg.repo}: cycle error: {type(e).__name__}: {e}")


def _reclaim(cfg: RepoConfig, _log=None, *, preserve_builds: bool = False) -> str:
    from agentflow import coordinated_build
    builds = 0 if preserve_builds else reclaim_claims(cfg, coordinated_build.owned_issues(cfg))
    triaging = reclaim_triage_claims(cfg)
    parts = []
    if builds:
        parts.append(f"reclaimed {builds} stale build claim(s)")
    if triaging:
        parts.append(f"reclaimed {triaging} stale triaging claim(s)")
    return ", ".join(parts) if parts else "no stale claims"


def _recheck(cfg: RepoConfig, _log=None) -> str:
    return f"recheck: {recheck_once(cfg)}"


def dispatch_cycle(repos: list[RepoConfig], _log=log) -> None:
    """One full dispatch cycle: reclaim stale claims (serial, keyed on live sessions), then
    dispatch every repo's ready work CONCURRENTLY (bounded by the governor + activity ceiling),
    then re-rebase merge survivors SERIALLY — merges never overlap (ADR 0009). The two serial
    passes bookend the concurrent one; each isolates per-repo errors via `cycle`."""
    from agentflow.coordinator import MODE_COORDINATED, Rollout

    rollout = Rollout(log=_log)
    try:
        preserve_builds = rollout.mode == MODE_COORDINATED
    except Exception as e:  # noqa: BLE001 — ambiguous intent must preserve possible ownership
        preserve_builds = True
        _log(f"rollout: state unreadable before reclaim ({type(e).__name__}: {e}) — "
             "preserving Build claims")
    cycle(
        repos,
        run=lambda cfg, _log=None: _reclaim(
            cfg, _log=_log, preserve_builds=preserve_builds),
        _log=_log,
    )
    dispatch.run_cycle(repos, rollout=rollout, _log=_log)
    cycle(repos, run=_recheck, _log=_log)


def publish_snapshot(repos: list[RepoConfig], produce=snapshot, _log=log) -> None:
    """Produce the GitHub-backed fleet snapshot and publish it for the console — the
    daemon is its only producer (ADR 0026), once per tick, dormant included (dormant is
    exactly when the operator watches). A `gh` outage skips one publish, never the loop;
    the console keeps serving the previous snapshot, honestly aged."""
    try:
        live.write_snapshot(produce(repos, dispatch_enabled=ENABLE_FLAG.exists()))
    except Exception as e:  # noqa: BLE001 — a bad publish must not kill the daemon
        _log(f"snapshot publish error: {type(e).__name__}: {e}")


def recover_worktrees(repos: list[RepoConfig], sweep=recover_stale_worktrees, _log=log) -> None:
    """Run the fail-closed worktree recovery pass once at daemon startup, then sweep the live
    board of any session whose worktree is no longer alive — a crashed run's phantom sessions,
    dropped with the same liveness signal the worktree recovery just used."""
    from agentflow import coordinated_build
    for cfg in repos:
        try:
            protected = coordinated_build.owned_worktrees(cfg)
            report = sweep(cfg.repo, cfg.workdir, protected)
            if report.removed or report.retained:
                _log(f"{cfg.repo}: startup worktree recovery removed {len(report.removed)}, "
                     f"retained {len(report.retained)} for recovery")
        except Exception as e:  # noqa: BLE001 — one repo cannot block daemon startup
            _log(f"{cfg.repo}: startup worktree recovery error: {type(e).__name__}: {e}")
    dropped = live.reap(lambda wt: _worktree_is_active(Path(wt)))
    if dropped:
        _log(f"startup: dropped {len(dropped)} dead session(s) from the live board")


class PollLoop:
    """The two-clock poll loop (issue #80). Each fast tick asks the change-probe one cheap
    question; a full dispatch pass runs only on change, or when the slow heartbeat clock is due.
    The full pass runs in a background worker so a fast tick never blocks behind a long build
    (dispatch-and-return) — and a single-flight guard means an in-flight pass is never doubled by
    a later tick, so the serial reclaim/recheck bookends and per-issue claim dedup still hold.

    Dormant (no enable flag) is genuinely free: the probe is not even asked, so a paused daemon
    makes zero network calls on the fast clock — it only republishes the console snapshot on the
    slow heartbeat, so the operator watching a paused fleet still sees a freshly-aged board."""

    def __init__(self, repos, *, probe=None, dispatch_pass=None, publish=None,
                 enabled=None, clock=time.monotonic, spawn=None, _log=log) -> None:
        self._repos = repos
        self._probe = probe if probe is not None else ChangeProbe(repos)
        self._dispatch = dispatch_pass or dispatch_cycle
        self._publish = publish or publish_snapshot
        self._enabled = enabled or ENABLE_FLAG.exists
        self._clock = clock
        self._spawn = spawn or (lambda fn: threading.Thread(target=fn, daemon=True).start())
        self._log = _log
        self._running = threading.Lock()   # single-flight: at most one full pass at a time
        self._last_full = None             # monotonic start of the last full pass, or None

    def _heartbeat_due(self, now: float) -> bool:
        return self._last_full is None or (now - self._last_full) >= FULL_PASS_SECONDS

    def _start_full_pass(self) -> None:
        """Dispatch-and-return: run one full pass in a worker so the fast clock keeps ticking.
        If a pass from an earlier tick is still running, skip — it already covers this tick, and
        overlapping passes would race the serial bookends and the single-daemon claim dedup."""
        if not self._running.acquire(blocking=False):
            self._log("full pass still running from an earlier tick — skipping this one")
            return

        def work():
            try:
                self._dispatch(self._repos)
                self._publish(self._repos)
            finally:
                self._running.release()

        self._spawn(work)

    def tick(self) -> None:
        """One fast tick. Enabled: probe (one call) and run a full pass on change or heartbeat.
        Dormant: never probe; only republish on the heartbeat so a paused board stays fresh."""
        now = self._clock()
        heartbeat_due = self._heartbeat_due(now)
        if self._enabled():
            # Heartbeat is the unconditional clock; the probe is the opportunistic one — so on a
            # heartbeat tick we skip the probe call entirely (it would run a full pass anyway).
            if heartbeat_due or self._probe.changed():
                self._last_full = now
                self._start_full_pass()
        elif heartbeat_due:
            self._last_full = now
            self._publish(self._repos)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="agentflow daemon")
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit (bypasses the enable flag; still respects the lock)",
    )
    args = parser.parse_args()

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
        recover_worktrees(REPOS)
        if args.once:
            log(f"--once: running one cycle over repos={[c.repo for c in REPOS]}")
            dispatch_cycle(REPOS)
            live.mark_cycle(FAST_TICK_SECONDS)
            publish_snapshot(REPOS)
            return
        log(
            f"daemon up — enable={ENABLE_FLAG}, fast={FAST_TICK_SECONDS}s, "
            f"heartbeat={FULL_PASS_SECONDS}s, repos={[c.repo for c in REPOS]}"
        )
        PollLoop(REPOS).run()
    finally:
        shutdown_requested = True
        stop.set()
        if acquired:
            _release_lock()
        if signals_blocked:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    main()
