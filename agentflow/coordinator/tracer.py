"""The tracer bridge (issues #103–#108) — the small set of reads and gates that connect the
session coordinator to dispatch while all six logical stages are coordinated.

Three things the dispatch layer needs when these stages run behind the coordinator, all derived
from the durable continuation records so there is no second source of truth:

- **All six logical stages are enabled.**
  :func:`build_review_revise_gate` is the coordinator's only stage admission gate: every listed
  logical stage admits; unknown stages may be submitted and sit visibly ``waiting`` but consume
  neither a permit nor an attempt.
- **The live board becomes a projection of running records.** :func:`live_projection` renders
  the running Build and Review records as live-session entries; waiting records reserve nothing
  and do not appear, exactly matching the reviewed admission demand they hold.
- **Coordinator ownership is authoritative for claims.** :func:`owned_issues` names the issues a
  coordinator record still owns, so outcome-first claim reconciliation cannot strip one.

Every function is pure over an iterable of records, so the behaviors are exercised without a
daemon, GitHub, or a live store; :func:`load_records` is the thin production reader.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agentflow.coordinator.record import RUNNING, STALL_STALLED_AFTER, Record, stalled_for
from agentflow.coordinator.store import Store, default_store_path
from agentflow.worktree_ref import WorktreeRef


# The logical stages enabled behind the coordinator — the one source
# :func:`build_review_revise_gate` consumes. The six pipeline stages (issues #103–#108), the
# Conversation-turn stage (``converse``) an Ask submits per operator message (ADR 0034), the
# unattended ``research`` stage the daemon dispatches for an AFK-able planning ticket (ADR 0037),
# and the cold ``attack`` stage that argues with a triage draft before it is ever published
# (ADR 380).
ENABLED_STAGES = ("intake", "build", "review", "revise", "mockup", "respond", "converse",
                  "research", "attack")


def build_review_revise_gate(record: Record) -> bool:
    """The production admission gate: admit exactly :data:`ENABLED_STAGES`, refuse every
    other logical stage. A refused stage stays ``waiting`` and reserves nothing — no permit, no
    attempt."""
    return record.stage in ENABLED_STAGES


def _issue_number(record: Record) -> int | None:
    try:
        return int(record.subject)
    except (TypeError, ValueError):
        return None


# Which GitHub claim label each logical stage owns. Intake owns the ``triaging`` claim; Build,
# Review, Revise, and Respond own ``building``; Mockup owns ``drawing``. Claim types are resolved
# independently so one live record never hides another lane's stale claim — an
# Intake record must never be read as owning a ``building`` claim, nor a Build record a
# ``triaging`` one, or one type's live record would shield the other type's stale claim from
# reclamation (and hide it from forward-activation evidence).
# The attack borrows Intake's ``triaging`` claim rather than taking one of its own (ADR 380):
# the rounds are one continuous ownership of an issue that is still being decided, transferred
# record-to-record down the chain, so both stages genuinely are the same lane at different
# moments and either one's live record legitimately shields the shared claim.
CLAIM_LANE = {"intake": "triaging", "build": "building", "review": "building",
              "revise": "building", "mockup": "drawing", "respond": "building",
              "research": "resolving", "attack": "triaging"}


def owned_issues(records, repo: str, lane: str | None = None) -> set[int]:
    """The issue numbers in ``repo`` that a coordinator record still owns — anything not retired
    that still holds its GitHub claim, whether it is running, waiting, or completed and awaiting
    the next stage's transfer. Legacy claim reclamation must skip these: an ``agentflow:building``
    claim can legitimately outlive its provider process now (ADR 0028).

    ``lane`` scopes ownership to one claim type (``"building"``, ``"triaging"``, or ``"drawing"``):
    Intake owns ``triaging``, Mockup owns ``drawing``, and code-change stages own ``building``.
    Reclamation and forward activation pass the lane they are reconciling so one claim type's live
    record can never hide the other type's stale claim. ``lane=None`` returns every owned issue
    (any claim type)."""
    owned: set[int] = set()
    for record in records:
        if record.repo != repo or record.retired or not record.claim:
            continue
        if lane is not None and CLAIM_LANE.get(record.stage) != lane:
            continue
        number = _issue_number(record)
        if number is not None:
            owned.add(number)
    return owned


# Code-writing stages retain the legacy board lane that names their operator-facing activity.
# The attack reports on the existing triage lane — the rounds are triage still arguing with
# itself, and they add no operator-visible surface of their own (ADR 380).
_STAGE_LANE = {"intake": "triaging", "build": "building", "review": "reviewing",
               "revise": "building", "mockup": "triaging", "respond": "building",
               "research": "resolving", "attack": "triaging"}


def live_projection(records) -> list[dict]:
    """Render the running coordinated records as live-session board entries (ADR 0030: the live
    board is a projection of running records, not an ownership source). One entry per running
    coordinated record, keyed by its worktree; waiting records reserve nothing
    and are omitted."""
    entries: list[dict] = []
    for record in records:
        if record.state != RUNNING or record.stage not in _STAGE_LANE:
            continue
        number = _issue_number(record)
        ref = WorktreeRef.parse(record.source)
        branch = ref.branch if ref else None
        started_at = (datetime.fromtimestamp(record.started_at, timezone.utc).isoformat()
                      if record.started_at else "")
        entries.append({
            "repo": record.repo,
            "number": number if number is not None else record.subject,
            "title": "",
            "stage": _STAGE_LANE[record.stage],
            "tool": record.pool,
            "model": record.model,
            "branch": branch,
            "worktree": record.source or "",
            "pid": int(record.family) if record.family and record.family.isdigit() else None,
            "started_at": started_at,
        })
    return entries


def refusal_projection(records) -> list[dict]:
    """Render every record that is currently being refused as a board row (#405).

    Deliberately *not* part of :func:`live_projection`: a refused record reserves nothing and is
    not running, so folding it into the running rows would inflate every pool's running count for
    work that has not started. One row per record whose latest cycle found something refusing it —
    the preparation check that said no, or the pool capacity that did — carrying ``expected`` so
    ordinary contention reads differently from a stage nobody can un-stick."""
    return [
        {
            "repo": record.repo,
            "subject": record.subject,
            "stage": record.stage,
            "pool": record.pool,
            "refusal": record.refusal,
            "expected": bool(record.refusal_expected),
        }
        for record in records
        if record.refusal and not record.retired
    ]


def stalled_projection(records, *, now: int) -> list[dict]:
    """Render every record stuck long enough on a refusal only a human can clear (#406).

    A third key of its own, beside the refusals and well away from the running rows. A stalled
    record has started nothing and reserves nothing, so putting it anywhere near ``running``
    would report an active session that does not exist and inflate its pool's count; and it is
    not merely *being refused*, which is the ordinary condition half the fleet is in at any
    moment. What separates it is elapsed time on a refusal that has been proved human-clearable,
    so each row carries when the clock started and lets whoever reads it do that arithmetic.
    """
    return [
        {
            "repo": record.repo,
            "subject": record.subject,
            "stage": record.stage,
            "pool": record.pool,
            "refusal_id": record.stall_refusal_id,
            "refusal": record.refusal,
            "stall_started_at": record.stall_started_at,
        }
        for record in records
        if not record.retired and stalled_for(record, now) >= STALL_STALLED_AFTER
    ]


def load_records(store_path=None) -> list[Record]:
    """The durable continuation records — the production reader behind the pure functions
    above. Reads the coordinator's private store directly; a fail-closed store surfaces its
    :class:`StoreUnavailable` rather than reporting a falsely empty, all-clear world."""
    store = Store(store_path or default_store_path())
    try:
        return list(store.load().values())
    finally:
        store.close()
