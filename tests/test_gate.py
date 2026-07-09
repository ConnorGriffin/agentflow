"""The auto-merge gate's decision matrix — pure, so fully unit-tested.

The one thing that must never happen: MERGE without independent review + green CI
+ clean verdict.
"""

import pytest

from agentflow.gate import MergeDecision, decide_merge
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


def test_blocking_verdict_revises_once_then_parks():
    assert d(verdict=DIRTY, revises_used=0) is MergeDecision.REVISE
    assert d(verdict=DIRTY, revises_used=1) is MergeDecision.PARK


def test_red_ci_revises_once_then_parks():
    assert d(ci_green=False, revises_used=0) is MergeDecision.REVISE
    assert d(ci_green=False, revises_used=1) is MergeDecision.PARK


def test_independence_dominates_even_a_clean_green_pr():
    # A same-tool review of an otherwise-perfect PR still must not auto-merge.
    assert d(reviewer_tool="claude", verdict=CLEAN, ci_green=True) is MergeDecision.PARK


@pytest.mark.parametrize("revises_used", [0, 1, 2])
def test_never_merges_without_independence(revises_used):
    assert d(reviewer_tool="claude", revises_used=revises_used) is not MergeDecision.MERGE
