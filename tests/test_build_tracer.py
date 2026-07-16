"""Build behind the session coordinator (issue #103), driven through the public
``submit_stage`` / ``cycle`` seam. The Build stage adapter's outcome-first PR verification,
worktree/lineage reuse on continuation, the build-only admission gate, the live-session
projection, the coordinator-owned claim guard, and the ADR 0028 log shapes are all asserted at
the coordinator interface — never by poking private transitions.
"""

from __future__ import annotations

from conftest import FakeSession, permits, record_of

from agentflow.coordinator import BuildStageAdapter, Submission
from agentflow.coordinator import tracer
from agentflow.coordinator.providers import ProviderCause


def _build(subject="7", *, pool="claude", source="/wt/issue-7", effort="high"):
    return Submission(repo="o/r", subject=subject, stage="build", pool=pool,
                      complexity="deep", effort=effort, source=source)


def _adapter(fake, *, pr, prep):
    """A Build adapter wired to test flags: ``pr``/``prep`` are single-element lists so a test
    flips PR existence and worktree readiness mid-flight; the fake plays observer + launcher."""
    return BuildStageAdapter(pr_exists=lambda r: pr[0],
                             worktree_ready=lambda r: prep[0], observer=fake)


def _records(coord):
    return list(coord._store.load().values())


# --- outcome-first PR verification -------------------------------------------------------

def test_build_completes_when_pr_exists_even_after_a_bad_exit(make_coord):
    fake = FakeSession()
    pr, prep = [True], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    assert coord.cycle("claude") == []            # attempt running
    fake.end(ident, cause=ProviderCause.PROCESS)  # provider exited badly (non-zero)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]  # PR present → done


def test_clean_exit_without_the_pr_stays_incomplete(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)     # clean exit, but no PR
    assert coord.cycle("claude") == []            # not completed — bounded continuation instead
    rec = record_of(coord, ident)
    assert rec.state != "completed" and rec.continuation and rec.attempts == 2


# --- interruption keeps the worktree, lineage, branch, and claim -------------------------

def test_interrupted_build_continues_in_a_fresh_session_keeping_ownership(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)  # interrupted after local changes

    coord.cycle("claude")  # reconciles to waiting, then continues in a fresh session same cycle
    rec = record_of(coord, ident)
    assert rec.attempts == 2                       # a second attempt was consumed
    assert rec.source == "/wt/issue-7"             # same retained worktree
    assert rec.lineage == "claude"                 # pinned tool lineage held
    assert rec.claim is True                        # claim retained across the interruption
    assert rec.state == "running"

    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]


# --- preparation happens before admission ------------------------------------------------

def test_preparation_failure_consumes_no_permit_or_attempt(make_coord):
    fake = FakeSession()
    pr, prep = [False], [False]                    # worktree not ready yet
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0           # nothing reserved
    assert record_of(coord, ident).attempts == 0   # no attempt consumed
    assert record_of(coord, ident).state == "waiting"

    prep[0] = True                                  # worktree recovered → admits normally
    coord.cycle("claude")
    assert permits(coord, "claude") == 5
    assert record_of(coord, ident).attempts == 1


# --- exhaustion ---------------------------------------------------------------------------

def test_exhaustion_holds_once_with_one_handoff_and_notification(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    outcome = None
    for _ in range(6):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held"
    assert outcome.handoff == "issue:needs-grilling"
    rec = record_of(coord, ident)
    assert rec.attempts == 3                        # initial + two continuations, no more
    assert rec.handoffs == 1 and rec.notifications == 1
    assert rec.source == "/wt/issue-7"              # worktree left untouched for human re-entry


# --- idempotent submission ---------------------------------------------------------------

def test_repeated_submission_and_restart_make_one_record(make_coord):
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter)
    first = coord.submit_stage(_build())
    again = coord.submit_stage(_build())
    restarted = make_coord(fake, adapter=adapter).submit_stage(_build())
    assert first == again == restarted
    assert len(_records(coord)) == 1


# --- build is the only enabled stage -----------------------------------------------------

def test_only_build_admits_other_stages_stay_waiting(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_only_gate)
    build = coord.submit_stage(_build())
    review = coord.submit_stage(Submission(repo="o/r", subject="7", stage="review",
                                           pool="claude"))
    coord.cycle("claude")
    assert record_of(coord, build).state == "running"
    review_rec = record_of(coord, review)
    assert review_rec.state == "waiting"           # visibly queued
    assert review_rec.attempts == 0                # consumed no attempt
    assert permits(coord, "claude") == 5           # only the build's demand is reserved


# --- live projection & claim ownership ---------------------------------------------------

def test_running_build_projects_to_live_board_waiting_does_not(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_only_gate)
    coord.submit_stage(_build("7"))
    coord.submit_stage(Submission(repo="o/r", subject="8", stage="review", pool="claude"))
    coord.cycle("claude")
    projection = tracer.live_projection(_records(coord))
    assert [e["number"] for e in projection] == [7]     # only the running build
    assert projection[0]["stage"] == "building" and projection[0]["tool"] == "claude"


def test_owned_issues_and_active_track_coordinator_ownership(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build("7"))
    coord.cycle("claude")
    # A running build owns its claim and is in flight.
    assert tracer.owned_issues(_records(coord), "o/r") == {7}
    assert tracer.coordinator_active(_records(coord)) is True
    assert tracer.owned_issues(_records(coord), "other/repo") == set()
    # Complete it: the PR is a durable boundary, so it no longer holds a rollback drain open,
    # but it still owns its claim until a next stage transfers it.
    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert tracer.coordinator_active(_records(coord)) is False
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


# --- ADR 0028 log shapes -----------------------------------------------------------------

def test_attempt_interrupt_continuation_and_completion_log_shapes(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep), log=lines.append)
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    assert "o/r: 7: build: attempt 1/3 → claude" in lines
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert ("o/r: 7: build: attempt 1/3 interrupted (process) — continuation 1/2 eligible "
            "next cycle; claim retained") in lines
    assert "o/r: 7: build: continuation 1/2 (attempt 2/3) → claude" in lines
    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert "o/r: 7: build: attempt 2/3 completed — pr opened; claim retained" in lines


def test_exhaustion_log_shape(make_coord):
    fake = FakeSession()
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]), log=lines.append)
    ident = coord.submit_stage(_build())
    for _ in range(6):
        if coord.cycle("claude"):
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert ("o/r: 7: build: attempt 3/3 interrupted (process) — continuation budget "
            "exhausted; held for human; claim released") in lines


def test_recovered_running_log_shape_after_restart(make_coord):
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter)
    coord.submit_stage(_build())
    coord.cycle("claude")                          # attempt running, family still alive
    # A fresh coordinator over the same store is the restart; the family is still alive.
    lines: list[str] = []
    restarted = make_coord(fake, adapter=adapter, log=lines.append)
    restarted.cycle("claude")
    assert any(line.startswith("o/r: 7: build: recovered running attempt 1/3 pid ")
               and "observing until" in line and "claim retained" in line for line in lines)


def test_claim_transfer_log_shape(make_coord):
    fake = FakeSession()
    pr, prep = [True], [True]
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep), log=lines.append)
    build = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")                          # build completes
    # The next stage assumes the claim — the transfer line names the completed stage and target.
    coord.submit_stage(Submission(repo="o/r", subject="7", stage="review", pool="claude",
                                  transfer_from=build))
    assert "o/r: 7: build: attempt 1/3 completed — pr opened; claim transferred to review" in lines
