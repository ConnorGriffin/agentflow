"""The daemon-owned Project workspace (ADR 0033/0034).

The workspace is the durable local memory the daemon keeps for one enrolled Project: its
Conversations and their append-only immutable turns. It is a *separate* store from the
coordinator's private continuation records — the two never mutate each other (ADR 0033).

Public surface:

- :class:`WorkspaceStore` — one durable per-Project SQLite database under ``AGENTFLOW_STATE``.
  Conversations are aggregates with an optimistic revision; turns are immutable rounds keyed by
  ``(repository, Conversation ID, turn ordinal)``. Every mutation is a domain command with an
  idempotency key and (for a turn) an expected aggregate revision, so a repeat is safe and a
  stale write is rejected.
- :class:`CommandOutcome` — the terminal fact a command returns; the same idempotency key always
  replays the same one.
- :func:`workspace_projection` — the bounded JSON read model the daemon publishes for the web
  layer (ADR 0033: reads are projections, never the store).
"""

from agentflow.workspace.store import (
    ACCEPTED, REJECTED, CommandOutcome, Conversation, Turn, WorkspaceStore, project_slug)
from agentflow.workspace.projection import workspace_projection

__all__ = [
    "WorkspaceStore", "CommandOutcome", "Conversation", "Turn",
    "ACCEPTED", "REJECTED", "project_slug", "workspace_projection",
]
