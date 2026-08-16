"""Reconcile held records whose GitHub subjects have definitively resolved."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from conftest import FakeSession, record_of, starts_until_held

from agentflow import dispatch, github, pipeline
from agentflow.coordinator import Submission
from agentflow.coordinator.providers import ProviderCause
from agentflow.loop import RepoConfig


def _held_record(make_coord, *, stage: str, subject: str = "7"):
    fake = FakeSession()
    lines = []
    coord = make_coord(fake, log=lines.append)
    identity = coord.submit_stage(Submission(
        repo="o/r", subject=subject, stage=stage, pool="claude", source="/held-worktree"))
    starts_until_held(coord, fake, identity, "claude", ProviderCause.PROCESS)
    assert record_of(coord, identity).state == "held"
    return coord, identity, lines


def _only_held_sweep(monkeypatch):
    """Keep this production reconciliation seam focused on the held-record sweep."""
    from agentflow import coordinated_intake, coordinated_review, coordinated_revise

    monkeypatch.setattr(coordinated_review, "_resume_tainted_reviews", lambda _coord: None)
    monkeypatch.setattr(coordinated_review, "_resettle_diverged_reviews", lambda _coord: None)
    monkeypatch.setattr(coordinated_revise, "_retire_dead_revises", lambda _coord: None)
    monkeypatch.setattr(coordinated_intake, "_retire_dead_intakes", lambda _coord: None)
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *args, **kwargs: None)


def _full_held_sweep(coord, monkeypatch):
    """Run the held sweep through the full dispatch-pass interface."""
    _only_held_sweep(monkeypatch)
    monkeypatch.setattr(dispatch, "_refresh_claude_quota", lambda _log: None)
    monkeypatch.setattr(pipeline, "reconcile_orphaned_claims", lambda *args, **kwargs: None)
    dispatch.run_cycle([RepoConfig("o/r", "/held-worktree")], coordinator=coord,
                       submit_new=False)


def test_a_closed_held_issue_retires_and_is_audited(make_coord, monkeypatch):
    """A parked Build on a closed issue disappears from the operator's held work."""
    from agentflow.labels import BUILDING

    coord, identity, lines = _held_record(make_coord, stage="build")
    labels = {BUILDING, "agentflow:needs-grilling"}
    removed = []
    monkeypatch.setattr(github, "issue_state", lambda repo, number: "CLOSED")
    monkeypatch.setattr(github, "issue_labels", lambda repo, number: frozenset(labels))

    def remove_label(repo, number, label):
        removed.append((repo, number, label))
        labels.remove(label)
        return True

    monkeypatch.setattr(github, "remove_label", remove_label)
    _full_held_sweep(coord, monkeypatch)

    record = record_of(coord, identity)
    assert record.retired is True and record.claim is False
    assert removed == [("o/r", 7, BUILDING), ("o/r", 7, "agentflow:needs-grilling")]
    assert any("o/r: 7: build:" in line and "subject was closed" in line for line in lines)


def test_an_open_held_issue_remains_for_the_operator(make_coord, monkeypatch):
    """A human handoff stays visible while its issue is still actionable."""
    coord, identity, _lines = _held_record(make_coord, stage="build")
    monkeypatch.setattr(github, "issue_state", lambda repo, number: "OPEN")
    monkeypatch.setattr(github, "issue_labels", lambda *args: pytest.fail("must not release"))
    _full_held_sweep(coord, monkeypatch)

    assert record_of(coord, identity).retired is False


def test_an_unreadable_held_issue_remains_for_the_operator(make_coord, monkeypatch):
    """An unknown GitHub state is never treated as a closed issue."""
    coord, identity, _lines = _held_record(make_coord, stage="build")
    monkeypatch.setattr(github, "issue_state", lambda repo, number: None)
    monkeypatch.setattr(github, "issue_labels", lambda *args: pytest.fail("must not release"))
    _full_held_sweep(coord, monkeypatch)

    assert record_of(coord, identity).retired is False


def test_pr_bound_holds_use_the_pr_state_not_an_issue_state(make_coord, monkeypatch):
    """GitHub's shared numbering cannot make a closed issue retire an open PR handoff."""
    closed_coord, closed, _lines = _held_record(make_coord, stage="review", subject="42")
    open_coord, open_record, _lines = _held_record(make_coord, stage="respond", subject="43")
    pr_states = {42: "CLOSED", 43: "OPEN"}
    checked = []
    monkeypatch.setattr(github, "pr_state",
                        lambda repo, number: checked.append((repo, number)) or pr_states[number])
    monkeypatch.setattr(github, "issue_state",
                        lambda *args: pytest.fail("PR-bound hold used issue_state"))
    _full_held_sweep(closed_coord, monkeypatch)

    assert record_of(closed_coord, closed).retired is True
    assert record_of(open_coord, open_record).retired is False
    assert checked == [("o/r", 42), ("o/r", 43)]


def test_a_closed_hold_with_an_unprovable_claim_release_waits(make_coord, monkeypatch):
    """A closed subject cannot erase a held record until its visible claim is proven gone."""
    from agentflow.labels import BUILDING

    failure = SimpleNamespace(
        ready=False, stage="build", provider="claude", contracts=(), state="missing",
        evidence="", repair_command="repair")
    fake = FakeSession()
    coord = make_coord(fake, capability_preflight=lambda record, materialize: failure)
    identity = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="build", pool="claude", source="/held-worktree"))
    assert coord.cycle("claude") == []
    held = record_of(coord, identity)
    assert held.state == "held" and held.claim is True

    monkeypatch.setattr(github, "issue_state", lambda repo, number: "CLOSED")
    monkeypatch.setattr(github, "issue_labels", lambda repo, number: frozenset({BUILDING}))
    monkeypatch.setattr(github, "remove_label", lambda repo, number, label: False)
    _full_held_sweep(coord, monkeypatch)

    held = record_of(coord, identity)
    assert held.retired is False and held.claim is True


def test_direct_pipeline_reconciliation_never_reads_held_subjects(make_coord, monkeypatch):
    """Only the daemon's full dispatch pass pays for held-subject reconciliation."""
    coord, _identity, _lines = _held_record(make_coord, stage="build")
    monkeypatch.setattr(github, "issue_state",
                        lambda *args: pytest.fail("held sweep ran outside a full pass"))
    _only_held_sweep(monkeypatch)

    pipeline.reconcile_and_project(coord)
