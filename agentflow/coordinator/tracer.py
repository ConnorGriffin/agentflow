"""The tracer bridge (issues #103, #104, #105) — the small set of reads and gates that connect the
session coordinator to the legacy dispatch surfaces while Build, Review, and Revise are the
coordinated stages.

Three things the dispatch layer needs when Build, Review, and Revise run behind the coordinator,
all derived from the durable continuation records so there is no second source of truth:

- **Build, Review, and Revise are the only enabled logical stages.** :func:`build_review_revise_gate`
  is the coordinator's admission gate in coordinated mode: every other logical stage may be
  submitted and sit visibly ``waiting``, but it never admits, so it consumes neither a permit nor an
  attempt. :func:`build_and_review_gate` and :func:`build_only_gate` are the retained gates for the
  earlier issue #104 and #103 slices.
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

from agentflow.coordinator.record import RUNNING, WAITING, Record
from agentflow.coordinator.store import Store, default_store_path


# The logical stages enabled behind the coordinator today (issues #103, #104, #105). Every other
# stage may be submitted and sit visibly ``waiting``, but the gate never admits it, so it
# consumes neither a permit nor an attempt.
ENABLED_STAGES = ("building", "reviewing", "revising")


def build_only_gate(record: Record) -> bool:
    """Legacy gate kept for the issue #103 slice: admit Build alone. Retained so the Build-only
    behaviour stays exercised; the live coordinator uses :func:`build_review_revise_gate`."""
    return record.stage == "build"


def build_and_review_gate(record: Record) -> bool:
    """Gate kept for the issue #104 slice: admit Build and Review alone. Retained so that
    Build+Review behaviour stays exercised; the live coordinator uses :func:`build_review_revise_gate`."""
    return record.stage in ("build", "review")


def build_review_revise_gate(record: Record) -> bool:
    """The coordinated-mode admission gate: admit Build, Review, and Revise, refuse every other
    logical stage. A refused stage stays ``waiting`` and reserves nothing — no permit, no attempt —
    so Intake, Respond, and Mockup remain visibly queued until their own slices land."""
    return record.stage in ("build", "review", "revise")


def _issue_number(record: Record) -> int | None:
    try:
        return int(record.subject)
    except (TypeError, ValueError):
        return None


def owned_issues(records, repo: str) -> set[int]:
    """The issue numbers in ``repo`` that a coordinator record still owns — anything not retired
    that still holds its GitHub claim, whether it is running, waiting, or completed and awaiting
    the next stage's transfer. Legacy claim reclamation must skip these: an ``agentflow:building``
    claim can legitimately outlive its provider process now (ADR 0028)."""
    owned: set[int] = set()
    for record in records:
        if record.repo != repo or record.retired or not record.claim:
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
    return any(not r.retired and r.state in (WAITING, RUNNING) for r in records)


# Revise shares Build's board lane: it works on the same PR branch/worktree, so it reads as a
# builder session (exactly as the legacy revise pass does) and is replaced with the Build lane.
_STAGE_LANE = {"build": "building", "review": "reviewing", "revise": "building"}


def live_projection(records) -> list[dict]:
    """Render the running coordinated records as live-session board entries (ADR 0030: the live
    board is a projection of running records, not an ownership source). One entry per running
    Build, Review, or Revise record, keyed by its worktree; waiting records reserve nothing and are
    omitted."""
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
