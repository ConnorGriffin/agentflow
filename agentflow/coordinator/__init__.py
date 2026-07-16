"""The session coordinator (ADR 0030) — one deep owner for one logical stage session.

Public surface: construct a :class:`Coordinator`, ``submit_stage`` the facts for one logical
stage, ``cycle`` a pool to admit work and collect the completed outcomes and human holds
it settles, and ``park_completed`` a completed stage the product policy leaves with no
successor to assume its claim. Everything else — the SQLite store under the agentflow state
directory, the
admission matrix and its gates, attempt numbers, the launcher handshake, the continuation
record, and provider observations — is a private implementation detail.

Build, Review, and Revise are the live stages behind this seam (issues #103, #104, #105): the
durable :class:`Rollout` switch and the :mod:`~agentflow.coordinator.tracer` bridge move them there
after a legacy drain, while every other logical stage stays queued and dormant.
"""

from agentflow.coordinator.build_stage import BuildStageAdapter
from agentflow.coordinator.coordinator import Coordinator, StageOutcome, Submission
from agentflow.coordinator.review_stage import ReviewStageAdapter
from agentflow.coordinator.revise_stage import ReviseStageAdapter
from agentflow.coordinator.rollout import (COORDINATED, DRAINING, LEGACY,
                                           MODE_COORDINATED, MODE_LEGACY, Phase, Rollout)
from agentflow.coordinator.stage_router import StageRouter

__all__ = [
    "Coordinator", "StageOutcome", "Submission", "BuildStageAdapter", "ReviewStageAdapter",
    "ReviseStageAdapter", "StageRouter", "Rollout", "Phase",
    "LEGACY", "DRAINING", "COORDINATED", "MODE_LEGACY", "MODE_COORDINATED",
]
