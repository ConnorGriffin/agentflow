"""Review policy through its public decision interface (ADR 0047)."""

import json
import inspect
from dataclasses import fields
from types import SimpleNamespace

import pytest

from agentflow.review_policy import (
    FollowUp,
    ReviewAssignment,
    ReviewAxis,
    ReviewDepth,
    ReviewAction,
    ReviewFinding,
    ReviewResult,
    ReviewState,
    Uncertainty,
    assign_depth,
    decode_findings,
    encode_findings,
    parse_review_result,
    proposed_depth,
    validate_follow_ups,
    conflict_uncertainty_from_message,
)


def test_permission_change_escalates_an_authors_focused_proposal_to_full():
    assignment = assign_depth(
        "focused", "one wording line", ["agentflow/permissions.py"])

    assert assignment.depth is ReviewDepth.FULL
    assert "sensitive" in assignment.reason


def test_a_changed_final_head_without_push_provenance_is_not_a_review_result():
    result = parse_review_result(json.dumps({
        "verdict": "PASS",
        "depth": "targeted",
        "depth_reason": "one journey",
        "axis": "combined",
        "change_author_tool": "claude",
        "reviewed_sha": "start",
        "final_sha": "fixed",
        "pushed_sha": "",
        "fixes": ["fixed it"],
        "follow_ups": [],
        "checks": ["tests passed"],
        "findings": [],
        "uncertainty": None,
    }), expected_sha="start")

    assert result.parsed is False
    assert "provenance" in result.detail


def test_structured_review_requires_recorded_checks():
    result = parse_review_result(json.dumps({
        "verdict": "PASS", "depth": "focused", "depth_reason": "exact link",
        "axis": "combined", "change_author_tool": "claude", "reviewed_sha": "head",
        "final_sha": "head", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": [], "findings": [], "uncertainty": None,
    }), expected_sha="head")

    assert result.parsed is False and "checks" in result.detail


def test_decision_pass_must_choose_or_return_structured_uncertainty():
    result = parse_review_result(json.dumps({
        "verdict": "PASS", "depth": "full", "depth_reason": "competing behavior",
        "axis": "decision", "change_author_tool": "claude", "reviewed_sha": "head",
        "final_sha": "head", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["compared product rules"], "findings": [], "uncertainty": None,
        "decision": "",
    }), expected_sha="head")

    assert result.parsed is False and "decision" in result.detail


def test_review_chain_state_round_trips_through_the_coordinator_store(tmp_path, monkeypatch):
    from agentflow.coordinator import Coordinator, Submission

    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    coord = Coordinator()
    identity = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="review", target="head", pool="codex",
        review=ReviewState(
            assignment=ReviewAssignment(
                ReviewDepth.FULL, "permission decision", ReviewAxis.PRODUCT),
            change_author_tool="claude", reviewed_from_sha="base", passes=2,
            cross_tool_covered=True, tainted=True, handoff="verify the permission",
            findings=(ReviewFinding(ReviewAction.FIX, "fix", "rule"),),
            fixes=("fixed copy",),
            follow_ups=(FollowUp("u", "evidence", "outcome", "query"),),
            uncertainty=Uncertainty(("a", "b"), "missing", "choose a"),
            uncertainty_handoffs=1)))

    record = coord.stage_record(identity)
    assert record.review_depth == "full" and record.review_axis == "product"
    assert record.change_author_tool == "claude" and record.review_passes == 2
    assert record.review_tainted is True and record.uncertainty_handoffs == 1
    assert decode_findings(record.review_findings)[0].summary == "fix"
    restored = ReviewState.from_record(record)
    assert restored is not None and restored.assignment.axis is ReviewAxis.PRODUCT
    assert restored.follow_ups[0].desired_outcome == "outcome"


def test_submission_and_review_mapping_take_one_cohesive_review_value():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator import Submission

    review_fields = [item.name for item in fields(Submission) if item.name.startswith("review")]
    assert review_fields == ["review"]
    parameters = inspect.signature(coordinated_review.review_submission).parameters
    assert "review" in parameters
    assert not {
        "review_depth", "review_axis", "review_findings", "review_fixes",
        "review_follow_ups", "review_checks",
    }.intersection(parameters)


def test_result_cannot_downgrade_or_change_its_durable_assignment():
    base = {
        "verdict": "PASS", "depth": "targeted", "depth_reason": "one journey",
        "axis": "combined", "change_author_tool": "claude", "reviewed_sha": "head",
        "final_sha": "head", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["verified"], "findings": [], "uncertainty": None, "decision": "",
    }
    downgraded = parse_review_result(
        json.dumps(base), expected_sha="head", expected_depth="full",
        expected_axis="combined", expected_author="claude")
    wrong_axis = parse_review_result(
        json.dumps({**base, "depth": "full", "axis": "standards"}), expected_sha="head",
        expected_depth="full", expected_axis="product", expected_author="claude")
    wrong_author = parse_review_result(
        json.dumps({**base, "change_author_tool": "codex"}), expected_sha="head",
        expected_depth="targeted", expected_axis="combined", expected_author="claude")

    assert not downgraded.parsed and "downgraded" in downgraded.detail
    assert not wrong_axis.parsed and "axis" in wrong_axis.detail
    assert not wrong_author.parsed and "author" in wrong_author.detail


def test_semantic_stakes_and_guarded_profile_enforce_full_without_filename_hints():
    destructive = assign_depth(
        "focused", "one line", ["agentflow/widget.py"],
        context="This changes the destructive delete action.")
    shared = assign_depth(
        "targeted", "one journey", ["agentflow/widget.py"],
        context="This changes a shared policy used across journeys.")
    guarded = assign_depth(
        "focused", "copy only", ["README.md"], guarded=True)

    assert destructive.depth is ReviewDepth.FULL
    assert shared.depth is ReviewDepth.FULL
    assert guarded.depth is ReviewDepth.FULL


def test_guarded_assignment_facts_force_full_product_and_standards_flow(monkeypatch):
    """A guarded repo overrides whatever the author proposed. The PR here reads perfectly well —
    the author asked for a Focused pass over nothing but README wording, which on any other repo
    would be granted — and the guarded profile still lifts it to a Full product review. The second
    half is the separate unreadable-PR path, which lands on the same answer for a different
    reason."""
    from agentflow import coordinated_review, pipeline

    monkeypatch.setattr(pipeline.github, "pr_content", lambda _repo, _pr: pipeline.github.PrContent(
        body="Review depth: Focused — wording only", paths=("README.md",), comments=[]))

    assignment, files = coordinated_review._review_assignment_facts(
        "o/r", 42, profile="guarded")

    assert assignment.depth is ReviewDepth.FULL and assignment.axis is ReviewAxis.PRODUCT
    assert assignment.reason == "guarded profile requires Full review"
    assert files == ("README.md",)   # the read PR's own surface, not the unreadable fallback

    monkeypatch.setattr(pipeline.github, "pr_content", lambda _repo, _pr: None)
    unreadable = coordinated_review._review_assignment_facts(
        "o/r", 42, profile="guarded")
    assert unreadable == (ReviewAssignment(
        ReviewDepth.FULL, "guarded profile requires Full review", ReviewAxis.PRODUCT), ())


def test_author_depth_proposal_is_read_from_the_pr_body_with_one_reason():
    assignment = proposed_depth("Summary\n\nReview depth: Focused — evidence link only")

    assert assignment.depth is ReviewDepth.FOCUSED
    assert assignment.reason == "evidence link only"


def test_reviewer_push_opens_an_exact_head_pass_for_the_other_tool():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    review = Record(
        identity="o/r|7|review|start", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="start", change_author_tool="claude",
        review_depth="targeted", depth_reason="one journey", review_axis="combined",
        builder_lineage="claude", builder_complexity="deep", builder_effort="extra",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix")
    verdict = Verdict(
        clean=True, reviewed_sha="start", final_sha="fixed", pushed_sha="fixed",
        fixes=("fixed the journey",), change_author_tool="claude")

    successor = coordinated_review.review_successor_submission(review, verdict)

    assert successor is not None and successor.pool == "claude"
    assert successor.target == "fixed" and successor.review.change_author_tool == "codex"
    assert successor.review.passes == 1 and successor.transfer_from == review.identity
    assert successor.builder_complexity == "deep" and successor.builder_effort == "extra"
    assert successor.effort is None


def test_reviewed_reviewer_fix_uses_immediate_same_tool_fallback_without_forced_taint(
        monkeypatch):
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    review = Record(
        identity="o/r|7|review|start", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="start", change_author_tool="claude",
        review_depth="targeted", depth_reason="one journey", review_axis="combined",
        builder_lineage="claude", builder_complexity="deep",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix")
    verdict = Verdict(
        clean=True, reviewed_sha="start", final_sha="fixed", pushed_sha="fixed",
        fixes=("fixed the journey",), change_author_tool="claude")
    calls = []
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda workdir: "reviewed")
    monkeypatch.setattr(
        coordinated_review, "pick_reviewer",
        lambda author, **kwargs: calls.append((author, kwargs)) or "codex")

    successor = coordinated_review.review_successor_submission(review, verdict)

    assert successor.pool == "codex"
    assert successor.review.tainted is False
    assert successor.review.cross_tool_covered is False
    assert calls == [("codex", {"allow_same_tool": True})]


def test_reviewer_fix_waits_for_capacity_but_third_mutating_pass_parks(monkeypatch):
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    record = Record(
        identity="review", stage="review", pool="codex", demand=2, repo="o/r",
        subject="7", target="start", change_author_tool="claude",
        review_depth="targeted", depth_reason="one journey", review_axis="combined",
        builder_lineage="claude", builder_complexity="deep",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix")
    verdict = Verdict(
        clean=True, reviewed_sha="start", final_sha="fixed", pushed_sha="fixed",
        fixes=("fixed",), change_author_tool="claude")
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [record])
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _record: verdict)
    monkeypatch.setattr(coordinated_review, "pick_reviewer", lambda *args, **kwargs: None)
    events = []
    coord = SimpleNamespace(
        submit_stage=lambda submission: events.append("submit"),
        park_completed=lambda identity: events.append("park"))

    pipeline._open_revise_on_blocking_review(coord, record.identity)
    assert events == []

    record.review_passes = 2
    pipeline._open_revise_on_blocking_review(coord, record.identity)
    assert events == ["park"]


def test_full_product_pass_opens_a_separate_read_only_standards_pass():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    review = Record(
        identity="o/r|7|review|head|aproduct", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="head", change_author_tool="claude",
        review_depth="full", depth_reason="shared permission", review_axis="product",
        builder_lineage="claude", builder_complexity="deep", builder_effort="high",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix")
    verdict = Verdict(
        clean=True, reviewed_sha="head", final_sha="head", change_author_tool="claude",
        checks=("product behavior verified",))

    successor = coordinated_review.review_axis_successor_submission(review, verdict)

    assert successor is not None and successor.review.assignment.axis is ReviewAxis.STANDARDS
    assert successor.target == "head" and successor.pool == "codex"
    assert successor.builder_complexity == "deep" and successor.builder_effort == "high"
    assert successor.effort is None
    assert "Do not edit during this axis pass" in successor.input_ptr


def test_full_standards_pass_durably_unions_product_and_standards_findings_for_fix():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    product = ReviewFinding(
        ReviewAction.FIX, "Product journey loses held reason", "Acceptance requires it",
        "agentflow/view.py", 12)
    standards = ReviewFinding(
        ReviewAction.FIX, "Interface exposes storage detail", "Charter deep-module rule",
        "agentflow/store.py", 8)
    review = Record(
        identity="o/r|7|review|head|astandards", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="head", change_author_tool="claude",
        review_depth="full", depth_reason="shared behavior", review_axis="standards",
        builder_lineage="claude", builder_complexity="deep",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        review_findings=encode_findings((product,)),
        review_checks='["product behavior checked"]')
    verdict = Verdict(
        clean=False, reviewed_sha="head", final_sha="head", change_author_tool="claude",
        depth=ReviewDepth.FULL, actions=(standards,), checks=("standards checked",))

    successor = coordinated_review.review_axis_successor_submission(
        review, verdict, axis="fix")
    persisted = successor.review.findings

    assert [item.summary for item in persisted] == [
        "Product journey loses held reason", "Interface exposes storage detail"]
    assert all(summary in successor.review.handoff for summary in (
        "Product journey loses held reason", "Interface exposes storage detail"))
    assert successor.review.checks == ("product behavior checked", "standards checked")


def test_reviewer_escalation_to_full_opens_product_axis_before_settlement():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    review = Record(
        identity="o/r|7|review|head", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="head", change_author_tool="claude",
        review_depth="targeted", depth_reason="one journey", review_axis="combined",
        builder_lineage="claude", builder_complexity="deep",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix")
    verdict = Verdict(
        clean=True, reviewed_sha="head", final_sha="head", change_author_tool="claude",
        depth=ReviewDepth.FULL, depth_reason="shared decision discovered",
        checks=("shared consumers traced",))

    successor = coordinated_review.review_axis_successor_submission(
        review, verdict, axis="product")

    assert successor.review.assignment.depth is ReviewDepth.FULL
    assert successor.review.assignment.axis is ReviewAxis.PRODUCT
    assert successor.review.assignment.reason == "shared decision discovered"


def test_full_fixer_push_restarts_product_then_standards_over_the_new_head(monkeypatch):
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    assigned = ReviewFinding(
        ReviewAction.FIX, "Repair shared behavior", "Product contract", "agentflow/x.py", 4)
    review = Record(
        identity="fix", stage="review", pool="codex", demand=2, repo="o/r", subject="7",
        target="old", review_depth="full", depth_reason="shared decision",
        review_axis="fix", change_author_tool="claude", builder_lineage="claude",
        builder_complexity="deep",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        review_findings=encode_findings((assigned,)))
    fixed = Verdict(
        clean=True, reviewed_sha="old", final_sha="new", pushed_sha="new",
        fixes=("repaired shared behavior",), depth=ReviewDepth.FULL,
        depth_reason="shared decision", change_author_tool="claude",
        checks=("focused fix check",))
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "reviewed")
    monkeypatch.setattr(coordinated_review, "pick_reviewer", lambda *_args, **_kwargs: "claude")

    product = coordinated_review.review_successor_submission(review, fixed)

    assert product is not None and product.target == "new"
    assert product.review.assignment.depth is ReviewDepth.FULL
    assert product.review.assignment.axis is ReviewAxis.PRODUCT
    assert product.review.findings == ()

    product_record = Record(
        identity="product", stage="review", pool=product.pool, demand=1,
        repo=product.repo, subject=product.subject, target=product.target,
        source=product.source, input_ptr=product.input_ptr,
        builder_lineage=product.builder_lineage, builder_complexity=product.builder_complexity,
        **product.review.record_fields())
    checked = Verdict(
        clean=True, reviewed_sha="new", final_sha="new", depth=ReviewDepth.FULL,
        depth_reason="shared decision", change_author_tool="codex",
        checks=("entire product axis checked",))
    standards = coordinated_review.review_axis_successor_submission(product_record, checked)

    assert standards is not None and standards.review.assignment.axis is ReviewAxis.STANDARDS
    assert standards.target == "new"


def test_fix_axis_cannot_dismiss_assigned_fixes_without_a_pushed_head():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record

    assigned = ReviewFinding(
        ReviewAction.FIX, "Repair shared decision", "Product rule", "agentflow/x.py", 4)
    record = Record(
        identity="fix", stage="review", pool="codex", demand=2, repo="o/r", subject="7",
        target="head", review_depth="full", depth_reason="shared decision",
        review_axis="fix", change_author_tool="claude",
        review_findings=encode_findings((assigned,)))
    payload = json.dumps({
        "verdict": "PASS", "depth": "full", "depth_reason": "shared decision",
        "axis": "fix", "change_author_tool": "claude", "reviewed_sha": "head",
        "final_sha": "head", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["inspected"], "findings": [], "uncertainty": None, "decision": "",
    })

    assert not coordinated_review._verdict_ready(
        record, SimpleNamespace(final_message=payload))


def test_fix_axis_accepts_a_no_push_verdict_that_rejudges_every_fix_as_no_defect():
    """A fix session may legitimately conclude the assigned fixes were not defects at all. Its
    verdict re-judges each ledger finding (here to discard_preference) and pushes nothing —
    that is a settled judgment, not a dodge, and refusing it burns the whole continuation
    budget and parks a PR no human needed to see (PR #393, 2026-07-31)."""
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record

    assigned = ReviewFinding(
        ReviewAction.FIX, "Repair shared decision", "Product rule", "agentflow/x.py", 4)
    record = Record(
        identity="fix", stage="review", pool="codex", demand=2, repo="o/r", subject="7",
        target="head", review_depth="full", depth_reason="shared decision",
        review_axis="fix", change_author_tool="claude",
        review_findings=encode_findings((assigned,)))
    payload = json.dumps({
        "verdict": "PASS", "depth": "full", "depth_reason": "shared decision",
        "axis": "fix", "change_author_tool": "claude", "reviewed_sha": "head",
        "final_sha": "head", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["inspected"], "findings": [
            {"action": "discard_preference", "summary": "Repair shared decision",
             "grounding": "the rule is scoped per-PR; nothing shared is altered",
             "file": "agentflow/x.py", "line": 4}],
        "uncertainty": None, "decision": "",
    })

    assert coordinated_review._verdict_ready(
        record, SimpleNamespace(final_message=payload))

    # The same verdict leaving even one finding as an outstanding fix is still refused.
    unfixed = json.loads(payload)
    unfixed["findings"][0]["action"] = "fix_before_completion"
    assert not coordinated_review._verdict_ready(
        record, SimpleNamespace(final_message=json.dumps(unfixed)))


def test_fix_axis_accepts_a_verified_pr_body_fix_without_a_pushed_head():
    """A PR-body correction changes the merge artifact but cannot produce a new Git head."""
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record

    assigned = ReviewFinding(
        ReviewAction.FIX, "Explain what the merger should check",
        "The charter requires a merge-facing check.", "PR body", 5)
    record = Record(
        identity="fix", stage="review", pool="codex", demand=2, repo="o/r", subject="7",
        target="head", review_depth="full", depth_reason="shared decision",
        review_axis="fix", change_author_tool="claude",
        review_findings=encode_findings((assigned,)))
    payload = json.dumps({
        "verdict": "PASS", "depth": "full", "depth_reason": "shared decision",
        "axis": "fix", "change_author_tool": "claude", "reviewed_sha": "head",
        "final_sha": "head", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["Updated and re-read the merge-facing PR body."],
        "findings": [], "uncertainty": None, "decision": "",
    })

    assert coordinated_review._verdict_ready(
        record, SimpleNamespace(final_message=payload))


def test_follow_up_must_exist_in_this_repo_and_carry_a_correct_origin_line():
    from agentflow.review_policy import FollowUp

    follow_up = FollowUp(
        "https://github.com/o/r/issues/9", "walkthrough is absent",
        "add routine browser proof", "browser walkthrough in:title")
    origin_body = "Discovered while reviewing #7 (pull request #42).\n\nMore detail here."
    viewed = []

    # A duplicate query that would match nothing in a live search still validates: the search
    # index lags fresh issues and a reasonable dedup query need not text-match the issue body, so
    # only the filed issue's own origin line is proof.
    valid = validate_follow_ups(
        "o/r", (follow_up,),
        issue_url=lambda number: viewed.append(number) or "https://github.com/o/r/issues/9",
        issue_body=lambda _n: origin_body, reviewed_issue=7, reviewed_pr=42)

    assert valid is True
    assert viewed == [9]

    # A nonexistent issue (issue_url disagrees, or reads None) fails closed.
    assert validate_follow_ups(
        "other/r", (follow_up,), issue_url=lambda _n: None, issue_body=lambda _n: origin_body,
        reviewed_issue=7, reviewed_pr=42) is False
    assert validate_follow_ups(
        "o/r", (follow_up,), issue_url=lambda _n: None, issue_body=lambda _n: origin_body,
        reviewed_issue=7, reviewed_pr=42) is False

    # A missing origin line fails.
    assert validate_follow_ups(
        "o/r", (follow_up,), issue_url=lambda _n: follow_up.url,
        issue_body=lambda _n: "No origin line here at all.",
        reviewed_issue=7, reviewed_pr=42) is False

    # An origin line naming the wrong issue or PR number fails.
    assert validate_follow_ups(
        "o/r", (follow_up,), issue_url=lambda _n: follow_up.url,
        issue_body=lambda _n: "Discovered while reviewing #8 (pull request #42).",
        reviewed_issue=7, reviewed_pr=42) is False
    assert validate_follow_ups(
        "o/r", (follow_up,), issue_url=lambda _n: follow_up.url,
        issue_body=lambda _n: "Discovered while reviewing #7 (pull request #43).",
        reviewed_issue=7, reviewed_pr=42) is False

    # An unreadable body is never proof: it fails closed rather than being skipped.
    assert validate_follow_ups(
        "o/r", (follow_up,), issue_url=lambda _n: follow_up.url, issue_body=lambda _n: None,
        reviewed_issue=7, reviewed_pr=42) is False


@pytest.mark.parametrize("decorated_body", [
    "**Discovered while reviewing #7 (pull request #42).**",
    "> Discovered while reviewing #7 (pull request #42).",
    "# Discovered while reviewing #7 (pull request #42).",
    "Discovered while reviewing issue #7 (PR #42).",
    "﻿Discovered while reviewing #7 (pull request #42).",
])
def test_origin_line_tolerates_light_markdown_decoration_and_phrasing_variants(decorated_body):
    from agentflow.review_policy import FollowUp

    follow_up = FollowUp(
        "https://github.com/o/r/issues/9", "walkthrough is absent",
        "add routine browser proof", "browser walkthrough in:title")

    assert validate_follow_ups(
        "o/r", (follow_up,), issue_url=lambda _n: follow_up.url,
        issue_body=lambda _n: decorated_body, reviewed_issue=7, reviewed_pr=42) is True


def test_review_follow_ups_valid_threads_the_reviewed_issue_and_pr_from_the_record(monkeypatch):
    """The call site must hand ``validate_follow_ups`` this review's own issue/PR, not any other
    pair: a swapped or wrong number must fail even though the origin line and issue lookup are
    otherwise honest."""
    from agentflow import coordinated_review, github
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    record = Record(
        identity="r", stage="review", pool="codex", demand=2, repo="o/r", subject="7",
        target="head", review_depth="targeted", depth_reason="one contained change",
        review_axis="combined", change_author_tool="claude",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix")
    follow_up = FollowUp(
        "https://github.com/o/r/issues/9", "walkthrough is absent",
        "add routine browser proof", "browser walkthrough in:title")
    verdict = Verdict(parsed=True, clean=True, follow_ups=(follow_up,))

    monkeypatch.setattr(github, "issue_url", lambda repo, number: follow_up.url)
    monkeypatch.setattr(
        github, "issue_body", lambda repo, number:
        "Discovered while reviewing #7 (pull request #42).")

    assert coordinated_review._review_follow_ups_valid(record, verdict) is True

    # The record's own subject (#7) and worktree PR (42) are what must reach the validator: an
    # origin line naming the swapped pair fails, proving the threading — not just the regex — is
    # under test.
    monkeypatch.setattr(
        github, "issue_body", lambda repo, number:
        "Discovered while reviewing #42 (pull request #7).")

    assert coordinated_review._review_follow_ups_valid(record, verdict) is False


def test_conflict_uncertainty_is_a_private_structured_provider_outcome():
    value = conflict_uncertainty_from_message(
        'CONFLICT-UNCERTAINTY: {"options":["keep shared rule","scope PR rule"],'
        '"missing_guidance":"which behavior owns ties",'
        '"recommendation":"keep the shared rule"}')

    assert value is not None
    assert value.options == ("keep shared rule", "scope PR rule")
    assert value.recommendation == "keep the shared rule"
    assert conflict_uncertainty_from_message("MISSING-CONTEXT: choose") is None


def test_tainted_same_tool_review_reopens_on_the_other_tool_at_the_same_head():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record

    prior = Record(
        identity="o/r|7|review|head", stage="review", pool="claude", demand=1,
        repo="o/r", subject="7", target="head", change_author_tool="claude",
        review_tainted=True, review_sequence=0, builder_lineage="claude",
        builder_complexity="standard", builder_effort="medium",
        source="/work/.agentflow/worktrees/claude-review/pr-42-fix",
        input_ptr="Review head as claude")

    successor = coordinated_review.tainted_review_submission(prior, "codex")

    assert successor is not None and successor.pool == "codex" and successor.target == "head"
    assert successor.review.assignment.axis is ReviewAxis.COMBINED
    assert successor.review.tainted is True and successor.review.taint_cleared is False
    assert successor.review.sequence == 1
    assert successor.builder_complexity == "standard" and successor.builder_effort == "medium"
    assert successor.effort is None
    assert successor.transfer_from is None


def test_full_taint_clears_only_after_clean_product_then_standards(monkeypatch):
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    prior = Record(
        identity="forced", stage="review", pool="claude", demand=1,
        repo="o/r", subject="7", target="head", change_author_tool="claude",
        review_depth="full", depth_reason="shared behavior", review_axis="product",
        review_tainted=True, builder_lineage="claude", builder_complexity="deep",
        source="/work/.agentflow/worktrees/claude-review/pr-42-fix",
        input_ptr="Review head")
    product_submission = coordinated_review.tainted_review_submission(prior, "codex")
    product = Record(
        identity="product", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="head", source=product_submission.source,
        input_ptr=product_submission.input_ptr, builder_lineage="claude",
        builder_complexity="deep", **product_submission.review.record_fields())

    def payload(axis, *, final="head", pushed="", fixes=()):
        return json.dumps({
            "verdict": "PASS", "depth": "full", "depth_reason": "shared behavior",
            "axis": axis, "change_author_tool": "claude", "reviewed_sha": "head",
            "final_sha": final, "pushed_sha": pushed, "fixes": list(fixes),
            "follow_ups": [], "checks": [f"{axis} checked"], "findings": [],
            "uncertainty": None, "decision": "",
        })

    assert coordinated_review._verdict_ready(
        product, SimpleNamespace(final_message=payload("product")))
    assert product.review_taint_cleared is False

    standards_submission = coordinated_review.review_axis_successor_submission(
        product, Verdict(
            clean=True, reviewed_sha="head", final_sha="head", depth=ReviewDepth.FULL,
            depth_reason="shared behavior", change_author_tool="claude",
            checks=("product checked",)))
    standards = Record(
        identity="standards", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="head", source=standards_submission.source,
        input_ptr=standards_submission.input_ptr, builder_lineage="claude",
        builder_complexity="deep", **standards_submission.review.record_fields())

    assert coordinated_review._verdict_ready(
        standards, SimpleNamespace(final_message=payload("standards")))
    assert standards.review_taint_cleared is True


def test_full_taint_stays_until_post_push_product_and_standards_complete(monkeypatch):
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict

    product = Record(
        identity="product", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="head", change_author_tool="claude",
        review_depth="full", depth_reason="shared behavior", review_axis="product",
        review_tainted=True, cross_tool_covered=True, builder_lineage="claude",
        builder_complexity="deep",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        input_ptr="Review head")
    pushed_payload = json.dumps({
        "verdict": "PASS", "depth": "full", "depth_reason": "shared behavior",
        "axis": "product", "change_author_tool": "claude", "reviewed_sha": "head",
        "final_sha": "fixed", "pushed_sha": "fixed", "fixes": ["fixed product issue"],
        "follow_ups": [], "checks": ["product checked"], "findings": [],
        "uncertainty": None, "decision": "",
    })
    assert coordinated_review._verdict_ready(
        product, SimpleNamespace(final_message=pushed_payload))
    assert product.review_taint_cleared is False

    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr(coordinated_review, "pick_reviewer", lambda *_args, **_kwargs: "claude")
    successor = coordinated_review.review_successor_submission(
        product, Verdict(
            clean=True, reviewed_sha="head", final_sha="fixed", pushed_sha="fixed",
            fixes=("fixed product issue",), depth=ReviewDepth.FULL,
            depth_reason="shared behavior", change_author_tool="claude",
            checks=("product checked",)))

    assert successor.review.assignment.axis is ReviewAxis.PRODUCT
    assert successor.review.change_author_tool == "codex"
    assert successor.review.tainted is True
    assert successor.review.taint_cleared is False


def test_successor_prompts_replace_the_private_assignment_instead_of_appending(monkeypatch):
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record
    from agentflow.reviewer import Verdict, with_review_assignment

    base_prompt = with_review_assignment(
        "Acceptance: preserve this exact sentence.",
        depth=ReviewDepth.FULL, reason="shared behavior", axis=ReviewAxis.FIX,
        change_author_tool="claude", handoff="obsolete fix handoff")
    record = Record(
        identity="fix", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="old", change_author_tool="claude",
        review_depth="full", depth_reason="shared behavior", review_axis="fix",
        builder_lineage="claude", builder_complexity="deep",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        input_ptr=base_prompt)
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "reviewed")
    monkeypatch.setattr(coordinated_review, "pick_reviewer", lambda *_args, **_kwargs: "claude")
    product = coordinated_review.review_successor_submission(
        record, Verdict(
            clean=True, reviewed_sha="old", final_sha="new", pushed_sha="new",
            fixes=("fixed",), depth=ReviewDepth.FULL, depth_reason="shared behavior",
            change_author_tool="claude", checks=("fix checked",)))

    def assert_assignment(submission, axis):
        prompt = submission.input_ptr
        assert prompt.count("<!-- agentflow-review-assignment:start -->") == 1
        assert prompt.count("Private review assignment") == 1
        assert prompt.count("- Prior handoff:") == 1
        assert f"- Axis: {axis}." in prompt
        assert "Acceptance: preserve this exact sentence." in prompt
        assert "obsolete fix handoff" not in prompt

    assert_assignment(product, "product")
    product_record = Record(
        identity="product", stage="review", pool="claude", demand=1,
        repo="o/r", subject="7", target="new", source=product.source,
        input_ptr=product.input_ptr, builder_lineage="claude", builder_complexity="deep",
        **product.review.record_fields())
    standards = coordinated_review.review_axis_successor_submission(
        product_record, Verdict(
            clean=True, reviewed_sha="new", final_sha="new", depth=ReviewDepth.FULL,
            depth_reason="shared behavior", change_author_tool="codex",
            checks=("product checked",)))
    assert_assignment(standards, "standards")

    standards_record = Record(
        identity="standards", stage="review", pool="claude", demand=1,
        repo="o/r", subject="7", target="new", source=standards.source,
        input_ptr=standards.input_ptr, builder_lineage="claude", builder_complexity="deep",
        **standards.review.record_fields())
    fix = coordinated_review.review_axis_successor_submission(
        standards_record, Verdict(
            clean=False, reviewed_sha="new", final_sha="new", depth=ReviewDepth.FULL,
            depth_reason="shared behavior", change_author_tool="codex",
            actions=(ReviewFinding(
                ReviewAction.FIX, "repair", "rule", "agentflow/x.py", 1),),
            checks=("standards checked",)), axis="fix")
    assert_assignment(fix, "fix")


def test_taint_recovery_chooses_only_latest_forced_autonomous_record(monkeypatch):
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record

    def tainted(identity, sequence, created):
        return Record(
            identity=identity, stage="review", pool="claude", demand=1, repo="o/r",
            subject="7", target="head", change_author_tool="claude", review_tainted=True,
            review_sequence=sequence, created_at=created, retired=True,
            source="/work/.agentflow/worktrees/claude-review/pr-42-fix")

    old, latest = tainted("old", 0, 1), tainted("latest", 2, 2)
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [old, latest])
    monkeypatch.setattr(
        coordinated_review, "review_source_facts", lambda record: ("/work", 42))
    monkeypatch.setattr(
        coordinated_review, "_review_pr_facts",
        lambda record: {"state": "OPEN", "head": "head"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(
        coordinated_review, "pick_reviewer", lambda author, **kwargs: "codex")
    chosen = []
    monkeypatch.setattr(
        coordinated_review, "tainted_review_submission",
        lambda record, tool: chosen.append(record.identity) or SimpleNamespace(stage="review"))
    submitted = []

    coordinated_review._resume_tainted_reviews(
        SimpleNamespace(submit_stage=submitted.append))

    assert chosen == ["latest"]
    assert len(submitted) == 1


def test_reverifying_continuation_settles_and_keeps_the_earlier_pushed_fix(monkeypatch):
    """After an earlier attempt pushed a fix, a continuation that only re-verifies that head
    reports no fixes and no push of its own — and the earlier fix survives in the ledger."""
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.coordinator.record import Record

    review = Record(
        identity="o/r|9|review|pushed-head|a2", stage="review", pool="codex", demand=2,
        repo="o/r", subject="9", target="pushed-head", change_author_tool="claude",
        review_depth="targeted", depth_reason="contained journey", review_axis="combined",
        builder_lineage="claude", builder_complexity="deep", attempts=2,
        source="/work/.agentflow/worktrees/codex-review/pr-9-reverify",
        review_fixes='["Corrected the held-reason wording"]',
        review_checks='["suite green at the pushed head"]')
    restated = json.dumps({
        "verdict": "PASS", "reviewed_sha": "pushed-head", "final_sha": "pushed-head",
        "pushed_sha": "", "fixes": ["Corrected the held-reason wording"],
        "checks": ["re-verified the pushed head"], "follow_ups": [], "findings": [],
        "depth": "targeted", "depth_reason": "contained journey", "axis": "combined",
        "change_author_tool": "claude"})
    assert not parse_review_result(restated, expected_sha="pushed-head").parsed

    clean = json.dumps({
        "verdict": "PASS", "reviewed_sha": "pushed-head", "final_sha": "pushed-head",
        "pushed_sha": "", "fixes": [], "checks": ["re-verified the pushed head"],
        "follow_ups": [], "findings": [], "depth": "targeted",
        "depth_reason": "contained journey", "axis": "combined",
        "change_author_tool": "claude"})
    monkeypatch.setattr(
        "agentflow.coordinator.providers.ProviderObserver.observe",
        lambda _self, _record: SimpleNamespace(final_message=clean))
    verdict = coordinated_review._review_verdict(review)

    assert verdict.parsed and verdict.clean and not verdict.pushed_sha
    assert verdict.fixes == ("Corrected the held-reason wording",)
    assert verdict.checks == ("suite green at the pushed head", "re-verified the pushed head")


# --- one exact head, one park/resume decision contract (#344) ------------------------------
# A PR head's Product/Standards/Fix passes are one durable chain. The decision that chain recorded
# belongs to the head, not to whichever pass happened to stop last, and only the maintainer's own
# answer retires it. The production loss was a park that read the terminal
# record alone, so an unanswered product decision became generic clarify/close boilerplate.

_RESCUE_DECISION = Uncertainty(
    ("Keep the conservative behavior for anyone who has never used the rescue log.",
     "Show the rescue-log prompt to every user on their first run."),
    "what a user who has never used the rescue log should see",
    "keep the conservative behavior")


def _chain_record(identity, *, sequence, created, axis, uncertainty=None, checks=(),
                  handoff=None, held=False, passes=0, outcome=None, prior_push=None,
                  hold_reason=None):
    """One durable Review record in a single PR exact head's chain. A parked pass is `held`,
    deliberately left unretired, and claimless — exactly what the coordinator's hold writes."""
    from agentflow.coordinator.record import Record

    review = ReviewState(
        assignment=ReviewAssignment(ReviewDepth.FULL, "shared behavior", ReviewAxis(axis)),
        change_author_tool="claude", sequence=sequence, uncertainty=uncertainty,
        checks=checks, handoff=handoff, passes=passes)
    return Record(
        identity=identity, stage="review", pool="codex", demand=2, repo="o/r", subject="479",
        target="c626f21bae01970c38b14711da5b38117c9f6872", created_at=created,
        state="held" if held else "completed", retired=not held, claim=False,
        builder_lineage="claude", builder_complexity="deep", builder_effort="extra",
        outcome=outcome, review_prior_push=prior_push, hold_reason=hold_reason,
        source="/work/.agentflow/worktrees/codex-review/pr-479-rescue-log",
        **review.record_fields())


def _park_body(monkeypatch, record):
    """Drive the live Review park through its durable handoff and return the PR comment it posted."""
    from agentflow import coordinated_review, github, pipeline, pr_park

    posted = []
    monkeypatch.setattr(github, "pr_comments",
                        lambda _repo, _pr: [github.Comment(body=body, created_at="")
                                            for body in posted])
    monkeypatch.setattr(github, "pr_comment",
                        lambda _repo, _pr, body: bool(posted.append(body)) or True)
    monkeypatch.setattr("agentflow.notify.notify", lambda *_args, **_kwargs: True)
    assert pr_park.park_pr(record) is not None
    assert len(posted) == 1
    return posted[0]


def test_the_chain_keeps_an_unanswered_decision_a_later_axis_recorded_none_for():
    from agentflow.review_policy import unresolved_uncertainty

    chain = [
        _chain_record("product", sequence=1, created=100, axis="product",
                      uncertainty=_RESCUE_DECISION),
        _chain_record("standards", sequence=2, created=200, axis="standards"),
        _chain_record("fix", sequence=3, created=300, axis="fix", held=True),
    ]

    assert unresolved_uncertainty(chain) == _RESCUE_DECISION
    assert unresolved_uncertainty([]) is None
    assert unresolved_uncertainty(chain[1:]) is None      # nothing recorded, nothing invented


def test_a_maintainers_answer_retires_the_chains_decision():
    """Only the maintainer's own answer settles a recorded decision, and it is bound to the exact
    comment it came from — so the next park never re-asks a question already answered."""
    from agentflow.review_policy import decision_answer_handoff, unresolved_uncertainty

    answered = _chain_record(
        "resumed", sequence=4, created=400, axis="product",
        handoff=decision_answer_handoff("IC_1", "keep the conservative behavior"))
    chain = [
        _chain_record("product", sequence=1, created=100, axis="product",
                      uncertainty=_RESCUE_DECISION),
        _chain_record("fix", sequence=3, created=300, axis="fix"),
        answered,
    ]

    assert unresolved_uncertainty(chain) is None
    assert unresolved_uncertainty(chain[:2]) == _RESCUE_DECISION


def test_a_parked_review_asks_the_decision_its_chain_recorded(monkeypatch):
    """The production regression: Product sequence 1 recorded the decision, Standards sequence 2
    completed, and Fix sequence 3 exhausted carrying none. The park must ask *that* decision — its
    exact missing guidance, both options, and the recommendation — not generic boilerplate, and it
    must not claim no review was completed."""
    from agentflow import coordinated_review, pipeline, pr_park

    chain = [
        _chain_record("product", sequence=1, created=100, axis="product",
                      uncertainty=_RESCUE_DECISION, checks=("product axis reviewed",)),
        _chain_record("standards", sequence=2, created=200, axis="standards",
                      checks=("product axis reviewed", "standards axis reviewed")),
        _chain_record("fix", sequence=3, created=300, axis="fix", held=True,
                      checks=("product axis reviewed", "standards axis reviewed")),
    ]
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: chain)

    body = _park_body(monkeypatch, chain[-1])

    assert _RESCUE_DECISION.missing_guidance in body
    assert _RESCUE_DECISION.recommendation in body
    for option in _RESCUE_DECISION.options:
        assert option in body
    assert "standards axis reviewed" in body               # what the chain did prove
    assert "No review was completed" not in body
    assert "Clarify the affected behavior" not in body
    assert "Reply on this PR with the behavior you want" in body


def test_a_review_that_recorded_no_decision_parks_as_an_execution_failure(monkeypatch):
    """A genuine no-verdict exhaustion is an execution failure, so the park says so and names the
    exact resume action. It invents no product choice for a change nobody ever judged."""
    from agentflow import coordinated_review, pipeline, pr_park

    chain = [_chain_record("only", sequence=0, created=100, axis="combined", held=True)]
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: chain)

    body = _park_body(monkeypatch, chain[0])

    assert "the review executions failed rather than judging the change" in body
    assert "`/agentflow review 479`" in body
    assert "Close the PR" not in body
    assert "Clarify the affected behavior" not in body
    # Every line of that comment has to agree with the rest of it: this park names no uncertainty
    # to resolve and offers no closing option, so it must send the maintainer after neither.
    assert "named uncertainty" not in body
    assert "closing preserves" not in body
    assert "Recommendation: Resume the review — nothing has judged this change yet." in body


def _parked_review_outcome(*, verdict="PASS", final=None, pushed="", findings=(), axis="fix",
                           decision=""):
    target = "c626f21bae01970c38b14711da5b38117c9f6872"
    return json.dumps({
        "verdict": verdict, "depth": "full", "depth_reason": "shared behavior",
        "axis": axis, "change_author_tool": "claude", "reviewed_sha": target,
        "final_sha": final or target, "pushed_sha": pushed,
        "fixes": (["repaired it"] if pushed else []), "follow_ups": [],
        "checks": ["reviewed"], "findings": list(findings),
        "uncertainty": None, "decision": decision,
    })


@pytest.mark.parametrize("hold_reason", [
    "completed stage has no successor",
    "PR head moved off the reviewed SHA and revise rounds are spent",
])
def test_a_three_pass_park_uses_its_ledger_in_every_exhaustion_branch(
        monkeypatch, hold_reason):
    pushed = "33549ff7bae01970c38b14711da5b38117c9f6872"
    record = _chain_record(
        "fix", sequence=3, created=300, axis="fix", held=True, passes=2,
        outcome=_parked_review_outcome(final=pushed, pushed=pushed),
        hold_reason=hold_reason)

    body = _park_body(monkeypatch, record)

    assert "Affected behavior: The requested PR behavior reached a human hand-off after 3 " \
        "verdict-recording review passes." in body
    assert "3 review passes recorded a verdict" in body
    assert "2 earlier passes pushed a repair at their own head" in body
    for false_claim in (
        "No review verdict was recorded for this exact head",
        "the review executions failed", "nothing has judged this change yet",
        "the review this change never got", "nothing has looked at this change at all",
        "no budget was drawn down",
    ):
        assert false_claim not in body


@pytest.mark.parametrize("hold_reason", [
    "completed stage has no successor",
    "PR head moved off the reviewed SHA and revise rounds are spent",
])
def test_a_zero_pass_park_keeps_the_existing_execution_failure_words(monkeypatch, hold_reason):
    record = _chain_record(
        "empty", sequence=0, created=100, axis="combined", held=True,
        hold_reason=hold_reason)

    body = _park_body(monkeypatch, record)

    assert "No review verdict was recorded for this exact head" in body
    assert "the review executions failed rather than judging the change" in body
    assert "Recommendation: Resume the review — nothing has judged this change yet." in body


@pytest.mark.parametrize("outcome,prior_push", [
    (_parked_review_outcome(
        verdict="BLOCK", findings=({
            "action": "fix_before_completion", "summary": "still blocked",
            "grounding": "the requested behavior is absent", "file": "agentflow/x.py", "line": 1,
        },)), None),
    (_parked_review_outcome(
        final="33549ff7bae01970c38b14711da5b38117c9f6872", pushed=""),
     "33549ff7bae01970c38b14711da5b38117c9f6872"),
])
def test_a_park_counts_any_stored_parsed_verdict_including_prior_push_provenance(
        monkeypatch, outcome, prior_push):
    record = _chain_record(
        "parsed", sequence=1, created=100, axis="fix", held=True,
        outcome=outcome, prior_push=prior_push,
        hold_reason="completed stage has no successor")

    body = _park_body(monkeypatch, record)

    assert "1 review pass recorded a verdict" in body
    assert "No review verdict was recorded for this exact head" not in body
    assert "the review executions failed" not in body


@pytest.mark.parametrize("hold_reason,cause,remedy", [
    ("refused before start — checkout-locked: review checkout pinned open",
     "latest review session did not run at all",
     "Release the pinned working copy on the machine agentflow runs on"),
    ("continuation budget exhausted — the last attempt was cut off at its turn cap",
     "last review session was cut off at its per-stage turn ceiling — it was stopped mid-review, "
     "not left short of an answer",
     "`/agentflow review 479`"),
])
def test_a_cause_specific_park_replaces_only_unsupported_zero_pass_claims(
        monkeypatch, hold_reason, cause, remedy):
    """The ledger silences claims the record disproves — never the one instruction that unblocks
    this park. A resume that skips releasing the pinned working copy walks into the same refusal."""
    record = _chain_record(
        "caused", sequence=1, created=100, axis="combined", held=True, passes=1,
        hold_reason=hold_reason)

    body = _park_body(monkeypatch, record)

    assert cause in body
    assert remedy in body
    assert "1 review pass recorded a verdict" in body
    assert "The 1 earlier pass pushed a repair at its own head" in body
    assert "nothing has looked at this change at all" not in body
    assert "no budget was drawn down" not in body
    assert "nothing has judged this change yet" not in body


def test_a_conflict_decision_park_counts_a_single_pass_in_readable_words(monkeypatch):
    """The park headline is also the proof marker, so its wording is durable, not decorative."""
    record = _chain_record(
        "decision", sequence=1, created=100, axis="decision", held=True,
        outcome=_parked_review_outcome(axis="decision", decision="kept the shipped behavior"),
        hold_reason="completed stage has no successor")

    body = _park_body(monkeypatch, record)

    assert "competing product behaviors after 1 verdict-recording review pass." in body
    assert "review passes" not in body


def test_a_resumed_review_keeps_the_head_lineage_and_ledger_and_settles_the_decision():
    from agentflow import coordinated_review, pipeline, pr_park
    from agentflow.review_policy import decision_answer_target, unresolved_uncertainty

    parked = _chain_record("fix", sequence=3, created=300, axis="fix", held=True,
                           uncertainty=_RESCUE_DECISION, checks=("standards axis reviewed",))
    submission = coordinated_review.decision_resume_review_submission(
        parked, "codex", target="IC_1", answer="keep the conservative behavior", sequence=4)

    assert submission.target == parked.target            # the immutable exact head
    assert submission.builder_lineage == "claude" and submission.review.change_author_tool == "claude"
    assert submission.builder_complexity == "deep" and submission.builder_effort == "extra"
    assert submission.effort is None
    assert submission.review.sequence == 4               # monotone in the same-head chain
    assert submission.review.checks == ("standards axis reviewed",)
    assert submission.review.uncertainty is None
    assert decision_answer_target(submission.review.handoff) == "IC_1"
    assert "keep the conservative behavior" in submission.input_ptr
    assert unresolved_uncertainty([parked, SimpleNamespace(
        created_at=400, review_sequence=4, identity="resumed",
        review_uncertainty=None, review_handoff=submission.review.handoff)]) is None


def test_a_changed_final_head_owned_by_a_prior_attempt_is_accepted():
    """The structured-result twin of the legacy rule: a moved final head with no in-payload push
    provenance parses when the caller proved that head durably (an earlier attempt of the same
    logical review pushed it and the retained checkout owns it). In-payload fixes still demand
    in-payload provenance — only the caller-proven head is excused."""
    result = parse_review_result(json.dumps({
        "verdict": "PASS", "depth": "targeted", "depth_reason": "one journey",
        "axis": "combined", "change_author_tool": "claude", "reviewed_sha": "start",
        "final_sha": "fixed", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["re-verified the prior fix on the branch"], "findings": [],
        "uncertainty": None,
    }), expected_sha="start", owned_heads=("fixed",))

    assert result.parsed is True
    assert result.final_sha == "fixed" and result.pushed_sha == ""
