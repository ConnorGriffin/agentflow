"""The session coordinator (ADR 0030) — one deep owner for one logical stage session.

Public surface: construct a :class:`Coordinator`, ``submit_stage`` the facts for one logical
stage, and ``cycle`` a pool to admit work and collect the completed outcomes and human holds
it settles. Everything else — the SQLite store under the agentflow state directory, the
admission matrix and its gates, attempt numbers, the launcher handshake, the continuation
record, and provider observations — is a private implementation detail.

Build is the first live stage behind this seam (issue #103): the durable :class:`Rollout`
switch and the :mod:`~agentflow.coordinator.tracer` bridge move it there after a legacy drain,
while every other logical stage stays queued and dormant.
"""

from agentflow.coordinator.build_stage import BuildStageAdapter
from agentflow.coordinator.coordinator import Coordinator, StageOutcome, Submission
from agentflow.coordinator.rollout import (COORDINATED, DRAINING, LEGACY,
                                           MODE_COORDINATED, MODE_LEGACY, Phase, Rollout)

__all__ = [
    "Coordinator", "StageOutcome", "Submission", "BuildStageAdapter", "Rollout", "Phase",
    "LEGACY", "DRAINING", "COORDINATED", "MODE_LEGACY", "MODE_COORDINATED",
]
