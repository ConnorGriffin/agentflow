"""The crash-safe provider start handshake (ADR 0030).

A provider is started through a small local launcher that spawns a child process. That child
claims ``started`` under the launch token, then durably records its supervisor pid together with
the gated provider's separate process-group id before provider code can execute. The family is
therefore observable if either process outlives the other. A launch that never records the start
fact consumes no attempt; the coordinator consumes an attempt if and only if the durable result
is ``started``.

The launcher genuinely spawns and hands off — it is not an in-process simulation, and the
family records the child supervisor and provider group, never the daemon. That is the crash
boundary: reconciliation reads the same durable fact and both members' real liveness, so a fresh
coordinator over the same store reconstructs exactly what happened. Tests inject a scripted
launcher double at construction to drive the boundaries without spawning, and focused integration
tests exercise the real spawn (see tests/test_coordinator_launcher.py).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agentflow.coordinator.record import NOT_STARTED, STARTED  # re-exported for callers
from agentflow.coordinator._launch_child import _NO_WORKTREE, _WORKTREE

# Bounded wait for the spawned child to durably record `started` before we treat the launch
# as one that never produced a provider family.
_HANDSHAKE_TIMEOUT_S = float(os.environ.get("AGENTFLOW_COORD_HANDSHAKE_S", "10"))
# Bootstrap imports must resolve from the daemon deployment; the child receives any provider
# worktree separately and applies it only in the gated provider fork.
_DAEMON_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class StartResult:
    fact: str                     # started | not_started
    family: str | None = None     # the durable process-family identity a `started` carries


def _pid_family_members(family: str | None) -> tuple[int, int | None] | None:
    """Parse current ``supervisor:provider-group`` and legacy ``supervisor`` families."""
    if not family:
        return None
    parts = family.split(":")
    if len(parts) not in {1, 2}:
        return None
    try:
        supervisor = int(parts[0])
        provider_group = int(parts[1]) if len(parts) == 2 else None
    except ValueError:
        return None
    if supervisor <= 0 or (provider_group is not None and provider_group <= 0):
        return None
    return supervisor, provider_group


def pid_family_supervisor(family: str | None) -> int | None:
    """Return the supervisor pid from a current or legacy durable family."""
    members = _pid_family_members(family)
    return members[0] if members is not None else None


def pid_family_alive(family: str | None) -> bool:
    """Whether a current or legacy durable provider family may still be executing.

    Current records carry the detached supervisor pid and the provider's separate process-group
    id. Either being live keeps recovery from launching another provider. Legacy one-part records
    retain their original supervisor-only probe, and unavailable OS probes remain conservatively
    alive; this liveness seam only observes and never adopts a process.
    """
    members = _pid_family_members(family)
    if members is None:
        return False
    supervisor, provider_group = members

    def provider_alive() -> bool:
        if provider_group is None:
            return False
        try:
            os.killpg(provider_group, 0)
        except PermissionError:
            return True
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True

    try:
        os.kill(supervisor, 0)  # probe only; signal 0 has no effect
    except PermissionError:
        return True
    except ProcessLookupError:
        return provider_alive()
    except OSError:
        return True
    try:
        if os.getpgid(supervisor) == supervisor:
            return True
        return provider_alive()
    except PermissionError:
        return True
    except ProcessLookupError:
        return provider_alive()
    except OSError:
        return True


def stop_pid_family(family: str | None, signum: int) -> None:
    """Signal both durable members without ever signalling the supervisor's process group."""
    members = _pid_family_members(family)
    if members is None:
        return
    supervisor, provider_group = members
    try:
        os.kill(supervisor, signum)
    except ProcessLookupError:
        pass
    if provider_group is not None:
        try:
            os.killpg(provider_group, signum)
        except ProcessLookupError:
            pass


class LocalLauncher:
    """Spawns a provider through the crash-safe child bootstrap (ADR 0030).

    ``start`` forks the bootstrap child, which claims ``started`` with its supervisor pid and
    attaches the gated provider group before releasing provider code. It then supervises the
    provider while structured stream and terminal facts go to durable per-attempt artifacts.
    ``start`` waits (bounded) for that complete family to appear or for the child to die without
    it. ``provider_command`` maps a record to the argv the child runs; the default builds the real
    Claude/Codex session command for a record that carries a prompt and a no-op for a bare record.
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

    def _session_timeout_for(self, record, admitted=None) -> float:
        """The wall-clock ceiling for this launch: an explicit constructor override, else the
        ``AGENTFLOW_SESSION_TIMEOUT`` ops override, else the record's stage-profile wall ceiling."""
        if admitted is not None:
            from agentflow.coordinator.providers import _validated_admitted_launch
            return float(_validated_admitted_launch(record, admitted).wall_ceiling_s)
        if self._session_timeout is not None:
            return self._session_timeout
        override = os.environ.get("AGENTFLOW_SESSION_TIMEOUT")
        if override:
            return float(override)
        from agentflow.coordinator.profiles import profile_for
        return float(profile_for(record).wall_ceiling_s)

    def _build_lease_for(self, record, admitted=None) -> tuple[float, float, float] | None:
        """Return Build's progress lease unless an operator pinned a fixed timeout.

        Constructor and environment overrides are intentionally a complete replacement for
        supervision policy: they retain the fixed, non-renewable timeout operators already use.
        """
        if admitted is not None:
            from agentflow.coordinator.providers import _validated_admitted_launch
            return _validated_admitted_launch(record, admitted).build_lease
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

    def start(self, record, store, admitted=None) -> StartResult:
        token = record.launch_token
        try:
            command = (self._provider_command(record, admitted) if admitted is not None
                       else self._provider_command(record))
        except OSError:
            return StartResult(NOT_STARTED)
        argv = [str(a) for a in command]
        lease = self._build_lease_for(record, admitted)
        lease_args = (["--build-lease", record.pool,
                       *(str(value) for value in lease)] if lease else [])
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "agentflow.coordinator._launch_child",
                 str(store.path), record.identity, str(token),
                 str(self._session_timeout_for(record, admitted)),
                 *lease_args,
                 *((_WORKTREE, record.source) if record.source else (_NO_WORKTREE,)),
                 *argv], cwd=_DAEMON_ROOT)
        except OSError:
            return StartResult(NOT_STARTED)  # no provider family ever came into existence
        # The intermediate exits at once; reap it so it does not linger. The provider
        # grandchild it forked records `started` and the complete process family, but only while
        # it still holds this reservation's launch token.
        try:
            child.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            pass
        deadline = time.monotonic() + self._timeout
        while time.monotonic() <= deadline:
            reserved = store.record_of(record.identity)
            if (reserved is not None and reserved.start_fact == STARTED
                    and reserved.launch_token == token
                    and (not argv or (_pid_family_members(reserved.family) is not None
                                      and ":" in reserved.family))):
                return StartResult(STARTED, reserved.family)
            time.sleep(0.01)
        # The child never durably recorded a start in time. Atomically disown this launch:
        # unless the child already won under this token, its token is rotated so any late
        # guarded write is refused and no unreserved, uncounted provider can start.
        fact, family = store.disown_launch(record.identity, token)
        return StartResult(fact, family)
