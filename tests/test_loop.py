"""Pure helpers of the M0 loop. The live orchestration (build/review/merge) is
proven by the first live run; these are the parsing bits that must be exact."""

import json
from types import SimpleNamespace

import pytest

from agentflow import loop
from agentflow.intake import INTAKE_MARK, IntakeRoute
from agentflow.loop import (BUILD_PROMPT, RESPOND_PROMPT, REVISE_PROMPT, RepoConfig,
                            _build_review_merge, _free_to_dispatch, _issues_in_flight,
                            _main_config, _next_pr_awaiting_reply, _next_ready_issue,
                            _next_resumable_issue, _untriaged, build_issue, complexity_from_labels,
                            effort_from_labels, held_build_result, intake_allowlist,
                            issue_of_branch, pr_number, reclaim_claims, repo_profile, respond_once,
                            slug, ui_surfaces)
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


def test_intake_allowlist_always_includes_owner(tmp_path):
    assert intake_allowlist("owner/repo", str(tmp_path)) == {"owner"}


def test_intake_allowlist_reads_extra_names_from_agents_md(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# repo\n\nintake-allowlist: alice, bob\n\n## facts\n")
    assert intake_allowlist("owner/repo", str(tmp_path)) == {"owner", "alice", "bob"}


def test_intake_allowlist_falls_back_to_claude_md(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("intake-allowlist: carol\n")
    assert intake_allowlist("owner/repo", str(tmp_path)) == {"owner", "carol"}


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
    body = BUILD_PROMPT.format(repo="o/r", n=7, title="Do a thing", body="details",
                               effort="medium", surfaces="`agentflow/static/`")
    assert "o/r" in body and "#7" in body and "Do a thing" in body
    assert "screenshot" in body.lower()   # UI-change evidence gate
    assert "jargon" in body.lower()        # plain-language gate


def test_build_prompt_names_the_charter_test_standard():
    # ADR 0022: the builder is told the bar up front, not only caught at cross-review.
    body = BUILD_PROMPT.format(repo="o/r", n=7, title="x", body="", effort="high",
                               surfaces="`agentflow/static/`")
    assert "public interface" in body
    assert "failed first" in body.lower()


def test_ui_surfaces_reads_the_declared_prefixes(tmp_path):
    # Parsed the same way as `profile:` — comma-separated path prefixes, per repo.
    (tmp_path / "AGENTS.md").write_text("# repo\n\nprofile: reviewed\n"
                                        "ui-surfaces: frontend/, agentflow/static/\n\n## facts\n")
    assert ui_surfaces(str(tmp_path)) == ["frontend/", "agentflow/static/"]


def test_ui_surfaces_empty_when_undeclared(tmp_path):
    # No declaration → no surfaces → the UI-evidence gate is inert for a non-UI repo.
    (tmp_path / "AGENTS.md").write_text("profile: reviewed\n")
    assert ui_surfaces(str(tmp_path)) == []


def test_revise_prompt_carries_both_evidence_gates():
    # A revise pass must not silently degrade compliance: it names both the screenshot
    # gate (with the repo's surfaces) and the plain-language body gate.
    body = REVISE_PROMPT.format(n=5, findings="- fix it", surfaces="`agentflow/static/`")
    assert "screenshot" in body.lower()
    assert "agentflow/static/" in body
    assert "plain" in body.lower()


def test_work_order_helper_is_gone():
    # ADR 0022 retired the separate frozen work-order comment; nothing should read one.
    assert not hasattr(loop, "_work_order")


def test_dispatch_build_builds_guarded_from_the_brief(monkeypatch):
    # ADR 0022: a guarded repo no longer needs a frozen work-order comment — it builds from
    # the Agent Brief in the issue body like every profile. Fails first if the guarded branch
    # still bails with "needs a frozen work order".
    monkeypatch.setattr(loop, "repo_profile", lambda wd: "guarded")
    monkeypatch.setattr(loop, "pick_pair", lambda operator=False: (object(), object()))
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
    monkeypatch.setattr(loop, "_dispatch_build",
                        lambda cfg, iss, operator=False: f"#{iss['number']}: dispatched op={operator}")
    assert build_issue(RepoConfig("o/r", "/tmp"), 5) == "#5: dispatched op=True"


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


_INTAKE_COMMENT = f"{INTAKE_MARK} — generated by AI.\n\nHold message."
_MAINTAINER_REPLY = "Here is my answer / waiver."


def _make_resumable_run(grilling_issues, mockup_issues, reply=_MAINTAINER_REPLY):
    """A fake _run that serves canned issue listings and a single-reply comment thread."""
    # The reply is from the repo owner (always allowlisted), so it counts as a resume-
    # triggering maintainer reply under the allowlist filter (issue #25).
    comments = [{"body": _INTAKE_COMMENT}, {"body": reply, "author": {"login": "o"}}]

    def fake_run(cmd):
        if "--label" in cmd:
            label_idx = cmd.index("--label") + 1
            label = cmd[label_idx]
            if label == "agentflow:needs-grilling":
                return _FakeRun(json.dumps(grilling_issues))
            if label == "agentflow:needs-mockup":
                return _FakeRun(json.dumps(mockup_issues))
        if "comments" in cmd:
            return _FakeRun(json.dumps({"comments": comments}))
        return _FakeRun("[]")

    return fake_run


def test_next_resumable_issue_picks_up_needs_grilling_reply(monkeypatch):
    issue = {"number": 10, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:needs-grilling"}]}
    monkeypatch.setattr(loop, "_run", _make_resumable_run([issue], []))
    result = _next_resumable_issue(RepoConfig("o/r", "."))
    assert result is not None
    found, reply = result
    assert found["number"] == 10
    assert _MAINTAINER_REPLY in reply


def test_next_resumable_issue_picks_up_needs_mockup_reply(monkeypatch):
    # Failed BEFORE the fix: the old code only queried needs-grilling, so a reply on a
    # needs-mockup issue was silently dropped and the mockup queue stalled forever.
    issue = {"number": 11, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:needs-mockup"}]}
    monkeypatch.setattr(loop, "_run", _make_resumable_run([], [issue]))
    result = _next_resumable_issue(RepoConfig("o/r", "."))
    assert result is not None
    found, reply = result
    assert found["number"] == 11
    assert _MAINTAINER_REPLY in reply


def test_next_resumable_issue_returns_none_when_last_comment_is_ours(monkeypatch):
    issue = {"number": 12, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:needs-mockup"}]}
    # last comment is ours (contains INTAKE_MARK) — not awaiting a reply
    our_last = [{"body": _INTAKE_COMMENT}]

    def fake_run(cmd):
        if "--label" in cmd and "agentflow:needs-grilling" in cmd:
            return _FakeRun(json.dumps([]))
        if "--label" in cmd and "agentflow:needs-mockup" in cmd:
            return _FakeRun(json.dumps([issue]))
        if "comments" in cmd:
            return _FakeRun(json.dumps({"comments": our_last}))
        return _FakeRun("[]")

    monkeypatch.setattr(loop, "_run", fake_run)
    assert _next_resumable_issue(RepoConfig("o/r", ".")) is None


def test_next_resumable_issue_returns_none_on_gh_error(monkeypatch):
    monkeypatch.setattr(loop, "_run", lambda cmd: _FakeRun(returncode=1))
    assert _next_resumable_issue(RepoConfig("o/r", ".")) is None


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


def test_reviewed_path_parks_a_screenshotless_ui_change_on_the_gate(monkeypatch):
    # ADR 0018: a reviewed/guarded repo hands the PR to a human either way, but the
    # mechanical UI-evidence gate still runs — a UI change with no screenshot must park
    # with the missing-screenshot reason, not the generic "a human merges". Fails first
    # if the reviewed branch skips ui_evidence_gap and parks with the profile reason.
    from agentflow.reviewer import Verdict
    from agentflow.runner import BuildOutcome, BuildStatus

    class _Builder:
        tool = "claude"
        def build(self, task):
            return BuildOutcome(BuildStatus.PR_OPENED, pr_url="https://github.com/o/r/pull/7")

    class _FakeReviewer:
        def __init__(self, runner): pass
        def review(self, *a, **k):
            return Verdict(clean=True)   # review says clean; the gate must still bite

    reviewer_runner = type("R", (), {"tool": "codex"})()
    monkeypatch.setattr(loop, "Reviewer", _FakeReviewer)
    monkeypatch.setattr(loop, "ui_surfaces", lambda wd: ["agentflow/static/"])
    monkeypatch.setattr(loop, "ui_evidence_gap", lambda repo, pr, surfaces: True)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: None)
    parked = {}
    monkeypatch.setattr(loop, "park", lambda repo, pr, verdict, reason: parked.update(reason=reason))

    loop._build_review_merge(RepoConfig("o/r", "/tmp"), {"body": ""}, 5, "x",
                             Complexity.STANDARD, Effort.MEDIUM, _Builder(),
                             reviewer_runner, "reviewed", "prompt")
    assert "screenshot" in parked["reason"].lower()


# --- issue #18: answering maintainer comments on parked PRs ---------------------

_PARK = "> *agentflow: parked for human review.*\n\nfindings"
_MAINT = "Show me a screenshot please?"


def _pr_gh(monkeypatch, prs, comments_by_pr):
    """Route loop._run's `gh pr list` / `gh pr view <n>` at canned payloads."""
    def fake_run(argv):
        if argv[:3] == ["gh", "pr", "list"]:
            return _FakeRun(json.dumps(prs))
        if argv[:3] == ["gh", "pr", "view"]:
            return _FakeRun(json.dumps({"comments": comments_by_pr.get(int(argv[3]), [])}))
        return _FakeRun("")
    monkeypatch.setattr(loop, "_run", fake_run)


def test_next_pr_awaiting_reply_picks_the_unanswered_one(monkeypatch):
    prs = [{"number": 7, "headRefName": "agentflow/claude/issue-3-do-thing"},
           {"number": 8, "headRefName": "agentflow/codex/issue-4-other"}]
    comments = {7: [{"body": _PARK}],                     # our marker last — answered
                8: [{"body": _PARK}, {"body": _MAINT}]}   # maintainer last — pending
    _pr_gh(monkeypatch, prs, comments)
    assert _next_pr_awaiting_reply(RepoConfig("o/r", ".")) == (8, "agentflow/codex/issue-4-other", _MAINT)


def test_next_pr_awaiting_reply_ignores_human_branches(monkeypatch):
    # A maintainer's own branch is not an agentflow PR — never spawn a responder on it.
    prs = [{"number": 9, "headRefName": "my-hotfix"}]
    _pr_gh(monkeypatch, prs, {9: [{"body": _MAINT}]})
    assert _next_pr_awaiting_reply(RepoConfig("o/r", ".")) is None


def test_respond_once_replies_without_merging_or_new_pr(monkeypatch):
    # The responder's contract: a marker-prefixed reply, same branch, never a merge and
    # never a new PR. Fails first if respond_once touches squash_merge or opens a PR.
    prs = [{"number": 8, "headRefName": "agentflow/claude/issue-4-other"}]
    _pr_gh(monkeypatch, prs, {8: [{"body": _PARK}, {"body": _MAINT}]})
    monkeypatch.setattr(loop, "_checkout_pr_branch", lambda cfg, branch, wt: True)

    launched = {}

    class _FakeRunner:
        tool = "claude"
        def provision(self, wt): pass
        def model_for(self, c): return "opus"
        def launch(self, prompt, cwd, model):
            launched["prompt"] = prompt
            return True, "replied"
        def build(self, task): pytest.fail("responder must never build/open a PR")

    monkeypatch.setattr(loop, "pick_pair", lambda: (_FakeRunner(), None))
    monkeypatch.setattr(loop, "squash_merge", lambda *a: pytest.fail("responder must never merge"))

    out = respond_once(RepoConfig("o/r", "/tmp"))
    assert "8" in out and "maintainer" in out.lower()
    assert _MAINT in launched["prompt"]              # answers what was asked
    assert "same branch" in launched["prompt"].lower()   # pushes fixes to the PR branch


def test_respond_once_noop_when_nothing_pending(monkeypatch):
    _pr_gh(monkeypatch, [{"number": 7, "headRefName": "agentflow/claude/issue-3-x"}],
           {7: [{"body": _PARK}]})   # our marker had the last word
    monkeypatch.setattr(loop, "pick_pair", lambda: pytest.fail("no PR pending — don't spawn"))
    assert respond_once(RepoConfig("o/r", ".")) == "no parked PRs awaiting reply"


def test_main_config_parses_repo_and_workdir():
    # Entrypoint takes repo and optional workdir from argv — no hardcoded sandbox default.
    cfg = _main_config(["owner/repo", "/some/path"])
    assert cfg.repo == "owner/repo"
    assert cfg.workdir == "/some/path"


def test_main_config_derives_workdir_from_repo():
    from pathlib import Path
    cfg = _main_config(["owner/myrepo"])
    assert cfg.repo == "owner/myrepo"
    assert cfg.workdir == str(Path.home() / "Code" / "owner" / "myrepo")


def test_main_config_requires_repo():
    # Passing no args must exit with a usage message, not proceed with the old sandbox default.
    with pytest.raises(SystemExit):
        _main_config([])


# --- intake no-spam: infra failures retry silently, backstop holds once (issue #23) ---

def _stub_intake_once(monkeypatch, result):
    """Drive intake_once with a canned intake result and record any apply_intake call."""
    issue = {"number": 5, "title": "t", "labels": []}
    applied = []
    loop._intake_infra_failures.clear()
    monkeypatch.setattr(loop, "_next_resumable_issue", lambda cfg: None)
    monkeypatch.setattr(loop, "_next_untriaged_issue", lambda cfg: issue)
    monkeypatch.setattr(loop, "pick_pair", lambda: (object(), None))
    monkeypatch.setattr(loop, "_claim_triage", lambda repo, n: None)
    monkeypatch.setattr(loop, "_release_triage", lambda repo, n: None)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: None)
    monkeypatch.setattr(loop, "Intake",
                        lambda builder: SimpleNamespace(intake=lambda *a, **k: result))
    monkeypatch.setattr(loop, "apply_intake",
                        lambda repo, n, title, labels, r: applied.append(r) or "applied")
    return applied


def test_intake_infra_failure_posts_nothing_and_leaves_it_untriaged(monkeypatch):
    from agentflow.intake import IntakeResult

    result = IntakeResult(IntakeRoute.GRILL, "", parsed=False, infra_failed=True, detail="launch non-zero")
    applied = _stub_intake_once(monkeypatch, result)
    out = loop.intake_once(RepoConfig("o/r", "/tmp"))
    assert applied == [], "an infra failure must post nothing (no apply)"
    assert "retrying silently" in out


def test_intake_infra_backstop_posts_exactly_one_held_comment(monkeypatch):
    from agentflow.intake import IntakeResult

    result = IntakeResult(IntakeRoute.GRILL, "", parsed=False, infra_failed=True, detail="launch non-zero")
    applied = _stub_intake_once(monkeypatch, result)
    cfg = RepoConfig("o/r", "/tmp")
    for _ in range(loop.INTAKE_MAX_INFRA_FAILURES):
        loop.intake_once(cfg)
    # exactly one held comment, and only on the last try
    assert len(applied) == 1
    assert applied[0].route is IntakeRoute.GRILL and applied[0].infra_failed is False


def test_intake_clean_run_ends_the_infra_streak(monkeypatch):
    from agentflow.intake import IntakeResult

    infra = IntakeResult(IntakeRoute.GRILL, "", parsed=False, infra_failed=True, detail="x")
    applied = _stub_intake_once(monkeypatch, infra)
    cfg = RepoConfig("o/r", "/tmp")
    loop.intake_once(cfg)   # one infra failure banked
    # now a clean routing lands; the streak must reset so a later blip doesn't hit the backstop early
    ok = IntakeResult(IntakeRoute.READY, "brief", complexity=Complexity.DEEP, effort=Effort.MEDIUM)
    monkeypatch.setattr(loop, "Intake", lambda builder: SimpleNamespace(intake=lambda *a, **k: ok))
    loop.intake_once(cfg)
    assert loop._intake_infra_failures.get((cfg.repo, 5)) is None
