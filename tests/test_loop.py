"""Pure helpers of the M0 loop. The live orchestration (build/review/merge) is
proven by the first live run; these are the parsing bits that must be exact."""

import json

import pytest

from agentflow import loop
from agentflow.intake import IntakeRoute
from agentflow.loop import (BUILD_PROMPT, RepoConfig, _build_review_merge, _free_to_dispatch,
                            _issues_in_flight, _next_ready_issue, _untriaged, build_issue,
                            complexity_from_labels, effort_from_labels, held_build_result,
                            issue_of_branch, pr_number, reclaim_claims, repo_profile, slug)
from agentflow.reviewer import Verdict
from agentflow.runner import BuildOutcome, BuildStatus, Complexity, Effort


class _FakeRun:
    """Stand-in for a `subprocess`-style result — only `.returncode`/`.stdout` are read."""
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_complexity_from_labels_reads_the_label():
    assert complexity_from_labels(["ready-for-agent", "agentflow:complexity:standard"]) is Complexity.STANDARD
    assert complexity_from_labels(["agentflow:complexity:deep"]) is Complexity.DEEP


def test_complexity_from_labels_is_none_without_one():
    # Hard gate (ADR 0018): no complexity label => the loop must skip, not guess.
    assert complexity_from_labels(["ready-for-agent", "bug"]) is None
    assert complexity_from_labels([]) is None


def test_complexity_from_labels_ignores_lookalikes():
    assert complexity_from_labels(["agentflow:complexity:xl", "tier:deep"]) is None


def test_effort_from_labels_defaults_to_medium():
    assert effort_from_labels(["agentflow:effort:high"]) is Effort.HIGH
    assert effort_from_labels(["ready-for-agent"]) is Effort.MEDIUM  # default, not a hard gate


@pytest.mark.parametrize("title,expected", [
    ("Add a slugify(text) helper", "add-a-slugify-text-helper"),
    ("  Foo__Bar!!  ", "foo-bar"),
    ("", "issue"),
    ("!!!", "issue"),
])
def test_slug(title, expected):
    assert slug(title) == expected


def test_slug_truncates_to_40():
    assert len(slug("word " * 40)) <= 40


def test_pr_number_from_url():
    assert pr_number("https://github.com/o/r/pull/42") == 42
    assert pr_number("https://github.com/o/r/pull/42/") == 42


def test_issue_of_branch_identifies_the_owned_issue():
    # an open agentflow PR on this branch means issue N is already being worked
    assert issue_of_branch("agentflow/codex/issue-2-harden-and-deploy") == 2
    assert issue_of_branch("agentflow/claude/issue-42-foo-bar") == 42


def test_issue_of_branch_is_none_for_non_agentflow_branches():
    assert issue_of_branch("some-human-branch") is None
    assert issue_of_branch("agentflow/codex/no-issue-marker") is None
    assert issue_of_branch("") is None


def test_free_to_dispatch_skips_claimed_or_in_flight():
    ready = {"number": 5, "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:complexity:standard"}]}
    assert _free_to_dispatch(ready, set()) is True
    assert _free_to_dispatch(ready, {5}) is False   # an open agentflow PR already owns it
    claimed = {"number": 6, "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:building"}]}
    assert _free_to_dispatch(claimed, set()) is False   # claimed — an agent is building it


def test_untriaged_skips_state_labels_and_triage_claim():
    fresh = {"number": 1, "labels": [{"name": "bug"}]}
    assert _untriaged(fresh) is True
    triaging = {"number": 2, "labels": [{"name": "bug"}, {"name": "agentflow:triaging"}]}
    assert _untriaged(triaging) is False   # a grounding session already owns it — no re-dispatch
    routed = {"number": 3, "labels": [{"name": "ready-for-agent"}]}
    assert _untriaged(routed) is False     # already has a state label


def test_repo_profile_reads_the_dial(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# repo\n\nprofile: autonomous\n\n## facts\n")
    assert repo_profile(str(tmp_path)) == "autonomous"


def test_repo_profile_prefers_agents_md_then_claude(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("profile: guarded\n")
    assert repo_profile(str(tmp_path)) == "guarded"


def test_repo_profile_defaults_reviewed_when_absent(tmp_path):
    # ADR 0002 safe default — never auto-merge a repo that didn't opt in.
    assert repo_profile(str(tmp_path)) == "reviewed"


def test_issues_in_flight_is_unknown_when_gh_fails(monkeypatch):
    # Unknown is not empty (ADR 0021): a `gh` blip must not read as "nothing in flight",
    # or every in-review issue gets a duplicate dispatch.
    monkeypatch.setattr(loop, "_run", lambda cmd: _FakeRun(returncode=1))
    assert _issues_in_flight(RepoConfig("o/r", ".")) is None


def test_next_ready_issue_fails_closed_when_in_flight_unknown(monkeypatch):
    ready = [{"number": 5, "title": "t", "body": "", "labels": [{"name": "ready-for-agent"}]}]
    monkeypatch.setattr(loop, "_run", lambda cmd: _FakeRun(json.dumps(ready)))
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: None)
    assert _next_ready_issue(RepoConfig("o/r", ".")) is None
    # sanity: same listing dispatches once in-flight is actually known
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    assert _next_ready_issue(RepoConfig("o/r", "."))["number"] == 5


def test_reclaim_claims_strips_nothing_when_in_flight_unknown(monkeypatch):
    # The reclaim exists to prevent duplicates; failing open here would *create* one by
    # clearing a live build's claim on a transient `gh` error.
    claimed = [{"number": 7}]
    released = []
    monkeypatch.setattr(loop, "_run", lambda cmd: _FakeRun(json.dumps(claimed)))
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: None)
    monkeypatch.setattr(loop, "_release", lambda repo, n: released.append(n))
    assert reclaim_claims(RepoConfig("o/r", ".")) == 0
    assert released == []


def test_held_build_result_holds_instead_of_requeueing():
    # A stuck build hands the issue back held — still-`ready` means a fresh build, a
    # duplicate bail comment, and a duplicate ping every cycle, with the queue stalled.
    result = held_build_result("bail", "draft PR https://github.com/o/r/pull/9")
    assert result.route is IntakeRoute.GRILL
    assert result.body.startswith("> *agentflow intake")   # resumes via the ADR 0019 path
    assert "pull/9" in result.body and "pickup" in result.body
    assert result.title == ""   # never retitles on a hold


def test_build_prompt_formats_and_tells_the_builder_the_pr_gates():
    # Formatted before every build (loop.py: dispatch). Guards the bracing and keeps the
    # builder's marching orders in step with what cross-review now blocks on (ADR 0018),
    # so a UI build self-complies instead of bouncing off the gate.
    body = BUILD_PROMPT.format(repo="o/r", n=7, title="Do a thing", body="details", effort="medium")
    assert "o/r" in body and "#7" in body and "Do a thing" in body
    assert "screenshot" in body.lower()   # UI-change evidence gate
    assert "jargon" in body.lower()        # plain-language gate


def test_build_prompt_names_the_charter_test_standard():
    # ADR 0022: the builder is told the bar up front, not only caught at cross-review.
    body = BUILD_PROMPT.format(repo="o/r", n=7, title="x", body="", effort="high")
    assert "public interface" in body
    assert "failed first" in body.lower()


def test_work_order_helper_is_gone():
    # ADR 0022 retired the separate frozen work-order comment; nothing should read one.
    assert not hasattr(loop, "_work_order")


def test_dispatch_build_builds_guarded_from_the_brief(monkeypatch):
    # ADR 0022: a guarded repo no longer needs a frozen work-order comment — it builds from
    # the Agent Brief in the issue body like every profile. Fails first if the guarded branch
    # still bails with "needs a frozen work order".
    monkeypatch.setattr(loop, "repo_profile", lambda wd: "guarded")
    monkeypatch.setattr(loop, "pick_pair", lambda: (object(), object()))
    monkeypatch.setattr(loop, "_claim", lambda repo, n: None)
    monkeypatch.setattr(loop, "_release", lambda repo, n: None)
    seen = {}

    def fake_brm(cfg, issue, n, sl, complexity, effort, builder, reviewer_runner, profile, build_prompt):
        seen["profile"], seen["prompt"] = profile, build_prompt
        return f"#{n}: built"

    monkeypatch.setattr(loop, "_build_review_merge", fake_brm)
    issue = {"number": 3, "title": "Insulin math", "body": "THE AGENT BRIEF BODY",
             "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:complexity:deep"}]}
    assert loop._dispatch_build(RepoConfig("o/r", "/tmp/x"), issue) == "#3: built"
    assert seen["profile"] == "guarded"
    assert "THE AGENT BRIEF BODY" in seen["prompt"]   # built from the Brief, not a work order


def _issue_view(monkeypatch, issue):
    """Point loop._run at a canned `gh issue view` payload for build_issue's fetch."""
    monkeypatch.setattr(loop, "_run", lambda argv: _FakeRun(json.dumps(issue)))


def test_build_issue_dispatches_a_ready_free_issue(monkeypatch):
    issue = {"number": 5, "state": "OPEN", "title": "t", "body": "b",
             "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:complexity:standard"}]}
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    monkeypatch.setattr(loop, "_dispatch_build", lambda cfg, iss: f"#{iss['number']}: dispatched")
    assert build_issue(RepoConfig("o/r", "/tmp"), 5) == "#5: dispatched"


def test_build_issue_refuses_a_held_issue_and_points_at_pickup(monkeypatch):
    issue = {"number": 7, "state": "OPEN", "title": "t", "body": "b",
             "labels": [{"name": "agentflow:needs-grilling"}]}
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_dispatch_build", lambda *a: pytest.fail("must not build a held issue"))
    out = build_issue(RepoConfig("o/r", "/tmp"), 7)
    assert "pickup" in out and "7" in out


def test_build_issue_refuses_an_untriaged_issue_and_points_at_triage(monkeypatch):
    issue = {"number": 8, "state": "OPEN", "title": "t", "body": "b", "labels": [{"name": "bug"}]}
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_dispatch_build", lambda *a: pytest.fail("must not build an un-triaged issue"))
    out = build_issue(RepoConfig("o/r", "/tmp"), 8)
    assert "triage" in out or "scope" in out


def test_build_issue_refuses_an_in_flight_issue(monkeypatch):
    issue = {"number": 9, "state": "OPEN", "title": "t", "body": "b",
             "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:complexity:deep"}]}
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: {9})   # an open agentflow PR owns it
    monkeypatch.setattr(loop, "_dispatch_build", lambda *a: pytest.fail("must not double-dispatch"))
    out = build_issue(RepoConfig("o/r", "/tmp"), 9)
    assert "flight" in out.lower() or "claim" in out.lower()


def test_failed_merge_parks_and_pings(monkeypatch):
    # A squash-merge failure (branch protection, conflict, transient error) must not
    # silently idle — it must park the PR, ping, and record a ratchet event.
    CLEAN_VERDICT = Verdict(clean=True)

    class _FakeBuilder:
        tool = "claude"
        def build(self, task):
            return BuildOutcome(BuildStatus.PR_OPENED, pr_url="https://github.com/o/r/pull/42")

    class _FakeReviewer:
        tool = "codex"

    parked, notified, recorded = [], [], []

    monkeypatch.setattr(loop, "ci_is_green", lambda repo, pr: True)
    monkeypatch.setattr(loop, "squash_merge", lambda repo, pr: False)
    monkeypatch.setattr(loop, "park",
                        lambda repo, pr, verdict, reason="": parked.append((repo, pr, reason)))
    monkeypatch.setattr(loop, "notify",
                        lambda title, msg, url="": notified.append((title, msg)))
    monkeypatch.setattr(loop.ratchet, "record",
                        lambda repo, outcome: recorded.append(outcome))

    class _PatchedReviewer:
        def __init__(self, runner): pass
        def review(self, *args, **kwargs): return CLEAN_VERDICT

    monkeypatch.setattr(loop, "Reviewer", _PatchedReviewer)

    cfg = RepoConfig("o/r", "/tmp")
    issue = {"number": 42, "title": "t", "body": ""}
    out = _build_review_merge(cfg, issue, 42, "t", Complexity.STANDARD, Effort.MEDIUM,
                              _FakeBuilder(), _FakeReviewer(), "autonomous", "build")

    assert "merge failed" in out
    assert parked, "park must be called on merge failure"
    assert parked[0][1] == 42
    assert "branch protection" in parked[0][2] or "squash" in parked[0][2]
    assert notified, "notify must be called on merge failure"
    assert "needs you" in notified[0][0]
    assert "parked" in recorded
