"""The auto-merge gate's decision matrix — pure, so fully unit-tested.

The one thing that must never happen: MERGE without independent review + green CI
+ clean verdict.
"""

import subprocess
import time

import pytest

import agentflow.gate as gate
from agentflow.gate import (MergeDecision, ci_is_green, decide_merge,
                            has_image_evidence, touches_ui_surface)
from agentflow.reviewer import Finding, Verdict

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


def _fail(_cmd, **_kw):
    return subprocess.CompletedProcess(_cmd, returncode=1, stdout="", stderr="pending")


def _pass(_cmd, **_kw):
    return subprocess.CompletedProcess(_cmd, returncode=0, stdout="", stderr="")


def test_ci_poll_returns_false_at_deadline(monkeypatch):
    """Checks that never complete return False once the deadline expires."""
    monkeypatch.setattr(gate, "_run", _fail)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    assert ci_is_green("o/r", 1, timeout=0) is False


def test_ci_poll_returns_true_when_checks_pass(monkeypatch):
    """Checks that pass on the first poll return True immediately."""
    monkeypatch.setattr(gate, "_run", _pass)
    assert ci_is_green("o/r", 1, timeout=30, interval=1) is True


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
