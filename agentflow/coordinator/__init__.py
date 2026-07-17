"""The session coordinator (ADR 0030) — one deep owner for one logical stage session.

Public surface: construct a :class:`Coordinator`, ``submit_stage`` the facts for one logical
stage, ``cycle`` a pool to admit work and collect the completed outcomes and human holds
it settles, and ``park_completed`` a completed stage the product policy leaves with no
successor to assume its claim. Everything else — the SQLite store under the agentflow state
directory, the
admission matrix and its gates, attempt numbers, the launcher handshake, the continuation
record, and provider observations — is a private implementation detail.

All six logical stages are live behind this seam (issues #103–#108): the durable
Unknown future stages stay queued and dormant.
"""

from agentflow.coordinator.build_stage import BuildStageAdapter
from agentflow.coordinator.converse_stage import ConverseStageAdapter
from agentflow.coordinator.intake_stage import IntakeStageAdapter
from agentflow.coordinator.mockup_stage import MockupStageAdapter
from agentflow.coordinator.coordinator import Coordinator, StageOutcome, Submission
from agentflow.coordinator.respond_stage import RespondStageAdapter
from agentflow.coordinator.review_stage import ReviewStageAdapter
from agentflow.coordinator.revise_stage import ReviseStageAdapter
from agentflow.coordinator.stage_router import StageRouter

__all__ = [
    "Coordinator", "StageOutcome", "Submission", "BuildStageAdapter", "ConverseStageAdapter",
    "IntakeStageAdapter", "MockupStageAdapter", "ReviewStageAdapter",
    "ReviseStageAdapter", "RespondStageAdapter", "StageRouter",
]
