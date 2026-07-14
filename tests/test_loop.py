"""Pure helpers of the M0 loop. The live orchestration (build/review/merge) is
proven by the first live run; these are the parsing bits that must be exact."""

import json
from types import SimpleNamespace

import pytest

from agentflow import loop
from agentflow.intake import INTAKE_MARK, IntakeRoute, awaiting_recheck, compose_ready_body
from agentflow.loop import (BUILD_PROMPT, DRAWING, MOCKUP_MARK, PRODUCE_PROMPT, RESPOND_PROMPT,
                            REVISE_PROMPT, RebaseResult, RepoConfig, _MOCKUP_DISCLAIMER,
                            _build_review_merge, _free_to_dispatch, _issues_in_flight,
                            _main_config, _mockup_eligible, _next_mockup_issue,
                            _next_pr_awaiting_reply, _next_ready_issue, _next_resumable_issue,
                            _rebase_survivor, _untriaged, base_advanced, build_issue,
                            complexity_from_labels, conflict_already_flagged, effort_from_labels,
                            held_build_result, intake_allowlist, issue_of_branch, pr_number,
                            produce_once, reclaim_claims, recheck_once, repo_profile,
                            respond_once, slug, ui_surfaces)
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
    cfg = RepoConfig("o/r", ".")
    ready = {"number": 5, "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:complexity:standard"}]}
    assert _free_to_dispatch(cfg, ready, set()) is True
    assert _free_to_dispatch(cfg, ready, {5}) is False   # an open agentflow PR already owns it
    claimed = {"number": 6, "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:building"}]}
    assert _free_to_dispatch(cfg, claimed, set()) is False   # claimed — an agent is building it


def test_free_to_dispatch_ignores_blocked_by_in_incidental_prose(monkeypatch):
    issue = {"number": 5, "body": "This may be Blocked by #41 after the next review.",
             "labels": [{"name": "ready-for-agent"}]}
    monkeypatch.setattr(loop, "_run", lambda cmd: pytest.fail("prose is not a declaration"))

    assert _free_to_dispatch(RepoConfig("o/r", "."), issue, set()) is True


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


def _ready_dispatch_run(ready, blocker_states):
    def fake_run(cmd):
        if cmd[1:3] == ["issue", "list"]:
            return _FakeRun(json.dumps(ready))
        if cmd[1:3] == ["issue", "view"]:
            state = blocker_states.get(int(cmd[3]))
            if state is None:
                return _FakeRun(returncode=1)
            return _FakeRun(json.dumps({"state": state}))
        raise AssertionError(cmd)

    return fake_run


def test_next_ready_issue_skips_an_issue_blocked_by_an_open_issue(monkeypatch):
    ready = [{"number": 42, "title": "dependent", "body": "Blocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    monkeypatch.setattr(loop, "_run", _ready_dispatch_run(ready, {41: "OPEN"}))
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", ".")) is None


def test_next_ready_issue_logs_blocked_skip_and_selects_next_issue(monkeypatch):
    ready = [
        {"number": 43, "title": "independent", "body": "",
         "labels": [{"name": "ready-for-agent"}]},
        {"number": 42, "title": "dependent", "body": "Blocked by #41",
         "labels": [{"name": "ready-for-agent"}]},
    ]
    logs = []
    monkeypatch.setattr(loop, "_run", _ready_dispatch_run(ready, {41: "OPEN"}))
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    selected = _next_ready_issue(RepoConfig("o/r", "."), _log=logs.append)

    assert selected["number"] == 43
    assert any("#42" in message and "#41" in message for message in logs)


def test_next_ready_issue_rechecks_all_blockers_each_pass(monkeypatch):
    ready = [{"number": 42, "title": "dependent",
              "body": "Blocked by #40\n\nBlocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    blocker_states = {40: "CLOSED", 41: "OPEN"}
    monkeypatch.setattr(loop, "_run", _ready_dispatch_run(ready, blocker_states))
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    cfg = RepoConfig("o/r", ".")

    assert _next_ready_issue(cfg) is None
    blocker_states[41] = "CLOSED"
    assert _next_ready_issue(cfg)["number"] == 42


def test_next_ready_issue_fails_closed_when_blocker_state_is_unknown(monkeypatch):
    ready = [{"number": 42, "title": "dependent", "body": "Blocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    logs = []
    monkeypatch.setattr(loop, "_run", _ready_dispatch_run(ready, {41: None}))
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", "."), _log=logs.append) is None
    assert any("#42" in message and "#41" in message
               and "could not be determined" in message for message in logs)


def test_next_ready_issue_fails_closed_on_malformed_blocker_state(monkeypatch):
    ready = [{"number": 42, "title": "dependent", "body": "Blocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    logs = []

    def fake_run(cmd):
        if cmd[1:3] == ["issue", "list"]:
            return _FakeRun(json.dumps(ready))
        if cmd[1:4] == ["issue", "view", "41"]:
            return _FakeRun("[]")
        raise AssertionError(cmd)

    monkeypatch.setattr(loop, "_run", fake_run)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", "."), _log=logs.append) is None
    assert any("#42" in message and "#41" in message
               and "could not be determined" in message for message in logs)


def test_next_ready_issue_sees_blocker_preserved_by_intake(monkeypatch):
    body = compose_ready_body("## Agent Brief\nBuild the dependent slice.",
                              "Original request.\n\nBlocked by #41")
    ready = [{"number": 42, "title": "dependent", "body": body,
              "labels": [{"name": "ready-for-agent"}]}]
    monkeypatch.setattr(loop, "_run", _ready_dispatch_run(ready, {41: "OPEN"}))
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert "Blocked by #41" in body
    assert _next_ready_issue(RepoConfig("o/r", ".")) is None


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
    monkeypatch.setattr(loop, "pick_pair", lambda operator=False: (object(), object(), ""))
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


def test_dispatch_build_emits_routing_log_before_session(monkeypatch):
    """_log is called with 'routing → <tool> (build)' the instant a builder is chosen,
    before the long session starts. Fails first if the log call is missing or comes after."""
    class _FakeBuilder:
        tool = "codex"

    log_calls = []
    brm_calls = []

    def fake_brm(*a, **k):
        brm_calls.append(list(log_calls))   # snapshot log state at session start
        return "#7: built"

    monkeypatch.setattr(loop, "repo_profile", lambda wd: "autonomous")
    monkeypatch.setattr(loop, "pick_pair", lambda operator=False: (_FakeBuilder(), None, ""))
    monkeypatch.setattr(loop, "_claim", lambda repo, n: None)
    monkeypatch.setattr(loop, "_release", lambda repo, n: None)
    monkeypatch.setattr(loop, "_build_review_merge", fake_brm)
    issue = {"number": 7, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:complexity:standard"}]}
    loop._dispatch_build(RepoConfig("o/r", "/tmp"), issue, _log=log_calls.append)
    assert any("routing → codex (build)" in m for m in log_calls)
    assert brm_calls and any("routing" in m for m in brm_calls[0])   # logged before session


def test_dispatch_build_deferral_includes_block_reason(monkeypatch):
    """When no pool has headroom the deferral message names the per-pool block reason."""
    monkeypatch.setattr(loop, "pick_pair",
                        lambda operator=False: (None, None, "codex: rate limited, claude: active session"))
    issue = {"number": 9, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:complexity:standard"}]}
    out = loop._dispatch_build(RepoConfig("o/r", "/tmp"), issue)
    assert "codex: rate limited" in out
    assert "claude: active session" in out
    assert "deferring" in out


def test_intake_once_emits_routing_log_before_session(monkeypatch):
    """_log is called with 'routing → <tool> (intake)' before the intake session starts."""
    class _FakeBuilder:
        tool = "claude"
        def __init__(self): pass

    log_calls = []
    intake_calls = []

    class _FakeIntake:
        def __init__(self, runner): pass
        def intake(self, *a, **k):
            intake_calls.append(list(log_calls))   # snapshot log state at session start
            from agentflow.intake import IntakeResult, IntakeRoute
            return IntakeResult(route=IntakeRoute.READY, body="scoped to ready")

    issue = {"number": 5, "title": "t", "labels": [], "state": "OPEN"}
    monkeypatch.setattr(loop, "_next_resumable_issue", lambda cfg: None)
    monkeypatch.setattr(loop, "_next_untriaged_issue", lambda cfg: issue)
    monkeypatch.setattr(loop, "pick_pair", lambda: (_FakeBuilder(), None, ""))
    monkeypatch.setattr(loop, "_claim_triage", lambda repo, n: None)
    monkeypatch.setattr(loop, "_release_triage", lambda repo, n: None)
    monkeypatch.setattr(loop, "Intake", _FakeIntake)
    monkeypatch.setattr(loop, "apply_intake", lambda *a, **k: "scoped to ready")
    loop.intake_once(RepoConfig("o/r", "/tmp"), _log=log_calls.append)
    assert any("routing → claude (intake)" in m for m in log_calls)
    assert intake_calls and any("routing" in m for m in intake_calls[0])   # logged before session


def test_intake_once_deferral_includes_block_reason(monkeypatch):
    """When no pool has headroom the intake deferral names the per-pool block reason."""
    issue = {"number": 3, "title": "t", "labels": []}
    monkeypatch.setattr(loop, "_next_resumable_issue", lambda cfg: None)
    monkeypatch.setattr(loop, "_next_untriaged_issue", lambda cfg: issue)
    monkeypatch.setattr(loop, "pick_pair", lambda: (None, None, "codex: busy, claude: you"))
    out = loop.intake_once(RepoConfig("o/r", "/tmp"))
    assert "codex: busy" in out and "claude: you" in out
    assert "deferring" in out


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


def test_build_issue_refuses_an_open_blocker(monkeypatch):
    issue = {"number": 42, "state": "OPEN", "title": "dependent", "body": "Blocked by #41",
             "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:complexity:standard"}]}

    def fake_run(cmd):
        if cmd[1:4] == ["issue", "view", "42"]:
            return _FakeRun(json.dumps(issue))
        if cmd[1:4] == ["issue", "view", "41"]:
            return _FakeRun(json.dumps({"state": "OPEN"}))
        raise AssertionError(cmd)

    monkeypatch.setattr(loop, "_run", fake_run)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    monkeypatch.setattr(loop, "_dispatch_build",
                        lambda *a, **k: pytest.fail("must not bypass the blocker"))

    out = build_issue(RepoConfig("o/r", "/tmp"), 42)

    assert "blocker" in out.lower()


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
    monkeypatch.setattr(loop, "_pr_comments",
                        lambda repo, pr: [{"body": "> *agentflow: parked for human review.*"}])
    removed = []
    monkeypatch.setattr(loop, "remove_worktree_if_safe",
                        lambda workdir, wt: removed.append(str(wt)) or True)
    parked = {}
    monkeypatch.setattr(loop, "park", lambda repo, pr, verdict, reason: parked.update(reason=reason))

    loop._build_review_merge(RepoConfig("o/r", "/tmp"), {"body": ""}, 5, "x",
                             Complexity.STANDARD, Effort.MEDIUM, _Builder(),
                             reviewer_runner, "reviewed", "prompt")
    assert "screenshot" in parked["reason"].lower()
    assert removed == ["/tmp/.agentflow/worktrees/codex-review/pr-7-x"]


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
    monkeypatch.setattr(loop, "_pr_comments",
                        lambda repo, pr: [{"body": _PARK}, {"body": _MAINT},
                                          {"body": loop._RESPOND_DISCLAIMER}])
    removed = []
    monkeypatch.setattr(loop, "remove_worktree_if_safe",
                        lambda workdir, wt: removed.append(str(wt)) or True)

    launched = {}

    class _FakeRunner:
        tool = "claude"
        def provision(self, wt): pass
        def model_for(self, c): return "opus"
        def launch(self, prompt, cwd, model):
            launched["prompt"] = prompt
            return True, "replied"
        def build(self, task): pytest.fail("responder must never build/open a PR")

    monkeypatch.setattr(loop, "pick_pair", lambda: (_FakeRunner(), None, ""))
    monkeypatch.setattr(loop, "squash_merge", lambda *a: pytest.fail("responder must never merge"))

    out = respond_once(RepoConfig("o/r", "/tmp"))
    assert "8" in out and "maintainer" in out.lower()
    assert _MAINT in launched["prompt"]              # answers what was asked
    assert "same branch" in launched["prompt"].lower()   # pushes fixes to the PR branch
    assert removed == ["/tmp/.agentflow/worktrees/claude/issue-4-other"]


def test_respond_once_noop_when_nothing_pending(monkeypatch):
    _pr_gh(monkeypatch, [{"number": 7, "headRefName": "agentflow/claude/issue-3-x"}],
           {7: [{"body": _PARK}]})   # our marker had the last word
    monkeypatch.setattr(loop, "pick_pair", lambda: pytest.fail("no PR pending — don't spawn"))  # never returns
    assert respond_once(RepoConfig("o/r", ".")) == "no parked PRs awaiting reply"


def test_responder_retains_worktree_when_reply_cannot_be_verified(monkeypatch):
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply",
                        lambda cfg: (8, "agentflow/claude/issue-4-other", _MAINT))
    monkeypatch.setattr(loop, "_checkout_pr_branch", lambda *a: True)
    monkeypatch.setattr(loop, "_pr_comments", lambda *a: None)
    monkeypatch.setattr(loop, "remove_worktree_if_safe",
                        lambda *a: pytest.fail("unknown state must retain the worktree"))

    runner = SimpleNamespace(tool="claude", provision=lambda wt: None,
                             model_for=lambda c: "opus",
                             launch=lambda *a, **k: (True, "done"))
    monkeypatch.setattr(loop, "pick_pair", lambda: (runner, None, ""))

    assert "retaining" in respond_once(RepoConfig("o/r", "/tmp"))


def test_pr_branch_checkout_refuses_to_reset_recoverable_work(monkeypatch, tmp_path):
    wt = tmp_path / "worktree"
    wt.mkdir()
    calls = []
    monkeypatch.setattr(loop, "_worktree_is_registered", lambda *a: True)
    monkeypatch.setattr(loop, "_worktree_is_disposable", lambda *a: False)
    monkeypatch.setattr(loop, "_run",
                        lambda cmd: calls.append(cmd) or _FakeRun("", 0))

    assert loop._checkout_pr_branch(RepoConfig("o/r", str(tmp_path)), "branch", wt) is False
    assert not any("reset" in cmd for cmd in calls)


# --- issue #29: the mockup-production phase --------------------------------------------

# Intake's park/kickoff comment on a needs-mockup issue: carries INTAKE_MARK, no MOCKUP_MARK.
_MOCKUP_PARK = f"{INTAKE_MARK} — let's mock this up.\n\nA `/ui-mockups` kickoff."
# The produce phase's own variant-round comment: the disclaimer + embedded screenshots.
_MOCKUP_VARIANTS = (f"{_MOCKUP_DISCLAIMER}\n\n"
                    "**A** — inbox.\n![A](https://raw.githubusercontent.com/o/r/br/mockups/a.png)\n"
                    "Reply with a pick.")


def test_produce_disclaimer_carries_both_marks():
    # The TRAP: the produced-variants comment must carry INTAKE_MARK so the daemon reads it as
    # ours, and MOCKUP_MARK so the produce phase can tell a drawn round from intake's park.
    assert INTAKE_MARK in _MOCKUP_DISCLAIMER
    assert MOCKUP_MARK in _MOCKUP_DISCLAIMER


def test_awaiting_recheck_false_right_after_our_variant_comment():
    # THE self-trigger guard (AC): immediately after the produce comment — intake park, then our
    # variant round — the daemon must NOT treat its own comment as a maintainer reply. Fails
    # first if the variant disclaimer omits INTAKE_MARK.
    comments = [{"body": _MOCKUP_PARK, "author": {"login": "o"}},
                {"body": _MOCKUP_VARIANTS, "author": {"login": "o"}}]
    assert awaiting_recheck(comments, {"o"}) is False
    # ...and True only once the maintainer actually replies with a pick
    comments.append({"body": "B please", "author": {"login": "o"}})
    assert awaiting_recheck(comments, {"o"}) is True


def test_mockup_eligible_picks_a_freshly_parked_issue():
    # A needs-mockup issue with only intake's park comment (no variants drawn, no reply, no
    # claim) is eligible for the produce phase.
    issue = {"number": 5, "labels": [{"name": "agentflow:needs-mockup"}]}
    comments = [{"body": _MOCKUP_PARK, "author": {"login": "o"}}]
    assert _mockup_eligible(issue, comments, {"o"}) is True


def test_mockup_eligible_skips_when_variants_already_drawn():
    # One round per issue — a drawn issue (our MOCKUP_MARK comment) is never re-drawn.
    issue = {"number": 5, "labels": [{"name": "agentflow:needs-mockup"}]}
    comments = [{"body": _MOCKUP_PARK, "author": {"login": "o"}},
                {"body": _MOCKUP_VARIANTS, "author": {"login": "o"}}]
    assert _mockup_eligible(issue, comments, {"o"}) is False


def test_mockup_eligible_skips_a_pending_reply_and_a_live_claim():
    # A pending maintainer reply belongs to the resume path, not a re-draw; a live drawing claim
    # means a session already owns it (no double-draw).
    issue = {"number": 5, "labels": [{"name": "agentflow:needs-mockup"}]}
    replied = [{"body": _MOCKUP_VARIANTS, "author": {"login": "o"}},
               {"body": "the second one", "author": {"login": "o"}}]
    assert _mockup_eligible(issue, replied, {"o"}) is False
    claimed = {"number": 6, "labels": [{"name": "agentflow:needs-mockup"}, {"name": DRAWING}]}
    assert _mockup_eligible(claimed, [{"body": _MOCKUP_PARK, "author": {"login": "o"}}], {"o"}) is False


def test_next_mockup_issue_picks_the_fresh_parked_issue(monkeypatch):
    # Regression (would fail before this change — the phase didn't exist): a needs-mockup issue
    # with only intake's park comment is selected for a variant round.
    issue = {"number": 11, "title": "t", "body": "b", "labels": [{"name": "agentflow:needs-mockup"}]}

    def fake_run(cmd):
        if cmd[:3] == ["gh", "issue", "list"]:
            return _FakeRun(json.dumps([issue]))
        if cmd[:4] == ["gh", "issue", "view", "11"]:
            return _FakeRun(json.dumps({"comments": [{"body": _MOCKUP_PARK, "author": {"login": "o"}}]}))
        return _FakeRun("[]")

    monkeypatch.setattr(loop, "_run", fake_run)
    monkeypatch.setattr(loop, "intake_allowlist", lambda repo, wd: {"o"})
    found = _next_mockup_issue(RepoConfig("o/r", "."))
    assert found is not None and found["number"] == 11


def test_produce_prompt_drives_ui_mockups_headless_and_one_marked_comment():
    # The produce session's marching orders: run /ui-mockups for the repo's surfaces, screenshot,
    # commit variants to a branch, and post exactly ONE issue comment starting with the marker.
    body = PRODUCE_PROMPT.format(repo="o/r", n=7, title="A screen", body="details",
                                 branch="agentflow/claude/mockup-7-a-screen",
                                 surfaces="`agentflow/static/`", disclaimer=_MOCKUP_DISCLAIMER)
    assert "/ui-mockups" in body
    assert "screenshot" in body.lower()
    assert "agentflow/static/" in body
    assert _MOCKUP_DISCLAIMER in body           # the marker line the comment must start with
    assert "one comment" in body.lower()        # exactly one issue comment
    assert "push" in body.lower()               # variant HTML preserved on a branch, not lost


def test_produce_once_selects_claims_and_spawns(monkeypatch):
    # The phase draws the selected issue: claims it, spawns a session with the produce prompt in a
    # mockup worktree, and releases the claim afterward. Never opens a PR.
    issue = {"number": 11, "title": "New panel", "body": "b", "labels": [{"name": "agentflow:needs-mockup"}]}
    monkeypatch.setattr(loop, "_next_mockup_issue", lambda cfg: issue)

    class _Builder:
        tool = "claude"
        def prepare_worktree(self, workdir, branch, wt, repo=None): pass
        def provision(self, wt): pass
        def model_for(self, c): return "opus"
        def launch(self, prompt, cwd, model):
            launched["prompt"] = prompt
            return True, "drew"
        def build(self, task): pytest.fail("produce must never build/open a PR")

    launched, claimed, released = {}, [], []
    monkeypatch.setattr(loop, "pick_pair", lambda: (_Builder(), None, ""))
    monkeypatch.setattr(loop, "ui_surfaces", lambda wd: ["agentflow/static/"])
    monkeypatch.setattr(loop, "_claim_mockup", lambda repo, n: claimed.append(n))
    monkeypatch.setattr(loop, "_release_mockup", lambda repo, n: released.append(n))
    monkeypatch.setattr(loop, "_issue_comments", lambda repo, n: [])
    monkeypatch.setattr(loop, "notify", lambda title, msg, url="": None)
    out = produce_once(RepoConfig("o/r", "/tmp"))
    assert "11" in out and "drew mockup variants" in out
    assert claimed == [11] and released == [11]
    assert "/ui-mockups" in launched["prompt"]
    assert _MOCKUP_DISCLAIMER in launched["prompt"]


def test_produce_once_noop_when_nothing_parked(monkeypatch):
    monkeypatch.setattr(loop, "_next_mockup_issue", lambda cfg: None)
    monkeypatch.setattr(loop, "pick_pair", lambda: pytest.fail("nothing to draw — don't spawn"))
    assert produce_once(RepoConfig("o/r", ".")) == "no needs-mockup issues to draw"


def test_produce_once_deferral_names_the_block_reason(monkeypatch):
    issue = {"number": 3, "title": "t", "body": "b", "labels": [{"name": "agentflow:needs-mockup"}]}
    monkeypatch.setattr(loop, "_next_mockup_issue", lambda cfg: issue)
    monkeypatch.setattr(loop, "pick_pair", lambda: (None, None, "codex: busy, claude: you"))
    out = produce_once(RepoConfig("o/r", "/tmp"))
    assert "codex: busy" in out and "deferring" in out


# --- issue #55: notify maintainer when produce_once posts a result ----------------------

def _stub_produce(monkeypatch, *, launch_ok, comments):
    """Drive produce_once with a canned launch result and issue comments; return recorded pings."""
    issue = {"number": 11, "title": "A screen", "body": "b",
             "labels": [{"name": "agentflow:needs-mockup"}]}
    monkeypatch.setattr(loop, "_next_mockup_issue", lambda cfg: issue)

    class _Builder:
        tool = "claude"
        def prepare_worktree(self, workdir, branch, wt, repo=None): pass
        def provision(self, wt): pass
        def model_for(self, c): return "opus"
        def launch(self, prompt, cwd, model): return launch_ok, ""

    monkeypatch.setattr(loop, "pick_pair", lambda: (_Builder(), None, ""))
    monkeypatch.setattr(loop, "ui_surfaces", lambda wd: ["agentflow/static/"])
    monkeypatch.setattr(loop, "_claim_mockup", lambda repo, n: None)
    monkeypatch.setattr(loop, "_release_mockup", lambda repo, n: None)
    monkeypatch.setattr(loop, "_issue_comments", lambda repo, n: comments)
    pings = []
    monkeypatch.setattr(loop, "notify", lambda title, msg, url="": pings.append((title, msg, url)))
    return pings


def test_produce_once_notifies_when_variants_posted(monkeypatch):
    # A confirmed variant-round comment triggers exactly 1 notification pointing at the issue.
    # Fails first if produce_once is silent after a real variant comment lands.
    pings = _stub_produce(monkeypatch, launch_ok=True,
                          comments=[{"body": f"{_MOCKUP_DISCLAIMER}\n\n## Variant A\n..."}])
    removed = []
    monkeypatch.setattr(loop, "remove_worktree_if_safe",
                        lambda workdir, wt: removed.append(str(wt)) or True)
    out = produce_once(RepoConfig("o/r", "/tmp"))
    assert "drew mockup variants" in out
    assert len(pings) == 1
    title, msg, url = pings[0]
    assert title == "agentflow needs you"
    assert "o/r" in msg and "11" in msg
    assert url == "https://github.com/o/r/issues/11"
    assert "MISSING-CONTEXT" not in msg
    assert removed == ["/tmp/.agentflow/worktrees/claude/mockup-11-a-screen"]


def test_produce_once_notifies_missing_context(monkeypatch):
    # A MISSING-CONTEXT comment triggers a distinct notification, never the variants-ready one.
    pings = _stub_produce(monkeypatch, launch_ok=True,
                          comments=[{"body": f"{_MOCKUP_DISCLAIMER}\nMISSING-CONTEXT: no surface found"}])
    out = produce_once(RepoConfig("o/r", "/tmp"))
    assert "MISSING-CONTEXT" in out
    assert len(pings) == 1
    title, msg, url = pings[0]
    assert title == "agentflow needs you"
    assert "MISSING-CONTEXT" in msg or "stuck" in msg.lower()
    assert url == "https://github.com/o/r/issues/11"


def test_produce_once_no_notify_on_session_error(monkeypatch):
    # A failed session (ok=False) causes 0 notifications — the issue stays eligible for retry.
    pings = _stub_produce(monkeypatch, launch_ok=False, comments=[])
    out = produce_once(RepoConfig("o/r", "/tmp"))
    assert "errored" in out
    assert pings == []


def test_produce_once_no_notify_without_confirmed_post(monkeypatch):
    # ok=True but no MOCKUP_MARK comment found — session exited without posting, no notification.
    pings = _stub_produce(monkeypatch, launch_ok=True, comments=[])
    out = produce_once(RepoConfig("o/r", "/tmp"))
    assert "drew mockup variants" in out
    assert pings == []


# --- issue #45: re-rebase survivors after main advances (ADR 0009 merge-time floor) -----

def test_base_advanced_only_when_main_moved_past_the_last_rebase():
    # The pure predicate that keeps a survivor whose base hasn't moved UNTOUCHED: when the
    # merge-base already IS main's tip the branch contains current main — no rebase, no
    # needless force-push. It only fires once main has commits the branch lacks.
    assert base_advanced("main123", "base456") is True      # main moved past the branch's base
    assert base_advanced("same789", "same789") is False     # merge-base is main's tip — up to date
    assert base_advanced("", "base456") is False            # a git blip → don't churn
    assert base_advanced("main123", "") is False


def test_conflict_already_flagged_pings_once_not_every_cycle():
    from agentflow.loop import _CONFLICT_MARK
    flagged = [{"body": "review"}, {"body": f"> *{_CONFLICT_MARK}.*\n\nrebase by hand"}]
    assert conflict_already_flagged(flagged) is True                 # our notice had the last word
    engaged = flagged + [{"body": "On it, thanks"}]                  # maintainer replied after
    assert conflict_already_flagged(engaged) is False
    assert conflict_already_flagged([]) is False


def _stub_survivor_router(monkeypatch, *, rebase, profile="reviewed"):
    """Drive _rebase_survivor with a canned rebase result; record park / merge side effects."""
    events = {"parked": [], "merged": []}
    monkeypatch.setattr(loop, "_builder_worktree", lambda cfg, tool, n, sl: "/tmp/wt")
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: rebase)
    monkeypatch.setattr(loop, "_park_conflicted_survivor",
                        lambda cfg, pr, n: events["parked"].append(pr))
    monkeypatch.setattr(loop, "_merge_autonomous_survivor",
                        lambda cfg, pr, n, sl, tool, branch: events["merged"].append(pr) or "merged")
    return events


def test_rebase_survivor_conflict_parks_and_pings_on_every_profile(monkeypatch):
    # THE acceptance criterion: a survivor that now conflicts is re-rebased within one cycle
    # and, still conflicting, parked-and-pinged — not left silent. Fails first if the conflict
    # branch stays silent instead of calling _park_conflicted_survivor.
    for profile in ("reviewed", "guarded", "autonomous"):
        events = _stub_survivor_router(monkeypatch, rebase=RebaseResult.CONFLICT, profile=profile)
        out = _rebase_survivor(RepoConfig("o/r", "/tmp"), 8, "agentflow/claude/issue-4-x", profile)
        assert events["parked"] == [8], f"conflict must ping ({profile})"
        assert events["merged"] == []
        assert "parked" in out


def test_conflict_rebase_disposes_only_after_the_notice_is_durable(monkeypatch):
    _stub_survivor_router(monkeypatch, rebase=RebaseResult.CONFLICT)
    monkeypatch.setattr(loop, "_pr_comments",
                        lambda repo, pr: [{"body": f"> *{loop._CONFLICT_MARK}.*"}])
    removed = []
    monkeypatch.setattr(loop, "remove_worktree_if_safe",
                        lambda workdir, wt: removed.append(str(wt)) or True)

    _rebase_survivor(RepoConfig("o/r", "/tmp"), 8, "agentflow/claude/issue-4-x", "reviewed")

    assert removed == ["/tmp/wt"]


def test_rebase_survivor_reviewed_clean_never_merges(monkeypatch):
    # A clean re-rebase on a reviewed repo just keeps the PR mergeable for the human — the
    # pass must never merge on reviewed/guarded.
    events = _stub_survivor_router(monkeypatch, rebase=RebaseResult.CLEAN, profile="reviewed")
    out = _rebase_survivor(RepoConfig("o/r", "/tmp"), 8, "agentflow/claude/issue-4-x", "reviewed")
    assert events["merged"] == [], "reviewed repo must never auto-merge a survivor"
    assert "mergeable for the human" in out


def test_rebase_survivor_autonomous_clean_reruns_the_merge_gate(monkeypatch):
    events = _stub_survivor_router(monkeypatch, rebase=RebaseResult.CLEAN, profile="autonomous")
    out = _rebase_survivor(RepoConfig("o/r", "/tmp"), 8, "agentflow/codex/issue-4-x", "autonomous")
    assert events["merged"] == [8]
    assert out.endswith(": merged")


def _stub_recheck(monkeypatch, prs, *, advanced, profile, comments=None):
    """Drive recheck_once: canned open-PR list, base-advanced verdicts, and per-PR routing."""
    routed = []
    monkeypatch.setattr(loop, "_open_agentflow_prs", lambda cfg: prs)
    monkeypatch.setattr(loop, "_run", lambda cmd: _FakeRun("", 0))   # fetch origin succeeds
    monkeypatch.setattr(loop, "repo_profile", lambda wd: profile)
    monkeypatch.setattr(loop, "_base_advanced_for", lambda wd, branch: advanced.get(branch))
    monkeypatch.setattr(loop, "_pr_comments", lambda repo, pr: (comments or {}).get(pr, []))

    def fake_router(cfg, pr, branch, prof):
        routed.append(pr)
        # first PR merges (autonomous), rest would too if reached
        return f"#{pr}: merged" if prof == "autonomous" else f"#{pr}: re-rebased clean"

    monkeypatch.setattr(loop, "_rebase_survivor", fake_router)
    return routed


def test_recheck_leaves_untouched_survivors_whose_base_has_not_moved(monkeypatch):
    # No force-push, no rebase, on a survivor whose base hasn't advanced (pure predicate says so).
    prs = [(7, "agentflow/claude/issue-3-a")]
    routed = _stub_recheck(monkeypatch, prs, advanced={"agentflow/claude/issue-3-a": False},
                           profile="reviewed")
    out = recheck_once(RepoConfig("o/r", "/tmp"))
    assert routed == [], "a survivor whose base hasn't moved must be left untouched"
    assert out == "no survivors to re-rebase"


def test_recheck_serializes_autonomous_merges_one_per_cycle(monkeypatch):
    # Two survivors both need a re-rebase; on autonomous only ONE lands per cycle — the rest
    # re-rebase against the new main next cycle. Fails first if recheck merges both in a pass.
    prs = [(7, "agentflow/claude/issue-3-a"), (8, "agentflow/codex/issue-4-b")]
    routed = _stub_recheck(monkeypatch, prs,
                           advanced={"agentflow/claude/issue-3-a": True,
                                     "agentflow/codex/issue-4-b": True},
                           profile="autonomous")
    recheck_once(RepoConfig("o/r", "/tmp"))
    assert routed == [7], "only one merge per cycle — stop after the first lands"


def test_recheck_skips_an_already_flagged_or_answered_survivor(monkeypatch):
    from agentflow.loop import _CONFLICT_MARK
    prs = [(7, "agentflow/claude/issue-3-a"), (8, "agentflow/codex/issue-4-b")]
    comments = {7: [{"body": f"> *{_CONFLICT_MARK}.*\n\nrebase by hand"}],   # we already pinged
                8: [{"body": "> *agentflow: parked.*"}, {"body": "Why did this conflict?"}]}  # #18 owns it
    routed = _stub_recheck(monkeypatch, prs,
                           advanced={"agentflow/claude/issue-3-a": True,
                                     "agentflow/codex/issue-4-b": True},
                           profile="reviewed", comments=comments)
    recheck_once(RepoConfig("o/r", "/tmp"))
    assert routed == [], "a flagged or maintainer-answered survivor must not be re-handled"


def _wire_survivor_merge(monkeypatch, *, reviewer_tool, ci_green, verdict):
    """Wire _merge_autonomous_survivor's real gate: a fresh review + CI + decide_merge."""
    from agentflow.loop import _merge_autonomous_survivor

    class _Reviewer:
        def __init__(self, runner): pass
        def review(self, *a, **k): return verdict

    monkeypatch.setattr(loop, "Reviewer", _Reviewer)
    monkeypatch.setattr(loop, "pick_pair",
                        lambda: (SimpleNamespace(tool="x"), SimpleNamespace(tool=reviewer_tool), ""))
    monkeypatch.setattr(loop, "_issue_meta", lambda cfg, n: {"body": "", "labels": []})
    monkeypatch.setattr(loop, "ui_surfaces", lambda wd: [])
    monkeypatch.setattr(loop, "ui_evidence_gap", lambda repo, pr, s: False)
    monkeypatch.setattr(loop, "ci_is_green", lambda repo, pr: ci_green)
    monkeypatch.setattr(loop, "_pr_comments", lambda repo, pr: [])
    monkeypatch.setattr(loop, "notify", lambda *a, **k: None)
    monkeypatch.setattr(loop.ratchet, "record", lambda *a, **k: None)
    merged, parked = [], []
    monkeypatch.setattr(loop, "squash_merge", lambda repo, pr: merged.append(pr) or True)
    monkeypatch.setattr(loop, "park", lambda repo, pr, v, reason: parked.append(pr))
    return _merge_autonomous_survivor, merged, parked


def test_autonomous_survivor_merges_only_through_the_full_gate(monkeypatch):
    # Never less safe than reviewed: a survivor lands only on independent review + green CI +
    # a clean verdict; a same-tool review (no independence) parks even when CI is green.
    fn, merged, parked = _wire_survivor_merge(monkeypatch, reviewer_tool="codex",
                                              ci_green=True, verdict=Verdict(clean=True))
    assert fn(RepoConfig("o/r", "/tmp"), 8, 8, "sl", "claude", "agentflow/claude/issue-8-sl") == "merged"
    assert merged == [8] and parked == []

    fn, merged, parked = _wire_survivor_merge(monkeypatch, reviewer_tool="claude",
                                              ci_green=True, verdict=Verdict(clean=True))
    assert fn(RepoConfig("o/r", "/tmp"), 8, 8, "sl", "claude", "agentflow/claude/issue-8-sl") == "parked"
    assert merged == [] and parked == [8]   # same-tool review can't auto-merge (ADR 0003)


def test_autonomous_survivor_parks_when_ci_is_red(monkeypatch):
    fn, merged, parked = _wire_survivor_merge(monkeypatch, reviewer_tool="codex",
                                              ci_green=False, verdict=Verdict(clean=True))
    assert fn(RepoConfig("o/r", "/tmp"), 8, 8, "sl", "claude", "agentflow/claude/issue-8-sl") == "parked"
    assert merged == [] and parked == [8]


def test_recheck_defers_when_pr_listing_fails(monkeypatch):
    # Unknown is not empty: a gh blip must defer, not read as 'no survivors'.
    monkeypatch.setattr(loop, "_open_agentflow_prs", lambda cfg: None)
    assert "deferring" in recheck_once(RepoConfig("o/r", "/tmp"))


def test_pipeline_once_reports_the_mockup_phase(monkeypatch):
    # AC: the produce phase's one-line result appears in the per-cycle log next to the others.
    monkeypatch.setattr(loop, "recover_stale_worktrees",
                        lambda *a: SimpleNamespace(removed=(), retained=()))
    monkeypatch.setattr(loop, "intake_once", lambda cfg, _log=None: "nothing")
    monkeypatch.setattr(loop, "run_once", lambda cfg, _log=None: "nothing")
    monkeypatch.setattr(loop, "produce_once", lambda cfg, _log=None: "#9: drew mockup variants")
    monkeypatch.setattr(loop, "respond_once", lambda cfg, _log=None: "nothing")
    monkeypatch.setattr(loop, "recheck_once", lambda cfg: "nothing")
    out = loop.pipeline_once(RepoConfig("o/r", "/tmp"))
    assert "mockup: #9: drew mockup variants" in out


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
    monkeypatch.setattr(loop, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(loop, "_claim_triage", lambda repo, n: None)
    monkeypatch.setattr(loop, "_release_triage", lambda repo, n: None)
    monkeypatch.setattr(loop, "notify", lambda *a, **k: None)
    monkeypatch.setattr(loop, "Intake",
                        lambda builder: SimpleNamespace(intake=lambda *a, **k: result))
    monkeypatch.setattr(loop, "apply_intake",
                        lambda repo, n, title, labels, r: applied.append(r) or "applied")
    monkeypatch.setattr(loop, "intake_result_is_durable", lambda repo, n, result: True)
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


def test_intake_disposes_after_the_routing_is_applied(monkeypatch):
    from agentflow.intake import IntakeResult

    result = IntakeResult(IntakeRoute.READY, "brief", complexity=Complexity.DEEP,
                          effort=Effort.MEDIUM)
    applied = _stub_intake_once(monkeypatch, result)
    removed = []
    monkeypatch.setattr(loop, "remove_worktree_if_safe",
                        lambda workdir, wt: removed.append((len(applied), str(wt))) or True)

    loop.intake_once(RepoConfig("o/r", "/tmp"))

    assert removed == [(1, "/tmp/.agentflow/worktrees/claude-intake/issue-5")]
