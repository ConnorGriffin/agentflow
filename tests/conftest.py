"""Shared fakes for the session-coordinator tests (ADR 0030).

The coordinator's crash boundaries are exercised through its public ``submit_stage`` /
``cycle`` seam, never by driving private transitions. A single :class:`FakeSession` stands
in for the injected collaborators — the spawning launcher, family liveness, and the provider
observation — so a test scripts *what the world did* (a provider started, stayed alive, then
ended a certain way) and then cycles. The fake is the persistent world: reusing it across a
fresh :class:`Coordinator` replays a daemon crash, because the durable store already carries
what the launcher wrote and the fake still answers liveness the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import count

import pytest

from agentflow.coordinator import Coordinator
from agentflow.coordinator.launcher import NOT_STARTED, STARTED, StartResult
from agentflow.coordinator.providers import ProviderCause, ProviderObservation


@dataclass
class _Ending:
    obs: ProviderObservation
    success: bool


class FakeSession:
    """An injectable stand-in for the launcher, liveness probe, and provider observer.

    A ``start`` durably records ``started`` with a fresh family (as the real child would) and
    marks that family alive. ``end`` scripts how a family's provider finished and marks it
    dead, so the next ``cycle`` reconciles it. ``crash_start`` makes a launch die after the
    coordinator has reserved but before any ``started`` is durable, reproducing the
    reserved-but-never-started boundary.
    """

    def __init__(self) -> None:
        self.alive: set[str] = set()
        self._pids = count(900001)
        self.family_of: dict[str, str] = {}
        self._script: dict[str, _Ending] = {}
        self.crash_start = False
        self.gate_open = True

    # --- injected as the coordinator's launcher ---
    def start(self, record, store) -> StartResult:
        if self.crash_start:
            raise RuntimeError("daemon died after reservation, before start was durable")
        family = str(next(self._pids))
        self.family_of[record.identity] = family
        record.start_fact = STARTED
        record.family = family
        record.process_alive = True
        store.upsert(record)
        self.alive.add(family)
        return StartResult(STARTED, family)

    # --- injected as the coordinator's liveness probe ---
    def is_alive(self, family: str | None) -> bool:
        return family in self.alive

    # --- injected as the coordinator's admission gate ---
    def gate(self, record) -> bool:
        return self.gate_open

    # --- injected as the coordinator's provider observer ---
    def observe(self, record) -> ProviderObservation:
        ending = self._script.get(record.identity)
        return ending.obs if ending else ProviderObservation()

    def verify(self, record, obs) -> bool:
        ending = self._script.get(record.identity)
        return bool(ending and ending.success)

    # --- test controls ---
    def kill(self, identity: str) -> None:
        """The provider family for ``identity`` ended without leaving a scripted observation
        (a bare death) — mark it dead so reconciliation classifies it."""
        self.alive.discard(self.family_of.get(identity, ""))

    def end(self, identity: str, *, success: bool = False,
            cause: ProviderCause = ProviderCause.UNKNOWN, reset_at: int | None = None) -> None:
        """Script how ``identity``'s provider ended and mark its family dead."""
        self._script[identity] = _Ending(
            ProviderObservation(cause=cause, reset_at=reset_at), success)
        self.kill(identity)


def starts_until_held(coord, fake, identity, pool, cause=ProviderCause.UNKNOWN):
    """Cycle the pool, ending each running attempt with ``cause``, until ``identity`` is held.
    Returns how many provider starts it took — the observable attempt budget. A fresh record
    starts three times; a record that already consumed an attempt (a durable ``started`` a
    crash recovered) starts fewer, which is how attempt accounting is checked at the seam."""
    starts = 0
    for _ in range(12):
        outcomes = coord.cycle(pool)
        if any(o.identity == identity and o.status == "held" for o in outcomes):
            return starts
        if coord.permits(pool) > 0:  # a provider is running this cycle
            starts += 1
            fake.end(identity, cause=cause)
    raise AssertionError("record never reached a hold")


class NeverStartsLauncher:
    """A launch that never creates a provider family: the coordinator records ``not_started``
    and consumes no attempt."""

    def start(self, record, store) -> StartResult:
        return StartResult(NOT_STARTED)


@pytest.fixture
def coord_state(tmp_path, monkeypatch):
    """Isolate each test's coordinator store under a private state directory. Reopening a
    Coordinator in the same test replays a crash over the same durable store."""
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    return tmp_path


@pytest.fixture
def make_coord(coord_state):
    """Build a Coordinator over the isolated store, wiring a :class:`FakeSession` in as the
    launcher, liveness probe, observer, and gate. Called again with the same fake replays a
    daemon crash over the same durable store."""
    def _make(fake=None, **kwargs):
        if fake is not None:
            kwargs.setdefault("launcher", fake)
            kwargs.setdefault("is_alive", fake.is_alive)
            kwargs.setdefault("observe", fake.observe)
            kwargs.setdefault("verify", fake.verify)
            kwargs.setdefault("gate", fake.gate)
        return Coordinator(**kwargs)
    return _make
