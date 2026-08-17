"""The auto-merge gate's decision matrix — pure, so fully unit-tested.

The one thing that must never happen: MERGE without independent review + green CI
+ clean verdict.
"""

import hashlib
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest

import agentflow.gate as gate
from agentflow import github
from agentflow.gate import (MergeDecision, ci_is_green, decide_merge,
                            has_committed_evidence, has_image_evidence,
                            maintainer_comment, maintainer_comment_id, reply_pending,
                            pinned_mutation_gap, pinned_path_mutation,
                            respond_reply_disclaimer, review_resume_passes, squash_merge,
                            touches_ui_surface, ui_evidence_gap)
from agentflow.coordinator.record import Record
from agentflow.reviewer import Finding, Verdict
from agentflow.review_policy import FollowUp, ReviewAction, ReviewFinding

CLEAN = Verdict(clean=True)
DIRTY = Verdict(clean=False, findings=(Finding("blocking", "bug"),))


def d(**kw):
    base = dict(verdict=CLEAN, ci_green=True, reviewer_tool="codex",
               builder_tool="claude", revises_used=0,
               ui_evidence_missing=False, reply_pending=False)
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


def _review_result(*, target="sha-a", final="sha-b", pushed="sha-b"):
    return json.dumps({
        "verdict": "PASS", "depth": "targeted", "depth_reason": "one journey",
        "axis": "combined", "change_author_tool": "claude", "reviewed_sha": target,
        "final_sha": final, "pushed_sha": pushed,
        "fixes": (["repaired it"] if pushed else []), "follow_ups": [],
        "checks": ["reviewed"], "findings": [], "uncertainty": None, "decision": "",
    })


def _review_record(identity, *, created, passes, outcome=None, sequence=0):
    return Record(
        identity=identity, stage="review", pool="codex", demand=1, repo="o/r", subject="7",
        target="sha-a", created_at=created, review_passes=passes, review_sequence=sequence,
        review_depth="targeted", depth_reason="one journey", review_axis="combined",
        change_author_tool="claude", outcome=outcome)


def test_manual_review_ledger_counts_only_a_newest_records_own_repair_push():
    pushed = _review_record("pushed", created=200, passes=2, outcome=_review_result())
    unchanged = replace(
        pushed, identity="unchanged", outcome=_review_result(final="sha-a", pushed=""))

    assert review_resume_passes([pushed], "o/r", 7) == 3
    assert review_resume_passes([unchanged], "o/r", 7) == 2


def test_manual_review_ledger_uses_recency_instead_of_the_highest_sequence():
    high_sequence = _review_record("older", created=100, passes=2, sequence=99)
    recent = _review_record("newer", created=200, passes=1, sequence=0)

    assert review_resume_passes([recent, high_sequence], "o/r", 7) == 1


@pytest.mark.parametrize("state", ["waiting", "running", "held"])
def test_an_uncompleted_revise_does_not_reset_the_manual_review_ledger(state):
    review = _review_record("review", created=100, passes=2)
    revise = Record(
        identity="revise", stage="revise", pool="claude", demand=1, repo="o/r", subject="7",
        created_at=200, state=state, review_passes=0)

    assert review_resume_passes([review, revise], "o/r", 7) == 2


@pytest.mark.parametrize("carried", [0, 2])
def test_a_completed_revise_starts_the_manual_review_ledger_from_its_carried_count(carried):
    old_review = _review_record("old-review", created=100, passes=2)
    boundary = Record(
        identity="boundary", stage="revise", pool="claude", demand=1,
        repo="o/r", subject="7", created_at=200, state="completed", review_passes=carried)

    assert review_resume_passes([old_review, boundary], "o/r", 7) == carried


def test_a_same_second_successor_is_after_its_completed_revise_boundary():
    predecessor = replace(_review_record("predecessor", created=200, passes=2), round=0)
    boundary = Record(
        identity="boundary", stage="revise", pool="claude", demand=1,
        repo="o/r", subject="7", created_at=200, state="completed", round=0,
        review_passes=0)
    successor = replace(_review_record("successor", created=200, passes=2), round=1)

    assert review_resume_passes([predecessor, boundary], "o/r", 7) == 0
    assert review_resume_passes([predecessor, boundary, successor], "o/r", 7) == 2


class _FakeGitHub:
    """Drive the gate through the agentflow.github interface it now leans on.

    The gate states facts (are the checks green? is the PR a draft? did the merge land?)
    by calling typed helpers — never by shelling out to `gh`. So these tests replay helper
    results and read back which helpers ran, rather than matching gh argument vectors
    (the whole point of the migration, ADR 0040)."""

    def __init__(self, monkeypatch, *, draft=None, merged=True, pr_ready=True):
        self.draft_reads = []
        self.merge_calls = []
        self.pr_ready_calls = []
        self._draft = draft
        self._merged = merged
        self._pr_ready = pr_ready
        monkeypatch.setattr(gate.github, "pr_is_draft", self._draft_stub)
        monkeypatch.setattr(gate.github, "merge_pr", self._merge_stub)
        monkeypatch.setattr(gate.github, "pr_ready", self._pr_ready_stub)

    def _draft_stub(self, repo, pr):
        self.draft_reads.append((repo, pr))
        return self._draft

    def _merge_stub(self, repo, pr):
        self.merge_calls.append((repo, pr))
        return self._merged

    def _pr_ready_stub(self, repo, pr):
        self.pr_ready_calls.append((repo, pr))
        return self._pr_ready


def test_ci_poll_returns_false_at_deadline(monkeypatch):
    """Checks that never complete return False once the deadline expires."""
    monkeypatch.setattr(gate.github, "pr_checks_passed", lambda *a: False)  # never green
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert ci_is_green("o/r", 1, timeout=0) is False


def test_ci_poll_returns_true_when_checks_pass(monkeypatch):
    """Checks that pass on the first poll return True immediately."""
    monkeypatch.setattr(gate.github, "pr_checks_passed", lambda *a: True)
    assert ci_is_green("o/r", 1, timeout=30, interval=1) is True


def test_squash_merge_marks_a_draft_ready_before_merging(monkeypatch):
    gh = _FakeGitHub(monkeypatch, draft=True, pr_ready=True)

    assert squash_merge("o/r", 7) is True
    assert gh.pr_ready_calls == [("o/r", 7)]   # a draft was undrafted before the merge
    assert gh.merge_calls == [("o/r", 7)]


def test_squash_merge_merges_an_already_ready_pr(monkeypatch):
    gh = _FakeGitHub(monkeypatch, draft=False)

    assert squash_merge("o/r", 7) is True
    assert gh.pr_ready_calls == []             # already ready — no undraft
    assert gh.merge_calls == [("o/r", 7)]


def test_squash_merge_does_not_merge_when_draft_state_cannot_be_determined(monkeypatch):
    # A PR whose draft state could not be read leaves it unknown — fail closed, never merge,
    # never even read on. What counts as unreadable is `github.pr_is_draft`'s own contract.
    gh = _FakeGitHub(monkeypatch, draft=None)

    assert squash_merge("o/r", 7) is False
    assert gh.draft_reads == [("o/r", 7)]      # stopped after the draft read
    assert gh.merge_calls == []
    assert gh.pr_ready_calls == []


def test_squash_merge_does_not_merge_when_marking_ready_fails(monkeypatch):
    gh = _FakeGitHub(monkeypatch, draft=True, pr_ready=False)

    assert squash_merge("o/r", 7) is False
    assert gh.pr_ready_calls == [("o/r", 7)]   # tried to undraft, then bailed
    assert gh.merge_calls == []                # never reached the merge


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
        content = github.PrContent(
            body=body, paths=("agentflow/webui/src/app.svelte",),
            comments=[github.Comment(body=b, created_at="") for b in comments])
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: content)
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
        # unknown must never pass as "no UI change / evidence present". The typed read
        # returns None on failure, so the gate reports a gap rather than auto-merging blind.
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: None)
        assert ui_evidence_gap("o/r", 7, self._SURFACES) is True


class TestBackfilledSurfacesActuallyGate:
    # Issue #337: the fleet's other frontends were undeclared, so this gate had never fired
    # outside agentflow. These are the exact shapes the backfill measured before it landed.
    _UI_FILES = ["frontend/diagnose.js", "frontend/diagnose.test.js",
                 "analysis_engine/analyzers/threshold.py"]

    def _gap(self, monkeypatch, surfaces, files, *, body=""):
        content = github.PrContent(body=body, paths=tuple(files), comments=[])
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: content)
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
        monkeypatch.setattr(gate.github, "pr_content", explode)
        assert ui_evidence_gap("o/r", 476, []) is False


class TestPinnedMutationGate:
    # Issue #735: a merged edit to a digest-pinned file bricks the repo's own enrollment, so the
    # mutation must fail a blocking check before merge. The one sanctioned way through is the
    # owning repo's deliberate re-pin: the pinned bytes and the recorded digest move in one PR.

    def test_touching_the_pinned_harness_in_an_enrolled_repo_is_a_gap(self):
        assert pinned_path_mutation(
            ["scripts/screenshots.mjs", "src/app.py"], owns_pin_manifest=False) is True

    def test_a_repin_shaped_pr_outside_the_owner_repo_is_still_a_gap(self):
        # Only the manifest's own repo may re-pin; a look-alike manifest path elsewhere is inert.
        assert pinned_path_mutation(
            ["scripts/screenshots.mjs", "agentflow/capabilities.toml"],
            owns_pin_manifest=False) is True

    def test_the_owners_lockstep_repin_passes(self):
        assert pinned_path_mutation(
            ["scripts/screenshots.mjs", "agentflow/capabilities.toml"],
            owns_pin_manifest=True) is False

    def test_the_owner_moving_pinned_bytes_without_the_digest_is_a_gap(self):
        # Half a re-pin is a broken enrollment for every enrolled repo: block it too.
        assert pinned_path_mutation(
            ["scripts/screenshots.mjs"], owns_pin_manifest=True) is True

    def test_a_repo_local_extension_never_trips_the_gate(self):
        # The sanctioned seam: the extension file and its declaration, pinned bytes untouched.
        assert pinned_path_mutation(
            ["scripts/screenshots.local.mjs", "AGENTS.md"], owns_pin_manifest=False) is False

    def test_an_unreadable_pr_defers_rather_than_parks(self, monkeypatch):
        # None defers settlement to a re-drive (like the head check gate) — a transient gh
        # failure must neither park the PR nor let it through.
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: None)
        assert pinned_mutation_gap("o/r", 7) is None

    def test_the_live_gap_reads_the_prs_changed_files(self, monkeypatch):
        # Path membership alone is a candidate gap, not the verdict: the digest read below is what
        # actually confirms these are repo-local tampering rather than a legitimate re-addition.
        content = github.PrContent(
            body="", paths=("scripts/screenshots.mjs",), comments=[])
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: content)
        monkeypatch.setattr(gate, "_owns_pin_manifest", lambda repo: False)
        monkeypatch.setattr(gate.github, "pr_facts",
                            lambda *a: github.PrFacts("branch", "deadbeef", "OPEN", ()))
        monkeypatch.setattr(gate, "_pinned_digests", lambda: frozenset({"the-canonical-digest"}))
        monkeypatch.setattr(gate.github, "file_at_ref", lambda *a: b"repo-local tampering")
        assert pinned_mutation_gap("o/r", 7) is True

    def test_a_pr_that_adds_back_exactly_the_canonical_bytes_is_not_a_gap(self, monkeypatch):
        # screenshot_crib.py tells every session without a copy of the harness to port agentflow's
        # own in at exactly this path — that PR's changed-file set touches the pinned path, but it
        # ships the canonical bytes and must not be parked as a mutation (#735).
        canonical = b"canonical harness bytes"
        content = github.PrContent(
            body="", paths=("scripts/screenshots.mjs",), comments=[])
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: content)
        monkeypatch.setattr(gate, "_owns_pin_manifest", lambda repo: False)
        monkeypatch.setattr(gate.github, "pr_facts",
                            lambda *a: github.PrFacts("branch", "deadbeef", "OPEN", ()))
        monkeypatch.setattr(
            gate, "_pinned_digests",
            lambda: frozenset({hashlib.sha256(canonical).hexdigest()}))
        monkeypatch.setattr(gate.github, "file_at_ref", lambda *a: canonical)
        assert pinned_mutation_gap("o/r", 7) is False

    def test_a_pr_that_restores_a_known_old_pin_is_not_a_gap(self, monkeypatch):
        # A digest the manifest once canonically held (before a deliberate re-pin) is also not a
        # mutation — the harness's own drift-repair path can land one of these too.
        old_bytes = b"a previously-canonical harness revision"
        content = github.PrContent(
            body="", paths=("scripts/screenshots.mjs",), comments=[])
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: content)
        monkeypatch.setattr(gate, "_owns_pin_manifest", lambda repo: False)
        monkeypatch.setattr(gate.github, "pr_facts",
                            lambda *a: github.PrFacts("branch", "deadbeef", "OPEN", ()))
        monkeypatch.setattr(
            gate, "_pinned_digests",
            lambda: frozenset({"unrelated-current-digest", hashlib.sha256(old_bytes).hexdigest()}))
        monkeypatch.setattr(gate.github, "file_at_ref", lambda *a: old_bytes)
        assert pinned_mutation_gap("o/r", 7) is False

    def test_unreadable_bytes_at_the_pr_head_defer_rather_than_park(self, monkeypatch):
        content = github.PrContent(
            body="", paths=("scripts/screenshots.mjs",), comments=[])
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: content)
        monkeypatch.setattr(gate, "_owns_pin_manifest", lambda repo: False)
        monkeypatch.setattr(gate.github, "pr_facts",
                            lambda *a: github.PrFacts("branch", "deadbeef", "OPEN", ()))
        monkeypatch.setattr(gate, "_pinned_digests", lambda: frozenset({"anything"}))
        monkeypatch.setattr(gate.github, "file_at_ref", lambda *a: None)
        assert pinned_mutation_gap("o/r", 7) is None

    def test_an_unreadable_head_sha_defers_rather_than_parks(self, monkeypatch):
        content = github.PrContent(
            body="", paths=("scripts/screenshots.mjs",), comments=[])
        monkeypatch.setattr(gate.github, "pr_content", lambda *a: content)
        monkeypatch.setattr(gate, "_owns_pin_manifest", lambda repo: False)
        monkeypatch.setattr(gate.github, "pr_facts", lambda *a: None)
        assert pinned_mutation_gap("o/r", 7) is None

    # -- _owns_pin_manifest itself: the sanctioned owner-repo path through the gate above was
    # previously exercised only through the pure predicate (owns_pin_manifest injected as a bool)
    # or monkeypatched away entirely. These drive the live function through its own seam.

    def test_owns_pin_manifest_is_true_for_the_packaged_repository(self, monkeypatch):
        monkeypatch.setattr("agentflow.provider_skills._github_repository",
                            lambda _root: "o/r")
        assert gate._owns_pin_manifest("o/r") is True
        assert gate._owns_pin_manifest("O/R") is True  # case-insensitive, like a GitHub slug

    def test_owns_pin_manifest_is_false_for_another_repository(self, monkeypatch):
        monkeypatch.setattr("agentflow.provider_skills._github_repository",
                            lambda _root: "o/r")
        assert gate._owns_pin_manifest("someone-else/other-repo") is False

    def test_owns_pin_manifest_is_false_when_the_package_repository_is_unreadable(
            self, monkeypatch):
        """A non-editable/site-packages install of agentflow has no git checkout behind the
        running package, so `provider_skills._github_repository` returns "" (`git rev-parse` finds
        no work tree there). Nothing in the code enforces that agentflow always runs from an
        editable checkout of its own repository when it evaluates its own PRs, so this is
        reachable, not a state ruled out by an invariant: under it, even the manifest-owning
        repo's own lockstep re-pin PR would be treated as a non-owner mutation and parked. This
        pins today's fail-closed behavior rather than papering over it; changing it is a separate
        decision."""
        monkeypatch.setattr("agentflow.provider_skills._github_repository", lambda _root: "")
        assert gate._owns_pin_manifest("o/r") is False


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
    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: [])
    gate.park("o/r", 99, verdict, reason="exhausted its review budget without a durable verdict")
    assert captured, "park() did not post a PR comment"
    return captured[0]


def test_park_comment_has_a_bounded_two_section_envelope_and_updates_in_place(monkeypatch):
    long = "unbounded-test-log-" * 200
    comments = []
    context = gate.ParkContext(
        behavior=long, options=(long, long, "excess option"), consequences=long,
        recommendation=long, locations=(long, long, "excess location"), conflicts=long,
        checks=(long, "excess check"), retained_work=long, next_action=long)
    verdict = Verdict(
        clean=False, actions=(ReviewFinding(ReviewAction.FIX, long, long, long, 1),),
        fixes=(long,), follow_ups=(FollowUp(evidence=long, desired_outcome=long),))
    marker = "agentflow-park:" + "a" * 20

    monkeypatch.setattr(gate.github, "pr_comments", lambda *_args: list(comments))
    monkeypatch.setattr(gate.github, "pr_comment", lambda _repo, _pr, body:
                        comments.append(github.Comment(body=body, created_at="", id="park")) or True)
    monkeypatch.setattr(gate.github, "edit_comment", lambda _id, body:
                        comments.__setitem__(0, github.Comment(body=body, created_at="", id="park")) or True)

    gate.park("o/r", 99, verdict, context=context, proof_marker=marker)
    gate.park("o/r", 99, verdict, context=context, proof_marker=marker)

    assert len(comments) == 1
    body = comments[0].body
    assert len(body) <= 2_000
    assert marker in body
    assert "## Action needed" in body and "## Agent handoff" in body
    assert "Exact next action:" in body
    assert long not in body and "excess option" not in body and "excess location" not in body


def test_no_verdict_park_says_no_review_was_completed(monkeypatch):
    # Fails before the fix: exhaustion park posted '(no blocking findings)' instead.
    body = _park_body(monkeypatch, None)
    assert "(no blocking findings)" not in body
    assert "No review was completed" in body
    assert "## Action needed" in body
    assert "## Maintainer decision needed" not in body


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


def test_findings_verdict_park_omits_unbounded_finding_lists(monkeypatch):
    verdict = Verdict(clean=False, findings=(Finding("blocking", "something bad", "f.py", 10),))
    body = _park_body(monkeypatch, verdict)
    assert "something bad" not in body
    assert "fix_before_completion" not in body
    assert "No review was completed" not in body


def test_real_product_uncertainty_keeps_the_maintainer_decision_heading(monkeypatch):
    verdict = Verdict(
        clean=False,
        actions=(ReviewFinding(
            ReviewAction.ASK, "Choose the launch behavior", "Product intent is unresolved.",
            "agentflow/launch.py", 10),))

    body = _park_body(monkeypatch, verdict)

    assert "## Maintainer decision needed" in body


def test_reviewed_park_shows_one_proposal_not_fixes_or_filed_issue_urls(monkeypatch):
    verdict = Verdict(
        clean=True, fixes=("Removed the stale helper",),
        follow_ups=(FollowUp("browser proof is absent", "add browser proof"),),
        follow_up_issues=("https://github.com/o/r/issues/12",))
    body = _park_body(monkeypatch, verdict)
    assert "Proposed follow-up:" in body and "add browser proof" in body
    assert "Removed the stale helper" not in body and "issues/12" not in body


def test_agentflow_skill_reads_the_current_four_action_park_contract(monkeypatch):
    verdict = Verdict(clean=False, actions=tuple(
        ReviewFinding(action, action.value, "grounded")
        for action in ReviewAction))
    body = _park_body(monkeypatch, verdict)
    skill = Path("skills/agentflow/SKILL.md").read_text()

    for action in ReviewAction:
        assert f"`{action.value}`" in skill
        assert action.value not in body
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
    # The park comment is written once; the ping is at-least-once by design, so a repeat pass
    # re-tells the operator rather than risking never having told them (ADR 0042).
    assert len(edits) == 1 and len(notifications) == 2

    assert run("review-2", "new reason") == subject.url
    assert len(comments) == 1 and posts == []
    assert len(edits) == 2 and len(notifications) == 3
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

    assert gate.post_clean_review_summary("o/r", 9, verdict, "sha-a") is True
    assert gate.post_clean_review_summary("o/r", 9, verdict, "sha-a") is True
    assert len(comments) == 1
    assert "Targeted" in comments[0].body and "affected tests passed" in comments[0].body
    assert "cross-tool review" in comments[0].body


def test_clean_summary_labels_one_proposal_and_only_labels_legacy_urls_as_historical(monkeypatch):
    comments = []
    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr(
        gate.github, "pr_comment",
        lambda _repo, _pr, body: comments.append(github.Comment(body=body, created_at="")) or True)
    verdict = Verdict(
        clean=True, reviewer_tool="codex", change_author_tool="claude",
        follow_ups=(FollowUp("the browser proof is absent", "add browser proof"),),
        follow_up_issues=("https://github.com/o/r/issues/12",))

    assert gate.post_clean_review_summary("o/r", 9, verdict, "sha-a") is True
    body = comments[0].body
    assert "Proposed follow-up:" in body
    assert "Desired outcome: add browser proof" in body
    assert "Evidence: the browser proof is absent" in body
    assert "Historical follow-up reference:" in body and "issues/12" in body
    assert "filed" not in body.lower()


def test_clean_summary_states_exact_same_tool_human_merge_status(monkeypatch):
    comments = []
    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr(
        gate.github, "pr_comment",
        lambda _repo, _pr, body: comments.append(github.Comment(body=body, created_at="")) or True)
    verdict = Verdict(
        clean=True, reviewer_tool="claude", change_author_tool="claude",
        depth_reason="reviewed fallback", checks=("affected checks passed",))

    assert gate.post_clean_review_summary("o/r", 9, verdict, "sha-a") is True
    assert "same-tool review; maintainer merge required" in comments[0].body


def test_clean_summary_replaces_stale_same_tool_status_without_duplicate_marker(monkeypatch):
    marker = "<!-- agentflow-clean-review-summary -->"
    comments = [github.Comment(
        body=f"> *agentflow: clean review.*\n{marker}\n"
             "<!-- agentflow-clean-review-head:sha-a -->\n\n"
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

    assert gate.post_clean_review_summary("o/r", 9, verdict, "sha-a") is True
    assert comments[0].body.count(marker) == 1
    assert "Review status: cross-tool review." in comments[0].body
    assert "same-tool review; maintainer merge required" not in comments[0].body


def test_clean_summary_at_a_new_head_preserves_and_retires_the_old_evidence(monkeypatch):
    marker = "<!-- agentflow-clean-review-summary -->"
    comments = [github.Comment(
        body=f"{marker}\n<!-- agentflow-clean-review-head:sha-a -->\n\nOutcome: clean.",
        created_at="", id="old")]
    posts = []
    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: list(comments))
    def post(_repo, _pr, body):
        posts.append(body)
        comments.append(github.Comment(body, "", id="new"))
        return True

    monkeypatch.setattr(gate.github, "pr_comment", post)
    monkeypatch.setattr(gate.github, "edit_comment", lambda comment_id, body:
                        comments.__setitem__(0, github.Comment(body, "", id=comment_id)) or True)

    assert gate.post_clean_review_summary("o/r", 9, CLEAN, "sha-b") is True
    assert "agentflow-superseded-review-summary" in comments[0].body
    assert "sha-a" in comments[0].body
    assert len(posts) == 1 and "sha-b" in posts[0]


def test_live_clean_review_reads_the_hand_off_and_stops_reading_it_once_retired():
    """"Finished, it's yours" is the current summary and nothing else: a retired one is rewritten
    in place, so it stops counting without a second fact to keep in step."""
    marker = "<!-- agentflow-clean-review-summary -->"
    retired = "<!-- agentflow-superseded-review-summary -->"
    live = [{"body": "a build note", "createdAt": "2026-07-30T08:00:00Z"},
            {"body": f"> *agentflow: clean review.*\n{marker}\n\nOutcome: clean.",
             "createdAt": "2026-07-30T09:00:00Z"}]
    assert gate.live_clean_review(live)["createdAt"] == "2026-07-30T09:00:00Z"
    taken_back = [{"body": f"{retired}\n\nOutcome: clean.", "createdAt": "2026-07-30T09:00:00Z"}]
    assert gate.live_clean_review(taken_back) is None
    assert gate.live_clean_review([]) is None


def test_supersede_clean_review_retires_the_hand_off_and_keeps_its_evidence(monkeypatch):
    marker = "<!-- agentflow-clean-review-summary -->"
    rows = [{"id": "clean", "createdAt": "",
             "body": f"{marker}\n<!-- agentflow-clean-review-head:sha-a -->"}]
    monkeypatch.setattr(gate.github, "pr_comments",
                        lambda *_args: pytest.fail("the caller already holds the comments"))
    monkeypatch.setattr(gate.github, "edit_comment", lambda comment_id, body:
                        rows[0].__setitem__("body", body) or True)
    monkeypatch.setattr(gate.github, "pr_comment",
                        lambda *_args: pytest.fail("retiring a summary posts nothing"))

    assert gate.supersede_clean_review(rows) is True
    assert "agentflow-superseded-review-summary" in rows[0]["body"]
    assert "sha-a" in rows[0]["body"]
    assert gate.live_clean_review(rows) is None


def test_supersede_clean_review_attempts_every_live_summary_even_after_a_failed_edit(monkeypatch):
    """Two hand-off summaries, the first un-editable: the second must still be retired, and the
    whole retirement must report failure so the caller can say so or retry. Fails first against a
    short-circuiting retirement, which never reaches the second summary."""
    marker = "<!-- agentflow-clean-review-summary -->"
    rows = [{"id": "first", "body": f"{marker}\nhead one"},
            {"id": "second", "body": f"{marker}\nhead two"},
            {"id": "chatter", "body": "just a build note"}]
    attempted = []

    def edit(comment_id, body):
        attempted.append(comment_id)
        if comment_id == "first":
            return False
        rows[1]["body"] = body
        return True

    monkeypatch.setattr(gate.github, "edit_comment", edit)

    assert gate.supersede_clean_review(rows) is False
    assert attempted == ["first", "second"]
    assert "agentflow-superseded-review-summary" in rows[1]["body"]
    assert rows[0]["body"].count(marker) == 1, "the un-edited summary is still live"


def test_park_retires_the_current_clean_summary_before_posting(monkeypatch):
    marker = "<!-- agentflow-clean-review-summary -->"
    comments = [github.Comment(body=f"{marker}\n<!-- agentflow-clean-review-head:sha-a -->",
                               created_at="", id="clean")]
    posts = []
    monkeypatch.setattr(gate.github, "pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr(gate.github, "edit_comment", lambda comment_id, body:
                        comments.__setitem__(0, github.Comment(body, "", id=comment_id)) or True)
    monkeypatch.setattr(gate.github, "pr_comment", lambda *_args: posts.append(_args) or True)

    gate.park("o/r", 9, None)
    assert "agentflow-superseded-review-summary" in comments[0].body
    assert len(posts) == 1
