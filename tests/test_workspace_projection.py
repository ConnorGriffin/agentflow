"""The bounded workspace read model for Build-Issue Proposals (ADR 0033), assembled by
:func:`agentflow.coordinated_converse.build_projection` over a real store.

The console renders the copper "awaiting your decision" weight and the "published ✓" weight from
this projection, so it must surface every staged version and its hash, the exact hash awaiting
decision, and — once published — the verified receipt. A discarded Proposal is not in the read
model at all.
"""

from __future__ import annotations

import pytest

from agentflow import coordinated_converse
from agentflow.workspace.store import WorkspaceStore


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    return tmp_path


REPO = "ConnorGriffin/agentflow"
CFG = type("Cfg", (), {"repo": REPO, "profile": "reviewed", "workdir": "/tmp"})()


def _project(projection):
    return projection["projects"][0]


def test_a_staged_proposal_surfaces_its_versions_and_the_latest_awaiting_decision(state):
    store = WorkspaceStore(REPO)
    try:
        store.open_conversation(title="Ask", conversation_id="c1", idempotency_key="o", now=1)
        store.stage_proposal("c1", title="Draft one", summary="s1", acceptance=["a"], body="",
                             content_hash="sha256:v1", idempotency_key="s1", now=10)
        store.stage_proposal("c1", title="Draft two", summary="s2", acceptance=["a", "b"], body="",
                             content_hash="sha256:v2", idempotency_key="s2", now=20)
    finally:
        store.close()
    proj = _project(coordinated_converse.build_projection([CFG], now=99))
    assert len(proj["proposals"]) == 1
    prop = proj["proposals"][0]
    assert prop["state"] == "staged" and prop["version"] == 2 and prop["title"] == "Draft two"
    assert prop["content_hash"] == "sha256:v2"                 # the copper card shows the latest
    assert [v["content_hash"] for v in prop["versions"]] == ["sha256:v1", "sha256:v2"]
    assert [v["current"] for v in prop["versions"]] == [False, True]
    assert prop["publication"] is None


def test_a_published_proposal_carries_its_verified_receipt(state):
    store = WorkspaceStore(REPO)
    try:
        store.open_conversation(title="Ask", conversation_id="c1", idempotency_key="o", now=1)
        store.stage_proposal("c1", title="Draft", summary="s", acceptance=["a"], body="",
                             content_hash="sha256:v1", idempotency_key="s1", now=10)
        store.approve_proposal("c1", "sha256:v1", idempotency_key="a1", now=20)
        store.record_publication("c1", "sha256:v1", issue_number=42,
                                 issue_url="https://x/issues/42", now=30)
    finally:
        store.close()
    prop = _project(coordinated_converse.build_projection([CFG], now=99))["proposals"][0]
    assert prop["state"] == "published"
    assert prop["publication"]["issue_number"] == 42
    assert prop["publication"]["issue_url"] == "https://x/issues/42"


def test_a_discarded_proposal_is_absent_from_the_read_model(state):
    store = WorkspaceStore(REPO)
    try:
        store.open_conversation(title="Ask", conversation_id="c1", idempotency_key="o", now=1)
        store.stage_proposal("c1", title="Draft", summary="s", acceptance=["a"], body="",
                             content_hash="sha256:v1", idempotency_key="s1", now=10)
        store.discard_proposal("c1", idempotency_key="d1", now=20)
    finally:
        store.close()
    proj = _project(coordinated_converse.build_projection([CFG], now=99))
    assert proj["proposals"] == []                             # dropped from the shelf
    assert len(proj["conversations"]) == 1                     # the conversation remains
