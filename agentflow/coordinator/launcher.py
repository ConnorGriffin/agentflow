"""The crash-safe provider start handshake (ADR 0030).

A provider is started through a small local launcher that durably records ``started`` with
its process-family identity *before* it replaces itself with the provider process. A launch
that never creates that family records ``not_started`` and consumes no attempt. The
coordinator consumes an attempt if and only if the durable result is ``started``.

The handshake is the whole crash boundary: the launcher may not let a provider process
escape without making its result recoverable, even if the process exits immediately or the
coordinator dies before reading it. The result therefore lives on the durable record (the
launcher persists it through the same store), so a fresh coordinator reconstructs it.

Production would run the launcher as a separate process that ``exec``s the provider; the
handshake itself is a pure, injectable step so the four crash boundaries are exercised by
fakes without spawning anything (see tests/test_coordinator_launcher.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass

STARTED = "started"
NOT_STARTED = "not_started"

# Where a fake launcher can be told to die, matching ADR 0030's four injected boundaries.
BEFORE_RESERVATION = "before_reservation"     # handled by the coordinator: nothing reserved
AFTER_RESERVATION = "after_reservation"       # reserved, but the launcher never confirmed
BEFORE_REPLACEMENT = "before_replacement"     # `started` is durable, provider not yet alive
AFTER_START = "after_start"                   # the provider family is alive


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
    """The small local launcher that starts a provider (ADR 0030). It persists ``started``
    with a family identity before the provider replaces it, so the result is recoverable
    across a crash. In production the replacement ``exec``s the provider; in this dormant
    slice (and in tests) the replacement is injected. ``crash_at`` lets a test inject daemon
    death at any of the four handshake boundaries; ``fact`` lets it script a launch that
    never creates a family."""

    def __init__(self, *, fact: str = STARTED, family: str | None = None,
                 crash_at: str | None = None) -> None:
        self.fact = fact
        # The dormant in-process family is the daemon itself, so reconciliation reads it as
        # alive; a test injects a distinct family (with its own liveness) to drive recovery.
        self.family = family or str(os.getpid())
        self.crash_at = crash_at

    def start(self, record, persist) -> StartResult:
        if self.crash_at == AFTER_RESERVATION:
            # The daemon died before the launcher confirmed anything: no start fact exists.
            raise _InjectedDeath(AFTER_RESERVATION)
        if self.fact == NOT_STARTED:
            record.start_fact = NOT_STARTED
            persist(record)
            return StartResult(NOT_STARTED)
        # Record `started` with the family identity durably, before provider replacement.
        record.start_fact = STARTED
        record.family = self.family
        record.process_alive = False
        persist(record)
        if self.crash_at == BEFORE_REPLACEMENT:
            raise _InjectedDeath(BEFORE_REPLACEMENT)
        record.process_alive = True
        persist(record)
        if self.crash_at == AFTER_START:
            raise _InjectedDeath(AFTER_START)
        return StartResult(STARTED, self.family)


class _InjectedDeath(RuntimeError):
    """A test-only signal that the daemon died at a named handshake boundary."""
