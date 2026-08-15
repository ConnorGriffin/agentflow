"""The stage-adapter protocol: its one set of defaults, and the router that dispatches on stage.

A stage adapter's hooks are all optional — a read-only stage prepares trivially, a stage with no
verifier can prove no outcome, a stage that classifies no recovery continues within budget, and a
stage with no settlement projects nothing. :class:`StageCalls` is the single place those defaults
live. The coordinator calls every adapter through it, so a bare coordinator, a partial adapter,
and a record whose stage has no adapter registered all behave the same way (ADR 0030).

:class:`StageRouter` sits on top: the coordinator owns one stage adapter, but a live coordinator
runs several logical stages, so the router dispatches each call on ``record.stage`` and keeps
stage policy local while the coordinator stays stage-agnostic.
"""

from __future__ import annotations

from agentflow.coordinator.admission import native_handoff_proof
from agentflow.coordinator.providers import ProviderObservation
from agentflow.coordinator.recovery import Recovery


class StageCalls:
    """One stage adapter's optional hooks, with the default for each one owned here.

    Wrapping rather than subclassing keeps the adapter itself free to implement only what its
    stage actually needs, which is what makes the eight stage modules small.
    """

    def __init__(self, adapter) -> None:
        self._adapter = adapter

    def _hook(self, name):
        return getattr(self._adapter, name, None) if self._adapter is not None else None

    def prepare(self, record):
        """A stage that owns no source to rebuild is always ready to be admitted. The adapter's
        answer passes through as-is: a typed Verification keeps the named check that refused for
        the coordinator to persist; a legacy bool stays a bool. Callers branch on truthiness."""
        fn = self._hook("prepare")
        return fn(record) if fn is not None else True

    def observe(self, record) -> ProviderObservation:
        fn = self._hook("observe")
        return fn(record) if fn is not None else ProviderObservation()

    def verify(self, record, obs):
        """No verifier means no outcome can be proven — never a completion by default. The
        adapter's answer passes through as-is: a typed Verification keeps its named miss for
        the coordinator to persist; a legacy bool stays a bool. Callers branch on truthiness."""
        fn = self._hook("verify")
        return fn(record, obs) if fn is not None else False

    def capture(self, record, obs) -> str | None:
        fn = self._hook("capture")
        return fn(record, obs) if fn is not None else None

    def project_outcome(self, record, obs) -> None:
        fn = self._hook("project_outcome")
        if fn is not None:
            fn(record, obs)

    def prepare_completed(self, record) -> bool:
        fn = self._hook("prepare_completed")
        return bool(fn(record)) if fn is not None else True

    def finalize_completed(self, record) -> str | None:
        fn = self._hook("finalize_completed")
        return fn(record) if fn is not None else None

    def finalize_hold(self, record) -> str | None:
        """A stage that creates no richer handoff still holds, against the durable proof of the
        handoff kind ADR 0028's exhaustion table names for its stage."""
        fn = self._hook("finalize_hold")
        return fn(record) if fn is not None else native_handoff_proof(record)

    def integration_collision(self, record) -> str | None:
        fn = self._hook("integration_collision")
        return fn(record) if fn is not None else None

    def recover(self, record, obs) -> Recovery:
        """A stage that classifies no recovery keeps the historical behavior — every
        non-permanent ending continues within the attempt budget (issue #225)."""
        fn = self._hook("recover")
        result = fn(record, obs) if fn is not None else None
        return result if isinstance(result, Recovery) else Recovery()


_NO_ADAPTER = StageCalls(None)


class StageRouter:
    """Dispatches every stage-adapter call on ``record.stage``."""

    def __init__(self, adapters: dict) -> None:
        self._adapters = {stage: StageCalls(adapter) for stage, adapter in adapters.items()}

    def _for(self, record) -> StageCalls:
        return self._adapters.get(record.stage, _NO_ADAPTER)

    def prepare(self, record):
        return self._for(record).prepare(record)

    def observe(self, record) -> ProviderObservation:
        return self._for(record).observe(record)

    def verify(self, record, obs):
        return self._for(record).verify(record, obs)

    def capture(self, record, obs) -> str | None:
        return self._for(record).capture(record, obs)

    def project_outcome(self, record, obs) -> None:
        self._for(record).project_outcome(record, obs)

    def prepare_completed(self, record) -> bool:
        return self._for(record).prepare_completed(record)

    def finalize_completed(self, record) -> str | None:
        return self._for(record).finalize_completed(record)

    def finalize_hold(self, record) -> str | None:
        return self._for(record).finalize_hold(record)

    def integration_collision(self, record) -> str | None:
        return self._for(record).integration_collision(record)

    def recover(self, record, obs) -> Recovery:
        return self._for(record).recover(record, obs)
