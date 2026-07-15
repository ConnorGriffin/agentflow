"""The crash-safe provider start handshake (ADR 0030).

A provider is started through a small local launcher that spawns a child process. That
child durably records ``started`` with its own process-family identity *before* it
``exec``-replaces itself with the provider, so the start fact and the family exist on the
durable record even if the provider exits immediately or the daemon dies before reading
it. A launch that never records that fact consumes no attempt. The coordinator consumes an
attempt if and only if the durable result is ``started``.

The launcher genuinely spawns and hands off — it is not an in-process simulation, and the
family it records is the child's own pid, not the daemon's. That is the whole crash
boundary: reconciliation reads the same durable fact and the family's real liveness, so a
fresh coordinator over the same store reconstructs exactly what happened. Tests inject a
scripted launcher double at construction to drive the four boundaries without spawning,
and a focused integration test exercises the real spawn (see tests/test_coordinator_launcher.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass

STARTED = "started"
NOT_STARTED = "not_started"

# Bounded wait for the spawned child to durably record `started` before we treat the launch
# as one that never produced a provider family.
_HANDSHAKE_TIMEOUT_S = float(os.environ.get("AGENTFLOW_COORD_HANDSHAKE_S", "10"))


@dataclass(frozen=True)
class StartResult:
    fact: str                     # started | not_started
    family: str | None = None     # the durable process-family identity a `started` carries


def pid_family_alive(family: str | None) -> bool:
    """Whether a recorded process family is still executing — the liveness signal the
    worktree-recovery pass already trusts, reused here rather than a second notion."""
    if not family:
        return False
    try:
        os.kill(int(family), 0)
        return True
    except PermissionError:
        return True
    except (OSError, ValueError):
        return False


class LocalLauncher:
    """Spawns a provider through the crash-safe child bootstrap (ADR 0030).

    ``start`` forks the bootstrap child, which records ``started`` with its own pid before
    ``exec``-replacing itself with the provider argv, then waits (bounded) for that durable
    fact to appear or for the child to die without it. ``provider_command`` maps a record to
    the argv the child ``exec``s; the dormant slice supplies a no-op provider because no live
    pipeline stage routes here yet.
    """

    def __init__(self, provider_command=None, *, timeout: float = _HANDSHAKE_TIMEOUT_S) -> None:
        self._provider_command = provider_command or _dormant_provider_command
        self._timeout = timeout

    def start(self, record, store) -> StartResult:
        argv = [str(a) for a in self._provider_command(record)]
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "agentflow.coordinator._launch_child",
                 str(store.path), record.identity, *argv])
        except OSError:
            return StartResult(NOT_STARTED)  # no provider family ever came into existence
        # The intermediate exits at once; reap it so it does not linger. The provider
        # grandchild it forked records `started` with its own pid as the family.
        try:
            child.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + self._timeout
        while time.monotonic() <= deadline:
            reserved = store.record_of(record.identity)
            if reserved is not None and reserved.start_fact == STARTED:
                return StartResult(STARTED, reserved.family)
            time.sleep(0.01)
        return StartResult(NOT_STARTED)  # the child never durably recorded a start


def _dormant_provider_command(record) -> list[str]:
    """No live pipeline stage routes through the coordinator yet (ADR 0030's dormant slice),
    so the provider is a no-op that starts and exits; a real stage supplies the Claude or
    Codex argv when Build is the next slice."""
    return [sys.executable, "-c", ""]
