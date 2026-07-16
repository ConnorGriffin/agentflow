"""Revise as the third coordinated stage (issue #105), driven through the public
``submit_stage`` / ``cycle`` seam. A blocking Review hands its change claim to one waiting Revise;
Revise adopts the builder's retained PR branch/worktree, stays pinned to the builder's tool lineage
and complexity, keeps its local work across an interrupted continuation, completes only on a
verified pushed revision (independent of provider exit), parks the PR on exhaustion without
discarding that work, and a completed Revise opens one new Review bound to the changed head SHA with
a fresh budget.

The Build → Review → Revise → Review path is exercised end to end through
:func:`coordinated_build.reconcile_and_project` — the production interface that actually opens each
transition — with only its external reads faked (the PR head a completed Build/Revise exposes, the
issue complexity label a revise trigger reads, and the parsed review verdict). This proves the real
transition wiring, not a hand-submitted stand-in for it. The crash boundaries are exercised through
the coordinator's public seam behind the same stage router that runs all three live stages.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_build
from agentflow.coordinator import (BuildStageAdapter, ReviewStageAdapter, ReviseStageAdapter,
                                    StageRouter, Submission, tracer)
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import Record
from agentflow.coordinator.rollout import COORDINATED
from agentflow.gate import MAX_REVISES
from agentflow.reviewer import Finding, Verdict

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


def _ident(subject, stage, target="-"):
    """The coordinator identity for one issue's stage (``repo|subject|stage|target``)."""
    return f"o/r|{subject}|{stage}|{target}"


_BLOCKING = Verdict(clean=False, findings=(Finding("blocking", "fix the thing"),))


class _Live:
    """Drives the real Build → Review → Revise → Review transitions through
    :func:`coordinated_build.reconcile_and_project`, faking only its external reads. ``head`` is the
    PR head SHA a completed Build/Revise exposes (mutable, so a pushed revision advances it),
    ``verdict`` is the parsed review verdict a blocking Review acts on, and ``labels`` are the issue
    complexity labels a revise trigger reads. Each :meth:`step` is one full reconcile of both build
    pools plus the transition projection it settles."""

    def __init__(self, coord, fake, monkeypatch, *, head, number=42,
                 labels=("agentflow:complexity:deep",), verdict=_BLOCKING):
        self.coord = coord
        self.fake = fake
        self.head = head
        self.number = number
        self.labels = list(labels)
        self.verdict = verdict
        self.projections = []
        monkeypatch.setattr("agentflow.loop._run", self._gh)
        monkeypatch.setattr(coordinated_build, "_review_verdict", lambda review: self.verdict)
        monkeypatch.setattr("agentflow.live.replace_projection",
                            lambda entries, **kw: self.projections.append(entries))

    def _gh(self, cmd, *args, **kwargs):
        if "list" in cmd:               # gh pr list --head ... --json number,headRefOid
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{"number": self.number, "headRefOid": self.head}]))
        if "view" in cmd:               # gh issue view <n> --json labels
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"labels": [{"name": name} for name in self.labels]}))
        return SimpleNamespace(returncode=0, stdout="")

    def step(self):
        return coordinated_build.reconcile_and_project(
            self.coord, SimpleNamespace(name=COORDINATED))

    def run_stage(self, identity, *, head=None):
        """Admit and start the one waiting stage, then end its provider and reconcile so its
        durable outcome settles and the next transition is projected. Returns the settled stages."""
        self.step()
        if head is not None:
            self.head = head
        self.fake.end(identity, cause=ProviderCause.PROCESS)
        return [o.stage for o in self.step()]


# --- the full Build → Review → Revise → Review path, opened by reconcile_and_project -------

def test_reconcile_opens_each_transition_and_transfers_the_claim_at_every_hop(make_coord,
                                                                              monkeypatch):
    """The production interface — not a hand-submitted stand-in — opens Review from a completed
    Build, Revise from a blocking Review, and the next Review from a completed Revise. Each hop
    transfers the change claim before the prior stage retires, keeps the builder lineage/worktree,
    and binds the review to the exact head SHA (ADR 0028/0030)."""
    fake = FakeSession()
    pr, verdict, revision = [True], [False], [False]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, revision=revision),
                       gate=tracer.build_review_revise_gate)
    live = _Live(coord, fake, monkeypatch, head="sha-a")

    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source=BUILD_WT))
    live.step()                                          # Build admitted and started
    fake.end(build, cause=ProviderCause.PROCESS)
    assert live.step()[0].stage == "build"               # Build completes → reconcile opens Review

    # Build → Review: reconcile bound it to the reviewed head SHA and transferred the claim.
    review = _ident("7", "review", "sha-a")
    assert record_of(coord, build).retired is True
    r = record_of(coord, review)
    assert r.claim is True and r.pool == "codex" and r.builder_lineage == "claude"
    assert r.target == "sha-a"
    assert tracer.owned_issues(_records(coord), "o/r") == {7}

    live.step()                                          # Review admitted and started (codex)
    verdict[0] = True                                    # a blocking verdict became durable
    fake.end(review, cause=ProviderCause.PROCESS)
    assert live.step()[0].stage == "review"              # Review completes → reconcile opens Revise

    # Review → Revise: pinned to the builder's tool lineage and its retained PR branch/worktree.
    revise = _ident("7", "revise", "sha-a")
    assert record_of(coord, review).retired is True
    rev = record_of(coord, revise)
    assert rev.claim is True and rev.pool == "claude" and rev.lineage == "claude"
    assert rev.source == BUILD_WT                        # adopts the builder's retained worktree
    assert tracer.owned_issues(_records(coord), "o/r") == {7}

    live.step()                                          # Revise admitted and started (claude)
    live.head = "sha-b"                                  # the revision was pushed to the same branch
    revision[0] = True
    fake.end(revise, cause=ProviderCause.PROCESS)
    assert live.step()[0].stage == "revise"              # Revise completes → reconcile opens Review

    # Revise → a new Review for the changed head SHA, with a fresh budget; the prior review is gone.
    review2 = _ident("7", "review", "sha-b")
    assert review2 != review                             # a new head SHA is a genuinely new review
    assert record_of(coord, revise).retired is True
    r2 = record_of(coord, review2)
    assert r2.attempts == 0 and r2.claim is True and r2.target == "sha-b"
    assert r2.pool == "codex" and r2.builder_lineage == "claude"
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


# --- a blocking review with the auto-revise round spent parks once and releases the claim --

def test_reconcile_parks_a_blocking_review_once_the_revise_round_is_spent(make_coord, monkeypatch):
    """Once the single auto-revise product round (ADR 0018) is used, a further blocking Review has
    no revise, review, or merge stage to hand its claim to. Reconcile must park the PR for a human
    exactly once and release the retained claim, not leave the PR owned forever."""
    fake = FakeSession()
    pr, verdict, revision = [True], [True], [True]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, revision=revision),
                       gate=tracer.build_review_revise_gate)
    live = _Live(coord, fake, monkeypatch, head="sha-a")

    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source=BUILD_WT))
    assert live.run_stage(build) == ["build"]            # Build completes → Review(sha-a)

    # MAX_REVISES logical revise rounds, each: blocking Review → Revise → pushed revision → Review.
    heads = iter(["sha-b", "sha-c", "sha-d", "sha-e"])
    for _ in range(MAX_REVISES):
        review = _ident("7", "review", live.head)
        assert live.run_stage(review) == ["review"]      # Review blocks → reconcile opens Revise
        revise = _ident("7", "revise", live.head)
        assert record_of(coord, revise).claim is True
        assert live.run_stage(revise, head=next(heads)) == ["revise"]  # pushed → opens next Review
    assert sum(1 for r in _records(coord) if r.stage == "revise") == MAX_REVISES

    # The final blocking Review has no round left: reconcile parks it and releases the claim.
    final_review = _ident("7", "review", live.head)
    assert live.run_stage(final_review) == ["review"]
    parked = record_of(coord, final_review)
    assert parked.state == "held" and parked.claim is False
    assert parked.handoffs == 1 and parked.notifications == 1
    assert tracer.owned_issues(_records(coord), "o/r") == set()   # the PR is no longer owned
    assert sum(1 for r in _records(coord) if r.stage == "revise") == MAX_REVISES  # no extra revise

    # Idempotent: another reconcile neither re-parks nor re-notifies, and opens no new stage.
    assert live.step() == []
    still = record_of(coord, final_review)
    assert still.handoffs == 1 and still.notifications == 1
    assert tracer.owned_issues(_records(coord), "o/r") == set()


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
