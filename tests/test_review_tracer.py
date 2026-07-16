"""Review as the second coordinated stage (issue #104), driven through the public
``submit_stage`` / ``cycle`` seam. Review binds to the exact PR head SHA, recreates its read-only
checkout on continuation, completes only on a durable verdict for that SHA, may move pools when
review safety allows it, and parks the PR on exhaustion — all asserted at the coordinator
interface, never by poking private transitions. The Build → Review claim transfer and its crash
boundaries are exercised here too, through a stage router that runs both live stages behind one
coordinator.
"""

from __future__ import annotations

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_build
from agentflow.coordinator import (BuildStageAdapter, ReviewStageAdapter, StageRouter, Submission,
                                    tracer)
from agentflow.coordinator.providers import ProviderCause, ProviderObservation
from agentflow.coordinator.record import Record


def _review(subject="7", *, pool="claude", target="sha-a", builder_lineage="codex",
            source="/wt/pr-7-x", transfer_from=None):
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool, complexity="deep",
                      target=target, source=source, builder_lineage=builder_lineage,
                      transfer_from=transfer_from)


def _review_adapter(fake, *, verdict, prep, handoff=None):
    """A Review adapter wired to test flags: ``verdict``/``prep`` are single-element lists so a
    test flips verdict durability and checkout readiness mid-flight; the fake plays observer."""
    return ReviewStageAdapter(verdict_ready=lambda r, o: verdict[0],
                              worktree_reset=lambda r: prep[0], observer=fake, handoff=handoff)


def _router(fake, *, pr, verdict, prep):
    """One coordinator owning both live stages, so the Build → Review transition is exercised
    end to end (ADR 0030)."""
    build = BuildStageAdapter(pr_exists=lambda r: pr[0], worktree_ready=lambda r: True,
                              observer=fake)
    review = _review_adapter(fake, verdict=verdict, prep=prep)
    return StageRouter({"build": build, "review": review})


def _records(coord):
    return list(coord._store.load().values())


# --- outcome-first verdict verification --------------------------------------------------

def test_review_completes_when_the_verdict_is_durable_even_after_a_bad_exit(make_coord):
    fake = FakeSession()
    verdict, prep = [True], [True]
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=verdict, prep=prep))
    ident = coord.submit_stage(_review())
    assert coord.cycle("claude") == []              # attempt running
    fake.end(ident, cause=ProviderCause.PROCESS)    # reviewer exited badly (non-zero)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]  # verdict present → done


def test_clean_exit_without_a_verdict_stays_incomplete(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    ident = coord.submit_stage(_review())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)       # clean exit, but no verdict
    assert coord.cycle("claude") == []              # not completed — bounded continuation instead
    rec = record_of(coord, ident)
    assert rec.state != "completed" and rec.continuation and rec.attempts == 2


def test_a_verdict_for_another_sha_does_not_complete_review():
    """The verdict must name the exact reviewed head SHA (ADR 0028). The production verifier is
    pure over the reviewer's captured final message and the record's immutable target."""
    record = Record(identity="o/r|7|review|sha-a", stage="review", pool="claude", demand=1,
                    repo="o/r", subject="7", target="sha-a")
    match = ProviderObservation(
        final_message='{"verdict": "PASS", "reviewed_sha": "sha-a", "findings": []}')
    other = ProviderObservation(
        final_message='{"verdict": "PASS", "reviewed_sha": "sha-b", "findings": []}')
    none = ProviderObservation(final_message="I looked but wrote no verdict object.")
    assert coordinated_build._verdict_ready(record, match) is True
    assert coordinated_build._verdict_ready(record, other) is False   # a different head SHA
    assert coordinated_build._verdict_ready(record, none) is False    # no verdict at all


# --- the exact PR head SHA is the review's identity --------------------------------------

def test_same_head_sha_is_one_review_a_new_head_sha_is_a_fresh_stage(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    first = coord.submit_stage(_review(target="sha-a"))
    again = coord.submit_stage(_review(target="sha-a"))
    assert first == again                            # same head SHA → the same review record
    coord.cycle("claude")
    assert record_of(coord, first).attempts == 1     # one running attempt so far

    newer = coord.submit_stage(_review(target="sha-b"))
    assert newer != first                            # a different head SHA → a new stage
    assert record_of(coord, newer).attempts == 0     # with a fresh, unused budget


# --- read-only checkout is recreated before admission ------------------------------------

def test_checkout_recreation_failure_consumes_no_permit_or_attempt_and_keeps_the_target(
        make_coord):
    fake = FakeSession()
    verdict, prep = [False], [False]                 # read-only checkout not ready yet
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=verdict, prep=prep))
    ident = coord.submit_stage(_review(target="sha-a"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0             # nothing reserved
    rec = record_of(coord, ident)
    assert rec.attempts == 0 and rec.state == "waiting"
    assert rec.target == "sha-a"                      # the durable target is never lost

    prep[0] = True                                    # checkout recreated → admits normally
    coord.cycle("claude")
    assert permits(coord, "claude") == 1
    assert record_of(coord, ident).attempts == 1


# --- a review continuation may move pools ------------------------------------------------

def test_review_continuation_moves_to_an_available_pool_and_cannot_same_tool_auto_merge(
        make_coord):
    """A read-only review whose home pool cannot fit it moves to the available pool, recomputing
    its admission demand there; landing on the builder's own tool strips auto-merge (ADR 0028)."""
    fake = FakeSession()
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    # A cross-tool review of a claude-built PR, assigned to codex (demand 2). Cross-tool, so it
    # may auto-merge for now.
    r1 = coord.submit_stage(_review("1", pool="codex", builder_lineage="claude"))
    coord.cycle("codex", now=0)
    assert record_of(coord, r1).auto_merge_allowed is True
    fake.end(r1, cause=ProviderCause.CAPACITY, reset_at=100)   # codex paused this attempt
    coord.cycle("codex", now=0)                                # freed; not eligible until 100

    # Meanwhile codex fills with two other reviews (2 + 2), leaving no room for r1's demand-2.
    coord.submit_stage(_review("2", pool="codex", builder_lineage="claude"))
    coord.submit_stage(_review("3", pool="codex", builder_lineage="claude"))
    coord.cycle("codex", now=0)
    assert permits(coord, "codex") == 4

    # r1 becomes eligible but codex is full, so it moves to claude — recomputing demand (1) and,
    # because claude is the builder's tool, it can no longer auto-merge.
    coord.cycle("claude", now=100)
    moved = record_of(coord, r1)
    assert moved.pool == "claude" and moved.state == "running"
    assert moved.demand == 1                          # recomputed for the destination pool
    assert moved.auto_merge_allowed is False          # same-tool review cannot auto-merge
    assert permits(coord, "claude") == 1


def test_a_code_writing_continuation_never_migrates(make_coord):
    """Only a read-only review moves pools; a code-writing stage stays on its builder lineage even
    when its pool is full (ADR 0028). A revise pinned to codex is never offered to claude."""
    fake = FakeSession()
    coord = make_coord(fake)
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise", pool="codex",
                                           builder_lineage="codex", complexity="deep"))
    coord.cycle("codex")
    fake.end(revise, cause=ProviderCause.PROCESS)     # interrupted → continuation on codex
    coord.cycle("claude", now=0)                       # claude cycle must not adopt it
    assert record_of(coord, revise).pool == "codex"
    assert permits(coord, "claude") == 0


# --- exhaustion parks the PR once --------------------------------------------------------

def test_exhaustion_parks_the_pr_once_with_one_handoff_and_notification(make_coord):
    fake = FakeSession()
    handoffs = []
    adapter = _review_adapter(
        fake, verdict=[False], prep=[True],
        handoff=lambda record: handoffs.append(record.identity) or "pr-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review())
    outcome = None
    for _ in range(6):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        # a restarted/waiting review keeps its claim while it still has budget
        assert record_of(coord, ident).claim is True
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held"
    assert outcome.handoff == "pr:parked"
    rec = record_of(coord, ident)
    assert rec.attempts == 3 and rec.handoffs == 1 and rec.notifications == 1
    assert rec.claim is False                          # claim released only at the park boundary
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]                          # restart cannot repeat the external handoff


# --- build → review claim transfer with no ownership gap ---------------------------------

def test_completed_build_opens_review_and_transfers_the_claim_before_retiring(make_coord):
    fake = FakeSession()
    pr, verdict, prep = [True], [False], [True]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, prep=prep),
                       gate=tracer.build_and_review_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    assert [o.stage for o in coord.cycle("claude")] == ["build"]  # build completed
    assert record_of(coord, build).claim is True and record_of(coord, build).retired is False

    # The completed Build opens the review for the exact head SHA and hands off the claim.
    review = coord.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                        target="head-sha", transfer_from=build))
    assert record_of(coord, build).claim is False and record_of(coord, build).retired is True
    assert record_of(coord, review).claim is True
    # Exactly one record owns the issue's claim at every point — no gap, no double-claim.
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


def test_daemon_death_between_stages_keeps_ownership_and_transfers_once(make_coord):
    """Fault injection (ADR 0028): a daemon death before the claim transfer must leave the
    completed Build owning the claim (no gap); a death after the verdict is durable but before
    retirement must not duplicate the review or lose ownership."""
    fake = FakeSession()
    pr, verdict, prep = [True], [False], [True]
    adapter = _router(fake, pr=pr, verdict=verdict, prep=prep)
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_and_review_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")                              # build completes, claim retained

    # Death before the transfer: a fresh coordinator still sees the Build owning its claim.
    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_and_review_gate)
    assert record_of(restarted, build).claim is True and record_of(restarted, build).retired is False
    review = restarted.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                            target="head-sha", transfer_from=build))
    assert record_of(restarted, build).retired is True

    # The review runs and its verdict becomes durable; then the daemon dies before retirement.
    restarted.cycle("codex")
    verdict[0] = True
    fake.end(review, cause=ProviderCause.PROCESS)
    again = make_coord(fake, adapter=adapter, gate=tracer.build_and_review_gate)
    assert [o.status for o in again.cycle("codex")] == ["completed"]   # finalized exactly once
    assert record_of(again, review).claim is True                     # kept until the next stage
    assert record_of(again, review).attempts == 1                     # attempt not double-counted
    assert again.cycle("codex") == []                                 # no duplicate review work


# --- build and review are the only enabled stages ----------------------------------------

def test_only_build_and_review_admit_other_stages_stay_waiting(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_router(fake, pr=[False], verdict=[False], prep=[True]),
                       gate=tracer.build_and_review_gate)
    # A low-effort build (4 permits) leaves room for the review (1) — both admit; revise is
    # gate-refused and never reserves, so it sits visibly waiting.
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="low", source="/wt/issue-7"))
    review = coord.submit_stage(_review("8", pool="claude", builder_lineage="codex"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise", pool="claude",
                                           builder_lineage="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, build).state == "running"
    assert record_of(coord, review).state == "running"
    revise_rec = record_of(coord, revise)
    assert revise_rec.state == "waiting" and revise_rec.attempts == 0   # visibly queued, dormant


# --- pure Build → Review submission mapping ----------------------------------------------

def test_review_submission_binds_to_the_head_sha_and_assumes_the_build_claim():
    build = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5, repo="o/r",
                   subject="7", source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing")
    sub = coordinated_build.review_submission(build, "head-sha-123", "codex", 42)
    assert sub is not None
    assert sub.stage == "review" and sub.target == "head-sha-123"
    assert sub.pool == "codex" and sub.builder_lineage == "claude"     # cross-tool reviewer
    assert sub.transfer_from == "o/r|7|build|-"                        # assumes the build's claim
    assert sub.complexity == "deep"                                   # review is the deep net
    assert "pr-42-fix-thing" in sub.source                            # read-only review worktree
    # A build whose worktree is unreadable, or a missing head SHA, yields no submission.
    assert coordinated_build.review_submission(build, "", "codex", 42) is None
    assert coordinated_build.review_submission(
        Record(identity="x", stage="build", pool="claude", demand=5, repo="o/r", subject="7"),
        "sha", "codex", 42) is None
