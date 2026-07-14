import json
from types import SimpleNamespace

import agentflow.dashboard_data as dd
from agentflow.dashboard_data import (
    _CONFLICT_MARK,
    _UI_GAP_REASON,
    _complexity_of,
    _effort_of,
    park_reason,
    pr_stage,
)


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
_HELD = {
    "agentflow:needs-grilling": [{"number": 10, "title": "held: real fork",
                                  "updatedAt": "2026-07-13T10:00:00Z"}],
    "agentflow:needs-mockup": [{"number": 11, "title": "held: needs a mockup",
                                "updatedAt": "2026-07-13T11:00:00Z"}],
}
_OPEN_PRS = [
    {"number": 20, "title": "drop pr", "headRefName": "agentflow/claude/issue-1", "mergedAt": None},
    {"number": 21, "title": "merge-fail pr", "headRefName": "agentflow/codex/issue-2", "mergedAt": None},
    {"number": 22, "title": "question pr", "headRefName": "agentflow/claude/issue-3", "mergedAt": None},
    {"number": 23, "title": "ui-gap pr", "headRefName": "agentflow/codex/issue-4", "mergedAt": None},
    {"number": 24, "title": "still building", "headRefName": "agentflow/claude/issue-5", "mergedAt": None},
]
_COMMENTS = {
    20: [_park("is a `reviewed` repo — a human merges") | {"createdAt": "2026-07-13T09:00:00Z"}],
    21: [_park("could not be squash-merged (branch protection)") | {"createdAt": "2026-07-13T08:00:00Z"}],
    22: [_park("is a `reviewed` repo — a human merges") | {"createdAt": "2026-07-13T07:00:00Z"},
         {"body": "one more question?", "createdAt": "2026-07-13T07:30:00Z"}],
    23: [_park(_UI_GAP_REASON) | {"createdAt": "2026-07-13T06:00:00Z"}],
    24: [{"body": "> *agentflow: reviewing…*\n\nno block yet", "createdAt": "2026-07-13T05:00:00Z"}],
}


def _fake_run(held=None):
    held = _HELD if held is None else held

    def run(args, **kw):
        def ok(payload):
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        if args[1] == "issue":
            if "ready-for-agent" in args:
                return ok([{"number": 1, "title": "ready one",
                            "labels": [{"name": "agentflow:complexity:standard"},
                                       {"name": "agentflow:effort:heavy"}]}])
            label = args[args.index("--label") + 1]
            return ok(held.get(label, []))
        if args[1] == "pr":
            state = args[args.index("--state") + 1]
            return ok(_OPEN_PRS if state == "open" else [])
        return SimpleNamespace(returncode=0, stdout="[]")

    return run


def _patch(monkeypatch, held=None):
    monkeypatch.setattr(dd, "_run", _fake_run(held))
    monkeypatch.setattr(dd, "_pr_comments", lambda repo, n: _COMMENTS.get(n, []))
    monkeypatch.setattr(dd, "repo_profile", lambda workdir: "reviewed")
    monkeypatch.setattr(dd.ratchet, "status",
                        lambda repo: {"samples": 0, "correction_rate": 0, "ready_to_loosen": False})


def test_repo_view_derives_held_and_parked(monkeypatch):
    _patch(monkeypatch)
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
    view = dd.repo_view(SimpleNamespace(repo="o/app", workdir="/tmp/app"))
    assert view["held"] == []
    assert view["parked"] == []
