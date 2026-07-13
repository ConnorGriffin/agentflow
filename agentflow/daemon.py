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
import os
import shutil
import threading
import time
from pathlib import Path

from agentflow.loop import RepoConfig, pipeline_once, reclaim_claims
from agentflow.runner import recover_stale_worktrees
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
    """Run the fail-closed worktree recovery pass once at daemon startup."""
    for cfg in repos:
        try:
            report = sweep(cfg.repo, cfg.workdir)
            if report.removed or report.retained:
                _log(f"{cfg.repo}: startup worktree recovery removed {len(report.removed)}, "
                     f"retained {len(report.retained)} for recovery")
        except Exception as e:  # noqa: BLE001 — one repo cannot block daemon startup
            _log(f"{cfg.repo}: startup worktree recovery error: {type(e).__name__}: {e}")


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
    try:
        age = time.time() - LOCK.stat().st_mtime
    except OSError:
        return _try_claim()  # lock vanished between checks — grab it fresh
    if age <= STALE_SECONDS:
        return False
    # Stale lock from a crashed run: take real ownership. Rename the stale dir out of
    # the way first — the rename is atomic, so if two starters race the reclaim exactly
    # one wins it (the losers' rename fails because the source is already gone) and
    # only the winner recreates + re-claims the lock.
    stale = LOCK.with_name(f"{LOCK.name}.stale.{os.getpid()}")
    try:
        os.rename(LOCK, stale)
    except OSError:
        return False  # lost the reclaim race — another starter owns the lock now
    log("reclaiming stale lock")
    shutil.rmtree(stale, ignore_errors=True)
    return _try_claim()


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

    if not _acquire_lock():
        log("another daemon is running; exiting")
        return
    stop = threading.Event()
    beat = threading.Thread(target=_heartbeat, args=(stop,), daemon=True)
    beat.start()
    try:
        recover_worktrees(REPOS)
        if args.once:
            log(f"--once: running one cycle over repos={[c.repo for c in REPOS]}")
            cycle(REPOS)
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
                time.sleep(POLL_SECONDS)
    finally:
        stop.set()
        _release_lock()


if __name__ == "__main__":
    main()
