"""The persistent orchestrator daemon (ADR 0011) — brain persistent, hands ephemeral.

Polls for `ready-for-agent` work and runs one issue per cycle through the pipeline
(`loop.run_once`, which balances the pool per ADR 0006 and spawns an ephemeral
worktree session per issue). Properties:

- **Dormant by default** — does nothing unless the enable flag exists, so it can be
  stopped instantly (`rm ~/.agentflow/enabled`). ADR 0011's kill switch.
- **Crash-tolerant** — each cycle is independent; an exception is logged and the loop
  continues. State of record is GitHub, so a restart loses nothing.
- **Single instance** — a lock dir prevents overlapping runs; a stale lock is reclaimed.

M1 is serial (one issue per repo per cycle). Concurrent dispatch across pools
(ADR 0006) is a later refinement — not a silent cap.

Requires a working `codex` (see AGENTFLOW_CODEX_BIN) and `claude`, `gh`, `git`, `uv`
on PATH, and — since it spawns tool sessions — an unsandboxed environment.
"""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

from agentflow.loop import RepoConfig, run_once

STATE_DIR = Path(os.environ.get("AGENTFLOW_STATE", os.path.expanduser("~/.agentflow")))
ENABLE_FLAG = STATE_DIR / "enabled"
LOCK = STATE_DIR / "daemon.lock"
POLL_SECONDS = int(os.environ.get("AGENTFLOW_POLL_SECONDS", "300"))

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
]


def log(msg: str) -> None:
    print(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} agentflow: {msg}", flush=True)


def cycle(repos: list[RepoConfig], run=run_once, _log=log) -> None:
    """One pass over the repos. Each is isolated: an error in one never stops the rest."""
    for cfg in repos:
        try:
            _log(f"{cfg.repo}: {run(cfg)}")
        except Exception as e:  # noqa: BLE001 — a bad cycle must not kill the daemon
            _log(f"{cfg.repo}: cycle error: {type(e).__name__}: {e}")


def _acquire_lock() -> bool:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.mkdir()
        return True
    except FileExistsError:
        # Reclaim a stale lock (>3h) from a crashed run.
        age = time.time() - LOCK.stat().st_mtime
        if age > 3 * 3600:
            log("reclaiming stale lock")
            return True
        return False


def main() -> None:
    if not _acquire_lock():
        log("another daemon is running; exiting")
        return
    log(f"daemon up — enable={ENABLE_FLAG}, poll={POLL_SECONDS}s, repos={[c.repo for c in REPOS]}")
    try:
        while True:
            if ENABLE_FLAG.exists():
                cycle(REPOS)
            else:
                log(f"dormant (no {ENABLE_FLAG}); sleeping")
            time.sleep(POLL_SECONDS)
    finally:
        try:
            LOCK.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    main()
