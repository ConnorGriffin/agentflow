"""The auto-merge gate's decision matrix — pure, so fully unit-tested.

The one thing that must never happen: MERGE without independent review + green CI
+ clean verdict.
"""

import subprocess
import time

import pytest

import agentflow.gate as gate
from agentflow.gate import MergeDecision, ci_is_green, decide_merge
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
