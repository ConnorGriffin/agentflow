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
from agentflow.gate import MAX_REVISES
from agentflow.reviewer import Finding, Verdict

BUILD_WT = "/w/.agentflow/worktrees/claude/issue-7-x"
REVIEW_WT = "/w/.agentflow/worktrees/codex-review/pr-42-x"

# Comment timestamps around a revise record's durable submission time (``created_at``, stamped at
# submission with the real clock): one that predates any record a test submits, one that postdates.
BEFORE_ROUND = "2001-01-01T00:00:00Z"
AFTER_ROUND = "2999-01-01T00:00:00Z"


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


def _ident(subject, stage, target="-", round=0):
    """The coordinator identity for one issue's stage (``repo|subject|stage|target[|r<round>]``
    — the auto-revise round joins the identity once one exists)."""
    base = f"o/r|{subject}|{stage}|{target}"
    return f"{base}|r{round}" if round else base


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
        self.fail_gh = False            # flip to make every gh read fail (a transient outage)
        self.projections = []
        monkeypatch.setattr("agentflow.loop._run", self._gh)
        monkeypatch.setattr(coordinated_build, "_review_verdict", lambda review: self.verdict)
        monkeypatch.setattr("agentflow.live.replace_projection",
                            lambda entries, **kw: self.projections.append(entries))

    def _gh(self, cmd, *args, **kwargs):
        if self.fail_gh:
            return SimpleNamespace(returncode=1, stdout="")
        if "list" in cmd:               # gh pr list --head ... --json number,headRefOid
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{"number": self.number, "headRefOid": self.head}]))
        if "view" in cmd:               # gh issue view <n> --json labels
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"labels": [{"name": name} for name in self.labels],
                 "body": "Issue acceptance"}))
        return SimpleNamespace(returncode=0, stdout="")

    def step(self):
        return coordinated_build.reconcile_and_project(self.coord)

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
    assert "Issue acceptance" in r.input_ptr
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
    review2 = _ident("7", "review", "sha-b", round=1)
    assert review2 != review                             # a new head SHA is a genuinely new review
    assert record_of(coord, revise).retired is True
    r2 = record_of(coord, review2)
    assert r2.attempts == 0 and r2.claim is True and r2.target == "sha-b"
    assert r2.pool == "codex" and r2.builder_lineage == "claude"
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


# --- a blocking review with the auto-revise round spent parks once and releases the claim --

def test_reconcile_parks_a_blocking_review_once_the_revise_round_is_spent(make_coord, monkeypatch):
    """Once the auto-revise product cap (``MAX_REVISES`` rounds, ADR 0004) is spent, a further
    blocking Review has no revise, review, or merge stage to hand its claim to. Reconcile must park
    the PR for a human exactly once and release the retained claim, not leave the PR owned
    forever."""
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
    for used in range(MAX_REVISES):
        review = _ident("7", "review", live.head, round=used)
        assert live.run_stage(review) == ["review"]      # Review blocks → reconcile opens Revise
        revise = _ident("7", "revise", live.head, round=used)
        assert record_of(coord, revise).claim is True
        assert live.run_stage(revise, head=next(heads)) == ["revise"]  # pushed → opens next Review
    assert sum(1 for r in _records(coord) if r.stage == "revise") == MAX_REVISES

    # The final blocking Review has no round left: reconcile parks it and releases the claim.
    final_review = _ident("7", "review", live.head, round=MAX_REVISES)
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


# --- Review → Revise carries the original builder complexity, never a mutable label reread --

def test_review_to_revise_keeps_the_builder_complexity_when_the_issue_label_changes(make_coord,
                                                                                    monkeypatch):
    """Review→Revise must run at the *original builder* complexity, carried durably on the record
    since the Build opened the Review — never re-read from the issue's live label. A label changed
    (or removed) after the build cannot alter or block the Revise (ADR 0018)."""
    fake = FakeSession()
    pr, verdict, revision = [True], [True], [False]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, revision=revision),
                       gate=tracer.build_review_revise_gate)
    live = _Live(coord, fake, monkeypatch, head="sha-a")

    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="standard", effort="high", source=BUILD_WT))
    assert live.run_stage(build) == ["build"]             # Build(standard) completes → Review(sha-a)
    review = _ident("7", "review", "sha-a")
    assert record_of(coord, review).builder_complexity == "standard"  # carried onto the review

    # The issue label is CHANGED before the blocking review opens the revise. The old behavior
    # re-read it and would revise at "deep"; the durable builder complexity must win instead.
    live.labels = ["agentflow:complexity:deep"]
    assert live.run_stage(review) == ["review"]           # Review blocks → reconcile opens Revise
    revise = record_of(coord, _ident("7", "revise", "sha-a"))
    assert revise.claim is True and revise.complexity == "standard"   # the original builder value
    assert revise.builder_complexity == "standard"        # still durable for the next hop


def test_review_without_durable_builder_complexity_parks_instead_of_stranding(make_coord,
                                                                              monkeypatch):
    """Issue #105 (spec): a blocking review carrying no durable builder complexity (a record from
    before that lineage fact existed) can never open a revise — a permanent condition. It must
    create exactly one parked-PR handoff and release the claim, not silently strand the PR."""
    fake = FakeSession()
    coord = make_coord(fake, adapter=_router(fake, pr=[True], verdict=[True], revision=[False]),
                       gate=tracer.build_review_revise_gate)
    live = _Live(coord, fake, monkeypatch, head="sha-a")
    review = coord.submit_stage(Submission(repo="o/r", subject="7", stage="review", pool="codex",
                                           complexity="deep", target="sha-a", source=REVIEW_WT,
                                           builder_lineage="claude"))   # no builder_complexity
    live.step()                                           # Review admitted and started
    fake.end(review, cause=ProviderCause.PROCESS)
    assert [o.stage for o in live.step()] == ["review"]   # completes; the opener finds no lineage

    parked = record_of(coord, review)
    assert parked.state == "held" and parked.claim is False
    assert parked.handoffs == 1 and parked.notifications == 1
    assert not any(r.stage == "revise" for r in _records(coord))
    assert tracer.owned_issues(_records(coord), "o/r") == set()
    assert live.step() == []                              # idempotent: no re-park, no revise
    still = record_of(coord, review)
    assert still.handoffs == 1 and still.notifications == 1


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


# --- outcome-first: the required durable non-code proof completes a revision too -----------

def test_revise_completes_on_durable_non_code_evidence_with_no_pushed_head(make_coord, monkeypatch):
    """Issue #105: a revision completes on a pushed head SHA OR the required durable non-code proof
    — an agentflow-marked PR comment that attaches evidence (e.g. a screenshot). The PRODUCTION
    verifier (:func:`coordinated_build._revision_ready`) must accept an evidence-only revision whose
    head never moved, not consume its continuations and park (the reported behavior)."""
    comments = [[]]                                       # the PR carries no evidence yet
    def _gh(cmd, *a, **k):                                # gh pr list → head is unchanged (== target)
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{"headRefOid": "sha-a", "number": 42}]))
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr("agentflow.loop._run", _gh)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: comments[0])

    fake = FakeSession()
    revise = ReviseStageAdapter(revision_ready=coordinated_build._revision_ready,
                                worktree_ready=lambda r: True, observer=fake)
    coord = make_coord(fake, adapter=StageRouter({"revise": revise}))
    ident = coord.submit_stage(_revise_sub(source=BUILD_WT, target="sha-a"))

    coord.cycle("claude")
    comments[0] = [{"body": "the maintainer drops a screenshot but with no agentflow marker "
                            "![shot](https://user-images.githubusercontent.com/1/x.png)",
                    "createdAt": AFTER_ROUND}]
    fake.end(ident, cause=ProviderCause.PROCESS)          # head unchanged, only an UNMARKED image
    assert coord.cycle("claude") == []                    # not our proof → continues, does not park
    assert record_of(coord, ident).continuation

    # The reviser answers the finding with an attached screenshot on an agentflow-marked comment.
    comments[0] = [{"body": "> *agentflow: reply from the build agent.*\n\n"
                            "![before/after](https://user-images.githubusercontent.com/1/x.png)",
                    "createdAt": AFTER_ROUND}]
    fake.end(ident, cause=ProviderCause.PROCESS)          # a bad exit cannot undo a durable outcome
    assert [o.status for o in coord.cycle("claude")] == ["completed"]


def test_evidence_predating_the_revise_round_cannot_complete_it(make_coord, monkeypatch):
    """Issue #118: an agentflow-marked image comment that already existed before this revise round
    opened (left during the Build or a prior round) is not this round's proof — the evidence must
    have been created after the revise record's durable submission time. Only a marked evidence
    comment postdating the round completes it."""
    stale = {"body": "> *agentflow: reply from the build agent.*\n\n"
                     "![old shot](https://user-images.githubusercontent.com/1/old.png)",
             "createdAt": BEFORE_ROUND}
    comments = [[stale]]                                  # the stale proof already sits on the PR
    def _gh(cmd, *a, **k):                                # gh pr list → head is unchanged (== target)
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{"headRefOid": "sha-a", "number": 42}]))
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr("agentflow.loop._run", _gh)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: comments[0])

    fake = FakeSession()
    revise = ReviseStageAdapter(revision_ready=coordinated_build._revision_ready,
                                worktree_ready=lambda r: True, observer=fake)
    coord = make_coord(fake, adapter=StageRouter({"revise": revise}))
    ident = coord.submit_stage(_revise_sub(source=BUILD_WT, target="sha-a"))

    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)          # only the pre-round evidence exists
    assert coord.cycle("claude") == []                    # stale proof → continues, not completed
    assert record_of(coord, ident).continuation

    # The reviser answers THIS round: a marked evidence comment created after the round opened.
    comments[0] = [stale,
                   {"body": "> *agentflow: reply from the build agent.*\n\n"
                            "![before/after](https://user-images.githubusercontent.com/1/new.png)",
                    "createdAt": AFTER_ROUND}]
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]


def test_restart_between_evidence_and_completion_still_completes_exactly_once(make_coord,
                                                                              monkeypatch):
    """Issue #118: the evidence anchor is the record's durable ``created_at`` and the proof is the
    PR comment itself, so a daemon death after the evidence appeared but before the completion pass
    changes nothing — the restarted coordinator reads both from the store and GitHub and completes
    the revise exactly once, never a second time on re-observing the same comment."""
    comments = [[{"body": "> *agentflow: reply from the build agent.*\n\n"
                          "![before/after](https://user-images.githubusercontent.com/1/x.png)",
                  "createdAt": AFTER_ROUND}]]
    def _gh(cmd, *a, **k):                                # gh pr list → head is unchanged (== target)
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{"headRefOid": "sha-a", "number": 42}]))
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr("agentflow.loop._run", _gh)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: comments[0])

    fake = FakeSession()
    revise = ReviseStageAdapter(revision_ready=coordinated_build._revision_ready,
                                worktree_ready=lambda r: True, observer=fake)
    coord = make_coord(fake, adapter=StageRouter({"revise": revise}))
    ident = coord.submit_stage(_revise_sub(source=BUILD_WT, target="sha-a"))
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)          # evidence durable; daemon dies here

    restarted = make_coord(fake, adapter=StageRouter({"revise": revise}))
    assert [o.status for o in restarted.cycle("claude")] == ["completed"]
    assert record_of(restarted, ident).state == "completed"

    # Re-observing the same evidence — same pass or another restart — creates no second completion.
    assert restarted.cycle("claude") == []
    assert make_coord(fake, adapter=StageRouter({"revise": revise})).cycle("claude") == []


def test_evidence_only_completion_opens_a_fresh_review_at_the_unchanged_head(make_coord,
                                                                             monkeypatch):
    """Issue #105 (spec): an evidence-only revision completes with the head SHA unchanged, so the
    next review binds to the *same* SHA as the retired prior review. The completed Revise must
    still hand its claim to a genuinely NEW review record with a fresh budget — the revise round
    joins the identity — never silently collide with the retired record and strand the chain."""
    fake = FakeSession()
    pr, verdict, revision = [True], [False], [False]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, revision=revision),
                       gate=tracer.build_review_revise_gate)
    live = _Live(coord, fake, monkeypatch, head="sha-a")
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source=BUILD_WT))
    assert live.run_stage(build) == ["build"]             # Build completes → Review(sha-a)
    review = _ident("7", "review", "sha-a")
    verdict[0] = True
    assert live.run_stage(review) == ["review"]           # Review blocks → reconcile opens Revise
    revise = _ident("7", "revise", "sha-a")

    live.step()                                           # Revise admitted and started
    revision[0] = True                                    # durable evidence; head UNCHANGED (sha-a)
    fake.end(revise, cause=ProviderCause.PROCESS)
    assert [o.stage for o in live.step()] == ["revise"]   # completes → must open a fresh review

    review2 = _ident("7", "review", "sha-a", round=1)     # same SHA, next round → a new identity
    r2 = record_of(coord, review2)
    assert r2.claim is True and r2.attempts == 0 and r2.target == "sha-a"  # fresh review budget
    assert record_of(coord, revise).retired is True       # the claim actually transferred
    assert record_of(coord, review).retired is True       # the prior review stays retired
    assert tracer.owned_issues(_records(coord), "o/r") == {7}
    live.step()                                           # a repeat pass opens nothing new
    assert sum(1 for r in _records(coord) if r.stage == "review") == 2


def test_revision_ready_rejects_a_head_that_does_not_descend_from_the_reviewed_sha(tmp_path,
                                                                                   monkeypatch):
    """A head that merely *differs* from the reviewed SHA is not a pushed revision: only a head
    descending from it counts, so a force-push back to an older commit keeps the revise incomplete
    instead of completing on a rewind (issue #105). The retained builder worktree answers the
    ancestry question."""
    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / "issue-9-x"
    wt.mkdir(parents=True)
    record = Record(identity="o/r|9|revise|sha-a", stage="revise", pool="claude", lineage="claude",
                    demand=3, repo="o/r", subject="9", target="sha-a", source=str(wt))
    ancestor = [1]                                        # git merge-base --is-ancestor exit code
    def _gh(cmd, *a, **k):
        if "merge-base" in cmd:
            return SimpleNamespace(returncode=ancestor[0], stdout="")
        if "list" in cmd:                                 # gh pr list → a DIFFERENT head
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{"headRefOid": "sha-rewound", "number": 42}]))
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr("agentflow.loop._run", _gh)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: [])

    assert coordinated_build._revision_ready(record, None) is False  # rewound head, no evidence
    ancestor[0] = 0                                       # the head descends from the reviewed SHA
    assert coordinated_build._revision_ready(record, None) is True


def _revise_head_read(tmp_path, monkeypatch, *, head, target="sha-a",
                      descends=False, rewound=False, local_head=None, status="", comments=()):
    """Wire ``_revision_ready`` against a real retained builder worktree on disk with a moved head.
    ``descends``/``rewound`` fake the two ``git merge-base --is-ancestor`` reads (reviewed→head and
    head→reviewed); ``local_head``/``status`` fake the worktree ``rev-parse``/``status`` ownership
    reads. Returns the record so the caller asserts the outcome."""
    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / "issue-9-x"
    wt.mkdir(parents=True)
    record = Record(identity="o/r|9|revise|sha-a", stage="revise", pool="claude", lineage="claude",
                    demand=3, repo="o/r", subject="9", target=target, source=str(wt))

    def _gh(cmd, *a, **k):
        if "merge-base" in cmd:
            reviewed_to_head = cmd[-2:] == [target, head]
            return SimpleNamespace(returncode=0 if (descends if reviewed_to_head else rewound) else 1,
                                   stdout="")
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout=(local_head if local_head is not None else head))
        if "status" in cmd:
            return SimpleNamespace(returncode=0, stdout=status)
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                [{"headRefOid": head, "number": 42}]))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agentflow.loop._run", _gh)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: list(comments))
    return record


def test_revision_ready_completes_on_a_rebased_head_the_worktree_owns(tmp_path, monkeypatch):
    """Reproduces PR #196: a finding asked for a rebase, so the pushed head does not descend from the
    reviewed SHA. It is still a genuine revision when the retained builder worktree owns it — sitting
    on that exact head with a clean tree — so completion must not be withheld for the rewritten
    history and burn the continuation budget."""
    record = _revise_head_read(tmp_path, monkeypatch, head="rebased-head",
                               descends=False, rewound=False)
    assert coordinated_build._revision_ready(record, None) is True


def test_revision_ready_rejects_a_rewound_head_even_with_a_clean_worktree(tmp_path, monkeypatch):
    """A force-push back to a commit that is an *ancestor* of the reviewed SHA is a rewind, not a
    revision — the worktree owning that older head must not complete it, with no qualifying evidence
    comment to fall back on either."""
    record = _revise_head_read(tmp_path, monkeypatch, head="older-head",
                               descends=False, rewound=True)
    assert coordinated_build._revision_ready(record, None) is False


def test_revision_ready_rejects_a_rewritten_head_the_worktree_does_not_own(tmp_path, monkeypatch):
    """A rewritten head that the retained worktree does not own — the worktree sits elsewhere or is
    dirty — is not proven the reviser's own pushed work, so it stays incomplete."""
    elsewhere = _revise_head_read(tmp_path / "a", monkeypatch, head="rebased-head",
                                  local_head="some-other-head")
    assert coordinated_build._revision_ready(elsewhere, None) is False
    dirty = _revise_head_read(tmp_path / "b", monkeypatch, head="rebased-head",
                              status=" M changed.py\n")
    assert coordinated_build._revision_ready(dirty, None) is False


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


def test_revise_exhaustion_parks_the_pr_resolved_from_the_builder_worktree(make_coord, monkeypatch):
    """A Revise owns the *builder* worktree (``.../<tool>/issue-<n>-<slug>``), not a review worktree,
    so its exhaustion handoff must resolve the PR by branch. The PRODUCTION park
    (:func:`coordinated_build._park_pr`) must post the durable park proof and release the claim —
    not return no proof and leave the handoff pending with the claim retained (the reported
    behavior)."""
    parked, notified, pr_comments = [], [], []
    def _gh(cmd, *a, **k):                                # gh pr list --head <branch> → PR number 42
        if "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"number": 42}]))
        return SimpleNamespace(returncode=0, stdout="")
    def _park(repo, pr, verdict, *, reason):
        parked.append((repo, pr))
        pr_comments.append({"body": "> *agentflow: parked for human review.*"})
    monkeypatch.setattr("agentflow.loop._run", _gh)
    monkeypatch.setattr("agentflow.gate.park", _park)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: list(pr_comments))
    monkeypatch.setattr("agentflow.notify.notify", lambda *a, **k: notified.append(a))

    fake = FakeSession()
    revise = ReviseStageAdapter(revision_ready=lambda r, o: False, worktree_ready=lambda r: True,
                                observer=fake, handoff=coordinated_build._park_pr)
    coord = make_coord(fake, adapter=StageRouter({"revise": revise}))
    ident = coord.submit_stage(_revise_sub(source=BUILD_WT))

    outcome = None
    for _ in range(8):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held" and outcome.handoff == "pr:parked"
    assert parked == [("o/r", 42)] and len(notified) == 1  # parked and notified exactly once
    rec = record_of(coord, ident)
    assert rec.state == "held" and rec.claim is False and rec.handoffs == 1
    assert rec.source == BUILD_WT                          # the builder worktree is left intact

    # Idempotent across a restart: the same durable park proof, no second park or notification.
    assert make_coord(fake, adapter=StageRouter({"revise": revise})).cycle("claude") == []
    assert parked == [("o/r", 42)] and len(notified) == 1


# --- crash boundaries at both transfer points ---------------------------------------------

def test_restart_at_transfer_points_redrives_the_next_stage_without_resubmission(make_coord,
                                                                                 monkeypatch):
    """Fault injection (ADR 0028): a daemon death between a stage persisting COMPLETED and its
    opener running must not strand the chain. On restart, the reconcile pass itself re-drives each
    transfer from the durable records — no hand-resubmitted successor — without dropping a claim,
    double-claiming, duplicating a revision or a review, or handing out a free attempt."""
    fake = FakeSession()
    pr, verdict, revision = [True], [True], [False]
    adapter = _router(fake, pr=pr, verdict=verdict, revision=revision)
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source=BUILD_WT))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    assert [o.stage for o in coord.cycle("claude")] == ["build"]  # completed; death before opener

    # Restart at Build → Review: reconcile re-drives the transfer from the completed record.
    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    live = _Live(restarted, fake, monkeypatch, head="sha-a")
    live.step()
    review = _ident("7", "review", "sha-a")
    assert record_of(restarted, build).retired is True
    assert record_of(restarted, review).claim is True
    assert tracer.owned_issues(_records(restarted), "o/r") == {7}

    # The review reaches its durable blocking verdict; the daemon dies again before its opener.
    live.step()                                           # Review admitted and started (codex)
    fake.end(review, cause=ProviderCause.PROCESS)         # verdict[0] already durable → completes
    assert [o.stage for o in restarted.cycle("codex")] == ["review"]

    # Restart at Review → Revise: re-driven with no hand-resubmitted successor.
    again = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    live2 = _Live(again, fake, monkeypatch, head="sha-a")
    live2.step()
    revise = _ident("7", "revise", "sha-a")
    assert record_of(again, review).retired is True
    rev = record_of(again, revise)
    assert rev.claim is True and rev.attempts == 0        # one claim owner, no free attempt
    assert sum(1 for r in _records(again) if r.stage == "revise") == 1
    assert tracer.owned_issues(_records(again), "o/r") == {7}

    # The revise runs and pushes; the daemon dies after completion, before its opener.
    live2.step()                                          # Revise admitted and started (claude)
    live2.head = "sha-b"
    revision[0] = True
    fake.end(revise, cause=ProviderCause.PROCESS)
    assert [o.stage for o in again.cycle("claude")] == ["revise"]  # finalized exactly once

    # Restart at Revise → Review: re-driven, one new review, nothing duplicated.
    final = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    live3 = _Live(final, fake, monkeypatch, head="sha-b")
    live3.step()
    review2 = _ident("7", "review", "sha-b", round=1)
    assert record_of(final, revise).retired is True
    assert record_of(final, revise).attempts == 1         # attempt not double-counted
    r2 = record_of(final, review2)
    assert r2.claim is True and r2.attempts == 0          # a fresh review budget
    assert tracer.owned_issues(_records(final), "o/r") == {7}
    live3.step()                                          # a repeat pass opens nothing new
    assert sum(1 for r in _records(final) if r.stage == "review") == 2
    assert sum(1 for r in _records(final) if r.stage == "revise") == 1


def test_transient_opener_failure_is_redriven_on_the_next_pass(make_coord, monkeypatch):
    """An opener that fails on a transient read (a gh non-zero exit) must not strand the chain:
    the completed record still holds the change claim, so the next reconcile pass re-drives the
    same transfer without any hand-resubmission."""
    fake = FakeSession()
    pr, verdict, revision = [True], [False], [False]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, revision=revision),
                       gate=tracer.build_review_revise_gate)
    live = _Live(coord, fake, monkeypatch, head="sha-a")
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source=BUILD_WT))
    live.step()                                           # Build admitted and started
    fake.end(build, cause=ProviderCause.PROCESS)
    live.fail_gh = True                                   # the PR lookup fails this pass
    assert [o.stage for o in live.step()] == ["build"]    # completed, but no review opened
    assert record_of(coord, build).claim is True and record_of(coord, build).retired is False
    assert not any(r.stage == "review" for r in _records(coord))

    live.fail_gh = False
    live.step()                                           # the next pass re-drives the transfer
    assert record_of(coord, _ident("7", "review", "sha-a")).claim is True
    assert record_of(coord, build).retired is True


# --- enabled stages admit; unknown future stages stay waiting ------------------------------

def test_build_review_revise_gate_keeps_unknown_stages_waiting(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_router(fake, pr=[False], verdict=[False], revision=[False]),
                       gate=tracer.build_review_revise_gate)
    revise = coord.submit_stage(_revise_sub("9", pool="claude"))
    future = coord.submit_stage(Submission(repo="o/r", subject="11", stage="future", pool="claude",
                                           complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, revise).state == "running"                # Revise admits
    rec = record_of(coord, future)
    assert rec.state == "waiting" and rec.attempts == 0


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
    assert sub.round == review.round                                  # stays in the review's round
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
    assert sub.round == revise.round + 1                             # the revise round it follows
    assert sub.transfer_from == "o/r|7|revise|sha-a"                 # assumes the revise's claim
    assert "pr-42-fix-thing" in sub.source


def test_continuation_attempts_do_not_expand_the_auto_revise_round_policy():
    """The per-stage continuation budget is separate from the auto-revise product cap
    (``MAX_REVISES`` rounds, ADR 0004): a logical Revise counts once no matter how many of its
    attempts it burned."""
    revises = [Record(identity=f"o/r|7|revise|sha-{i}", stage="revise", pool="claude", demand=3,
                      repo="o/r", subject="7", attempts=3) for i in range(MAX_REVISES)]
    assert coordinated_build.revise_round_budget_remains([], "o/r", "7") is True
    assert coordinated_build.revise_round_budget_remains(revises, "o/r", "7") is False
    assert coordinated_build.revise_round_budget_remains(revises[:-1], "o/r", "7") is (MAX_REVISES > 1)
    # A revise for another issue never counts against this one.
    assert coordinated_build.revise_round_budget_remains(revises, "o/r", "8") is True
