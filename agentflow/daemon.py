"""The persistent orchestrator daemon (ADR 0011) — brain persistent, hands ephemeral.

Polls each repo and runs one full pass per cycle through the pipeline
(`loop.pipeline_once`: triage one un-triaged issue, then build one ready issue —
balancing the pool per ADR 0006 and spawning an ephemeral worktree session).
Properties:

- **Dormant by default** — does nothing unless the enable flag exists, so it can be
  stopped instantly (`rm ~/.agentflow/enabled`). ADR 0011's kill switch.
- **Crash-tolerant** — each cycle is independent; an exception is logged and the loop
  continues. State of record is GitHub, so a restart loses nothing.
- **Single instance** — a lock dir (stamped with the owner's pid) prevents overlapping
  runs; a background thread heartbeats the lock every ~60s so even a cycle longer than
  the stale threshold is never seen as stale, and only a genuinely stale lock (from a
  crashed run) is reclaimed — atomically, taking real ownership. Shutdown removes the
  lock only if this process still owns it. Single-instance is load-bearing:
  dispatch dedup (the `agentflow:building` and `agentflow:triaging` claims) assumes one
  daemon — each is check-then-claim, not atomic, so it dedups within one serial daemon.

M1 is serial (one issue per repo per cycle). Concurrent dispatch across pools
(ADR 0006) is a later refinement — not a silent cap.

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

from agentflow import live
from agentflow.loop import RepoConfig, pipeline_once, reclaim_claims
from agentflow.runner import _worktree_is_active, recover_stale_worktrees
from agentflow.server import dashboard

STATE_DIR = Path(os.environ.get("AGENTFLOW_STATE", os.path.expanduser("~/.agentflow")))
ENABLE_FLAG = STATE_DIR / "enabled"
LOCK = STATE_DIR / "daemon.lock"
POLL_SECONDS = int(os.environ.get("AGENTFLOW_POLL_SECONDS", "300"))

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
    """One pass over the repos. Each is isolated: an error in one never stops the rest."""
    for cfg in repos:
        try:
            _log(f"{cfg.repo}: {run(cfg, _log=_log)}")
        except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the daemon
            _log(f"{cfg.repo}: cycle error: {type(e).__name__}: {e}")


def recover_worktrees(repos: list[RepoConfig], sweep=recover_stale_worktrees, _log=log) -> None:
    """Run the fail-closed worktree recovery pass once at daemon startup, then sweep the live
    board of any session whose worktree is no longer alive — a crashed run's phantom sessions,
    dropped with the same liveness signal the worktree recovery just used."""
    for cfg in repos:
        try:
            report = sweep(cfg.repo, cfg.workdir)
            if report.removed or report.retained:
                _log(f"{cfg.repo}: startup worktree recovery removed {len(report.removed)}, "
                     f"retained {len(report.retained)} for recovery")
        except Exception as e:  # noqa: BLE001 — one repo cannot block daemon startup
            _log(f"{cfg.repo}: startup worktree recovery error: {type(e).__name__}: {e}")
    dropped = live.reap(lambda wt: _worktree_is_active(Path(wt)))
    if dropped:
        _log(f"startup: dropped {len(dropped)} dead session(s) from the live board")


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
            cycle(REPOS)
            live.mark_cycle(POLL_SECONDS)
            return
        with dashboard(REPOS, lambda: ENABLE_FLAG.exists()) as (host, port):
            log(
                f"daemon up — dashboard=http://{host}:{port}, enable={ENABLE_FLAG}, "
                f"poll={POLL_SECONDS}s, repos={[c.repo for c in REPOS]}"
            )
            while True:
                if ENABLE_FLAG.exists():
                    # Self-heal build claims stranded by a crash or a swallowed `gh` release,
                    # every cycle (serial builds mean none is live at the top of a cycle).
                    for cfg in REPOS:
                        cleared = reclaim_claims(cfg)
                        if cleared:
                            log(f"{cfg.repo}: reclaimed {cleared} stale build claim(s)")
                    cycle(REPOS)
                else:
                    log(f"dormant (no {ENABLE_FLAG}); sleeping")
                # Stamp the daemon's status every tick (dormant or not) so the console's
                # daemon block reflects a live, polling daemon even while it's paused.
                live.mark_cycle(POLL_SECONDS)
                time.sleep(POLL_SECONDS)
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
