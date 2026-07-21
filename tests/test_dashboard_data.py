from agentflow import github
from agentflow.github import IssueRow, PipelinePrRow, SnapshotPrRow

import agentflow.dashboard_data as dd
from agentflow.dashboard_data import (
    _CONFLICT_MARK,
    _UI_GAP_REASON,
    _complexity_of,
    _effort_of,
    park_reason,
    pr_stage,
)
from agentflow.gate import _RESPOND_PARK_TARGET_RE


def test_complexity_of_labels():
    assert _complexity_of([{"name": "ready-for-agent"},
                           {"name": "agentflow:complexity:standard"}]) == "standard"
    assert _complexity_of([{"name": "agentflow:complexity:deep"}]) == "deep"
    assert _complexity_of([{"name": "bug"}]) is None
    assert _complexity_of([]) is None


def test_effort_of_labels():
    assert _effort_of([{"name": "agentflow:effort:extra"}]) == "extra"
    assert _effort_of([{"name": "bug"}]) is None


def test_pr_stage_from_branch():
    assert pr_stage("agentflow/claude/issue-1-foo") == "claude"
    assert pr_stage("agentflow/codex/issue-2-bar") == "codex"
    assert pr_stage("some-human-branch") == "other"
    assert pr_stage("") == "other"


# --- park-reason classifier (issue #71) — the four markers the pipeline posts ----------

def _park(reason: str) -> dict:
    return {"body": f"> *agentflow: parked for human review.*\n\nThis PR {reason}. Findings:\n- x"}


def test_park_reason_classifies_all_four_markers():
    assert park_reason([_park("is a `reviewed` repo — a human merges")]) == "drop-to-reviewed"
    assert park_reason([_park(_UI_GAP_REASON)]) == "ui-evidence"
    assert park_reason([_park("could not be squash-merged (branch protection)")]) == "failed-merge"
    assert park_reason([{"body": f"> *{_CONFLICT_MARK}.*\n\nmain moved"}]) == "failed-merge"
    # An unanswered maintainer question is the freshest word, even over an earlier park.
    assert park_reason([_park("is a `reviewed` repo — a human merges"),
                        {"body": "what about the edge case?"}]) == "open-question"


def test_park_reason_none_when_not_parked():
    assert park_reason([]) is None
    assert park_reason([{"body": "a build note — agentflow: marker, our own word"}]) is None


# --- repo_view derivation through the interface (issue #71) -----------------------------

# One repo's worth of live GitHub state: a ready issue (with effort), two held issues, four
# parked PRs (one per reason) + one still-building PR that is NOT parked.
_HELD_ROWS = {
    "agentflow:needs-grilling": [
        IssueRow(number=10, title="held: real fork", body="", labels=frozenset(),
                 updated_at="2026-07-13T10:00:00Z"),
    ],
    "agentflow:needs-mockup": [
        IssueRow(number=11, title="held: needs a mockup", body="", labels=frozenset(),
                 updated_at="2026-07-13T11:00:00Z"),
    ],
}
_OPEN_PR_ROWS = [
    SnapshotPrRow(number=20, title="drop pr", head_ref_name="agentflow/claude/issue-1", merged_at=None),
    SnapshotPrRow(number=21, title="merge-fail pr", head_ref_name="agentflow/codex/issue-2", merged_at=None),
    SnapshotPrRow(number=22, title="question pr", head_ref_name="agentflow/claude/issue-3", merged_at=None),
    SnapshotPrRow(number=23, title="ui-gap pr", head_ref_name="agentflow/codex/issue-4", merged_at=None),
    SnapshotPrRow(number=24, title="still building", head_ref_name="agentflow/claude/issue-5", merged_at=None),
]
_COMMENTS = {
    20: [_park("is a `reviewed` repo — a human merges") | {"createdAt": "2026-07-13T09:00:00Z"}],
    21: [_park("could not be squash-merged (branch protection)") | {"createdAt": "2026-07-13T08:00:00Z"}],
    22: [_park("is a `reviewed` repo — a human merges") | {"createdAt": "2026-07-13T07:00:00Z"},
         {"body": "one more question?", "createdAt": "2026-07-13T07:30:00Z"}],
    23: [_park(_UI_GAP_REASON) | {"createdAt": "2026-07-13T06:00:00Z"}],
    24: [{"body": "> *agentflow: reviewing…*\n\nno block yet", "createdAt": "2026-07-13T05:00:00Z"}],
}

_READY_ROW = IssueRow(number=1, title="ready one", body="", labels=frozenset([
    "agentflow:complexity:standard", "agentflow:effort:heavy",
]))


def _patch(monkeypatch, held=None):
    held_rows = _HELD_ROWS if held is None else held

    def fake_list_issues(repo, *, label=None, limit=100):
        if label == "ready-for-agent":
            return [_READY_ROW]
        return held_rows.get(label, [])

    def fake_list_prs(repo, state, *, limit=30):
        return _OPEN_PR_ROWS if state == "open" else []

    monkeypatch.setattr(github, "list_issues", fake_list_issues)
    monkeypatch.setattr(github, "list_prs", fake_list_prs)
    monkeypatch.setattr(dd, "_pr_comments", lambda repo, n: _COMMENTS.get(n, []))
    monkeypatch.setattr(dd, "repo_profile", lambda workdir: "reviewed")
    monkeypatch.setattr(dd.ratchet, "status",
                        lambda repo: {"samples": 0, "correction_rate": 0, "ready_to_loosen": False})


def test_repo_view_derives_held_and_parked(monkeypatch):
    _patch(monkeypatch)
    from types import SimpleNamespace
    view = dd.repo_view(SimpleNamespace(repo="o/app", workdir="/tmp/app"))

    # effort rides alongside complexity on ready issues.
    assert view["ready"][0]["complexity"] == "standard"
    assert view["ready"][0]["effort"] == "heavy"

    # held: both states, with human-readable reason + since.
    held = {h["number"]: h for h in view["held"]}
    assert held[10]["state"] == "needs-grilling" and held[10]["since"] == "2026-07-13T10:00:00Z"
    assert held[11]["state"] == "needs-mockup"
    assert held[10]["reason"] and held[11]["reason"]

    # parked: one row per PR that carries a park marker, each classified + attributed.
    parked = {p["number"]: p for p in view["parked"]}
    assert set(parked) == {20, 21, 22, 23}          # #24 is still building → not parked
    assert parked[20]["reason"] == "drop-to-reviewed"
    assert parked[21]["reason"] == "failed-merge"
    assert parked[22]["reason"] == "open-question"
    assert parked[23]["reason"] == "ui-evidence"
    assert parked[20]["builder"] == "claude" and parked[20]["reviewer"] == "codex"
    assert parked[21]["builder"] == "codex" and parked[21]["reviewer"] == "claude"
    assert parked[23]["since"] == "2026-07-13T06:00:00Z"


def test_held_and_parked_leave_when_resolved(monkeypatch):
    # Label removed → no held issues; every PR merged/closed → nothing open to park.
    _patch(monkeypatch, held={})
    monkeypatch.setattr(dd, "_prs", lambda repo, state: [])
    from types import SimpleNamespace
    view = dd.repo_view(SimpleNamespace(repo="o/app", workdir="/tmp/app"))
    assert view["held"] == []
    assert view["parked"] == []


# --- workspace pipeline join (issue #155): coarse state + landed evidence per published issue ---

def test_issue_of_branch_recovers_the_issue_number():
    assert dd.issue_of_branch("agentflow/claude/issue-155-project-workspace") == 155
    assert dd.issue_of_branch("agentflow/codex/issue-7") == 7
    assert dd.issue_of_branch("agentflow/claude/hotfix") is None     # no issue-<N> segment
    assert dd.issue_of_branch("some-human-branch") is None
    assert dd.issue_of_branch("") is None


def _open_pr(number, head_ref, *, review_decision=None) -> PipelinePrRow:
    return PipelinePrRow(number=number, title="", head_ref_name=head_ref, url=f"u/{number}",
                         merged_at=None, merge_commit_oid=None,
                         review_decision=review_decision, ci_rollup=[])


def _merged_pr(number, head_ref, *, merge_commit_oid=None, review_decision=None,
               ci_rollup=None) -> PipelinePrRow:
    return PipelinePrRow(number=number, title="", head_ref_name=head_ref, url=f"u/{number}",
                         merged_at="2026-07-17T00:00:00Z",
                         merge_commit_oid=merge_commit_oid,
                         review_decision=review_decision,
                         ci_rollup=ci_rollup or [])


def test_pipeline_state_building_when_no_pr_yet():
    pipe = dd.pipeline_state(155, open_prs=[], merged_prs=[])
    assert pipe["state"] == "building" and pipe["pr_number"] is None and pipe["pr_url"] is None


def test_pipeline_state_pr_open_and_in_review():
    pipe = dd.pipeline_state(155, open_prs=[_open_pr(200, "agentflow/claude/issue-155-x")],
                             merged_prs=[])
    assert pipe["state"] == "pr_open" and pipe["pr_number"] == 200 and pipe["pr_url"] == "u/200"

    pipe = dd.pipeline_state(155,
                             open_prs=[_open_pr(200, "agentflow/claude/issue-155-x",
                                                review_decision="APPROVED")],
                             merged_prs=[])
    assert pipe["state"] == "in_review" and pipe["review"] == "approved"


def test_pipeline_state_merged_carries_evidence():
    merged = [_merged_pr(200, "agentflow/claude/issue-155-x",
                         merge_commit_oid="abc1234def", review_decision="APPROVED",
                         ci_rollup=[{"conclusion": "SUCCESS"}, {"state": "SUCCESS"}])]
    pipe = dd.pipeline_state(155, open_prs=[], merged_prs=merged)
    assert pipe["state"] == "merged" and pipe["pr_number"] == 200 and pipe["pr_url"] == "u/200"
    assert pipe["merge_commit"] == "abc1234def" and pipe["merged_at"] == "2026-07-17T00:00:00Z"
    assert pipe["review"] == "approved" and pipe["ci"] == "passing"


def test_pipeline_state_merged_wins_over_stale_open_pr():
    # A rebuild PR that merged supersedes any lingering open PR for the same issue.
    open_prs = [_open_pr(200, "agentflow/claude/issue-155-x")]
    merged = [_merged_pr(210, "agentflow/codex/issue-155-y", merge_commit_oid="deadbeef")]
    assert dd.pipeline_state(155, open_prs=open_prs, merged_prs=merged)["state"] == "merged"


def test_ci_verdict_reduces_mixed_checks():
    assert dd._ci_verdict([{"conclusion": "SUCCESS"}]) == "passing"
    assert dd._ci_verdict([{"conclusion": "SUCCESS"}, {"conclusion": "FAILURE"}]) == "failing"
    assert dd._ci_verdict([{"conclusion": "SUCCESS"}, {"state": "PENDING"}]) == "pending"
    assert dd._ci_verdict([]) is None


# --- Respond park comment detectors (issue #197) -----------------------------------------

def _respond_park_body(target: str) -> str:
    """Build a Respond park comment body the same way _park_respond does."""
    proof = f"<!-- agentflow-respond-park-target:{target} -->"
    return ("> *agentflow: parked for human review (Respond).*\n"
            f"{proof}\n\n"
            f"Respond could not finish answering maintainer comment `{target}` "
            "within its continuation budget. The PR branch and local work were retained.")


def test_respond_park_body_satisfies_park_reason():
    # A Respond park comment must register as a park, not None (was broken before fix).
    body = _respond_park_body("comment-42")
    assert park_reason([{"body": body}]) is not None


def test_respond_park_body_most_recent_wins():
    # An older conflict comment followed by a newer Respond park → classify from Respond.
    older_conflict = {"body": f"> *{_CONFLICT_MARK}.*\n\nmain advanced", "createdAt": "T1"}
    newer_respond = {"body": _respond_park_body("comment-99"), "createdAt": "T2"}
    reason = park_reason([older_conflict, newer_respond])
    assert reason is not None
    assert reason != "failed-merge"


def test_respond_park_proof_marker_round_trips():
    # _RESPOND_PARK_TARGET_RE must extract the target id from the new body format.
    body = _respond_park_body("comment-42")
    m = _RESPOND_PARK_TARGET_RE.search(body)
    assert m is not None and m.group(1) == "comment-42"


def test_respond_park_old_format_idempotency():
    # A re-park against the old-format body must be a no-op: the proof marker is format-independent.
    target = "comment-7"
    proof = f"<!-- agentflow-respond-park-target:{target} -->"
    old_body = ("> *agentflow: Respond parked for human review.*\n"
                f"{proof}\n\n"
                "Respond could not finish answering maintainer comment `comment-7` "
                "within its continuation budget. The PR branch and local work were retained.")
    # The idempotency check in _park_respond keys on the proof marker, not the quote text.
    assert proof in old_body
    assert _RESPOND_PARK_TARGET_RE.search(old_body).group(1) == target


def test_workspace_pipeline_reads_only_for_wanted_issues(monkeypatch):
    open_pr = _open_pr(200, "agentflow/claude/issue-155-x")
    merged_pr = _merged_pr(190, "agentflow/codex/issue-140-z", merge_commit_oid="cafef00d")

    def fake_list_pipeline_prs(repo, state, *, limit=50):
        return [open_pr] if state == "open" else [merged_pr]

    monkeypatch.setattr(github, "list_pipeline_prs", fake_list_pipeline_prs)

    out = dd.workspace_pipeline("o/app", [155, 140])
    assert set(out) == {155, 140}
    assert out[155]["state"] == "pr_open"
    assert out[140]["state"] == "merged" and out[140]["merge_commit"] == "cafef00d"
    # An empty request never touches GitHub.
    assert dd.workspace_pipeline("o/app", []) == {}
