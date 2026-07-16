"""The tracer bridge (issues #103–#108) — the small set of reads and gates that connect the
session coordinator to dispatch while all six logical stages are coordinated.

Three things the dispatch layer needs when these stages run behind the coordinator, all derived
from the durable continuation records so there is no second source of truth:

- **All six logical stages are enabled.**
  :func:`build_review_revise_gate` is the coordinator's admission gate in coordinated mode: every
  listed logical stage admits; unknown stages may be submitted and sit visibly ``waiting`` but
  consume neither a permit nor an attempt. :func:`build_and_review_gate` and
  :func:`build_only_gate` are retained for the earlier issue #104 and #103 slices.
- **The live board becomes a projection of running records.** :func:`live_projection` renders
  the running Build and Review records as live-session entries; waiting records reserve nothing
  and do not appear, exactly matching the reviewed admission demand they hold.
- **Coordinator ownership is authoritative for claims.** :func:`owned_issues` names the issues a
  coordinator record still owns, so legacy claim reclamation can never strip one; and
  :func:`coordinator_active` reports whether any record still owns in-flight work, which gates a
  rollback drain from resuming legacy launching.

Every function is pure over an iterable of records, so the behaviors are exercised without a
daemon, GitHub, or a live store; :func:`load_records` is the thin production reader.
"""

from __future__ import annotations

from datetime import datetime, timezone

from agentflow.coordinator.record import COMPLETED, RUNNING, WAITING, Record
from agentflow.coordinator.store import Store, default_store_path


# The six logical stages enabled behind the coordinator (issues #103–#108) — the one source
# :func:`build_review_revise_gate` consumes.
ENABLED_STAGES = ("intake", "build", "review", "revise", "mockup", "respond")


def build_only_gate(record: Record) -> bool:
    """Legacy gate kept for the issue #103 slice: admit Build alone. Retained so the Build-only
    behaviour stays exercised; the live coordinator uses :func:`build_review_revise_gate`."""
    return record.stage == "build"


def build_and_review_gate(record: Record) -> bool:
    """Gate kept for the issue #104 slice: admit Build and Review alone. Retained so that
    Build+Review behaviour stays exercised; the live coordinator uses :func:`build_review_revise_gate`."""
    return record.stage in ("build", "review")


def build_review_revise_gate(record: Record) -> bool:
    """The coordinated-mode admission gate: admit exactly :data:`ENABLED_STAGES`, refuse every
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
CLAIM_LANE = {"intake": "triaging", "build": "building", "review": "building",
              "revise": "building", "mockup": "drawing", "respond": "building"}


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


def coordinator_active(records) -> bool:
    """Whether any coordinator record still owns in-flight work — a waiting continuation/queued
    stage or a running provider. A completed record at its durable PR boundary and a held record
    at its human handoff are not in-flight, so they do not hold a rollback drain open (issue
    #103): rollback keeps reconciling until every record reaches such a boundary, then legacy
    launching may resume."""
    return any(
        not r.retired and (
            r.state in (WAITING, RUNNING)
            or (r.stage == "intake" and r.state == COMPLETED)
        )
        for r in records
    )


# Code-writing stages retain the legacy board lane that names their operator-facing activity.
_STAGE_LANE = {"intake": "triaging", "build": "building", "review": "reviewing",
               "revise": "building", "mockup": "triaging", "respond": "building"}


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
        branch = None
        if record.source and "/.agentflow/worktrees/" in record.source:
            branch = "agentflow/" + record.source.split("/.agentflow/worktrees/", 1)[1]
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


def load_records(store_path=None) -> list[Record]:
    """The durable continuation records — the production reader behind the pure functions
    above. Reads the coordinator's private store directly; a fail-closed store surfaces its
    :class:`StoreUnavailable` rather than reporting a falsely empty, all-clear world."""
    store = Store(store_path or default_store_path())
    try:
        return list(store.load().values())
    finally:
        store.close()
