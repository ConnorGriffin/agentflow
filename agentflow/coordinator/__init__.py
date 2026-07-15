"""The session coordinator (ADR 0030) — one deep owner for one logical stage session.

Public surface: construct a :class:`Coordinator` against a store path, ``submit_stage`` the
facts for one logical stage, and ``cycle`` a pool to admit and start work. Everything else —
the SQLite store, the admission matrix, attempt numbers, the launcher handshake, and
provider observations — is a private implementation detail. This slice is dormant: no
production pipeline stage submits work here yet.
"""

from agentflow.coordinator.coordinator import Coordinator, Gates, Submission
from agentflow.coordinator.record import Record
from agentflow.coordinator.store import StoreUnavailable

__all__ = ["Coordinator", "Gates", "Submission", "Record", "StoreUnavailable"]
