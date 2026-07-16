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

# The durable launcher-handshake results (ADR 0030). Defined here, beside the record they are
# written onto, so the store can guard on them without importing the launcher (which imports
# the store); the launcher re-exports them.
STARTED = "started"
NOT_STARTED = "not_started"


@dataclass
class Record:
    """One logical stage's continuation state. Only the coordinator writes these fields."""

    identity: str          # stable (repo, subject, stage, target) key — submission is idempotent on it
    stage: str             # logical stage: intake | build | review | revise | mockup | respond
    pool: str              # claude | codex — the pool this record is charged against
    demand: int            # permits this attempt reserves on `pool` (from the admission matrix)
    repo: str = ""         # the originating repo — kept for the ADR 0028 logs and claim ownership
    subject: str = ""      # the issue/PR subject — kept for the ADR 0028 logs and claim ownership
    target: str | None = None  # immutable target (head SHA / comment id), part of the identity
    continuation: bool = False   # eligible ahead of cold work on its pool (ADR 0028 order)
    eligible_at: int = 0         # when a paused continuation may be admitted again
    created_at: int = 0          # epoch of first submission: continuation-queue tie-breaker, and
                                 # the anchor a Revise binds its non-code evidence to (issue #118)
    model: str = "opus"
    complexity: str = "deep"
    effort: str | None = None
    attempts: int = 0            # provider attempts consumed (initial + up to two continuations)
    attempt_committed: bool = False  # this attempt's count is already consumed (idempotent on recovery)
    state: str = WAITING
    claim: bool = True           # holds the GitHub dedup claim while the stage is owned
    lineage: str | None = None   # pinned tool for code-writing stages; None once free to move
    source: str | None = None    # durable working-directory/worktree pointer for provider launch
    input_ptr: str | None = None # durable pointer the provider adapter rebuilds the prompt from
    outcome: str | None = None   # stage-native durable outcome, captured before external projection
    started_at: int = 0                  # epoch when the current attempt was admitted
    deadline: int = 0                    # supervisor observe-until deadline, for the recovered-running log
    start_fact: str | None = None        # durable launcher handshake result: started | not_started
    launch_token: str | None = None      # nonce a reservation stamps; only the child holding it may record `started`
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
    builder_complexity: str | None = None  # the original builder complexity, carried so a later
                                           # Revise never re-reads a mutable issue label (ADR 0018)
    round: int = 0                       # completed auto-revise rounds behind this stage; part of
                                         # the identity so an evidence-only revision's re-review at
                                         # the same head SHA is still a fresh stage
    auto_merge_allowed: bool = True
    root: str | None = None              # the root stage this descends from; it shares the root's reservation
