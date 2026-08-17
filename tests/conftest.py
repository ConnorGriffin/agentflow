"""Shared fakes for the session-coordinator tests (ADR 0030).

The coordinator's crash boundaries are exercised through its public ``submit_stage`` /
``cycle`` seam, never by driving private transitions. A single :class:`FakeSession` stands
in for the injected collaborators — the spawning launcher, family liveness, and the provider
observation — so a test scripts *what the world did* (a provider started, stayed alive, then
ended a certain way) and then cycles. The fake is the persistent world: reusing it across a
fresh :class:`Coordinator` replays a daemon crash, because the durable store already carries
what the launcher wrote and the fake still answers liveness the same way.

It also carries the suite-wide `gh` guard: no test anywhere may reach the real GitHub CLI, so a
stub pointed at a seam the code no longer uses fails loudly instead of passing for the wrong
reason.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from itertools import count

import pytest

from agentflow import coordinated_review, daemon, live, pipeline, ratchet
from agentflow.coordinator import Coordinator
from agentflow.coordinator.launcher import NOT_STARTED, STARTED, StartResult
from agentflow.coordinator.providers import (EndingReason, ProviderCause,
                                             ProviderObservation)


# --- the `gh` guard -------------------------------------------------------------

def _is_gh(cmd) -> bool:
    """Is this argv a `gh` invocation? Everything else — git plumbing, launchd, the rate-limit
    gate, the agent children — passes straight through the guard."""
    if isinstance(cmd, (str, bytes, os.PathLike)):
        head = os.fsdecode(cmd).split()[:1]
    else:
        head = [os.fsdecode(a) for a in list(cmd)[:1]]
    return bool(head) and os.path.basename(head[0]) == "gh"


class _RealGhProcess(BaseException):
    """A test let a real `gh` run. Not an ``Exception``, so the pipeline's bare
    ``except Exception`` handlers cannot swallow it into a log line."""


@pytest.fixture(autouse=True)
def _no_real_gh(request, monkeypatch):
    """Fail any test that lets a real `gh` process execute.

    A test that stubs the wrong GitHub seam — the generic ``github.api`` escape hatch when the
    code under test reads a typed method like ``github.pr_content`` — used to shell out to the
    real `gh` for a repo that does not exist, get a failure back, and then quietly assert
    against the fail-closed "GitHub unreadable" branch. It looks green while proving something
    else entirely, and it makes the suite depend on the machine having an authenticated `gh`.
    This turns that silence into a named failure.

    The guard sits on ``subprocess.run`` rather than on ``runner._run`` because a dozen modules
    bind ``_run`` into their own namespace at import time, so patching it in one place would
    miss most of them; every `gh` invocation in the pipeline reaches ``subprocess.run`` in the
    end. Only `gh` is blocked — tests that shell out to git plumbing or to the enrollment
    script are untouched.
    """
    real_run = subprocess.run

    def guarded(cmd, *args, **kwargs):
        if _is_gh(cmd):
            argv = " ".join(os.fsdecode(a) for a in cmd) if not isinstance(cmd, str) else cmd
            # Deliberately not an ``AssertionError``: several pipeline paths wrap their work in a
            # bare ``except Exception`` and log it (``dispatch._run_and_log`` and friends), which
            # would turn this into a log line and let the test pass green — the exact silence the
            # guard exists to end. Only a BaseException gets past them.
            raise _RealGhProcess(
                f"{request.node.nodeid} let a real `gh` process run: {argv}\n"
                "Some GitHub seam the code under test is unstubbed — stub the typed "
                "agentflow.github method it actually calls, not the generic github.api escape "
                "hatch."
            )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)


class _RealDiscoveryProbe(BaseException):
    """A test reached an unstubbed native-discovery probe, which would spawn a real provider
    session (up to 300 s of real quota). Not an ``Exception`` for the same reason as
    :class:`_RealGhProcess`."""


@pytest.fixture(autouse=True)
def _no_real_discovery_probe(request, monkeypatch):
    """Fail any test that lets the real native-discovery probe spawn a provider session.

    Repair now proves discovery as a tail step of every successful materialization, so a test
    that exercises repair without stubbing ``provider_skills._run_native_discovery_probe`` can
    reach a live ``claude``/``codex`` launch on a machine that has the binary — and silently
    take a different branch on CI where it doesn't. A test that wants the probe stubs the seam
    itself with ``monkeypatch.setattr``, which overrides this guard.
    """
    from agentflow import provider_skills

    def probe(_root, provider):
        raise _RealDiscoveryProbe(
            f"{request.node.nodeid} reached an unstubbed native-discovery probe for "
            f"{provider} — stub agentflow.provider_skills._run_native_discovery_probe."
        )

    monkeypatch.setattr(provider_skills, "_run_native_discovery_probe", probe)


def _isolate_ratchet_defaults(monkeypatch, path):
    """``ratchet.record``, ``record_once``, and ``status`` default their ``path`` argument to
    the module-level ``STATE`` constant captured when the function was defined, so patching
    ``ratchet.STATE`` alone cannot redirect a caller that relies on the default. Patch each
    function's own bound default directly."""
    for name in ("record", "record_once", "status"):
        fn = getattr(ratchet, name)
        monkeypatch.setattr(fn, "__defaults__", (path,))


@pytest.fixture(autouse=True)
def _isolated_agentflow_state(tmp_path, monkeypatch):
    """Give every test a private agentflow state directory without asking for a fixture.

    A test that never requested ``coord_state`` used to fall through to whatever
    ``AGENTFLOW_STATE`` (or the ``~/.agentflow`` default) the operator's session happened to
    have: a pacing test could read the live fleet's durable coordinator ledger and pass or fail
    by how busy the machine was, and daemon CLI tests could write the operator's live
    ``daemon-status.json`` through ``agentflow.live``. Setting ``AGENTFLOW_STATE`` alone is not
    enough — ``agentflow.live``, ``agentflow.daemon``, and ``agentflow.ratchet`` each bind their
    own state paths at import time, before any fixture runs — so those already-bound module
    constants (and ratchet's default ``path`` arguments) are patched here directly.
    """
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))

    monkeypatch.setattr(live, "STATE_DIR", tmp_path)
    monkeypatch.setattr(live, "LIVE_FILE", tmp_path / "live-sessions.json")
    monkeypatch.setattr(live, "REFUSALS_FILE", tmp_path / "refusals.json")
    monkeypatch.setattr(live, "STALLED_FILE", tmp_path / "stalled.json")
    monkeypatch.setattr(live, "DAEMON_FILE", tmp_path / "daemon-status.json")
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")

    monkeypatch.setattr(daemon, "STATE_DIR", tmp_path)
    monkeypatch.setattr(daemon, "ENABLE_FLAG", tmp_path / "enabled")
    monkeypatch.setattr(daemon, "LOCK", tmp_path / "daemon.lock")

    ratchet_path = tmp_path / "ratchet.json"
    monkeypatch.setattr(ratchet, "STATE", ratchet_path)
    _isolate_ratchet_defaults(monkeypatch, ratchet_path)

    return tmp_path


@pytest.fixture(autouse=True)
def _deterministic_reviewer(monkeypatch):
    """Pin the reviewer to the cross-tool default so tests that drive the review-open path never
    consult the live rate-limit gate (which is timing-sensitive and would make the suite flaky).
    ADR 0020's same-tool fallback — when the cross-tool pool is exhausted — is exercised directly
    in tests/test_balancer.py via ``choose_reviewer``. A test may still override this."""
    for module in (coordinated_review, pipeline):
        monkeypatch.setattr(module, "pick_reviewer",
                            lambda builder, **_kwargs: "codex" if builder == "claude" else "claude")


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
            cause: ProviderCause = ProviderCause.UNKNOWN, reset_at: int | None = None,
            end_fact: bool = True, usage=None,
            ending_reason: EndingReason = EndingReason.UNSPECIFIED) -> None:
        """Script how ``identity``'s provider ended and mark its family dead. A scripted end is a
        provider that finished on its own, so by default it leaves a durable supervisor end fact —
        the signal that separates a genuine failure from a family a daemon restart took down (which
        leaves none: see :meth:`kill`). An optional ``usage`` scripts the normalized spend the
        attempt reported, so a test can drive per-attempt telemetry through the public seam."""
        from agentflow.coordinator.telemetry import AttemptUsage
        self._script[identity] = _Ending(
            ProviderObservation(cause=cause, ending_reason=ending_reason,
                                reset_at=reset_at, has_end_fact=end_fact,
                                usage=usage or AttemptUsage()), success)
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
        if permits(coord, pool) > 0:  # a provider is running this cycle
            starts += 1
            fake.end(identity, cause=cause)
    raise AssertionError("record never reached a hold")


class NeverStartsLauncher:
    """A launch that never creates a provider family: the coordinator records ``not_started``
    and consumes no attempt."""

    def start(self, record, store) -> StartResult:
        return StartResult(NOT_STARTED)

    def is_alive(self, family: str | None) -> bool:
        return False  # nothing ever started, so no family is ever alive


def permits(coord, pool: str) -> int:
    """Test-only read of the coordinator's private permit ledger (the durable running rows).
    Permit accounting is an internal invariant, not a public operation, so the tests observe
    it through the store rather than a coordinator method (ADR 0030's two-call seam)."""
    return coord._store.permits_used(pool)


def record_of(coord, identity: str):
    """Test-only read of one durable record — white-box *observation* of ownership facts (the
    GitHub claim, tool lineage, auto-merge eligibility) that never cross the terminal seam,
    without driving any private transition."""
    return coord._store.load()[identity]


@pytest.fixture
def coord_state(_isolated_agentflow_state):
    """The private state directory the suite-wide ``_isolated_agentflow_state`` fixture already
    established — a compatibility accessor for tests that seed or inspect their private
    coordinator store. Isolation itself is automatic; this fixture no longer creates it.
    Reopening a Coordinator in the same test replays a crash over the same durable store."""
    return _isolated_agentflow_state


@pytest.fixture
def make_coord(coord_state):
    """Build a Coordinator over the isolated store, wiring a :class:`FakeSession` in as the
    launcher, liveness probe, observer, and gate. Called again with the same fake replays a
    daemon crash over the same durable store."""
    def _make(fake=None, **kwargs):
        if fake is not None:
            # The one fake plays all three collaborators: launcher (start + is_alive), stage
            # adapter (observe + verify), and admission gate.
            kwargs.setdefault("launcher", fake)
            kwargs.setdefault("adapter", fake)
            kwargs.setdefault("gate", fake.gate)
        return Coordinator(**kwargs)
    return _make
