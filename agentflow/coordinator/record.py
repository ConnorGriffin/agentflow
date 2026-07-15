"""The durable continuation record — one per logical stage (ADR 0028).

This is the entity the store persists and the coordinator transitions through its four
states. Its ``running`` rows are the only permit ledger (ADR 0029/0030): there is no
second counter to reconcile. The coordinator owns every transition; nothing outside the
coordinator package should mutate a record's state, attempts, or reservation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The four persisted states (ADR 0028). `completed`/`held` are reconciliation states that
# retire once ownership transfers or the durable boundary is confirmed.
WAITING = "waiting"
RUNNING = "running"
COMPLETED = "completed"
HELD = "held"


@dataclass
class Record:
    """One logical stage's continuation state. Only the coordinator writes these fields."""

    identity: str          # stable (repo, subject, stage, target) key — submission is idempotent on it
    stage: str             # logical stage: intake | build | review | revise | mockup | respond
    pool: str              # claude | codex — the pool this record is charged against
    demand: int            # permits this attempt reserves on `pool` (from the admission matrix)
    continuation: bool = False   # eligible ahead of cold work on its pool (ADR 0028 order)
    eligible_at: int = 0         # when a paused continuation may be admitted again
    created_at: int = 0          # tie-breaker after eligible_at in the continuation queue
    model: str = "opus"
    complexity: str = "deep"
    effort: str | None = None
    attempts: int = 0            # provider attempts consumed (initial + up to two continuations)
    attempt_committed: bool = False  # this attempt's count is already consumed (idempotent on recovery)
    state: str = WAITING
    claim: bool = True           # holds the GitHub dedup claim while the stage is owned
    lineage: str | None = None   # pinned tool for code-writing stages; None once free to move
    start_fact: str | None = None        # durable launcher handshake result: started | not_started
    family: str | None = None            # the provider process-family identity a `started` carries
    process_alive: bool = False          # whether that family is still executing
    descendants: set[str] = field(default_factory=set)  # subagents charged to the root reservation
    handoffs: int = 0
    handoff_kind: str | None = None
    notifications: int = 0
    handoff_proof: str | None = None     # proof the stage-native human handoff exists (crash-safe)
    hold_pending: bool = False           # classified as a hold, awaiting its durable handoff
    retired: bool = False
    builder_lineage: str | None = None   # who built the diff — a same-tool review cannot auto-merge
    auto_merge_allowed: bool = True
