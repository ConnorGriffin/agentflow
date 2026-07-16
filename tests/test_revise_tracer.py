"""Revise as the third coordinated stage (issue #105), driven through the public
``submit_stage`` / ``cycle`` seam. A blocking Review hands its change claim to one waiting Revise;
Revise adopts the builder's retained PR branch/worktree, stays pinned to the builder's tool lineage
and complexity, keeps its local work across an interrupted continuation, completes only on a
verified pushed revision (independent of provider exit), parks the PR on exhaustion without
discarding that work, and a completed Revise opens one new Review bound to the changed head SHA with
a fresh budget. The Build → Review → Revise → Review claim transfers and their crash boundaries are
exercised here through a stage router that runs all three live stages behind one coordinator.
"""

from __future__ import annotations

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_build
from agentflow.coordinator import (BuildStageAdapter, ReviewStageAdapter, ReviseStageAdapter,
                                    StageRouter, Submission, tracer)
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import Record
from agentflow.gate import MAX_REVISES

BUILD_WT = "/w/.agentflow/worktrees/claude/issue-7-x"
REVIEW_WT = "/w/.agentflow/worktrees/codex-review/pr-42-x"


def _revise_sub(subject="9", *, pool="claude", target="sha-a", source=None):
    return Submission(repo="o/r", subject=subject, stage="revise", pool=pool, complexity="deep",
                      target=target, builder_lineage=pool,
                      source=source or f"/w/.agentflow/worktrees/{pool}/issue-{subject}-x")


def _revise_adapter(fake, *, revision, prep, handoff=None):
    """A Revise adapter wired to test flags: ``revision``/``prep`` are single-element lists so a
    test flips pushed-revision durability and retained-worktree readiness mid-flight; the fake plays
    observer."""
    return ReviseStageAdapter(revision_ready=lambda r, o: revision[0],
                              worktree_ready=lambda r: prep[0], observer=fake, handoff=handoff)


def _router(fake, *, pr, verdict, revision, prep=None):
    """One coordinator owning all three live stages, so the Build → Review → Revise → Review path is
    exercised end to end (ADR 0030)."""
    prep = prep or [True]
    build = BuildStageAdapter(pr_exists=lambda r: pr[0], worktree_ready=lambda r: True, observer=fake)
    review = ReviewStageAdapter(verdict_ready=lambda r, o: verdict[0],
                                worktree_reset=lambda r: True, observer=fake)
    revise = ReviseStageAdapter(revision_ready=lambda r, o: revision[0],
                                worktree_ready=lambda r: prep[0], observer=fake)
    return StageRouter({"build": build, "review": review, "revise": revise})


def _records(coord):
    return list(coord._store.load().values())


# --- the full Build → Review → Revise → Review path, one claim at every hop ---------------

def test_full_path_transfers_the_claim_at_each_hop_and_keeps_lineage(make_coord):
    fake = FakeSession()
    pr, verdict, revision = [True], [False], [False]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, revision=revision),
                       gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source=BUILD_WT))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    assert [o.stage for o in coord.cycle("claude")] == ["build"]

    # Build → Review, bound to the reviewed head SHA, claim transferred before Build retires.
    review = coord.submit_stage(Submission(repo="o/r", subject="7", stage="review", pool="codex",
                                           complexity="deep", target="sha-a", source=REVIEW_WT,
                                           builder_lineage="claude", transfer_from=build))
    assert record_of(coord, build).retired is True
    assert tracer.owned_issues(_records(coord), "o/r") == {7}
    coord.cycle("codex")
    verdict[0] = True                                   # a blocking verdict became durable
    fake.end(review, cause=ProviderCause.PROCESS)
    assert [o.stage for o in coord.cycle("codex")] == ["review"]

    # Review → Revise, pinned to the builder's tool lineage and its retained PR branch/worktree.
    revise = coord.submit_stage(Submission(repo="o/r", subject="7", stage="revise", pool="claude",
                                           complexity="deep", target="sha-a", source=BUILD_WT,
                                           builder_lineage="claude", transfer_from=review))
    assert record_of(coord, review).retired is True
    rev = record_of(coord, revise)
    assert rev.claim is True and rev.pool == "claude" and rev.lineage == "claude"
    assert rev.source == BUILD_WT                        # adopts the builder's retained worktree
    assert tracer.owned_issues(_records(coord), "o/r") == {7}
    coord.cycle("claude")
    revision[0] = True                                  # the revision was pushed to the same branch
    fake.end(revise, cause=ProviderCause.PROCESS)
    assert [o.stage for o in coord.cycle("claude")] == ["revise"]

    # Revise → a new Review for the changed head SHA, with a fresh budget; the prior review is gone.
    review2 = coord.submit_stage(Submission(repo="o/r", subject="7", stage="review", pool="codex",
                                            complexity="deep", target="sha-b", source=REVIEW_WT,
                                            builder_lineage="claude", transfer_from=revise))
    assert review2 != review                             # a new head SHA is a genuinely new review
    assert record_of(coord, revise).retired is True
    r2 = record_of(coord, review2)
    assert r2.attempts == 0 and r2.claim is True         # fresh budget; prior SHA's review not reused
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


# --- outcome-first: a pushed revision completes; local-only changes do not ----------------

def test_revise_completes_only_on_a_pushed_revision_even_after_a_bad_exit(make_coord):
    fake = FakeSession()
    revision, prep = [False], [True]
    coord = make_coord(fake, adapter=_revise_adapter(fake, revision=revision, prep=prep))
    ident = coord.submit_stage(_revise_sub())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)         # clean or not, only local changes so far
    assert coord.cycle("claude") == []                   # not completed — bounded continuation
    assert record_of(coord, ident).continuation and record_of(coord, ident).attempts == 2

    revision[0] = True                                   # the branch now carries the pushed revision
    fake.end(ident, cause=ProviderCause.PROCESS)         # a bad exit cannot undo a durable outcome
    assert [o.status for o in coord.cycle("claude")] == ["completed"]


# --- retained worktree reuse: a miss costs nothing; an interrupt keeps local work ---------

def test_worktree_miss_consumes_no_permit_or_attempt_and_keeps_local_work(make_coord):
    fake = FakeSession()
    revision, prep = [False], [False]                    # the retained worktree is not ready yet
    coord = make_coord(fake, adapter=_revise_adapter(fake, revision=revision, prep=prep))
    ident = coord.submit_stage(_revise_sub(source=BUILD_WT))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0                 # nothing reserved
    rec = record_of(coord, ident)
    assert rec.attempts == 0 and rec.state == "waiting"
    assert rec.source == BUILD_WT                         # the retained worktree is untouched

    prep[0] = True
    coord.cycle("claude")
    assert permits(coord, "claude") == 3                 # revise (claude, deep) reserves three
    assert record_of(coord, ident).attempts == 1


def test_interrupted_revise_continues_on_the_same_retained_worktree(make_coord):
    fake = FakeSession()
    revision = [False]
    coord = make_coord(fake, adapter=_revise_adapter(fake, revision=revision, prep=[True]))
    ident = coord.submit_stage(_revise_sub(source=BUILD_WT))
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)         # interrupted with only local changes
    coord.cycle("claude")                                # continues on the same worktree
    rec = record_of(coord, ident)
    assert rec.continuation is True and rec.attempts == 2
    assert rec.source == BUILD_WT and rec.pool == "claude" and rec.lineage == "claude"


# --- Revise is pinned to the builder pool and never switches tools ------------------------

def test_revise_never_migrates_to_the_other_pool(make_coord):
    """A closed pool makes Revise wait, never switch tools — it is code-writing, pinned to its
    builder lineage (ADR 0028). Only a read-only review may move pools."""
    fake = FakeSession()
    coord = make_coord(fake)
    ident = coord.submit_stage(_revise_sub("9", pool="codex"))
    coord.cycle("codex")
    fake.end(ident, cause=ProviderCause.CAPACITY, reset_at=0)  # paused → continuation on codex
    coord.cycle("claude", now=0)                               # the claude cycle must not adopt it
    assert record_of(coord, ident).pool == "codex"
    assert permits(coord, "claude") == 0


# --- exhaustion parks the PR once, keeping the local work ---------------------------------

def test_exhaustion_parks_the_pr_once_and_does_not_discard_local_work(make_coord):
    fake = FakeSession()
    handoffs = []
    adapter = _revise_adapter(
        fake, revision=[False], prep=[True],
        handoff=lambda record: handoffs.append(record.identity) or "pr-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_revise_sub(source=BUILD_WT))
    outcome = None
    for _ in range(8):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        assert record_of(coord, ident).claim is True     # keeps its claim while budget remains
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held" and outcome.handoff == "pr:parked"
    rec = record_of(coord, ident)
    assert rec.attempts == 3 and rec.handoffs == 1 and rec.notifications == 1
    assert rec.claim is False                             # claim released only at the park boundary
    assert rec.source == BUILD_WT                         # local work is neither discarded nor forced
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]                            # a restart never repeats the external park


# --- crash boundaries at both transfer points ---------------------------------------------

def test_restart_at_transfer_points_keeps_one_claim_and_never_duplicates(make_coord):
    """Fault injection (ADR 0028): a daemon death at the Review→Revise and Revise→Review transfer
    points must never drop a claim, double-claim, duplicate a revision or a review, or hand out a
    free attempt."""
    fake = FakeSession()
    pr, verdict, revision = [True], [True], [False]
    adapter = _router(fake, pr=pr, verdict=verdict, revision=revision)
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source=BUILD_WT))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    review = coord.submit_stage(Submission(repo="o/r", subject="7", stage="review", pool="codex",
                                           complexity="deep", target="sha-a", source=REVIEW_WT,
                                           builder_lineage="claude", transfer_from=build))
    coord.cycle("codex")
    fake.end(review, cause=ProviderCause.PROCESS)         # verdict[0] already durable → completes
    coord.cycle("codex")

    # Death before the Review → Revise transfer: a fresh coordinator still sees Review owning it.
    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    assert record_of(restarted, review).claim is True and record_of(restarted, review).retired is False
    revise = restarted.submit_stage(Submission(repo="o/r", subject="7", stage="revise", pool="claude",
                                               complexity="deep", target="sha-a", source=BUILD_WT,
                                               builder_lineage="claude", transfer_from=review))
    assert record_of(restarted, review).retired is True
    assert tracer.owned_issues(_records(restarted), "o/r") == {7}

    # The revise runs and pushes; then the daemon dies before retirement.
    restarted.cycle("claude")
    revision[0] = True
    fake.end(revise, cause=ProviderCause.PROCESS)
    again = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    assert [o.status for o in again.cycle("claude")] == ["completed"]  # finalized exactly once
    assert record_of(again, revise).claim is True                     # kept until the next stage
    assert record_of(again, revise).attempts == 1                     # attempt not double-counted
    assert again.cycle("claude") == []                                # no duplicate revision work


# --- Build, Review, and Revise are the only enabled stages --------------------------------

def test_build_review_revise_admit_other_stages_stay_waiting(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_router(fake, pr=[False], verdict=[False], revision=[False]),
                       gate=tracer.build_review_revise_gate)
    revise = coord.submit_stage(_revise_sub("9", pool="claude"))
    respond = coord.submit_stage(Submission(repo="o/r", subject="10", stage="respond", pool="claude",
                                            builder_lineage="claude", complexity="deep"))
    mockup = coord.submit_stage(Submission(repo="o/r", subject="11", stage="mockup", pool="claude",
                                           complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, revise).state == "running"                # Revise now admits
    for later in (respond, mockup):
        rec = record_of(coord, later)
        assert rec.state == "waiting" and rec.attempts == 0           # still visibly queued, dormant


# --- pure mappings ------------------------------------------------------------------------

def test_revise_submission_adopts_the_builder_branch_and_assumes_the_review_claim():
    review = Record(identity="o/r|7|review|sha-a", stage="review", pool="codex", demand=2,
                    repo="o/r", subject="7", target="sha-a", builder_lineage="claude",
                    source="/home/w/.agentflow/worktrees/codex-review/pr-42-fix-thing")
    sub = coordinated_build.revise_submission(review, "deep", "- fix the thing")
    assert sub is not None
    assert sub.stage == "revise" and sub.target == "sha-a"            # revises away from reviewed SHA
    assert sub.pool == "claude" and sub.builder_lineage == "claude"   # the builder's tool lineage
    assert sub.complexity == "deep"                                   # the original builder complexity
    assert sub.transfer_from == "o/r|7|review|sha-a"                  # assumes the review's claim
    assert sub.source == "/home/w/.agentflow/worktrees/claude/issue-7-fix-thing"  # retained build wt
    # A review with no builder lineage, an unreadable source, or a missing SHA yields no submission.
    assert coordinated_build.revise_submission(
        Record(identity="x", stage="review", pool="codex", demand=2, repo="o/r", subject="7",
               target="sha-a", source="/nope"), "deep") is None
    assert coordinated_build.revise_submission(
        Record(identity="x", stage="review", pool="codex", demand=2, repo="o/r", subject="7",
               builder_lineage="claude",
               source="/home/w/.agentflow/worktrees/codex-review/pr-42-fix-thing"), "deep") is None


def test_review_submission_from_a_completed_revise_binds_the_new_head_and_keeps_lineage():
    revise = Record(identity="o/r|7|revise|sha-a", stage="revise", pool="claude", demand=3,
                    repo="o/r", subject="7", builder_lineage="claude", lineage="claude",
                    source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing")
    sub = coordinated_build.review_submission(revise, "sha-b-new", "codex", 42)
    assert sub is not None
    assert sub.stage == "review" and sub.target == "sha-b-new"       # bound to the changed head SHA
    assert sub.pool == "codex" and sub.builder_lineage == "claude"   # original builder still recorded
    assert sub.transfer_from == "o/r|7|revise|sha-a"                 # assumes the revise's claim
    assert "pr-42-fix-thing" in sub.source


def test_continuation_attempts_do_not_expand_the_auto_revise_round_policy():
    """The per-stage continuation budget is separate from the single auto-revise product round
    (ADR 0018): a logical Revise counts once no matter how many of its attempts it burned."""
    revises = [Record(identity=f"o/r|7|revise|sha-{i}", stage="revise", pool="claude", demand=3,
                      repo="o/r", subject="7", attempts=3) for i in range(MAX_REVISES)]
    assert coordinated_build.revise_round_budget_remains([], "o/r", "7") is True
    assert coordinated_build.revise_round_budget_remains(revises, "o/r", "7") is False
    assert coordinated_build.revise_round_budget_remains(revises[:-1], "o/r", "7") is (MAX_REVISES > 1)
    # A revise for another issue never counts against this one.
    assert coordinated_build.revise_round_budget_remains(revises, "o/r", "8") is True
