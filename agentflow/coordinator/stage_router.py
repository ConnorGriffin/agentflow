"""Route each coordinator adapter call to the per-stage adapter for the record's stage.

The coordinator owns one stage adapter, but a live coordinator runs several logical stages.
This thin router dispatches preparation, observation, outcome capture/verification, completed
projection, and hold finalization on ``record.stage`` so stage policy stays local while the
coordinator remains stage-agnostic (ADR 0030).

A record whose stage has no registered adapter prepares trivially, verifies to False (no outcome
can be proven for a stage with no verifier), and produces no handoff — the same conservative
defaults a bare coordinator uses.
"""

from __future__ import annotations

from agentflow.coordinator.providers import ProviderObservation


class StageRouter:
    """Dispatches the four stage-adapter calls on ``record.stage``."""

    def __init__(self, adapters: dict) -> None:
        self._adapters = dict(adapters)

    def _for(self, record):
        return self._adapters.get(record.stage)

    def prepare(self, record) -> bool:
        adapter = self._for(record)
        prep = getattr(adapter, "prepare", None) if adapter is not None else None
        return bool(prep(record)) if prep is not None else True

    def observe(self, record) -> ProviderObservation:
        adapter = self._for(record)
        if adapter is None:
            return ProviderObservation()
        return adapter.observe(record)

    def verify(self, record, obs) -> bool:
        adapter = self._for(record)
        return bool(adapter.verify(record, obs)) if adapter is not None else False

    def capture(self, record, obs) -> str | None:
        adapter = self._for(record)
        fn = getattr(adapter, "capture", None) if adapter is not None else None
        return fn(record, obs) if fn is not None else None

    def finalize_completed(self, record) -> str | None:
        adapter = self._for(record)
        fn = getattr(adapter, "finalize_completed", None) if adapter is not None else None
        return fn(record) if fn is not None else None

    def prepare_completed(self, record) -> bool:
        adapter = self._for(record)
        fn = getattr(adapter, "prepare_completed", None) if adapter is not None else None
        return bool(fn(record)) if fn is not None else True

    def finalize_hold(self, record) -> str | None:
        adapter = self._for(record)
        fn = getattr(adapter, "finalize_hold", None) if adapter is not None else None
        return fn(record) if fn is not None else None

    def integration_collision(self, record) -> str | None:
        adapter = self._for(record)
        fn = getattr(adapter, "integration_collision", None) if adapter is not None else None
        return fn(record) if fn is not None else None
