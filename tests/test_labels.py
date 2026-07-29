"""The pipeline's label vocabulary: taking a lane's claim and proving it released."""

import pytest

from agentflow import github, labels
from agentflow.labels import TRIAGING


def test_release_triage_proves_the_claim_is_absent(monkeypatch):
    # The claim is present, the removal succeeds, and a re-read confirms it's gone.
    reads = iter((frozenset({"agentflow:triaging"}), frozenset()))
    monkeypatch.setattr(github, "issue_labels", lambda repo, n: next(reads))
    monkeypatch.setattr(github, "remove_label", lambda repo, n, label: True)

    assert labels.release("o/r", 69, TRIAGING) is True


def test_release_triage_fails_closed_when_github_still_reports_the_claim(monkeypatch):
    # The re-read still shows the claim — the release cannot be proven, so it fails closed.
    reads = iter((frozenset({"agentflow:triaging"}), frozenset({"agentflow:triaging"})))
    monkeypatch.setattr(github, "issue_labels", lambda repo, n: next(reads))
    monkeypatch.setattr(github, "remove_label", lambda repo, n, label: True)

    assert labels.release("o/r", 69, TRIAGING) is False


def test_release_triage_accepts_an_already_absent_claim(monkeypatch):
    reads = []
    monkeypatch.setattr(github, "issue_labels",
                        lambda repo, n: reads.append(n) or frozenset())
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, n, label: pytest.fail("nothing to remove"))

    assert labels.release("o/r", 69, TRIAGING) is True
    assert len(reads) == 1

# --- issue #159: agentflow:triaging label description must not overclaim a live session ----

def test_claim_triage_description_is_ownership_not_live_session(monkeypatch):
    # Regression: the old text "A grounding session is triaging this issue" asserted active
    # execution even when the record is waiting with 0 attempts and no provider process.
    # The new description must be true whether the record is queued or running.
    captured = []
    monkeypatch.setattr(github, "create_label",
                        lambda repo, name, color, description="":
                        captured.append(description) or True)
    monkeypatch.setattr(github, "add_label", lambda repo, n, label: True)
    result = labels.claim("o/r", 7, TRIAGING)

    assert result is True
    description = captured[0]
    # Must not assert a live/active/running session (the AC: no "is triaging" phrasing)
    assert "is triaging" not in description
    assert "A grounding session" not in description
    # Must convey ownership/admission purpose
    assert any(word in description.lower() for word in ("claim", "ownership", "dispatch"))
