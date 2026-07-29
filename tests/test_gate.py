"""The auto-merge gate's decision matrix — pure, so fully unit-tested.

The one thing that must never happen: MERGE without independent review + green CI
+ clean verdict.
"""

import time
from pathlib import Path

import pytest

import agentflow.gate as gate
from agentflow import github
from agentflow.gate import (MergeDecision, ci_is_green, decide_merge,
                            has_committed_evidence, has_image_evidence,
                            maintainer_comment, maintainer_comment_id, reply_pending,
                            respond_reply_disclaimer, squash_merge,
                            touches_ui_surface, ui_evidence_gap)
from agentflow.reviewer import Finding, Verdict
from agentflow.review_policy import ReviewAction, ReviewFinding

CLEAN = Verdict(clean=True)
DIRTY = Verdict(clean=False, findings=(Finding("blocking", "bug"),))


def d(**kw):
    base = dict(verdict=CLEAN, ci_green=True, reviewer_tool="codex",
               builder_tool="claude", revises_used=0)
    return decide_merge(**{**base, **kw})


def test_clean_green_and_independent_merges():
    assert d() is MergeDecision.MERGE


def test_same_tool_review_never_merges():
    assert d(reviewer_tool="claude") is MergeDecision.PARK  # not independent


def test_missing_reviewer_never_merges():
    assert d(reviewer_tool="") is MergeDecision.PARK


def test_blocking_verdict_revises_then_bails():
    # ADR 0020: revise until clean, then bail after MAX_REVISES (=2) rounds.
    assert d(verdict=DIRTY, revises_used=0) is MergeDecision.REVISE
    assert d(verdict=DIRTY, revises_used=1) is MergeDecision.REVISE
    assert d(verdict=DIRTY, revises_used=2) is MergeDecision.PARK


def test_red_ci_revises_then_bails():
    assert d(ci_green=False, revises_used=0) is MergeDecision.REVISE
    assert d(ci_green=False, revises_used=2) is MergeDecision.PARK


def test_independence_dominates_even_a_clean_green_pr():
    # A same-tool review of an otherwise-perfect PR still must not auto-merge.
    assert d(reviewer_tool="claude", verdict=CLEAN, ci_green=True) is MergeDecision.PARK


@pytest.mark.parametrize("revises_used", [0, 1, 2])
def test_never_merges_without_independence(revises_used):
    assert d(reviewer_tool="claude", revises_used=revises_used) is not MergeDecision.MERGE


def test_unusable_review_parks_never_revises():
    # A review that failed to parse is an infra failure, not a code problem —
    # re-running the builder can't help, so park (don't waste a revise).
    unparsed = Verdict(clean=False, parsed=False, findings=(Finding("blocking", "no verdict"),))
    assert d(verdict=unparsed, revises_used=0) is MergeDecision.PARK


class _FakeGitHub:
    """Drive the gate through the agentflow.github interface it now leans on.

    The gate states facts (are the checks green? is the PR a draft? did the merge land?)
    by calling typed helpers and the named escape hatch — never by shelling out to `gh`.
    So these tests replay helper results and read back which helpers ran, rather than
    matching gh argument vectors (the whole point of the migration, ADR 0040)."""

    def __init__(self, monkeypatch, *, api=(), pr_ready=True):
        self.api_calls = []
        self.pr_ready_calls = []
        self._api = iter(api)
        self._pr_ready = pr_ready
        monkeypatch.setattr(gate.github, "api", self._api_stub)
        monkeypatch.setattr(gate.github, "pr_ready", self._pr_ready_stub)

    def _api_stub(self, args, *, parse_json=False):
        self.api_calls.append(args)
        return next(self._api)

    def _pr_ready_stub(self, repo, pr):
        self.pr_ready_calls.append((repo, pr))
        return self._pr_ready


def test_ci_poll_returns_false_at_deadline(monkeypatch):
    """Checks that never complete return False once the deadline expires."""
    monkeypatch.setattr(gate.github, "api", lambda *a, **k: None)  # never green
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert ci_is_green("o/r", 1, timeout=0) is False


def test_ci_poll_returns_true_when_checks_pass(monkeypatch):
    """Checks that pass on the first poll return True immediately."""
    monkeypatch.setattr(gate.github, "api", lambda *a, **k: "")  # all checks green
    assert ci_is_green("o/r", 1, timeout=30, interval=1) is True


def test_squash_merge_marks_a_draft_ready_before_merging(monkeypatch):
    gh = _FakeGitHub(monkeypatch, api=[{"isDraft": True}, ""], pr_ready=True)

    assert squash_merge("o/r", 7) is True
    assert gh.pr_ready_calls == [("o/r", 7)]   # a draft was undrafted before the merge
    assert len(gh.api_calls) == 2              # draft read, then the merge


def test_squash_merge_merges_an_already_ready_pr(monkeypatch):
    gh = _FakeGitHub(monkeypatch, api=[{"isDraft": False}, ""])

    assert squash_merge("o/r", 7) is True
    assert gh.pr_ready_calls == []             # already ready — no undraft
    assert len(gh.api_calls) == 2


@pytest.mark.parametrize("draft_read", [None, {}, {"isDraft": "true"}])
def test_squash_merge_does_not_merge_when_draft_state_cannot_be_determined(
        monkeypatch, draft_read):
    # An unreadable PR (None), a read missing the field ({}), or a non-boolean value all
    # leave the draft state unknown — fail closed, never merge, never even read on.
    gh = _FakeGitHub(monkeypatch, api=[draft_read])

    assert squash_merge("o/r", 7) is False
    assert len(gh.api_calls) == 1              # stopped after the draft read
    assert gh.pr_ready_calls == []


def test_squash_merge_does_not_merge_when_marking_ready_fails(monkeypatch):
    gh = _FakeGitHub(monkeypatch, api=[{"isDraft": True}], pr_ready=False)

    assert squash_merge("o/r", 7) is False
    assert gh.pr_ready_calls == [("o/r", 7)]   # tried to undraft, then bailed
    assert len(gh.api_calls) == 1              # never reached the merge


# --- the mechanical UI-evidence gate (ADR 0018) --------------------------------

def test_missing_ui_screenshot_parks_even_a_clean_green_review():
    # The unwaivable gate: a declared UI surface changed with no screenshot cannot
    # auto-merge, even on an otherwise-perfect independent review.
    assert d(ui_evidence_missing=True) is MergeDecision.PARK


def test_ui_gate_is_independent_of_the_reviewer_verdict():
    # A reviewer that PASSes a screenshot-less UI change cannot clear the gate — the
    # decision is read from the diff + attachments, not from the verdict.
    assert d(verdict=CLEAN, ci_green=True, ui_evidence_missing=True) is MergeDecision.PARK


def test_no_ui_gap_still_merges_a_clean_pr():
    # The gate is inert when evidence is present (or the change isn't UI).
    assert d(ui_evidence_missing=False) is MergeDecision.MERGE


class TestTouchesUiSurface:
    def test_a_file_under_a_declared_prefix_intersects(self):
        assert touches_ui_surface(["agentflow/static/dashboard.html", "agentflow/gate.py"],
                                  ["agentflow/static/"])

    def test_backend_only_change_does_not_intersect(self):
        assert not touches_ui_surface(["agentflow/gate.py", "tests/test_gate.py"],
                                      ["agentflow/static/"])

    def test_no_declared_surfaces_is_never_a_ui_change(self):
        assert not touches_ui_surface(["frontend/app.js"], [])


class TestHasImageEvidence:
    def test_markdown_image(self):
        assert has_image_evidence("before/after:\n![dark mode](shot.png)")

    def test_github_user_asset_url(self):
        # Drag-dropped uploads render as bare links, not markdown images.
        assert has_image_evidence(
            "see https://github.com/o/r/assets/123/abcd-efgh proof")

    def test_user_images_host(self):
        assert has_image_evidence(
            "https://user-images.githubusercontent.com/1/2.png")

    def test_html_img_tag(self):
        assert has_image_evidence('<img src="x.png">')

    def test_prose_only_body_has_no_image(self):
        assert not has_image_evidence("This changes the dashboard layout. Looks great.")


class TestHasCommittedEvidence:
    # The browserless attachment path: agents can't drag-drop into GitHub (that needs a
    # signed-in browser), so screenshots committed on the branch count as evidence.
    def test_committed_screenshot_under_the_convention_counts(self):
        assert has_committed_evidence(
            ["frontend/index.html", "docs/screenshots/issue-395/before-light.png"])

    def test_an_unrelated_image_elsewhere_is_not_evidence(self):
        assert not has_committed_evidence(["frontend/favicon.png", "frontend/app.js"])

    def test_a_non_image_file_under_the_convention_is_not_evidence(self):
        assert not has_committed_evidence(["docs/screenshots/issue-395/notes.md"])

    def test_no_files_no_evidence(self):
        assert not has_committed_evidence([])

    def test_gate_is_existence_only_never_a_contract_matcher(self):
        # ADR 0048 leaves the mechanical gate existence-only: a committed screenshot satisfies it
        # regardless of what the image shows or whether it matches the locked visual contract.
        # Contract fidelity is reviewer judgment, not a new mechanical matcher.
        assert has_committed_evidence(
            ["agentflow/webui/src/app.svelte",
             "docs/screenshots/issue-321/deadbeef/wildly-wrong-but-present.png"])
        assert has_image_evidence("![anything at all](whatever.png)")


class TestUiEvidenceGapAnchorsToUs:
    # issue #205: evidence counts only in the PR body or an agentflow-marked comment.
    # A UI file changed with no committed screenshot, so the gate falls through to the
    # body/comment check every time.
    _SURFACES = ["agentflow/webui/src/"]
    _IMG = "![before](x.png)"

    def _gap(self, monkeypatch, *, body="", comments=()):
        data = {
            "files": [{"path": "agentflow/webui/src/app.svelte"}],
            "body": body,
            "comments": [{"body": b} for b in comments],
        }
        monkeypatch.setattr(gate.github, "api", lambda *a, **k: data)
        return ui_evidence_gap("o/r", 7, self._SURFACES)

    def test_maintainer_comment_image_does_not_count(self, monkeypatch):
        # A stray image in an unmarked (maintainer) comment must not satisfy the gate.
        assert self._gap(monkeypatch, comments=[f"looks good {self._IMG}"]) is True

    def test_image_in_the_pr_body_counts(self, monkeypatch):
        assert self._gap(monkeypatch, body=f"proof:\n{self._IMG}") is False

    def test_image_in_an_agentflow_marked_comment_counts(self, monkeypatch):
        assert self._gap(
            monkeypatch, comments=[f"agentflow: build agent\n{self._IMG}"]) is False

    def test_no_images_anywhere_is_a_gap(self, monkeypatch):
        assert self._gap(monkeypatch, body="prose only", comments=["nice"]) is True

    def test_unreadable_pr_fails_closed_to_a_gap(self, monkeypatch):
        # The load-bearing rule: a read that couldn't reach GitHub stays unknown, and
        # unknown must never pass as "no UI change / evidence present". The escape hatch
        # returns None on failure, so the gate reports a gap rather than auto-merging blind.
        monkeypatch.setattr(gate.github, "api", lambda *a, **k: None)
        assert ui_evidence_gap("o/r", 7, self._SURFACES) is True


class TestBackfilledSurfacesActuallyGate:
    # Issue #337: the fleet's other frontends were undeclared, so this gate had never fired
    # outside agentflow. These are the exact shapes the backfill measured before it landed.
    _UI_FILES = ["frontend/diagnose.js", "frontend/diagnose.test.js",
                 "analysis_engine/analyzers/threshold.py"]

    def _gap(self, monkeypatch, surfaces, files, *, body=""):
        monkeypatch.setattr(gate.github, "api", lambda *a, **k: {
            "files": [{"path": p} for p in files], "body": body, "comments": []})
        return ui_evidence_gap("o/r", 476, surfaces)

    def test_a_frontend_change_with_no_shots_is_a_gap(self, monkeypatch):
        assert self._gap(monkeypatch, ["frontend/"], self._UI_FILES) is True

    def test_the_same_change_with_committed_shots_clears(self, monkeypatch):
        assert self._gap(
            monkeypatch, ["frontend/"],
            [*self._UI_FILES, "docs/screenshots/issue-476/abc1234/after-dark.png"]) is False

    def test_a_frontend_test_change_is_not_a_ui_change(self, monkeypatch):
        # Browser tests and backend files sit outside the declared surface,
        # so declaring surfaces must not park work that never touched the UI itself.
        assert self._gap(monkeypatch, ["sample-app/frontend/src/"],
                         ["sample-app/frontend/tests/browser/results-shelf.mjs",
                          "sample-app/backend/envelope.py", "Dockerfile"]) is False

    def test_declared_headless_never_reads_github(self, monkeypatch):
        # `ui-surfaces: none` resolves to an empty surface list, which must land on the inert
        # path — never the fail-closed one that parks a PR when a `gh` read fails.
        def explode(*a, **k):
            raise AssertionError("a headless repo must not be read for UI evidence")
        monkeypatch.setattr(gate.github, "api", explode)
        assert ui_evidence_gap("o/r", 476, []) is False


# --- issue #18: an unanswered maintainer comment blocks auto-merge --------------

_PARK = {"body": "> *agentflow: parked for human review.*\n\nfindings..."}
_REPLY = {"body": "> *agentflow: reply from the build agent.*\n\nhere's the screenshot"}
_MAINT = {"body": "Show me a screenshot please?"}


def test_unanswered_maintainer_comment_blocks_merge():
    # The whole point of #18: an otherwise-perfect PR still must NOT auto-merge while the
    # human who merges has an open question. Fails first if the block isn't wired in.
    assert d(reply_pending=True) is MergeDecision.PARK


def test_answered_comment_does_not_block_merge():
    assert d(reply_pending=False) is MergeDecision.MERGE


def test_reply_pending_true_when_maintainer_spoke_last():
    assert reply_pending([_PARK, _MAINT]) is True


def test_reply_pending_false_when_our_marker_spoke_last():
    # Don't wake on our own park notice or our own reply — that's the loop-forever trap.
    assert reply_pending([_MAINT, _REPLY]) is False        # we already answered
    assert reply_pending([_MAINT, _PARK, _REPLY]) is False
    assert reply_pending([_PARK]) is False
    assert reply_pending([]) is False


def test_reply_pending_ignores_trailing_blank_comments():
    assert reply_pending([_PARK, _MAINT, {"body": "   "}]) is True


def test_each_unanswered_comment_keeps_its_own_target_until_its_reply():
    comments = [
        _PARK,
        {"body": "First follow-up", "id": "IC_1"},
        {"body": "Show me a screenshot please?", "id": "IC_2"},
    ]
    assert maintainer_comment(comments) == "First follow-up"
    assert maintainer_comment_id(comments) == "IC_1"

    comments.append({"body": respond_reply_disclaimer("IC_1") + "\n\nDone."})
    assert reply_pending(comments) is True
    assert maintainer_comment(comments) == "Show me a screenshot please?"
    assert maintainer_comment_id(comments) == "IC_2"

    comments.append({"body": respond_reply_disclaimer("IC_2") + "\n\nAlso done."})
    assert reply_pending(comments) is False
    assert maintainer_comment(comments) == ""
    assert maintainer_comment_id(comments) == ""


def test_legacy_generic_agentflow_reply_answers_the_pending_run():
    assert maintainer_comment([_MAINT, _REPLY]) == ""   # our reply was the last word


def test_respond_park_closes_only_its_target_and_leaves_later_comment_pending():
    comments = [
        _PARK,
        {"body": "First follow-up", "id": "IC_1"},
        {"body": "Second follow-up", "id": "IC_2"},
        {"body": ("> *agentflow: Respond parked for human review.*\n"
                  "<!-- agentflow-respond-park-target:IC_1 -->")},
    ]
    assert reply_pending(comments) is True
    assert maintainer_comment_id(comments) == "IC_2"
    assert maintainer_comment(comments) == "Second follow-up"


# --- park() body rendering (issue #210) ----------------------------------------

def _park_body(monkeypatch, verdict):
    """Call park() and return the body string it posted through the PR-comment helper."""
    captured = []

    def pr_comment(repo, pr, body):
        captured.append(body)
        return True

    monkeypatch.setattr(gate.github, "pr_comment", pr_comment)
    gate.park("o/r", 99, verdict, reason="exhausted its review budget without a durable verdict")
    assert captured, "park() did not post a PR comment"
    return captured[0]


def test_no_verdict_park_says_no_review_was_completed(monkeypatch):
    # Fails before the fix: exhaustion park posted '(no blocking findings)' instead.
    body = _park_body(monkeypatch, None)
    assert "(no blocking findings)" not in body
    assert "No review was completed" in body


def test_no_verdict_park_has_no_findings_list(monkeypatch):
    body = _park_body(monkeypatch, None)
    assert "Review findings:" not in body


def test_no_verdict_park_carries_the_canonical_marker(monkeypatch):
    body = _park_body(monkeypatch, None)
    assert "agentflow: parked for human review" in body


def test_clean_verdict_park_uses_domain_sections_not_legacy_severity(monkeypatch):
    body = _park_body(monkeypatch, Verdict(clean=True))
    assert "Affected behavior:" in body
    assert "blocking findings" not in body


def test_findings_verdict_park_renders_findings(monkeypatch):
    verdict = Verdict(clean=False, findings=(Finding("blocking", "something bad", "f.py", 10),))
    body = _park_body(monkeypatch, verdict)
    assert "something bad" in body
    assert "**fix_before_completion**" in body
    assert "**blocking**" not in body
    assert "No review was completed" not in body


def test_reviewed_park_reports_fixes_shipped_and_follow_ups_filed(monkeypatch):
    verdict = Verdict(
        clean=True, fixes=("Removed the stale helper",),
        follow_up_issues=("https://github.com/o/r/issues/12",))
    body = _park_body(monkeypatch, verdict)
    assert "Review fixes shipped:" in body and "Removed the stale helper" in body
    assert "Follow-up issues filed:" in body and "issues/12" in body


def test_agentflow_skill_reads_the_current_four_action_park_contract(monkeypatch):
    verdict = Verdict(clean=False, actions=tuple(
        ReviewFinding(action, action.value, "grounded")
        for action in ReviewAction))
    body = _park_body(monkeypatch, verdict)
    skill = Path("skills/agentflow/SKILL.md").read_text()

    for action in ReviewAction:
        assert f"**{action.value}**" in body
        assert f"`{action.value}`" in skill
    revise_section = skill.split("### `revise <PR>`", 1)[1].split("## Land it as ready", 1)[0]
    assert "severity" not in revise_section.lower()
    assert "blocking" not in revise_section.lower()
    assert " nit" not in revise_section.lower()


def test_current_stage_park_replaces_prior_reason_once_and_notifies_each_new_identity(monkeypatch):
    from agentflow.handoff import DurableHandoff, Notification, Subject

    subject = Subject("o/r", 9, "pr")
    comments = [github.Comment(
        "> *agentflow: parked for human review.*\n\nold reason", "", id="park-1")]
    edits, posts, notifications = [], [], []
    monkeypatch.setattr(gate.github, "pr_comments", lambda *_args: list(comments))

    def edit(comment_id, body):
        edits.append((comment_id, body))
        comments[0] = github.Comment(body, "", id=comment_id)
        return True

    monkeypatch.setattr(gate.github, "edit_comment", edit)
    monkeypatch.setattr(
        gate.github, "pr_comment",
        lambda *_args: posts.append(_args) or True)
    handoff = DurableHandoff(
        notify=lambda *args: notifications.append(args) or True)

    def run(identity, reason):
        marker = f"agentflow-park:{identity}:{reason}"
        return handoff.hand_off(
            subject, identity=identity, stage="review", marker=marker,
            action=lambda: gate.park(
                "o/r", 9, None, reason=reason, proof_marker=marker),
            notification=Notification("agentflow needs you", reason))

    assert run("review-1", "first reason") == subject.url
    assert run("review-1", "first reason") == subject.url
    assert len(edits) == 1 and len(notifications) == 1

    assert run("review-2", "new reason") == subject.url
    assert len(comments) == 1 and posts == []
    assert len(edits) == 2 and len(notifications) == 2
    assert "new reason" in comments[0].body
    assert "agentflow-park:review-2:new reason" in comments[0].body
    assert "agentflow-park:review-1:first reason" not in comments[0].body


def test_clean_summary_posts_once_with_depth_proof_and_cross_tool_status(monkeypatch):
    comments = []
    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr(
        gate.github, "pr_comment",
        lambda _repo, _pr, body: comments.append(github.Comment(body=body, created_at="")) or True)
    verdict = Verdict(
        clean=True, reviewer_tool="codex", change_author_tool="claude",
        depth_reason="one journey", checks=("affected tests passed",))

    assert gate.post_clean_review_summary("o/r", 9, verdict) is True
    assert gate.post_clean_review_summary("o/r", 9, verdict) is True
    assert len(comments) == 1
    assert "Targeted" in comments[0].body and "affected tests passed" in comments[0].body
    assert "cross-tool review" in comments[0].body


def test_clean_summary_states_exact_same_tool_human_merge_status(monkeypatch):
    comments = []
    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr(
        gate.github, "pr_comment",
        lambda _repo, _pr, body: comments.append(github.Comment(body=body, created_at="")) or True)
    verdict = Verdict(
        clean=True, reviewer_tool="claude", change_author_tool="claude",
        depth_reason="reviewed fallback", checks=("affected checks passed",))

    assert gate.post_clean_review_summary("o/r", 9, verdict) is True
    assert "same-tool review; maintainer merge required" in comments[0].body


def test_clean_summary_replaces_stale_same_tool_status_without_duplicate_marker(monkeypatch):
    marker = "<!-- agentflow-clean-review-summary -->"
    comments = [github.Comment(
        body=f"> *agentflow: clean review.*\n{marker}\n\n"
             "Review status: same-tool review; maintainer merge required.",
        created_at="", id="comment-1")]

    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr(
        gate.github, "pr_comment",
        lambda *_args: pytest.fail("the existing summary must be updated, not duplicated"))

    def edit(comment_id, body):
        assert comment_id == "comment-1"
        comments[0] = github.Comment(body=body, created_at="", id=comment_id)
        return True

    monkeypatch.setattr(gate.github, "edit_comment", edit)
    verdict = Verdict(
        clean=True, reviewer_tool="codex", change_author_tool="claude",
        depth_reason="independent recovery", checks=("exact head checked",))

    assert gate.post_clean_review_summary("o/r", 9, verdict) is True
    assert comments[0].body.count(marker) == 1
    assert "Review status: cross-tool review." in comments[0].body
    assert "same-tool review; maintainer merge required" not in comments[0].body
