"""The crash-safe provider start handshake (ADR 0030).

A provider is started through a small local launcher that spawns a child process. That
child durably records ``started`` with its own process-family identity *before* it
spawns the provider beneath itself, so the start fact and the family exist on the
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

from agentflow.coordinator.record import NOT_STARTED, STARTED  # re-exported for callers
from agentflow.coordinator._launch_child import _INHERITED_WORKTREE, _NO_WORKTREE

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
    supervising the provider (whose structured stream and terminal facts go to durable
    per-attempt artifacts), then waits (bounded) for that durable fact to
    appear or for the child to die without it. ``provider_command`` maps a record to the argv
    the child runs; the default builds the real Claude/Codex session command for a record that
    carries a prompt and a no-op for a bare record that does not.
    """

    def __init__(self, provider_command=None, *, timeout: float = _HANDSHAKE_TIMEOUT_S,
                 session_timeout: float | None = None,
                 build_lease: tuple[float, float, float] | None = None) -> None:
        from agentflow.coordinator.providers import provider_command as real_command
        self._provider_command = provider_command or real_command
        self._timeout = timeout
        # ``None`` (the production default) sizes each launch's wall ceiling from its stage
        # profile (ADR 0044); an explicit value pins one ceiling for all launches (tests/ops).
        self._session_timeout = session_timeout
        self._build_lease = build_lease

    def _session_timeout_for(self, record) -> float:
        """The wall-clock ceiling for this launch: an explicit constructor override, else the
        ``AGENTFLOW_SESSION_TIMEOUT`` ops override, else the record's stage-profile wall ceiling."""
        if self._session_timeout is not None:
            return self._session_timeout
        override = os.environ.get("AGENTFLOW_SESSION_TIMEOUT")
        if override:
            return float(override)
        from agentflow.coordinator.profiles import profile_for
        return float(profile_for(record).wall_ceiling_s)

    def _build_lease_for(self, record) -> tuple[float, float, float] | None:
        """Return Build's progress lease unless an operator pinned a fixed timeout.

        Constructor and environment overrides are intentionally a complete replacement for
        supervision policy: they retain the fixed, non-renewable timeout operators already use.
        """
        if record.stage != "build" or self._session_timeout is not None:
            return None
        if os.environ.get("AGENTFLOW_SESSION_TIMEOUT"):
            return None
        if self._build_lease is not None:
            return self._build_lease
        from agentflow.coordinator.profiles import profile_for
        return profile_for(record).build_lease

    @staticmethod
    def is_alive(family: str | None) -> bool:
        """Whether the recorded provider family is still executing. The launcher owns the
        family it started, so it also answers the liveness the coordinator reconciles on."""
        return pid_family_alive(family)

    def start(self, record, store) -> StartResult:
        token = record.launch_token
        try:
            command = self._provider_command(record)
        except OSError:
            return StartResult(NOT_STARTED)
        argv = [str(a) for a in command]
        lease = self._build_lease_for(record)
        lease_args = (["--build-lease", record.pool,
                       *(str(value) for value in lease)] if lease else [])
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "agentflow.coordinator._launch_child",
                 str(store.path), record.identity, str(token),
                 str(self._session_timeout_for(record)),
                 *lease_args,
                 _INHERITED_WORKTREE if record.source else _NO_WORKTREE,
                 *argv], cwd=record.source or None)
        except OSError:
            return StartResult(NOT_STARTED)  # no provider family ever came into existence
        # The intermediate exits at once; reap it so it does not linger. The provider
        # grandchild it forked records `started` with its own pid as the family, but only
        # while it still holds this reservation's launch token.
        try:
            child.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + self._timeout
        while time.monotonic() <= deadline:
            reserved = store.record_of(record.identity)
            if (reserved is not None and reserved.start_fact == STARTED
                    and reserved.launch_token == token):
                return StartResult(STARTED, reserved.family)
            time.sleep(0.01)
        # The child never durably recorded a start in time. Atomically disown this launch:
        # unless the child already won under this token, its token is rotated so any late
        # guarded write is refused and no unreserved, uncounted provider can start.
        fact, family = store.disown_launch(record.identity, token)
        return StartResult(fact, family)
