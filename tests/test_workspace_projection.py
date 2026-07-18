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


def _publish(store, issue_number, *, hash_="sha256:v1"):
    store.open_conversation(title="Ask", conversation_id="c1", idempotency_key="o", now=1)
    store.stage_proposal("c1", title="Draft", summary="s", acceptance=["a"], body="",
                         content_hash=hash_, idempotency_key="s1", now=10)
    store.approve_proposal("c1", hash_, idempotency_key="a1", now=20)
    store.record_publication("c1", hash_, issue_number=issue_number,
                             issue_url=f"https://x/issues/{issue_number}", now=30)


def test_a_published_proposal_mirrors_coarse_pipeline_state(state):
    store = WorkspaceStore(REPO)
    try:
        _publish(store, 42)
    finally:
        store.close()
    # The daemon join reports the handed-off issue is building (no PR open yet).
    pipeline_for = lambda repo, issues: {42: {"state": "pr_open", "pr_number": 200,
                                              "pr_url": "https://x/pull/200"}}
    prop = _project(coordinated_converse.build_projection(
        [CFG], now=99, pipeline_for=pipeline_for))["proposals"][0]
    assert prop["pipeline"] == {"state": "pr_open", "pr_number": 200,
                                "pr_url": "https://x/pull/200"}
    assert prop["evidence"] is None                            # not merged → no landed evidence


def test_a_merged_published_proposal_projects_landed_acceptance_evidence(state):
    """The core loop-closing regression: a published proposal whose issue matches a merged
    agentflow PR projects a landed/evidence block — merged-PR link, commit, review/CI verdict —
    linked back to the originating Ask. Fails if the card regresses to the publish-only stub."""
    store = WorkspaceStore(REPO)
    try:
        _publish(store, 42)
    finally:
        store.close()
    pipeline_for = lambda repo, issues: {42: {
        "state": "merged", "pr_number": 200, "pr_url": "https://x/pull/200",
        "merged_at": "2026-07-17T00:00:00Z", "merge_commit": "abc1234def",
        "review": "approved", "ci": "passing"}}
    prop = _project(coordinated_converse.build_projection(
        [CFG], now=99, pipeline_for=pipeline_for))["proposals"][0]
    assert prop["pipeline"]["state"] == "merged"
    ev = prop["evidence"]
    assert ev is not None                                      # NOT the publish-only stub
    assert ev["merged_pr_url"] == "https://x/pull/200" and ev["pr_number"] == 200
    assert ev["merge_commit"] == "abc1234def"
    assert ev["review"] == "approved" and ev["ci"] == "passing"
    assert prop["conversation_id"] == "c1"                     # linked back to the Ask


def test_pipeline_absent_when_no_daemon_join(state):
    """The web layer never reads GitHub: with no join injected (the projection-serving path),
    a published card carries no pipeline/evidence and falls back to its publication receipt."""
    store = WorkspaceStore(REPO)
    try:
        _publish(store, 42)
    finally:
        store.close()
    prop = _project(coordinated_converse.build_projection([CFG], now=99))["proposals"][0]
    assert prop["pipeline"] is None and prop["evidence"] is None
    assert prop["publication"]["issue_number"] == 42          # receipt still shown, honestly aged


def test_a_failed_proposal_surfaces_its_reason_and_stays_on_the_shelf(state):
    """A parked publish failure must reach the console: the danger "Publish failed" weight reads
    ``state`` + the plain ``fail_reason`` + ``failed_at``, while the approval stays bound to its
    hash. A staged/approved/published Proposal carries no failure fields."""
    store = WorkspaceStore(REPO)
    try:
        store.open_conversation(title="Ask", conversation_id="c1", idempotency_key="o", now=1)
        store.stage_proposal("c1", title="Draft", summary="s", acceptance=["a"], body="",
                             content_hash="sha256:v1", idempotency_key="s1", now=10)
        store.approve_proposal("c1", "sha256:v1", idempotency_key="a1", now=20)
        store.fail_publication("c1", "sha256:v1",
                               reason="GitHub rejected the request — bad credentials.", now=30)
    finally:
        store.close()
    prop = _project(coordinated_converse.build_projection([CFG], now=99))["proposals"][0]
    assert prop["state"] == "failed"                           # danger weight, never copper
    assert prop["fail_reason"] == "GitHub rejected the request — bad credentials."
    assert prop["failed_at"] is not None
    assert prop["approved_hash"] == "sha256:v1"                # still bound so Retry re-publishes it
    assert prop["publication"] is None


def test_a_staged_proposal_carries_no_failure_fields(state):
    store = WorkspaceStore(REPO)
    try:
        store.open_conversation(title="Ask", conversation_id="c1", idempotency_key="o", now=1)
        store.stage_proposal("c1", title="Draft", summary="s", acceptance=["a"], body="",
                             content_hash="sha256:v1", idempotency_key="s1", now=10)
    finally:
        store.close()
    prop = _project(coordinated_converse.build_projection([CFG], now=99))["proposals"][0]
    assert prop["fail_reason"] is None and prop["failed_at"] is None


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
