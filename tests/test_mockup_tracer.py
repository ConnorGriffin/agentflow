"""Mockup as the sixth coordinated stage, exercised through the public coordinator seam."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest
from conftest import FakeSession, permits, record_of

from agentflow import (coordinated_build, coordinated_mockup, github, loop, stage_worktree,
                       worktree_ref)
from agentflow.coordinator import MockupStageAdapter, StageRouter
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator import tracer
from agentflow.coordinator.record import Record
from agentflow.worktree_ref import WorktreeRef


def test_mockup_submission_is_one_stable_variant_round_on_the_original_lineage():
    cfg = SimpleNamespace(repo="o/r", workdir="/home/w")
    issue = {"number": 11, "title": "Compare navigation concepts", "body": "Draw variants"}

    first = coordinated_mockup.mockup_submission(cfg, issue, "claude")
    again = coordinated_mockup.mockup_submission(cfg, issue, "claude")

    assert first == again
    assert first.stage == "mockup" and first.subject == "11" and first.target is None
    assert first.pool == first.builder_lineage == "claude"
    assert first.complexity == "deep" and first.claim is True
    # State the owned worktree through the layout owner, not a hand-written path: the submission
    # and the assertion read the same convention, and the drawing/review pair reads as one issue.
    expected = WorktreeRef.for_mockup("/home/w", "claude", 11, "compare-navigation-concepts")
    assert first.source == expected.path
    assert "/ui-craft" in first.input_ptr and "EXACTLY ONE comment" in first.input_ptr
    assert "continuation" in first.input_ptr and "NEVER post another" in first.input_ptr


def test_mockup_source_reads_back_to_its_own_branch_through_the_layout_owner():
    """The worktree a Mockup submission builds and the branch the admission parser later derives
    from it are two directions of one convention, so they cannot drift: parsing the submitted
    source back must yield the same branch the checkout is registered on."""
    cfg = SimpleNamespace(repo="o/r", workdir="/home/w")
    issue = {"number": 11, "title": "A screen", "body": "Draw it"}

    sub = coordinated_mockup.mockup_submission(cfg, issue, "claude")
    record = SimpleNamespace(source=sub.source, pool="claude", lineage="claude",
                             branch_lineage=None, stage="mockup", subject="11")
    workdir, branch, path = worktree_ref.source_facts(record)

    expected = WorktreeRef.for_mockup("/home/w", "claude", 11, "a-screen")
    assert (workdir, branch, str(path)) == (expected.workdir, expected.branch, expected.path)


def test_resubmission_cannot_switch_the_original_mockup_lineage(make_coord):
    fake = FakeSession()
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: False,
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    cfg = SimpleNamespace(repo="o/r", workdir="/w")
    issue = {"number": 11, "title": "A screen", "body": "Draw it"}
    ident = coord.submit_stage(coordinated_mockup.mockup_submission(cfg, issue, "claude"))
    assert coord.submit_stage(coordinated_mockup.mockup_submission(cfg, issue, "codex")) == ident

    rec = record_of(coord, ident)
    assert rec.pool == rec.lineage == "claude"
    assert "/claude/mockup-11-a-screen" in rec.source


def test_mockup_waiting_and_preparation_miss_retain_claim_lineage_and_local_work(make_coord):
    fake = FakeSession()
    ready = [False]
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: ready[0],
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    sub = coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 11, "title": "A screen", "body": "Draw it"}, "claude")
    ident = coord.submit_stage(sub)

    assert coord.cycle("claude") == []
    rec = record_of(coord, ident)
    assert rec.state == "waiting" and rec.attempts == 0 and permits(coord, "claude") == 0
    assert rec.claim is True and rec.lineage == "claude" and rec.source == sub.source

    ready[0] = True
    coord.cycle("claude")
    assert record_of(coord, ident).attempts == 1
    assert permits(coord, "claude") == 5


def test_interrupted_mockup_continues_on_the_same_branch_and_pinned_pool(make_coord):
    fake = FakeSession()
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    sub = coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 11, "title": "A screen", "body": "Draw it"}, "claude")
    ident = coord.submit_stage(sub)
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)

    coord.cycle("claude")
    rec = record_of(coord, ident)
    assert rec.state == "running" and rec.continuation is True and rec.attempts == 2
    assert rec.pool == rec.lineage == "claude" and rec.source == sub.source and rec.claim is True
    assert coord.cycle("codex") == []


def test_live_admission_gate_enables_mockup_at_its_reviewed_five_permit_demand(make_coord):
    fake = FakeSession()
    adapter = StageRouter({"mockup": MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        observer=fake,
    )})
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    sub = coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 11, "title": "A screen", "body": "Draw it"}, "claude")
    ident = coord.submit_stage(sub)

    coord.cycle("claude")
    assert record_of(coord, ident).state == "running"
    assert permits(coord, "claude") == 5


def test_public_mockup_seam_completes_only_on_pushed_variants_screenshots_and_one_comment(
        make_coord, monkeypatch, tmp_path):
    fake = FakeSession()
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    comment = (
        "> *agentflow intake: mockup variants — generated by AI.*\n\n"
        "![A](https://github.com/o/r/raw/refs/heads/agentflow/claude/mockup-11-a-screen/"
        "mockups/a.png)\n![B](https://github.com/o/r/raw/refs/heads/agentflow/claude/"
        "mockup-11-a-screen/mockups/b.png)\n![C](https://github.com/o/r/raw/refs/heads/"
        "agentflow/claude/mockup-11-a-screen/mockups/c.png)")
    monkeypatch.setattr("agentflow.github.issue_comment_rows",
                        lambda repo, number: [{"body": comment}])

    def external_read(argv):
        if "rev-parse" in argv and "HEAD" in argv:
            return SimpleNamespace(returncode=0, stdout="pushed-head\n")
        if "rev-parse" in argv and any(str(arg).startswith("origin/") for arg in argv):
            return SimpleNamespace(returncode=0, stdout="pushed-head\n")
        if "diff" in argv and "--name-only" in argv:
            files = [f"mockups/{name}.{ext}" for name in "abc" for ext in ("html", "png")]
            return SimpleNamespace(returncode=0, stdout="\n".join(files) + "\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agentflow.coordinated_mockup._run", external_read)
    settled = []
    adapter = MockupStageAdapter(
        outcome_ready=coordinated_mockup._mockup_outcome_ready,
        worktree_ready=lambda record: True,
        observer=fake,
        settle=lambda record: settled.append(record.identity) or "issue-url",
    )
    coord = make_coord(fake, adapter=adapter)
    sub = coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir=str(tmp_path)),
        {"number": 11, "title": "A screen", "body": "Draw it"}, "claude")
    ident = coord.submit_stage(sub)
    coord.cycle("claude")
    fake.end(ident, success=False, cause=ProviderCause.PROCESS)

    restarted = make_coord(fake, adapter=adapter)
    assert [out.status for out in restarted.cycle("claude")] == ["completed"]
    assert record_of(restarted, ident).claim is True
    restarted.cycle("claude")
    rec = record_of(restarted, ident)
    assert rec.retired is True and rec.claim is False and settled == [ident]


def test_duplicate_marked_comments_cannot_complete_mockup(monkeypatch, tmp_path):
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    comment = {"body": "> *agentflow intake: mockup variants — generated by AI.*"}
    monkeypatch.setattr("agentflow.github.issue_comment_rows",
                        lambda repo, number: [comment, comment])
    monkeypatch.setattr("agentflow.coordinated_mockup._run",
                        lambda argv: (_ for _ in ()).throw(AssertionError(
                            "duplicate comments must fail before git verification")))
    rec = Record(identity="o/r|11|mockup|-", stage="mockup", pool="claude", demand=5,
                 repo="o/r", subject="11", lineage="claude", source=str(wt))

    assert coordinated_mockup._mockup_outcome_ready(rec, SimpleNamespace()) is False


def test_deleted_mockup_artifacts_do_not_count_as_committed_outcome(monkeypatch, tmp_path):
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    comment = {"body": (
        "> *agentflow intake: mockup variants — generated by AI.*\n"
        "mockups/a.png mockups/b.png mockups/c.png")}
    monkeypatch.setattr("agentflow.github.issue_comment_rows", lambda repo, number: [comment])

    def git(argv):
        if "rev-parse" in argv:
            return SimpleNamespace(returncode=0, stdout="head\n")
        if "diff" in argv:
            assert "--diff-filter=ACMRT" in argv
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agentflow.coordinated_mockup._run", git)
    rec = Record(identity="o/r|11|mockup|-", stage="mockup", pool="claude", demand=5,
                 repo="o/r", subject="11", lineage="claude", source=str(wt))

    assert coordinated_mockup._mockup_outcome_ready(rec, SimpleNamespace()) is False


def test_missing_context_is_an_immediate_single_human_boundary_that_preserves_work(
        make_coord, monkeypatch):
    fake = FakeSession()
    monkeypatch.setattr("agentflow.github.issue_comment_rows", lambda repo, number: [{
        "body": ("> *agentflow intake: mockup variants — generated by AI.*\n\n"
                 "MISSING-CONTEXT: no runnable surface")
    }])
    handoffs = []
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        missing_context=coordinated_mockup._mockup_missing_context,
        observer=fake,
        handoff=lambda record: handoffs.append(record.identity) or "issue-proof",
    )
    coord = make_coord(fake, adapter=adapter)
    sub = coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 11, "title": "A screen", "body": "Draw it"}, "claude")
    ident = coord.submit_stage(sub)
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)

    outcomes = coord.cycle("claude")
    assert [out.status for out in outcomes] == ["held"]
    rec = record_of(coord, ident)
    assert rec.attempts == 1 and rec.claim is False and rec.source == sub.source
    assert rec.handoffs == rec.notifications == 1 and handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]


def test_exhaustion_creates_one_mockup_handoff_and_preserves_unfinished_work(make_coord):
    fake = FakeSession()
    handoffs = []
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        observer=fake,
        handoff=lambda record: handoffs.append(record.identity) or "issue-proof",
    )
    coord = make_coord(fake, adapter=adapter)
    sub = coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 11, "title": "A screen", "body": "Draw it"}, "claude")
    ident = coord.submit_stage(sub)
    outcome = None
    for _ in range(8):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)

    assert outcome is not None and outcome.status == "held"
    rec = record_of(coord, ident)
    assert rec.attempts == 3 and rec.handoffs == rec.notifications == 1
    assert rec.claim is False and rec.source == sub.source
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]


def test_public_prepare_proves_drawing_claim_before_creating_or_admitting_worktree(
        make_coord, monkeypatch, tmp_path):
    fake = FakeSession()
    claimed = [False]
    calls = []

    def git(argv):
        calls.append(argv)
        if "show-ref" in argv:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    def issue_labels(repo, number):
        return (frozenset({"agentflow:needs-mockup", "agentflow:drawing-mockup"}) if claimed[0]
                else frozenset({"agentflow:needs-mockup"}))

    monkeypatch.setattr("agentflow.stage_worktree._run", git)
    monkeypatch.setattr("agentflow.github.issue_labels", issue_labels)
    monkeypatch.setattr("agentflow.runner.ClaudeRunner.provision", lambda self, wt: None)
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: (
            coordinated_mockup._mockup_claim_ready(record)
            and stage_worktree.worktree_ready(record)),
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    sub = coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir=str(tmp_path)),
        {"number": 11, "title": "A screen", "body": "Draw it"}, "claude")
    ident = coord.submit_stage(sub)

    coord.cycle("claude")
    assert record_of(coord, ident).attempts == 0 and permits(coord, "claude") == 0
    assert not any("worktree" in call and "add" in call for call in calls)

    claimed[0] = True
    coord.cycle("claude")
    assert record_of(coord, ident).attempts == 1 and permits(coord, "claude") == 5
    added = next(call for call in calls if "worktree" in call and "add" in call)
    assert "origin/main" in added


def test_drawing_ownership_comes_only_from_durable_mockup_records():
    records = [Record(identity="m", stage="mockup", pool="claude", demand=5,
                      repo="o/r", subject="2", claim=True)]
    assert tracer.owned_issues(records, "o/r", lane="drawing") == {2}


def test_completed_mockup_releases_claim_keeps_human_boundary_and_disposes_worktree(
        monkeypatch, tmp_path):
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}

    def remove_label(repo, number, label):
        labels.discard(label)
        return True

    def settlement(repo, number):
        return github.IssueSettlement(labels=frozenset(labels),
                                      url="https://github.com/o/r/issues/11")

    monkeypatch.setattr("agentflow.github.remove_label", remove_label)
    monkeypatch.setattr("agentflow.github.issue_settlement", settlement)
    monkeypatch.setattr("agentflow.coordinated_mockup.remove_worktree_if_safe",
                        lambda workdir, path: (path.rmdir() is None))
    rec = Record(identity="o/r|11|mockup|-", stage="mockup", pool="claude", demand=5,
                 repo="o/r", subject="11", lineage="claude", source=str(wt))

    assert coordinated_mockup._settle_mockup(rec) == "https://github.com/o/r/issues/11"
    assert not wt.exists() and labels == {"agentflow:needs-mockup"}
    assert coordinated_mockup._settle_mockup(rec) == "https://github.com/o/r/issues/11"


def _mockup_hold_seams(monkeypatch, comments, labels, *, notified, labels_readable=None,
                       die_after_comment=False, edits_fail=False):
    """Wire the mockup hold's shared-envelope seams (ADR 0042): the durable comment thread it
    reads and proves the handoff through, the one marked comment it posts or edits, the label
    edit that hands the round back to the maintainer's choice, and the operator ping. Everything
    is stated as a fact about the issue, never as a ``gh`` argument vector. ``labels_readable``
    is a one-element list a test flips to stand a label read that couldn't reach GitHub,
    ``die_after_comment`` a daemon that dies the instant its comment is durable, and
    ``edits_fail`` a comment GitHub refuses to rewrite.
    """
    from agentflow import github

    def post(repo, number, body):
        comments.append(github.Comment(body=body, created_at="", id=f"IC_{len(comments)}"))
        if die_after_comment:
            raise RuntimeError("daemon died after the handoff comment landed")
        return True

    def edit(comment_id, body):
        if edits_fail:
            return False
        for index, existing in enumerate(comments):
            if existing.id == comment_id:
                comments[index] = github.Comment(body=body, created_at="", id=comment_id)
                return True
        return False

    def label_edit(args, *, parse_json=False):
        labels.add("agentflow:needs-mockup")
        labels.discard("agentflow:drawing-mockup")
        return ""

    monkeypatch.setattr(github, "issue_comments", lambda repo, number: list(comments))
    monkeypatch.setattr(github, "issue_labels",
                        lambda repo, number: None if labels_readable == [False]
                        else frozenset(labels))
    monkeypatch.setattr(github, "comment", post)
    monkeypatch.setattr(github, "edit_comment", edit)
    monkeypatch.setattr(github, "api", label_edit)
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args) or True)


def _refuse_a_second_comment(monkeypatch, why):
    def refuse(repo, number, body):
        raise AssertionError(why)
    monkeypatch.setattr("agentflow.github.comment", refuse)


def _mockup_record(wt, **extra):
    return Record(identity="o/r|11|mockup|-", stage="mockup", pool="claude", demand=5,
                  repo="o/r", subject="11", lineage="claude", source=str(wt), **extra)


def test_exhausted_mockup_posts_one_stable_handoff_and_retains_worktree(monkeypatch, tmp_path):
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}
    comments, notified = [], []
    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified)
    rec = _mockup_record(wt, hold_reason="continuation budget exhausted")

    first = coordinated_mockup._hold_mockup(rec)
    second = coordinated_mockup._hold_mockup(rec)
    assert first == second == "https://github.com/o/r/issues/11"
    assert len(comments) == 1 and "agentflow-mockup-hold" in comments[0].body
    assert labels == {"agentflow:needs-mockup"} and wt.exists()
    # The second pass observes the same marker and restates nothing. It does ping again — a
    # duplicate ping is the accepted cost of never dropping one (ADR 0042).
    assert len(notified) == 2


def test_mockup_hold_interrupted_after_its_comment_still_reaches_the_operator(
        monkeypatch, tmp_path):
    # The crash window that matters: the handoff comment reaches GitHub and the daemon dies
    # before the push goes out. Gating the ping on having posted the comment lost it for good —
    # the round sat held, and the maintainer whose choice it waits on was never told.
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}
    comments, notified = [], []
    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified, die_after_comment=True)
    rec = _mockup_record(wt, hold_reason="continuation budget exhausted")

    with pytest.raises(RuntimeError):
        coordinated_mockup._hold_mockup(rec)
    assert len(comments) == 1 and notified == []

    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified)   # restarted daemon
    assert coordinated_mockup._hold_mockup(rec) == "https://github.com/o/r/issues/11"
    assert len(comments) == 1 and len(notified) == 1


def test_mockup_hold_ping_carries_a_stable_sequence_id(monkeypatch, tmp_path):
    # The same held round always derives the same delivery key, so a repeat is recognizable as
    # the same handoff rather than as new work.
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    keys = []
    for _ in range(2):
        comments, notified = [], []
        _mockup_hold_seams(monkeypatch, comments,
                           {"agentflow:needs-mockup", "agentflow:drawing-mockup"},
                           notified=notified)
        coordinated_mockup._hold_mockup(_mockup_record(wt, hold_reason="budget exhausted"))
        keys.append(notified[0][3])
    assert keys[0] and keys[0] == keys[1]


def test_missing_context_comment_is_the_handoff_and_is_left_exactly_as_it_is(
        monkeypatch, tmp_path):
    # MISSING-CONTEXT already says why the round stopped and is itself the durable handoff, so
    # the hold writes nothing at all: no second comment, and no rewrite of the one that is there.
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}
    notified = []
    body = ("> *agentflow intake: mockup variants — generated by AI.*\n\n"
            "MISSING-CONTEXT: no runnable surface")
    comments = [SimpleNamespace(id="IC_missing", body=body)]
    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified)
    _refuse_a_second_comment(monkeypatch, "MISSING-CONTEXT already is the durable handoff")
    rec = _mockup_record(wt)

    assert coordinated_mockup._hold_mockup(rec) == "https://github.com/o/r/issues/11"
    assert len(comments) == 1 and comments[0].body == body and wt.exists()
    assert len(notified) == 1 and "missing context" in notified[0][1]


def test_a_missing_context_hold_is_not_wedged_by_a_comment_github_will_not_rewrite(
        monkeypatch, tmp_path):
    # Requiring an edit to land before the hold counts as proven made an unwritable comment
    # permanent: no ping, no release of the drawing claim, and a round that could never finish.
    # Nothing needs writing here, so a refused edit cannot hold the stage hostage.
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}
    notified = []
    comments = [SimpleNamespace(id="IC_missing", body=(
        "> *agentflow intake: mockup variants — generated by AI.*\n\n"
        "MISSING-CONTEXT: no runnable surface"))]
    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified, edits_fail=True)

    assert coordinated_mockup._hold_mockup(_mockup_record(wt)) == \
        "https://github.com/o/r/issues/11"
    assert labels == {"agentflow:needs-mockup"} and len(notified) == 1


def test_an_exhausted_round_whose_comment_cannot_be_rewritten_still_says_so(
        monkeypatch, tmp_path):
    # The other half of the same wedge: an unfinished round's explanation belongs on the comment
    # it already has, but if GitHub refuses that rewrite the explanation is posted on its own
    # rather than leaving the round stuck forever with nothing said and nobody told.
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}
    notified = []
    comments = [SimpleNamespace(id="IC_partial", body=(
        "> *agentflow intake: mockup variants — generated by AI.*\n\n"
        "Only variant A was finished."))]
    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified, edits_fail=True)

    assert coordinated_mockup._hold_mockup(_mockup_record(wt)) == \
        "https://github.com/o/r/issues/11"
    assert len(comments) == 2 and "continuation budget" in comments[1].body
    assert len(notified) == 1


def test_an_exhausted_round_does_not_report_an_earlier_rounds_missing_context(
        monkeypatch, tmp_path):
    # Round one ended at MISSING-CONTEXT; the maintainer answered and asked for another round,
    # which then ran out of budget with no missing context of its own. Reading the whole thread,
    # the hold found round one's comment, decided nothing needed saying, and told the maintainer
    # the round was missing context — the wrong reason, and no explanation on the issue at all.
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}
    notified = []
    comments = [github.Comment(id="IC_round1", created_at="2026-07-01T00:00:00Z", body=(
        "> *agentflow intake: mockup variants — generated by AI.*\n\n"
        "MISSING-CONTEXT: no runnable surface"))]
    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified)
    # The second round's record was opened after round one's comment was posted.
    rec = _mockup_record(wt, created_at=int(
        datetime.fromisoformat("2026-07-02T00:00:00+00:00").timestamp()))

    assert coordinated_mockup._hold_mockup(rec) == "https://github.com/o/r/issues/11"
    assert len(comments) == 2 and "continuation budget" in comments[1].body
    assert "continuation budget exhausted" in notified[0][1]


def test_partial_marked_comment_is_edited_into_the_exhaustion_handoff(monkeypatch, tmp_path):
    wt = tmp_path / ".agentflow/worktrees/claude/mockup-11-a-screen"
    wt.mkdir(parents=True)
    labels = {"agentflow:needs-mockup", "agentflow:drawing-mockup"}
    notified = []
    comments = [SimpleNamespace(id="IC_partial", body=(
        "> *agentflow intake: mockup variants — generated by AI.*\n\n"
        "Only variant A was finished."))]
    _mockup_hold_seams(monkeypatch, comments, labels, notified=notified)
    _refuse_a_second_comment(monkeypatch, "the one existing variant-round comment must be edited")
    rec = _mockup_record(wt)

    assert coordinated_mockup._hold_mockup(rec) == "https://github.com/o/r/issues/11"
    assert len(comments) == 1
    assert "agentflow-mockup-hold" in comments[0].body
    assert "continuation budget" in comments[0].body and wt.exists()
