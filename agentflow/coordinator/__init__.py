"""The session coordinator (ADR 0030) — one deep owner for one logical stage session.

Public surface: construct a :class:`Coordinator`, ``submit_stage`` the facts for one logical
stage, and ``cycle`` a pool to admit work and collect the completed outcomes and human holds
it settles. Everything else — the SQLite store under the agentflow state directory, the
admission matrix and its gates, attempt numbers, the launcher handshake, the continuation
record, and provider observations — is a private implementation detail. This slice is
dormant: no production pipeline stage submits work here yet.
"""

from agentflow.coordinator.coordinator import Coordinator, StageOutcome, Submission

__all__ = ["Coordinator", "StageOutcome", "Submission"]
