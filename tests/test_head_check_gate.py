"""The head check gate (ADR 417): a review may not finish clean while the exact reviewed
commit has a red check.

The gate is decided from GitHub at settlement — a reviewer cannot clear it by not looking. A
caught red opens a revise round (the machine-fixable failure the revise budget exists for);
``action_required`` and spent rounds park with the failing check named; pending, absent,
skipped, and cancelled checks change nothing; an unreadable answer defers only the clean
settlement while every park still completes.
"""

from types import SimpleNamespace

import pytest

from agentflow import coordinated_review, github, pipeline
from agentflow.coordinator.record import Record
from agentflow.github import HeadChecks, head_checks_from_rollup
from agentflow.reviewer import Verdict


def _check_run(name, status="COMPLETED", conclusion="SUCCESS"):
    return {"__typename": "CheckRun", "name": name, "status": status,
            "conclusion": conclusion}


def _status(context, state):
    return {"__typename": "StatusContext", "context": context, "state": state}


# --- the pure state mapping, across both vocabularies in one context list ----------------


def test_check_run_conclusions_map_red_exactly_as_decided():
    red = head_checks_from_rollup(
        [_check_run("python", conclusion="FAILURE"),
         _check_run("slow", conclusion="TIMED_OUT")], "sha-a")
    assert red.failing == ("python", "slow") and not red.action_required


def test_action_required_is_red_and_named_as_asking_for_a_human():
    checks = head_checks_from_rollup(
        [_check_run("deploy", conclusion="ACTION_REQUIRED")], "sha-a")
    assert checks.failing == ("deploy",) and checks.action_required


@pytest.mark.parametrize("conclusion", ["SUCCESS", "NEUTRAL", "SKIPPED", "CANCELLED", "STALE"])
def test_completed_non_red_conclusions_change_nothing(conclusion):
    checks = head_checks_from_rollup([_check_run("python", conclusion=conclusion)], "sha-a")
    assert not checks.failing and not checks.pending


@pytest.mark.parametrize("status", ["QUEUED", "IN_PROGRESS", "PENDING", "WAITING"])
def test_a_not_completed_check_run_is_pending_not_red(status):
    checks = head_checks_from_rollup([_check_run("python", status=status)], "sha-a")
    assert not checks.failing and checks.pending


def test_legacy_status_states_map_across_the_second_vocabulary():
    checks = head_checks_from_rollup(
        [_status("ci/legacy", "FAILURE"), _status("ci/crashed", "ERROR"),
         _status("ci/waiting", "PENDING"), _status("ci/expected", "EXPECTED"),
         _status("ci/good", "SUCCESS")], "sha-a")
    assert checks.failing == ("ci/legacy", "ci/crashed") and checks.pending


def test_a_commit_with_no_checks_at_all_is_neither_red_nor_pending():
    checks = head_checks_from_rollup([], "sha-a")
    assert not checks.failing and not checks.pending and not checks.action_required


def test_red_wins_over_pending_in_one_mixed_rollup():
    checks = head_checks_from_rollup(
        [_check_run("python", conclusion="FAILURE"), _check_run("console", status="QUEUED")],
        "sha-a")
    assert checks.failing == ("python",) and checks.pending


# --- settlement: the three clean exits consult the gate ----------------------------------


def _completed_review_record(*, profile="reviewed", round=0):
    return Record(
        identity=f"o/r|7|review|sha-a|{profile}", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="sha-a", builder_lineage="claude",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix", state="completed",
        auto_merge_allowed=True, round=round)


def _wire_clean_settlement(monkeypatch, record, *, profile="reviewed", head_checks):
    """Fix every read a clean exact-head settlement performs except the check rollup under
    test, and report what was posted, parked, and merged."""
    summarized, parked, park_checks, edited = [], [], [], []

    def _park(_repo, _pr, _verdict, *, reason, context=None, proof_marker="", **_kwargs):
        parked.append(reason)
        park_checks.extend(context.checks if context is not None else ())

    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: profile)
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.github.pr_comments", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.github.edit_comment",
                        lambda comment_id, body: edited.append(comment_id) or True)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: head_checks)
    monkeypatch.setattr("agentflow.gate.park", _park)
    monkeypatch.setattr(
        "agentflow.gate.post_clean_review_summary",
        lambda repo, pr, verdict, head: summarized.append((repo, pr, head)) or True)
    monkeypatch.setattr("agentflow.coordinated_review._finish_review",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    return SimpleNamespace(summarized=summarized, parked=parked, park_checks=park_checks,
                           edited=edited)


def _spent_rounds():
    return [SimpleNamespace(stage="revise", conflict_round=None, repo="o/r", subject="7")] * 2


def test_a_red_reviewed_head_never_posts_the_clean_summary_on_a_reviewed_repo(monkeypatch):
    """The PR #412 shape: a clean PASS verdict on a `reviewed` repository whose reviewed head
    has one red check among green ones. The outcome is a revise round, not a clean review:
    settlement posts nothing and leaves the record for the revise opener."""
    record = _completed_review_record()
    red = head_checks_from_rollup(
        [_check_run("python", conclusion="FAILURE"),
         _check_run("console"), _check_run("dco"), _check_run("CodeQL"),
         _check_run("Analyze (python)"), _check_run("Analyze (actions)"),
         _check_run("Analyze (javascript-typescript)")], "sha-a")
    world = _wire_clean_settlement(monkeypatch, record, head_checks=red)
    monkeypatch.setattr(coordinated_review.tracer, "load_records", lambda *a, **k: [])

    assert coordinated_review._settle_review(record) is None
    assert world.summarized == [] and world.parked == []


def test_a_red_head_with_spent_revise_rounds_parks_naming_check_and_sha(monkeypatch):
    record = _completed_review_record()
    red = HeadChecks(sha="sha-a", failing=("python",))
    world = _wire_clean_settlement(monkeypatch, record, head_checks=red)
    monkeypatch.setattr(coordinated_review.tracer, "load_records",
                        lambda *a, **k: _spent_rounds())

    coordinated_review._settle_review(record)
    assert world.parked == [coordinated_review.RED_CHECK_SPENT_REASON]
    assert any("python" in line and "sha-a" in line for line in world.park_checks)


def test_action_required_parks_immediately_without_spending_a_revise_round(monkeypatch):
    """The revise budget is untouched (no revise records exist), yet the park is the
    action-required one: the check itself is asking for a human, so no round is spent."""
    record = _completed_review_record()
    red = HeadChecks(sha="sha-a", failing=("deploy",), action_required=True)
    world = _wire_clean_settlement(monkeypatch, record, head_checks=red)
    monkeypatch.setattr(coordinated_review.tracer, "load_records", lambda *a, **k: [])

    coordinated_review._settle_review(record)
    assert world.parked == [coordinated_review.ACTION_REQUIRED_REASON]
    assert any("deploy" in line for line in world.park_checks)


def test_all_pending_or_absent_or_skipped_checks_settle_exactly_as_today(monkeypatch):
    for rollup in (HeadChecks(sha="sha-a", pending=True),
                   HeadChecks(sha="sha-a"),
                   head_checks_from_rollup([_check_run("dco", conclusion="SKIPPED")], "sha-a")):
        record = _completed_review_record()
        world = _wire_clean_settlement(monkeypatch, record, head_checks=rollup)
        assert (coordinated_review._settle_review(record)
                == "https://github.com/o/r/pull/42")
        assert world.summarized == [("o/r", 42, "sha-a")] and world.parked == []


def test_only_the_check_read_unreadable_defers_the_clean_settlement_silently(monkeypatch):
    """Comment thread, PR facts, and PR content all readable; only the check-status read
    returns unknown: no clean summary, no merge, no park — repeatedly — and the record stays
    unsettled. That is the accepted cost, matching settlement's adjacent unreadable reads."""
    record = _completed_review_record()
    world = _wire_clean_settlement(monkeypatch, record, head_checks=None)

    for _ in range(3):
        assert coordinated_review._settle_review(record) is None
    assert world.summarized == [] and world.parked == [] and world.edited == []


def test_a_park_unrelated_to_checks_still_completes_when_the_check_read_is_unreadable(
        monkeypatch):
    """Decision 6: the gate is consulted only on exits that would otherwise finish clean. A
    UI-evidence gap is already a park, and it must park even while the rollup is unreadable."""
    record = _completed_review_record()
    world = _wire_clean_settlement(monkeypatch, record, head_checks=None)
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces",
                        lambda _workdir: ["app/ui/"])
    monkeypatch.setattr("agentflow.gate.ui_evidence_gap", lambda *_args: True)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, _sha: pytest.fail(
                            "a park path must never consult the check rollup"))

    coordinated_review._settle_review(record)
    assert len(world.parked) == 1 and world.summarized == []


def test_the_tainted_same_tool_arm_gates_like_the_reviewed_arm(monkeypatch):
    record = _completed_review_record(profile="autonomous")
    record.review_tainted = True
    red = HeadChecks(sha="sha-a", failing=("python",))
    world = _wire_clean_settlement(monkeypatch, record, profile="autonomous", head_checks=red)
    monkeypatch.setattr(coordinated_review.tracer, "load_records", lambda *a, **k: [])

    assert coordinated_review._settle_review(record) is None
    assert world.summarized == [] and world.parked == []


def test_the_auto_merge_arm_gates_ahead_of_the_merge_decision(monkeypatch):
    record = _completed_review_record(profile="autonomous")
    red = HeadChecks(sha="sha-a", failing=("python",))
    world = _wire_clean_settlement(monkeypatch, record, profile="autonomous", head_checks=red)
    monkeypatch.setattr("agentflow.gate.ui_evidence_gap", lambda *_args: False)
    monkeypatch.setattr("agentflow.gate.reply_pending", lambda _comments: False)
    monkeypatch.setattr("agentflow.gate.squash_merge",
                        lambda *_args: pytest.fail("a red head must never merge"))
    monkeypatch.setattr(coordinated_review.tracer, "load_records", lambda *a, **k: [])
    coordinated_review._REVIEW_CI_OBSERVED[record.identity] = True
    try:
        assert coordinated_review._settle_review(record) is None
    finally:
        coordinated_review._REVIEW_CI_OBSERVED.pop(record.identity, None)
    assert world.summarized == [] and world.parked == []


def test_a_repeat_park_with_a_changed_failing_set_leaves_the_comment_byte_identical(
        monkeypatch):
    """The park reason is a fixed constant, so the proof marker is stable: a second settlement
    against the same head with a *different* failing set proves the existing comment and makes
    no edit call. The names in the body are the first observation's snapshot."""
    record = _completed_review_record()
    posted, edited = [], []

    def _park(_repo, _pr, _verdict, *, reason, context=None, proof_marker="", **_kwargs):
        posted.append(f"> *agentflow: parked for human review.*\n<!-- {proof_marker} -->")

    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _w: "reviewed")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _w: [])
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.github.pr_comments",
                        lambda _repo, _pr: [github.Comment(body=body, created_at="")
                                            for body in posted])
    monkeypatch.setattr("agentflow.github.edit_comment",
                        lambda comment_id, body: edited.append(comment_id) or True)
    monkeypatch.setattr("agentflow.gate.park", _park)
    monkeypatch.setattr("agentflow.coordinated_review._finish_review",
                        lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    monkeypatch.setattr(coordinated_review.tracer, "load_records",
                        lambda *a, **k: _spent_rounds())

    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: HeadChecks(sha=sha, failing=("python",)))
    coordinated_review._settle_review(record)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: HeadChecks(sha=sha, failing=("python", "console")))
    coordinated_review._settle_review(record)

    assert len(posted) == 1 and edited == []


# --- the revise opener: a caught red spends a round --------------------------------------


def _wire_opener(monkeypatch, review, *, head_checks, pr_state="OPEN", pr_head="sha-a",
                 ui_gap=False, builder_source=("/work/build", 42)):
    submitted, parked = [], []
    coord = SimpleNamespace(
        submit_stage=lambda submission: submitted.append(submission),
        park_completed=lambda identity: parked.append(identity))
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": pr_head, "state": pr_state})
    monkeypatch.setattr("agentflow.gate.ui_evidence_gap", lambda *_args: ui_gap)
    monkeypatch.setattr("agentflow.repo_facts.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, _sha: head_checks)
    monkeypatch.setattr("agentflow.coordinated_revise._revise_builder_source",
                        lambda _r: builder_source)
    captured = {}

    def _submission(review_record, complexity, findings, *, target_sha=""):
        captured.update(complexity=complexity, findings=findings, target_sha=target_sha)
        return SimpleNamespace(identity="revise-sub")

    monkeypatch.setattr("agentflow.coordinated_revise.revise_submission", _submission)
    return SimpleNamespace(coord=coord, submitted=submitted, parked=parked, captured=captured)


def _reviewed_record_for_opener(**kwargs):
    record = _completed_review_record(**kwargs)
    record.builder_complexity = "standard"
    return record


def test_a_caught_red_check_opens_a_revise_round_named_check_and_sha_only(monkeypatch):
    review = _reviewed_record_for_opener()
    world = _wire_opener(monkeypatch, review,
                         head_checks=HeadChecks(sha="sha-a", failing=("python",)))

    pipeline._open_revise_on_red_check(world.coord, review, {})
    assert len(world.submitted) == 1
    assert world.captured["target_sha"] == "sha-a"
    assert world.captured["complexity"] == "standard"
    assert "`python`" in world.captured["findings"] and "sha-a" in world.captured["findings"]
    assert "log" in world.captured["findings"].lower()  # the no-CI-log instruction rides along


def test_spent_rounds_open_nothing_here_because_settlement_owns_that_park(monkeypatch):
    review = _reviewed_record_for_opener(round=2)
    world = _wire_opener(monkeypatch, review,
                         head_checks=HeadChecks(sha="sha-a", failing=("python",)))

    pipeline._open_revise_on_red_check(world.coord, review, {})
    assert world.submitted == [] and world.parked == []


def test_action_required_and_green_and_unreadable_all_open_nothing(monkeypatch):
    for rollup in (HeadChecks(sha="sha-a", failing=("deploy",), action_required=True),
                   HeadChecks(sha="sha-a"), None):
        review = _reviewed_record_for_opener()
        world = _wire_opener(monkeypatch, review, head_checks=rollup)
        pipeline._open_revise_on_red_check(world.coord, review, {})
        assert world.submitted == [] and world.parked == []


def test_a_moved_or_merged_head_belongs_to_settlement_not_a_revise(monkeypatch):
    review = _reviewed_record_for_opener()
    for state, head in (("MERGED", "sha-a"), ("OPEN", "sha-b")):
        world = _wire_opener(monkeypatch, review, pr_state=state, pr_head=head,
                             head_checks=HeadChecks(sha="sha-a", failing=("python",)))
        pipeline._open_revise_on_red_check(world.coord, review, {})
        assert world.submitted == []


def test_a_ui_evidence_gap_outranks_the_revise_round(monkeypatch):
    review = _reviewed_record_for_opener()
    world = _wire_opener(monkeypatch, review, ui_gap=True,
                         head_checks=HeadChecks(sha="sha-a", failing=("python",)))
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, _sha: pytest.fail(
                            "the screenshot park must precede the check read"))

    pipeline._open_revise_on_red_check(world.coord, review, {})
    assert world.submitted == [] and world.parked == []


def test_the_reviewer_is_told_to_read_the_checks_and_prove_its_baseline():
    """The mechanical gate is a backstop, not the only reader: the reviewer's own instructions
    require reading the reviewed head's checks, treat an unfixable red as blocking, and admit a
    differential local run as evidence only when the baseline is demonstrably the baseline — the
    rule the PR #412 incident actually calls for (its comparison run was never running `main`)."""
    from agentflow.reviewer import REVIEW_PROMPT

    prompt = " ".join(REVIEW_PROMPT.split())
    assert "gh pr checks" in prompt
    assert "A red check you cannot fix is `fix_before_completion`" in prompt
    assert "baseline is demonstrably the baseline" in prompt
    assert "A local run too noisy to read is unusable evidence" in prompt


def test_missing_builder_lineage_parks_once_like_the_blocking_path(monkeypatch):
    review = _reviewed_record_for_opener()
    world = _wire_opener(monkeypatch, review, builder_source=None,
                         head_checks=HeadChecks(sha="sha-a", failing=("python",)))

    pipeline._open_revise_on_red_check(world.coord, review, {})
    assert world.submitted == [] and world.parked == [review.identity]
