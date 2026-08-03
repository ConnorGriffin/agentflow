"""Pure helpers of the M0 loop. The live orchestration (build/review/merge) is
proven by the first live run; these are the parsing bits that must be exact."""

from types import SimpleNamespace

import pytest

from agentflow import coordinated_review, github, labels as label_vocab, loop, pipeline
from agentflow.gate import respond_reply_disclaimer
from agentflow.github import pr_number
from agentflow.intake import (INTAKE_MARK, IntakeRoute, awaiting_recheck, compose_ready_body,
                              held_build_result)
from agentflow.labels import (DRAWING, MOCKUP_MARK, complexity_from_labels, effort_from_labels,
                              mockup_scope_from_labels)
from agentflow.loop import (RebaseResult, RepoConfig, _free_to_dispatch, _issues_in_flight,
                            _native_blockers,
                            _next_pr_awaiting_reply, _next_ready_issue, _next_resumable_issue,
                            _rebase_survivor, _untriaged, base_advanced, build_issue,
                            conflict_already_flagged, recheck_once)
from agentflow.prompts import (BUILD_PROMPT, MOCKUP_DISCLAIMER, PRODUCE_PROMPT, RESPOND_PROMPT,
                               REVISE_PROMPT, SCOPE_GUIDANCE)
from agentflow.repo_facts import (SurfaceDeclaration, intake_allowlist, repo_profile,
                                  surface_declaration, surfaces_phrase, ui_surfaces)
from agentflow.runner import Complexity, Effort, MockupScope
from agentflow.worktree_ref import issue_of_branch, slug


class _FakeRun:
    """Stand-in for a `subprocess`-style result — only `.returncode`/`.stdout` are read."""
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _issue_row(number, labels=(), title="t", body="b"):
    """A typed issue row as the shared GitHub module's discovery listing returns."""
    return github.IssueRow(number=number, title=title, body=body,
                               labels=frozenset(labels))


def _pr_row(number, head_ref_name, head_ref_oid=""):
    """A typed open-PR row as the shared GitHub module's discovery listing returns."""
    return github.PrRow(number=number, head_ref_name=head_ref_name,
                            head_ref_oid=head_ref_oid)


def test_complexity_from_labels_reads_the_label():
    assert complexity_from_labels(["ready-for-agent", "agentflow:complexity:standard"]) is Complexity.STANDARD
    assert complexity_from_labels(["agentflow:complexity:deep"]) is Complexity.DEEP


def test_complexity_from_labels_is_none_without_one():
    # Hard gate (ADR 0018): no complexity label => the loop must skip, not guess.
    assert complexity_from_labels(["ready-for-agent", "bug"]) is None


def test_a_double_stamped_dial_resolves_the_same_way_every_run():
    # A typed row carries its labels as a set, and the dial decoders take the first match, so an
    # issue a human stamped with both sizes must not pick a different builder model per process.
    row = github.IssueRow(number=7, title="t", body="",
                          labels=frozenset({"agentflow:complexity:standard",
                                            "agentflow:complexity:deep", "ready-for-agent"}))
    names = [label["name"] for label in loop._row_dict(row)["labels"]]
    assert names == sorted(names)
    assert complexity_from_labels(names) is Complexity.DEEP
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


def test_free_to_dispatch_skips_claimed_or_in_flight(monkeypatch):
    monkeypatch.setattr(loop, "_native_blockers", lambda cfg, n: set())   # no native edges
    cfg = RepoConfig("o/r", ".")
    ready = {"number": 5, "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:complexity:standard"}]}
    assert _free_to_dispatch(cfg, ready, set()) is True
    assert _free_to_dispatch(cfg, ready, {5}) is False   # an open agentflow PR already owns it
    claimed = {"number": 6, "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:building"}]}
    assert _free_to_dispatch(cfg, claimed, set()) is False   # claimed — an agent is building it


def test_free_to_dispatch_ignores_blocked_by_in_incidental_prose(monkeypatch):
    issue = {"number": 5, "body": "This may be Blocked by #41 after the next review.",
             "labels": [{"name": "ready-for-agent"}]}
    # No native edges, and the prose isn't a real `Blocked by #N` declaration — so no blocker
    # state is ever read (a read here would mean the incidental prose was treated as a blocker).
    monkeypatch.setattr(loop, "_native_blockers", lambda cfg, n: set())
    monkeypatch.setattr(github, "issue_state",
                        lambda repo, n: pytest.fail("prose is not a declaration"))

    assert _free_to_dispatch(RepoConfig("o/r", "."), issue, set()) is True


def test_untriaged_skips_state_labels_and_triage_claim():
    fresh = {"number": 1, "labels": [{"name": "bug"}]}
    assert _untriaged(fresh) is True
    triaging = {"number": 2, "labels": [{"name": "bug"}, {"name": "agentflow:triaging"}]}
    assert _untriaged(triaging) is False   # a grounding session already owns it — no re-dispatch
    routed = {"number": 3, "labels": [{"name": "ready-for-agent"}]}
    assert _untriaged(routed) is False     # already has a state label


def test_untriaged_skips_downstream_claim_labels():
    # A mid-pipeline issue whose only label is a build/mockup claim is not un-triaged, so
    # intake never re-picks it after the reconciler strips its stale triaging label (#201).
    building = {"number": 6, "labels": [{"name": "agentflow:building"}]}
    drawing = {"number": 7, "labels": [{"name": "agentflow:drawing-mockup"}]}
    unlabeled = {"number": 8, "labels": []}

    assert _untriaged(building) is False
    assert _untriaged(drawing) is False
    assert _untriaged(unlabeled) is True


def test_untriaged_skips_wayfinder_issues():
    map_issue = {"number": 4, "labels": [{"name": "wayfinder:map"}]}
    decision_ticket = {"number": 5, "labels": [{"name": "wayfinder:research"}]}

    assert _untriaged(map_issue) is False
    assert _untriaged(decision_ticket) is False


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
    monkeypatch.setattr(github, "list_open_prs", lambda repo, **k: None)
    assert _issues_in_flight(RepoConfig("o/r", ".")) is None


def test_issues_in_flight_recognizes_closing_reference_off_convention_branch(monkeypatch):
    # A hand-driven build opens a PR on an off-convention branch (`codex/40-foo`) that the
    # branch regex can't parse — but its declared closing reference #40 must still dedup.
    prs = [github.PrRow(number=1, head_ref_name="codex/40-foo", head_ref_oid="sha",
                        closing_issues=(40,))]
    monkeypatch.setattr(github, "list_open_prs", lambda repo, **k: prs)
    assert 40 in _issues_in_flight(RepoConfig("o/r", "."))


def test_issues_in_flight_still_recognizes_conventional_branch(monkeypatch):
    prs = [github.PrRow(number=1, head_ref_name="agentflow/codex/issue-7-slug",
                        head_ref_oid="sha")]
    monkeypatch.setattr(github, "list_open_prs", lambda repo, **k: prs)
    assert 7 in _issues_in_flight(RepoConfig("o/r", "."))


def test_next_ready_issue_fails_closed_when_in_flight_unknown(monkeypatch):
    ready = [_issue_row(5, ["ready-for-agent"], body="")]
    monkeypatch.setattr(github, "list_issues", lambda repo, **k: list(ready))
    monkeypatch.setattr(loop, "_native_blockers", lambda cfg, n: set())   # no blockers
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: None)
    assert _next_ready_issue(RepoConfig("o/r", ".")) is None
    # sanity: same listing dispatches once in-flight is actually known
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    assert _next_ready_issue(RepoConfig("o/r", "."))["number"] == 5


def _ready_dispatch(monkeypatch, ready, blocker_states, native=None):
    """Install the ready-issue dispatch gate through the module interface: the labelled listing
    (`github.list_issues` → typed rows), each issue's native blocked-by set (`loop._native_blockers`),
    and each blocker's state (`github.issue_state`). `blocker_states` maps blocker number → state
    (absent ⇒ unreadable → None). `native` maps issue number → its resolved same-repo blocker set."""
    native = native or {}
    rows = [_issue_row(d["number"], [lbl["name"] for lbl in d.get("labels", [])],
                       title=d.get("title", "t"), body=d.get("body", ""))
            for d in ready]
    monkeypatch.setattr(github, "list_issues", lambda repo, **k: list(rows))
    monkeypatch.setattr(loop, "_native_blockers", lambda cfg, n: set(native.get(n, set())))
    monkeypatch.setattr(github, "issue_state", lambda repo, n: blocker_states.get(n))


def _native_edge(number, state, repo="o/r"):
    return {"number": number, "state": state, "repository": {"full_name": repo}}


def test_next_ready_issue_skips_an_issue_blocked_by_an_open_issue(monkeypatch):
    ready = [{"number": 42, "title": "dependent", "body": "Blocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    _ready_dispatch(monkeypatch, ready, {41: "OPEN"})
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
    _ready_dispatch(monkeypatch, ready, {41: "OPEN"})
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    selected = _next_ready_issue(RepoConfig("o/r", "."), _log=logs.append)

    assert selected["number"] == 43
    assert any("#42" in message and "#41" in message for message in logs)


def test_next_ready_issue_rechecks_all_blockers_each_pass(monkeypatch):
    ready = [{"number": 42, "title": "dependent",
              "body": "Blocked by #40\n\nBlocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    blocker_states = {40: "CLOSED", 41: "OPEN"}
    _ready_dispatch(monkeypatch, ready, blocker_states)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    cfg = RepoConfig("o/r", ".")

    assert _next_ready_issue(cfg) is None
    blocker_states[41] = "CLOSED"
    assert _next_ready_issue(cfg)["number"] == 42


def test_next_ready_issue_fails_closed_when_blocker_state_is_unknown(monkeypatch):
    ready = [{"number": 42, "title": "dependent", "body": "Blocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    logs = []
    _ready_dispatch(monkeypatch, ready, {41: None})
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", "."), _log=logs.append) is None
    assert any("#42" in message and "#41" in message
               and "could not be determined" in message for message in logs)


def test_next_ready_issue_fails_closed_on_malformed_blocker_state(monkeypatch):
    ready = [{"number": 42, "title": "dependent", "body": "Blocked by #41",
              "labels": [{"name": "ready-for-agent"}]}]
    logs = []
    # A malformed blocker-state response reads back as None (unknown) through the module.
    _ready_dispatch(monkeypatch, ready, {41: None})
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", "."), _log=logs.append) is None
    assert any("#42" in message and "#41" in message
               and "could not be determined" in message for message in logs)


def test_next_ready_issue_sees_blocker_preserved_by_intake(monkeypatch):
    body = compose_ready_body("## Agent Brief\nBuild the dependent slice.",
                              "Original request.\n\nBlocked by #41")
    ready = [{"number": 42, "title": "dependent", "body": body,
              "labels": [{"name": "ready-for-agent"}]}]
    _ready_dispatch(monkeypatch, ready, {41: "OPEN"})
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert "Blocked by #41" in body
    assert _next_ready_issue(RepoConfig("o/r", ".")) is None


def test_native_blockers_unions_same_repo_edges_and_ignores_cross_repo(monkeypatch):
    # The native blocked-by read, exercised through the shared GitHub module: the endpoint's
    # edges come back via `github.api`, and only same-repo edges join the gate.
    edges = [_native_edge(41, "OPEN"), _native_edge(7, "CLOSED", "other/repo")]
    monkeypatch.setattr(github, "api", lambda *a, **k: edges)

    blockers = _native_blockers(RepoConfig("o/r", "."), 42)

    assert blockers == {41}   # same-repo edge unioned; cross-repo edge ignored


def test_native_blockers_is_unknown_when_the_read_fails(monkeypatch):
    # Unreadable edges are None (unknown), never an empty set — the caller then fails closed.
    monkeypatch.setattr(github, "api", lambda *a, **k: None)
    assert _native_blockers(RepoConfig("o/r", "."), 42) is None


def test_next_ready_issue_holds_on_open_native_blocker(monkeypatch):
    # A native blocked-by edge with no prose line at all must still hold the issue —
    # the regression this ticket exists to fix (native edges were invisible before).
    ready = [{"number": 42, "title": "dependent", "body": "",
              "labels": [{"name": "ready-for-agent"}]}]
    native = {42: {41}}
    _ready_dispatch(monkeypatch, ready, {41: "OPEN"}, native)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", ".")) is None


def test_next_ready_issue_frees_on_closed_native_blocker(monkeypatch):
    ready = [{"number": 42, "title": "dependent", "body": "",
              "labels": [{"name": "ready-for-agent"}]}]
    native = {42: {41}}
    _ready_dispatch(monkeypatch, ready, {41: "CLOSED"}, native)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", "."))["number"] == 42


def test_next_ready_issue_unions_prose_and_native_blockers(monkeypatch):
    # Prose blocker #40 is closed; a native blocker #41 is open. Either open source holds.
    ready = [{"number": 42, "title": "dependent", "body": "Blocked by #40",
              "labels": [{"name": "ready-for-agent"}]}]
    native = {42: {41}}
    blocker_states = {40: "CLOSED", 41: "OPEN"}
    _ready_dispatch(monkeypatch, ready, blocker_states, native)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    cfg = RepoConfig("o/r", ".")

    assert _next_ready_issue(cfg) is None
    blocker_states[41] = "CLOSED"
    assert _next_ready_issue(cfg)["number"] == 42


def test_next_ready_issue_fails_closed_when_native_read_fails(monkeypatch):
    ready = [_issue_row(42, ["ready-for-agent"], title="dependent", body="")]
    logs = []
    monkeypatch.setattr(github, "list_issues", lambda repo, **k: list(ready))
    monkeypatch.setattr(github, "api", lambda *a, **k: None)   # native fetch failed — unknown
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", "."), _log=logs.append) is None
    assert any("#42" in message and "could not be determined" in message for message in logs)


def test_next_ready_issue_fails_closed_on_malformed_native_response(monkeypatch):
    ready = [_issue_row(42, ["ready-for-agent"], title="dependent", body="")]
    # A malformed native response reads back as None through the module — never "no blockers".
    monkeypatch.setattr(github, "list_issues", lambda repo, **k: list(ready))
    monkeypatch.setattr(github, "api", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", ".")) is None


def test_next_ready_issue_ignores_cross_repo_native_blocker(monkeypatch):
    # A native edge pointing at another repo does not by itself hold the issue.
    ready = [_issue_row(42, ["ready-for-agent"], title="dependent", body="")]
    edges = [_native_edge(41, "OPEN", repo="other/repo")]
    monkeypatch.setattr(github, "list_issues", lambda repo, **k: list(ready))
    monkeypatch.setattr(github, "api", lambda *a, **k: edges)   # real filtering drops it
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())

    assert _next_ready_issue(RepoConfig("o/r", "."))["number"] == 42


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


def test_build_prompt_prescribes_the_browserless_screenshot_path():
    # A guarded-project PR parked because the builder tried GitHub's drag-drop upload,
    # which needs a signed-in browser this host doesn't have. The prompt names the committed
    # path (docs/screenshots/ on the branch), pins the image host to the immutable commit
    # (issue #205), and forbids the browser route.
    body = BUILD_PROMPT.format(repo="o/r", n=7, title="x", body="", effort="high",
                               surfaces="`frontend/`")
    assert "docs/screenshots/issue-7/" in body
    assert "https://github.com/o/r/raw/<commit-sha>/" in body
    assert "upload images through a web browser" in body


def test_revise_prompt_prescribes_the_browserless_screenshot_path():
    # A revise sent to fix missing screenshots must not bounce off the same browser wall.
    body = REVISE_PROMPT.format(n=5, repo="o/r", findings="- fix it", surfaces="`frontend/`")
    assert "docs/screenshots/" in body
    assert "upload images through a web browser" in body


def test_build_prompt_names_the_charter_test_standard():
    # ADR 0022: the builder is told the bar up front, not only caught at cross-review.
    body = BUILD_PROMPT.format(repo="o/r", n=7, title="x", body="", effort="high",
                               surfaces="`agentflow/static/`")
    assert "public interface" in body
    assert "failed first" in body.lower()


def test_build_prompt_states_the_deletion_test_self_check():
    # issue #382: the builder is told to apply the charter's deep-module rules before the
    # PR goes up, the same way it is told the test standard up front.
    body = BUILD_PROMPT.format(repo="o/r", n=7, title="x", body="", effort="high",
                               surfaces="`agentflow/static/`")
    assert "deletion" in body.lower()
    assert "interface-depth" in body.lower() or "interface depth" in body.lower()


def test_ui_surfaces_reads_the_declared_prefixes(tmp_path):
    # Parsed the same way as `profile:` — comma-separated path prefixes, per repo.
    (tmp_path / "AGENTS.md").write_text("# repo\n\nprofile: reviewed\n"
                                        "ui-surfaces: frontend/, agentflow/static/\n\n## facts\n")
    assert ui_surfaces(str(tmp_path)) == ["frontend/", "agentflow/static/"]


def test_ui_surfaces_empty_when_undeclared(tmp_path):
    # No declaration → no surfaces → the UI-evidence gate is inert for a non-UI repo.
    (tmp_path / "AGENTS.md").write_text("profile: reviewed\n")
    assert ui_surfaces(str(tmp_path)) == []


@pytest.mark.parametrize("written", ["none", "None", "NONE", "  none  "])
def test_declared_headless_is_told_apart_from_silence(tmp_path, written):
    # Issue #337: a repo that says it has no UI reads as declared-with-no-surfaces, so the
    # gate stays inert while the fleet audit can still see it was answered.
    (tmp_path / "AGENTS.md").write_text(f"profile: reviewed\nui-surfaces: {written}\n")
    declaration = surface_declaration(str(tmp_path))
    assert declaration.surfaces == ()
    assert declaration.declared
    assert declaration.headless


def test_silence_reports_undeclared(tmp_path):
    (tmp_path / "AGENTS.md").write_text("profile: reviewed\n")
    declaration = surface_declaration(str(tmp_path))
    assert not declaration.declared
    assert not declaration.headless


def test_prompts_do_not_demand_screenshots_of_a_headless_repo():
    # A declared-headless repo must stop hearing the vague "any user-facing surface" ask;
    # a real declaration and an unanswered one are unchanged.
    assert "no screenshot is required" in surfaces_phrase(
        SurfaceDeclaration(declared=True))
    assert surfaces_phrase(
        SurfaceDeclaration(surfaces=("frontend/",), declared=True)) == "`frontend/`"
    assert surfaces_phrase(SurfaceDeclaration()) == \
        "any user-facing surface (frontend, UI templates, etc.)"


def test_revise_prompt_carries_both_evidence_gates():
    # A revise pass must not silently degrade compliance: it names both the screenshot
    # gate (with the repo's surfaces) and the plain-language body gate.
    body = REVISE_PROMPT.format(n=5, repo="o/r", findings="- fix it",
                                surfaces="`agentflow/static/`")
    assert "screenshot" in body.lower()
    assert "agentflow/static/" in body
    assert "plain" in body.lower()


def test_work_order_helper_is_gone():
    # ADR 0022 retired the separate frozen work-order comment; nothing should read one.
    assert not hasattr(loop, "_work_order")


def _canned_issue(*, state="OPEN", title="t", body="b", labels=()):
    return github.IssueView(title=title, body=body, state=state, url="",
                            labels=frozenset(labels), comments=[])


def _issue_view(monkeypatch, issue):
    """Serve build_issue's whole-issue read a canned issue, with no native blocked-by edges
    (the dispatch gate reads those too)."""
    monkeypatch.setattr(github, "issue_view", lambda repo, n: issue)
    monkeypatch.setattr(loop, "_native_blockers", lambda cfg, n: set())


def test_build_issue_submits_a_ready_issue_to_the_coordinator(monkeypatch):
    from agentflow import coordinated_build
    from agentflow.coordinator.record import Record, WAITING

    issue = _canned_issue(state="OPEN", title="t", body="b",
                         labels=["ready-for-agent", "agentflow:complexity:standard"])
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    monkeypatch.setattr(loop, "pick_pair",
                        lambda operator=False, floodgates=False: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(loop, "claim", lambda repo, n, _label: True)
    submission = SimpleNamespace(resume=0, pool="claude")
    monkeypatch.setattr(coordinated_build, "build_submission", lambda *_, **__: submission)
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    monkeypatch.setattr(coordinated_build, "resume_if_held", lambda sub, records: sub)
    submitted = []
    waiting = Record(identity="o/r|5|build|-", stage="build", pool="claude", demand=5,
                     state=WAITING)
    coordinator = SimpleNamespace(
        submit_stage=lambda sub: submitted.append(sub) or "o/r|5|build|-",
        stage_record=lambda identity: waiting)
    monkeypatch.setattr(pipeline, "build_coordinator", lambda: coordinator)
    reconciled = []
    monkeypatch.setattr(pipeline, "reconcile_and_project", reconciled.append)

    out = build_issue(RepoConfig("o/r", "/tmp"), 5)

    assert out == "#5: submitted to coordinator → claude (build)"
    assert submitted == [submission]
    assert reconciled == [coordinator]


def test_build_issue_resumes_an_exhausted_held_build_on_the_original_worktree(monkeypatch):
    # After a maintainer `pickup` returns a budget-exhausted issue to `ready-for-agent`, `build <N>`
    # is the explicit, durable resume: it opens a fresh bounded execution at a new resume identity,
    # pinned back to the original builder's pool and retained worktree — never the re-picked tool
    # (#245). resume_if_held runs for real here.
    from agentflow import coordinated_build
    from agentflow.coordinator import Submission
    from agentflow.coordinator.record import HELD, WAITING, Record

    issue = _canned_issue(state="OPEN", title="t", body="b",
                         labels=["ready-for-agent", "agentflow:complexity:standard"])
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    # pick_pair offers codex, but the held Build was built by claude — the resume must pin back.
    monkeypatch.setattr(loop, "pick_pair",
                        lambda operator=False, floodgates=False: (SimpleNamespace(tool="codex"), None, ""))
    claimed = []
    monkeypatch.setattr(loop, "claim", lambda repo, n, _label: claimed.append(n) or True)
    base = Submission(repo="o/r", subject="5", stage="build", pool="codex",
                      complexity="standard", source="/w/.agentflow/worktrees/codex/issue-5-t")
    monkeypatch.setattr(coordinated_build, "build_submission", lambda *_, **__: base)
    held = Record(identity="o/r|5|build|-", stage="build", pool="claude", demand=5,
                  repo="o/r", subject="5", state=HELD, claim=False,
                  source="/w/.agentflow/worktrees/claude/issue-5-t", input_ptr="the original brief")
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [held])
    submitted = []
    resumed_rec = Record(identity="o/r|5|build|-|s1", stage="build", pool="claude", demand=5,
                         repo="o/r", subject="5", state=WAITING, resume=1)
    coordinator = SimpleNamespace(
        submit_stage=lambda sub: submitted.append(sub) or "o/r|5|build|-|s1",
        stage_record=lambda identity: resumed_rec)
    monkeypatch.setattr(pipeline, "build_coordinator", lambda: coordinator)
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda c: None)

    out = build_issue(RepoConfig("o/r", "/tmp"), 5)

    assert out == "#5: resumed to coordinator → claude (build)"
    assert claimed == [5]
    assert len(submitted) == 1
    resumed_sub = submitted[0]
    assert resumed_sub.resume == 1                        # a new bounded execution, not the held one
    assert resumed_sub.pool == "claude"                  # pinned to the held builder's pool
    assert resumed_sub.source == "/w/.agentflow/worktrees/claude/issue-5-t"  # retained worktree
    assert resumed_sub.input_ptr == "the original brief"  # the same durable brief, re-run


def test_build_issue_withdraws_the_submission_when_the_claim_race_is_lost(monkeypatch):
    # The resume submits the coordinator record before claiming the issue (#245). If the claim then
    # loses a rare race, the runnable WAITING record must be withdrawn — otherwise it is orphaned
    # without the building label and a later cycle launches it unguarded.
    from agentflow import coordinated_build
    from agentflow.coordinator.record import Record, WAITING

    issue = _canned_issue(state="OPEN", title="t", body="b",
                         labels=["ready-for-agent", "agentflow:complexity:standard"])
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    monkeypatch.setattr(loop, "pick_pair",
                        lambda operator=False, floodgates=False: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(loop, "claim", lambda repo, n, _label: False)   # the race is lost
    submission = SimpleNamespace(resume=0, pool="claude")
    monkeypatch.setattr(coordinated_build, "build_submission", lambda *_, **__: submission)
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    monkeypatch.setattr(coordinated_build, "resume_if_held", lambda sub, records: sub)
    waiting = Record(identity="o/r|5|build|-", stage="build", pool="claude", demand=5,
                     state=WAITING)
    withdrawn = []
    coordinator = SimpleNamespace(
        submit_stage=lambda sub: "o/r|5|build|-",
        stage_record=lambda identity: waiting,
        withdraw_stage=lambda identity: withdrawn.append(identity) or True)
    monkeypatch.setattr(pipeline, "build_coordinator", lambda: coordinator)
    monkeypatch.setattr(pipeline, "reconcile_and_project",
                        lambda c: pytest.fail("must not project a withdrawn submission"))

    out = build_issue(RepoConfig("o/r", "/tmp"), 5)

    assert "could not claim" in out and "withdrew" in out
    assert withdrawn == ["o/r|5|build|-"]                       # the orphan was rolled back


def test_build_issue_acknowledges_a_resume_already_running(monkeypatch):
    # A repeated `build <N>` while a resume is already live is correctly non-duplicating — it
    # idempotently reuses the terminal held record — but must acknowledge the running resume rather
    # than report the issue as merely 'still held' (#245).
    from agentflow import coordinated_build
    from agentflow.coordinator.record import HELD, Record, WAITING

    issue = _canned_issue(state="OPEN", title="t", body="b",
                         labels=["ready-for-agent", "agentflow:complexity:standard"])
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    monkeypatch.setattr(loop, "pick_pair",
                        lambda operator=False, floodgates=False: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(loop, "claim",
                        lambda repo, n, _label: pytest.fail("must not claim a held record"))
    submission = SimpleNamespace(repo="o/r", subject="5", resume=0, pool="claude")
    monkeypatch.setattr(coordinated_build, "build_submission", lambda *_, **__: submission)
    # The held original at resume 0 plus a live resume successor already running at resume 1.
    held = Record(identity="o/r|5|build|-", stage="build", pool="claude", demand=5,
                  repo="o/r", subject="5", state=HELD, resume=0)
    live = Record(identity="o/r|5|build|-|s1", stage="build", pool="claude", demand=5,
                  repo="o/r", subject="5", state=WAITING, resume=1)
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [held, live])
    # resume_if_held runs for real: the latest live Build is not held, so the submission is unchanged
    # and idempotently reuses the terminal held record.
    coordinator = SimpleNamespace(
        submit_stage=lambda sub: "o/r|5|build|-",
        stage_record=lambda identity: held)
    monkeypatch.setattr(pipeline, "build_coordinator", lambda: coordinator)

    out = build_issue(RepoConfig("o/r", "/tmp"), 5)

    assert "already running" in out


def test_build_issue_dispatches_again_after_a_withdrawn_never_run_attempt(monkeypatch, tmp_path):
    # A by-hand build whose claim race is lost withdraws the never-run record; a later `build <N>`
    # must dispatch a genuinely runnable build rather than reporting the issue 'still held' forever
    # (#251). Drives the real coordinator over an isolated store so the withdraw/resubmit seam is
    # exercised end-to-end, not faked.
    from agentflow import coordinated_build
    from agentflow.coordinator import Submission

    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path))
    issue = _canned_issue(state="OPEN", title="t", body="b",
                         labels=["ready-for-agent", "agentflow:complexity:standard"])
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    monkeypatch.setattr(loop, "pick_pair",
                        lambda operator=False, floodgates=False: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(coordinated_build, "build_submission",
                        lambda *_, **__: Submission(repo="o/r", subject="5", stage="build",
                                              pool="claude", complexity="deep"))
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda c: None)
    claim = {"ok": False}
    monkeypatch.setattr(loop, "claim", lambda repo, n, _label: claim["ok"])

    lost = build_issue(RepoConfig("o/r", "/tmp"), 5)          # the claim race is lost
    assert "could not claim" in lost and "withdrew" in lost

    claim["ok"] = True
    out = build_issue(RepoConfig("o/r", "/tmp"), 5)           # a fresh by-hand build
    assert out == "#5: submitted to coordinator → claude (build)"  # dispatches, not 'still held'


def test_build_issue_refuses_an_open_blocker(monkeypatch):
    issue = _canned_issue(state="OPEN", title="dependent", body="Blocked by #41",
                         labels=["ready-for-agent", "agentflow:complexity:standard"])
    monkeypatch.setattr(github, "issue_view", lambda repo, n: issue)
    monkeypatch.setattr(loop, "_native_blockers", lambda cfg, n: set())
    monkeypatch.setattr(github, "issue_state", lambda repo, n: "OPEN")   # blocker #41 open
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set())
    out = build_issue(RepoConfig("o/r", "/tmp"), 42)

    assert "blocker" in out.lower()


def test_build_issue_refuses_a_held_issue_and_points_at_pickup(monkeypatch):
    issue = _canned_issue(state="OPEN", title="t", body="b",
                         labels=["agentflow:needs-grilling"])
    _issue_view(monkeypatch, issue)
    out = build_issue(RepoConfig("o/r", "/tmp"), 7)
    assert "pickup" in out and "7" in out


def test_build_issue_refuses_an_untriaged_issue_and_points_at_triage(monkeypatch):
    _issue_view(monkeypatch, _canned_issue(labels=["bug"]))
    out = build_issue(RepoConfig("o/r", "/tmp"), 8)
    assert "triage" in out or "scope" in out


_INTAKE_COMMENT = f"{INTAKE_MARK} — generated by AI.\n\nHold message."
_MAINTAINER_REPLY = "Here is my answer / waiver."


def _install_resumable(monkeypatch, grilling_issues, mockup_issues, reply=_MAINTAINER_REPLY,
                       comments=None):
    """Install a resumable-issue queue through the module interface: the held-label listings
    (`github.list_issues`) and each issue's comment thread (`github.issue_comment_rows`)."""
    # The reply is from the repo owner (always allowlisted), so it counts as a resume-
    # triggering maintainer reply under the allowlist filter (issue #25).
    if comments is None:
        comments = [{"body": _INTAKE_COMMENT}, {"body": reply, "author": {"login": "o"}}]

    def rows(dicts):
        return [_issue_row(d["number"], [lbl["name"] for lbl in d.get("labels", [])],
                           title=d.get("title", "t"), body=d.get("body", "b")) for d in dicts]

    by_label = {"agentflow:needs-grilling": rows(grilling_issues),
                "agentflow:needs-mockup": rows(mockup_issues)}
    monkeypatch.setattr(github, "list_issues",
                        lambda repo, *, label=None, limit=100: list(by_label.get(label, [])))
    monkeypatch.setattr(github, "issue_comment_rows", lambda repo, n: comments)


def test_next_resumable_issue_picks_up_needs_grilling_reply(monkeypatch):
    issue = {"number": 10, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:needs-grilling"}]}
    _install_resumable(monkeypatch, [issue], [])
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
    _install_resumable(monkeypatch, [], [issue])
    result = _next_resumable_issue(RepoConfig("o/r", "."))
    assert result is not None
    found, reply = result
    assert found["number"] == 11
    assert _MAINTAINER_REPLY in reply


def test_next_resumable_issue_waits_while_mockup_owns_the_drawing_claim(monkeypatch):
    issue = {"number": 11, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:needs-mockup"}, {"name": DRAWING}]}
    _install_resumable(monkeypatch, [], [issue])

    assert _next_resumable_issue(RepoConfig("o/r", ".")) is None


def test_next_resumable_issue_returns_none_when_last_comment_is_ours(monkeypatch):
    issue = {"number": 12, "title": "t", "body": "b",
             "labels": [{"name": "agentflow:needs-mockup"}]}
    # last comment is ours (contains INTAKE_MARK) — not awaiting a reply
    _install_resumable(monkeypatch, [], [issue], comments=[{"body": _INTAKE_COMMENT}])
    assert _next_resumable_issue(RepoConfig("o/r", ".")) is None


def test_next_resumable_issue_returns_none_on_gh_error(monkeypatch):
    monkeypatch.setattr(github, "list_issues", lambda repo, **k: None)
    assert _next_resumable_issue(RepoConfig("o/r", ".")) is None


def test_build_issue_refuses_an_in_flight_issue(monkeypatch):
    issue = _canned_issue(state="OPEN", title="t", body="b",
                         labels=["ready-for-agent", "agentflow:complexity:deep"])
    _issue_view(monkeypatch, issue)
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: {9})   # an open agentflow PR owns it
    out = build_issue(RepoConfig("o/r", "/tmp"), 9)
    assert "flight" in out.lower() or "claim" in out.lower()


# --- issue #18: answering maintainer comments on parked PRs ---------------------

_PARK = "> *agentflow: parked for human review.*\n\nfindings"
_MAINT = "Show me a screenshot please?"


def _pr_gh(monkeypatch, prs, comments_by_pr):
    """Serve _next_pr_awaiting_reply canned open-PR rows (`github.list_open_prs`) and per-PR
    comment threads (`github.pr_comment_rows`) through the module interface."""
    monkeypatch.setattr(github, "list_open_prs", lambda repo, **k: list(prs))
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: comments_by_pr.get(pr, []))


def test_next_pr_awaiting_reply_picks_the_unanswered_one(monkeypatch):
    prs = [_pr_row(7, "agentflow/claude/issue-3-do-thing", "head-7"),
           _pr_row(8, "agentflow/codex/issue-4-other", "head-8")]
    comments = {7: [{"body": _PARK}],                                    # our marker last — answered
                8: [{"body": _PARK}, {"body": _MAINT, "id": "IC_8"}]}    # maintainer last — pending
    _pr_gh(monkeypatch, prs, comments)
    # The fourth element is the stable id of the unanswered comment — the Respond target (issue #107).
    assert _next_pr_awaiting_reply(RepoConfig("o/r", ".")) == (
        8, "agentflow/codex/issue-4-other", _MAINT, "IC_8", "head-8")


def test_next_pr_awaiting_reply_advances_one_comment_target_at_a_time(monkeypatch):
    branch = "agentflow/claude/issue-4-other"
    comments = [
        {"body": _PARK},
        {"body": "First question", "id": "IC_1"},
        {"body": "Second question", "id": "IC_2"},
    ]
    _pr_gh(monkeypatch, [_pr_row(8, branch, "head-8")], {8: comments})
    assert _next_pr_awaiting_reply(RepoConfig("o/r", ".")) == (
        8, branch, "First question", "IC_1", "head-8")

    comments.append({"body": respond_reply_disclaimer("IC_1") + "\n\nAnswered."})
    assert _next_pr_awaiting_reply(RepoConfig("o/r", ".")) == (
        8, branch, "Second question", "IC_2", "head-8")


def test_next_pr_awaiting_reply_ignores_human_branches(monkeypatch):
    # A maintainer's own branch is not an agentflow PR — never spawn a responder on it.
    prs = [_pr_row(9, "my-hotfix")]
    _pr_gh(monkeypatch, prs, {9: [{"body": _MAINT}]})
    assert _next_pr_awaiting_reply(RepoConfig("o/r", ".")) is None


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


def test_rebase_checks_out_before_claiming_the_worktree_session(monkeypatch, tmp_path):
    # Regression: the survivor rebase must run its worktree checkout BEFORE it marks the worktree
    # active. `_checkout_pr_branch` reuses an existing worktree only when its own idle guard passes,
    # and that guard rejects an *active* worktree — so claiming the session first made the rebase
    # refuse its own worktree and fail 'plumbing failed' every cycle. Fails before the fix
    # (active_at_checkout would be True); passes after.
    from agentflow.runner import _worktree_is_active
    wt = tmp_path / "wt"
    wt.mkdir()
    seen = {}

    def fake_checkout(cfg, branch, w):
        seen["active_at_checkout"] = _worktree_is_active(w)
        return True

    monkeypatch.setattr(loop, "_checkout_pr_branch", fake_checkout)
    # rev-parse HEAD returns the same SHA twice → a no-op rebase, short-circuiting before push.
    monkeypatch.setattr(loop, "_run", lambda cmd: _FakeRun("same-head", 0))

    result = loop._rebase_branch(RepoConfig("o/r", str(tmp_path)), "branch", wt)

    assert seen["active_at_checkout"] is False        # checkout ran before the session was claimed
    assert result is RebaseResult.NOOP
    assert _worktree_is_active(wt) is False            # and the session is released afterward


# --- issue #29: the mockup-production phase --------------------------------------------

# Intake's park/kickoff comment on a needs-mockup issue: carries INTAKE_MARK, no MOCKUP_MARK.
_MOCKUP_PARK = f"{INTAKE_MARK} — let's mock this up.\n\nA `/ui-craft lock` kickoff."
# The produce phase's own variant-round comment: the disclaimer + embedded screenshots.
_MOCKUP_VARIANTS = (f"{MOCKUP_DISCLAIMER}\n\n"
                    "**A** — inbox.\n![A](https://raw.githubusercontent.com/o/r/br/mockups/a.png)\n"
                    "Reply with a pick.")


def test_produce_disclaimer_carries_both_marks():
    # The TRAP: the produced-variants comment must carry INTAKE_MARK so the daemon reads it as
    # ours, and MOCKUP_MARK so the produce phase can tell a drawn round from intake's park.
    assert INTAKE_MARK in MOCKUP_DISCLAIMER
    assert MOCKUP_MARK in MOCKUP_DISCLAIMER


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


def test_produce_prompt_drives_ui_mockups_headless_and_one_marked_comment():
    # The produce session's marching orders: run /ui-craft lock for the repo's surfaces, screenshot,
    # commit variants to a branch, and post exactly ONE issue comment starting with the marker.
    body = PRODUCE_PROMPT.format(repo="o/r", n=7, title="A screen", body="details",
                                 branch="agentflow/claude/mockup-7-a-screen",
                                 surfaces="`agentflow/static/`",
                                 scope_guidance=SCOPE_GUIDANCE[MockupScope.SURFACE],
                                 disclaimer=MOCKUP_DISCLAIMER)
    assert "/ui-craft" in body
    assert "screenshot" in body.lower()
    assert "agentflow/static/" in body
    assert MOCKUP_DISCLAIMER in body           # the marker line the comment must start with
    assert "one comment" in body.lower()        # exactly one issue comment
    assert "push" in body.lower()               # variant HTML preserved on a branch, not lost
    assert "LOCKED" in body                      # every variant must carry a locked contract
    assert "150" in body                         # the ≤150-word bound on the contract


def test_produce_prompt_scope_branches_local_vs_surface():
    # The two scopes give materially different draw instructions (ADR 0048): surface reopens
    # the whole visual world; local inherits the shipping surface and varies only the addition.
    def body(scope):
        return PRODUCE_PROMPT.format(repo="o/r", n=7, title="A screen", body="details",
                                     branch="b", surfaces="`agentflow/webui/src/`",
                                     scope_guidance=SCOPE_GUIDANCE[scope],
                                     disclaimer=MOCKUP_DISCLAIMER)
    surface, local = body(MockupScope.SURFACE), body(MockupScope.LOCAL)
    assert "wildly-different" in surface.lower() or "genuinely-different" in surface.lower()
    assert "3-4" in surface
    assert "inherit" in local.lower()            # local inherits the shipping surface identity
    assert "addition" in local.lower()
    assert surface != local                       # materially different instructions
    # both still demand the locked contract — scope never weakens the charter gate
    assert "LOCKED" in surface and "LOCKED" in local


def test_mockup_scope_from_labels_reads_the_scope_or_defaults_local():
    assert mockup_scope_from_labels(["agentflow:needs-mockup",
                                     "agentflow:mockup:surface"]) is MockupScope.SURFACE
    assert mockup_scope_from_labels(["agentflow:mockup:local"]) is MockupScope.LOCAL
    # a legacy park with no scope label draws the safer, narrower round
    assert mockup_scope_from_labels(["agentflow:needs-mockup"]) is MockupScope.LOCAL


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


def _stub_survivor_router(monkeypatch, *, rebase, profile="reviewed", conflict_revise=None):
    """Drive _rebase_survivor with a canned rebase result; record park / merge side effects. By
    default a conflict falls through to the park (``conflict_revise=None``), so these tests exercise
    the ADR 0038 fallback; pass a status to model a conflict Revise that was opened instead.

    Nothing has been said on the PR — the park notice itself is stubbed out here — so the
    survivor's worktree is left where it is."""
    events = {"parked": [], "merged": []}
    monkeypatch.setattr(github, "pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: rebase)
    monkeypatch.setattr(loop, "_conflict_revise_survivor",
                        lambda cfg, pr, n, sl, tool, branch: conflict_revise)
    monkeypatch.setattr(loop, "_park_conflicted_survivor",
                        lambda cfg, pr, n: events["parked"].append(pr))
    monkeypatch.setattr(loop, "_merge_autonomous_survivor",
                        lambda cfg, pr, n, sl, tool, branch: events["merged"].append(pr) or "merged")
    return events


def test_rebase_survivor_conflict_parks_when_the_conflict_revise_budget_is_spent(monkeypatch):
    # ADR 0038: the park is now the FALLBACK. Once the two-round conflict budget is spent
    # (_conflict_revise_survivor returns None), a still-conflicting survivor is parked-and-pinged
    # on every profile — never left silent. Fails first if the conflict branch stays silent.
    for profile in ("reviewed", "guarded", "autonomous"):
        events = _stub_survivor_router(monkeypatch, rebase=RebaseResult.CONFLICT, profile=profile,
                                       conflict_revise=None)
        out = _rebase_survivor(RepoConfig("o/r", "/tmp"), 8, "agentflow/claude/issue-4-x", profile)
        assert events["parked"] == [8], f"exhausted conflict must ping ({profile})"
        assert events["merged"] == []
        assert "parked" in out


def test_rebase_survivor_conflict_opens_a_revise_before_parking(monkeypatch):
    # ADR 0038: a survivor's re-rebase conflict opens a conflict Revise instead of parking. When
    # _conflict_revise_survivor handles it (returns a status), no park notice is posted.
    events = _stub_survivor_router(monkeypatch, rebase=RebaseResult.CONFLICT,
                                   conflict_revise="#8: conflict — revise round 1 opened")
    out = _rebase_survivor(RepoConfig("o/r", "/tmp"), 8, "agentflow/claude/issue-4-x", "reviewed")
    assert events["parked"] == [], "an opened conflict Revise must not also park"
    assert out == "#8: conflict — revise round 1 opened"


def test_conflict_rebase_disposes_only_after_the_notice_is_durable(monkeypatch):
    _stub_survivor_router(monkeypatch, rebase=RebaseResult.CONFLICT, conflict_revise=None)
    monkeypatch.setattr(github, "pr_comment_rows",
                        lambda repo, pr: [{"body": f"> *{loop._CONFLICT_MARK}.*"}])
    removed = []
    monkeypatch.setattr(loop, "remove_worktree_if_safe",
                        lambda workdir, wt: removed.append(str(wt)) or True)

    _rebase_survivor(RepoConfig("o/r", "/tmp"), 8, "agentflow/claude/issue-4-x", "reviewed")

    assert removed == ["/tmp/.agentflow/worktrees/claude/issue-4-x"]


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


def test_autonomous_survivor_review_is_a_cold_coordinator_submission(monkeypatch):
    from agentflow import coordinated_build

    monkeypatch.setattr(loop, "_run", lambda argv: _FakeRun("head-a\n"))
    monkeypatch.setattr(loop, "_issue_acceptance", lambda cfg, number: "Issue acceptance")
    monkeypatch.setattr(loop, "claim", lambda repo, number, _label: True)
    monkeypatch.setattr(loop, "pick_reviewer", lambda tool, **kwargs: "codex")
    monkeypatch.setattr(github, "pr_comments", lambda repo, pr: [])
    submission = SimpleNamespace(stage="review", transfer_from=None)
    monkeypatch.setattr(coordinated_review, "survivor_review_submission",
                        lambda *args, **kwargs: submission)
    submitted = []
    coord = SimpleNamespace(submit_stage=submitted.append)
    monkeypatch.setattr(pipeline, "build_coordinator", lambda: coord)
    reconciled = []
    monkeypatch.setattr(pipeline, "reconcile_and_project",
                        lambda current: reconciled.append(current))

    result = loop._merge_autonomous_survivor(
        RepoConfig("o/r", "/tmp"), 42, 7, "fix", "claude",
        "agentflow/claude/issue-7-fix")

    assert result == "review submitted"
    assert submitted == [submission] and reconciled == [coord]


def test_autonomous_survivor_waits_instead_of_falling_back_to_same_tool(monkeypatch):
    """An autonomous survivor remains open when the independent tool is unavailable."""
    from agentflow import coordinated_build

    monkeypatch.setattr(loop, "_run", lambda argv: _FakeRun("head-a\n"))
    monkeypatch.setattr(loop, "_issue_acceptance", lambda cfg, number: "Issue acceptance")
    monkeypatch.setattr(loop, "claim", lambda repo, number, _label: True)
    calls = []
    monkeypatch.setattr(
        loop, "pick_reviewer",
        lambda tool, **kwargs: calls.append((tool, kwargs)) or None)  # codex out of budget
    captured = {}

    def fake_submission(cfg, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(stage="review", transfer_from=None)

    monkeypatch.setattr(coordinated_review, "survivor_review_submission", fake_submission)
    monkeypatch.setattr(pipeline, "build_coordinator",
                        lambda: SimpleNamespace(submit_stage=lambda s: None))
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda current: None)

    result = loop._merge_autonomous_survivor(
        RepoConfig("o/r", "/tmp"), 42, 7, "fix", "claude", "agentflow/claude/issue-7-fix")

    assert result == "no reviewer pool available — deferring"
    assert captured == {}
    assert calls == [("claude", {"allow_same_tool": False})]


def test_manual_same_tool_review_requires_warning_then_explicit_confirmation(monkeypatch):
    from agentflow import coordinated_build
    from agentflow.review_policy import ReviewAssignment

    warning = loop.review_pr(
        RepoConfig("o/r", "/work"), 42, force_same_tool=True,
        maintainer_confirmed=False)
    assert "Warning: same-tool review is not independent" in warning
    assert "maintainer_confirmed=True" in warning

    monkeypatch.setattr(github, "pr_facts", lambda repo, pr: github.PrFacts(
        head_ref_name="agentflow/claude/issue-7-fix", head_ref_oid="head",
        state="OPEN", closing_issues=(7,)))
    monkeypatch.setattr(loop, "_issue_acceptance", lambda cfg, issue: "acceptance")
    monkeypatch.setattr(loop, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(
        coordinated_review, "_review_assignment_facts",
        lambda *args, **kwargs: (ReviewAssignment(reason="one journey"), ("agentflow/x.py",)))
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    monkeypatch.setattr(loop, "claim", lambda repo, issue, _label: True)
    captured = {}
    submission = SimpleNamespace(stage="review")
    monkeypatch.setattr(
        coordinated_review, "survivor_review_submission",
        lambda *args, **kwargs: captured.update(kwargs) or submission)
    submitted = []
    monkeypatch.setattr(
        pipeline, "build_coordinator",
        lambda: SimpleNamespace(submit_stage=submitted.append))
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda coord: [])

    result = loop.review_pr(
        RepoConfig("o/r", "/work"), 42, force_same_tool=True,
        maintainer_confirmed=True)

    assert result == "same-tool review submitted; maintainer merge required"
    assert captured["reviewer_tool"] == "claude"
    assert captured["review"].tainted is True
    assert submitted == [submission]


def test_manual_review_uses_latest_exact_head_author_not_branch_builder(monkeypatch):
    """Claude built the branch, then Codex pushed a review fix. The durable current author is
    Codex: default review selects Claude; forced same-tool selects Codex and records taint."""
    from agentflow import coordinated_build
    from agentflow.coordinator.record import Record
    from agentflow.review_policy import ReviewAssignment

    monkeypatch.setattr(github, "pr_facts", lambda repo, pr: github.PrFacts(
        head_ref_name="agentflow/claude/issue-7-fix", head_ref_oid="fixed",
        state="OPEN", closing_issues=(7,)))
    monkeypatch.setattr(loop, "_issue_acceptance", lambda cfg, issue: "acceptance")
    monkeypatch.setattr(loop, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(
        coordinated_review, "_review_assignment_facts",
        lambda *args, **kwargs: (ReviewAssignment(reason="one journey"), ()))
    history = Record(
        identity="review-fixed", stage="review", pool="claude", demand=1,
        repo="o/r", subject="7", target="fixed", builder_lineage="claude",
        change_author_tool="codex", review_sequence=2, review_passes=1, retired=True)
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [history])
    choices = []
    monkeypatch.setattr(
        loop, "pick_reviewer",
        lambda author, **kwargs: choices.append((author, kwargs)) or "claude")
    monkeypatch.setattr(loop, "claim", lambda *_args: True)
    captured = []
    monkeypatch.setattr(
        coordinated_review, "survivor_review_submission",
        lambda *args, **kwargs: captured.append(kwargs) or SimpleNamespace(stage="review"))
    monkeypatch.setattr(
        pipeline, "build_coordinator",
        lambda: SimpleNamespace(submit_stage=lambda _submission: None))
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda _coord: None)

    assert loop.review_pr(RepoConfig("o/r", "/work"), 42) == "review submitted"
    assert choices == [("codex", {"allow_same_tool": False})]
    assert captured[-1]["reviewer_tool"] == "claude"
    assert captured[-1]["review"].change_author_tool == "codex"
    assert captured[-1]["review"].tainted is False

    assert loop.review_pr(
        RepoConfig("o/r", "/work"), 42, force_same_tool=True,
        maintainer_confirmed=True) == "same-tool review submitted; maintainer merge required"
    assert captured[-1]["reviewer_tool"] == "codex"
    assert captured[-1]["review"].change_author_tool == "codex"
    assert captured[-1]["review"].tainted is True


def test_survivor_review_defers_when_no_reviewer_pool_can_launch_it(monkeypatch):
    """With neither pool able to launch a review, the survivor re-review defers to a later cycle
    rather than submitting into a dead pool (the frozen state this path used to create)."""
    from agentflow import coordinated_build

    monkeypatch.setattr(loop, "_run", lambda argv: _FakeRun("head-a\n"))
    monkeypatch.setattr(loop, "pick_reviewer", lambda tool, **kwargs: None)
    submitted = []
    monkeypatch.setattr(coordinated_review, "survivor_review_submission",
                        lambda *a, **k: submitted.append(True))
    claimed = []
    monkeypatch.setattr(loop, "claim", lambda repo, number, _label: claimed.append(number) or True)

    result = loop._merge_autonomous_survivor(
        RepoConfig("o/r", "/tmp"), 42, 7, "fix", "claude", "agentflow/claude/issue-7-fix")

    assert result == "no reviewer pool available — deferring"
    assert submitted == [] and claimed == []


# --- issue #212: a survivor's conflict opens a conflict Revise, not a park (ADR 0038) --------

def _conflict_revise(conflict_round, target, *, repo="o/r", subject="4"):
    """A durable conflict-Revise record stand-in (the fields the budget/idempotency helpers read)."""
    return SimpleNamespace(stage="revise", conflict_round=conflict_round, repo=repo,
                           subject=subject, target=target)


def _stub_conflict_env(monkeypatch, *, priors, head="sha-conf", claim=True):
    """Fake the git head read and coordinator store for _conflict_revise_survivor; capture every
    submission handed to the coordinator. No real process is ever launched.

    The PR carries no clean-review summary here, so opening the Revise has nothing to retire —
    the retirement itself is exercised on its own fixture below."""
    from agentflow import coordinated_build

    monkeypatch.setattr(loop, "_run",
                        lambda argv: _FakeRun(head + "\n") if "rev-parse" in argv else _FakeRun(""))
    monkeypatch.setattr(github, "pr_comments", lambda repo, pr: [])
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: list(priors))
    monkeypatch.setattr(loop, "claim", lambda repo, n, _label: claim)
    submitted = []
    monkeypatch.setattr(pipeline, "build_coordinator",
                        lambda: SimpleNamespace(submit_stage=submitted.append))
    reconciled = []
    monkeypatch.setattr(pipeline, "reconcile_and_project",
                        lambda coord: reconciled.append(coord))
    return submitted, reconciled


def test_survivor_conflict_opens_a_conflict_revise_on_the_builder_lineage(monkeypatch):
    """AC1: a survivor whose re-rebase conflicts opens a conflict Revise on the builder's own
    lineage with the ADR's findings text, and posts no park comment. Reproduces the #194 park —
    before the fix, this path called _park_conflicted_survivor and submitted nothing."""
    submitted, reconciled = _stub_conflict_env(monkeypatch, priors=[], head="sha-conf")
    parked = []
    monkeypatch.setattr(loop, "_park_conflicted_survivor", lambda cfg, pr, n: parked.append(pr))
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: RebaseResult.CONFLICT)

    out = _rebase_survivor(RepoConfig("o/r", "/w"), 8, "agentflow/claude/issue-4-fix", "reviewed")

    assert parked == [], "opening a conflict Revise must not also park the PR"
    assert len(submitted) == 1 and reconciled
    sub = submitted[0]
    assert sub.stage == "revise" and sub.pool == "claude" and sub.builder_lineage == "claude"
    assert sub.subject == "4" and sub.target == "sha-conf" and sub.conflict_round == 1
    assert sub.continuation is True                       # admitted ahead of cold build work
    assert "resolve the merge conflicts" in sub.input_ptr
    assert "Preserve both sides" in sub.input_ptr
    assert "revise round 1 opened" in out


def test_second_survivor_conflict_opens_a_distinct_second_round_idempotently(monkeypatch):
    """AC3: a second conflict on the same PR opens a second conflict Revise with a distinct round
    identity; re-reconciling the same still-open conflict head opens nothing new (idempotent)."""
    first = _conflict_revise(1, "sha-old")               # a prior conflict Revise already resolved
    submitted, _ = _stub_conflict_env(monkeypatch, priors=[first], head="sha-new")
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: RebaseResult.CONFLICT)

    out = _rebase_survivor(RepoConfig("o/r", "/w"), 8, "agentflow/claude/issue-4-fix", "reviewed")
    assert submitted[0].conflict_round == 2 and submitted[0].target == "sha-new"
    assert "revise round 2 opened" in out

    # Re-reconcile with that second conflict now open at the same head: nothing new is submitted.
    second = _conflict_revise(2, "sha-new")
    submitted2, _ = _stub_conflict_env(monkeypatch, priors=[first, second], head="sha-new")
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: RebaseResult.CONFLICT)
    out2 = _rebase_survivor(RepoConfig("o/r", "/w"), 8, "agentflow/claude/issue-4-fix", "reviewed")
    assert submitted2 == [] and "already open" in out2


def test_third_survivor_conflict_opens_another_bounded_revise(monkeypatch):
    """A PR-lifetime count never turns a fresh, resolvable conflict into human work. The third
    distinct conflicting head opens round three; that stage's own attempt budget remains bounded."""
    priors = [_conflict_revise(1, "sha-1"), _conflict_revise(2, "sha-2")]
    submitted, _ = _stub_conflict_env(monkeypatch, priors=priors, head="sha-3")
    parked = []
    monkeypatch.setattr(loop, "_park_conflicted_survivor", lambda cfg, pr, n: parked.append(pr))
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: [])   # notice not yet durable
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: RebaseResult.CONFLICT)

    out = _rebase_survivor(RepoConfig("o/r", "/w"), 8, "agentflow/claude/issue-4-fix", "reviewed")

    assert len(submitted) == 1
    assert submitted[0].conflict_round == 3 and submitted[0].target == "sha-3"
    assert parked == [] and "revise round 3 opened" in out


def _clean_summary_pr(monkeypatch):
    """A PR the engine already handed to the maintainer: one current clean-review summary,
    editable in place. Returns the live comment list so a test can read what it became."""
    marker = "<!-- agentflow-clean-review-summary -->"
    comments = [github.Comment(body=f"{marker}\n<!-- agentflow-clean-review-head:sha-a -->",
                               created_at="", id="clean")]
    monkeypatch.setattr(github, "pr_comments", lambda repo, pr: list(comments))
    monkeypatch.setattr(github, "edit_comment", lambda comment_id, body:
                        comments.__setitem__(0, github.Comment(body, "", id=comment_id)) or True)
    return comments


def test_opening_a_conflict_revise_takes_the_pr_back_from_the_maintainer(monkeypatch):
    """A clean-reviewed PR that conflicts against main is being resolved by the engine, not
    waiting on a merge — so the hand-off it carries is retired when the Revise opens."""
    _stub_conflict_env(monkeypatch, priors=[], head="sha-conf")
    comments = _clean_summary_pr(monkeypatch)
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: RebaseResult.CONFLICT)

    out = _rebase_survivor(RepoConfig("o/r", "/w"), 8, "agentflow/claude/issue-4-fix", "reviewed")

    assert "revise round 1 opened" in out
    assert "agentflow-superseded-review-summary" in comments[0].body
    assert "sha-a" in comments[0].body, "the evidence the summary recorded survives"


def test_an_autonomous_re_review_takes_the_pr_back_from_the_maintainer(monkeypatch):
    """The same on the autonomous path: a fresh Review over a stale tainted summary means the
    engine owns the PR again, so the old summary stops reading as a merge prompt."""
    monkeypatch.setattr(loop, "_run", lambda argv: _FakeRun("head-a\n"))
    monkeypatch.setattr(loop, "_issue_acceptance", lambda cfg, number: "Issue acceptance")
    monkeypatch.setattr(loop, "claim", lambda repo, number, _label: True)
    monkeypatch.setattr(loop, "pick_reviewer", lambda tool, **kwargs: "codex")
    monkeypatch.setattr(coordinated_review, "survivor_review_submission",
                        lambda *args, **kwargs: SimpleNamespace(stage="review", transfer_from=None))
    monkeypatch.setattr(pipeline, "build_coordinator",
                        lambda: SimpleNamespace(submit_stage=lambda s: None))
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda current: None)
    comments = _clean_summary_pr(monkeypatch)

    result = loop._merge_autonomous_survivor(
        RepoConfig("o/r", "/tmp"), 42, 7, "fix", "claude", "agentflow/claude/issue-7-fix")

    assert result == "review submitted"
    assert "agentflow-superseded-review-summary" in comments[0].body


def test_conflict_revise_defers_without_parking_when_the_head_is_unreadable(monkeypatch):
    """A transient git failure reading the conflicting head must defer, never park: parking on a
    blip would strand the PR for a human on a recoverable error."""
    _stub_conflict_env(monkeypatch, priors=[])
    monkeypatch.setattr(loop, "_run", lambda argv: _FakeRun(returncode=1))   # rev-parse fails
    parked = []
    monkeypatch.setattr(loop, "_park_conflicted_survivor", lambda cfg, pr, n: parked.append(pr))
    monkeypatch.setattr(loop, "_rebase_branch", lambda cfg, branch, wt: RebaseResult.CONFLICT)

    out = _rebase_survivor(RepoConfig("o/r", "/w"), 8, "agentflow/claude/issue-4-fix", "reviewed")

    assert parked == [] and "retry next cycle" in out


def _stub_recheck(monkeypatch, prs, *, advanced, profile, comments=None):
    """Drive recheck_once: canned open-PR list, base-advanced verdicts, and per-PR routing."""
    routed = []
    monkeypatch.setattr(loop, "_open_agentflow_prs", lambda cfg: prs)
    monkeypatch.setattr(loop, "_run", lambda cmd: _FakeRun("", 0))   # fetch origin succeeds
    monkeypatch.setattr(loop, "repo_profile", lambda wd: profile)
    monkeypatch.setattr(loop, "_base_advanced_for", lambda wd, branch: advanced.get(branch))
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: (comments or {}).get(pr, []))

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


def test_recheck_defers_when_pr_listing_fails(monkeypatch):
    # Unknown is not empty: a gh blip must defer, not read as 'no survivors'.
    monkeypatch.setattr(loop, "_open_agentflow_prs", lambda cfg: None)
    assert "deferring" in recheck_once(RepoConfig("o/r", "/tmp"))


def test_waiting_intake_record_omitted_from_live_projection():
    # Guards the Live-view premise: a waiting Intake record (queued for admission, no attempt,
    # no family) must never appear as a running session on the live board. This is what keeps
    # the console truthful when pool capacity is saturated.
    from agentflow.coordinator.record import Record, WAITING, RUNNING
    from agentflow.coordinator import tracer

    waiting_intake = Record(identity="o/r:7:intake:None", stage="intake",
                            pool="claude", demand=1, repo="o/r", subject="7",
                            state=WAITING, started_at=0, family=None)
    running_build = Record(identity="o/r:8:build:None", stage="build",
                           pool="claude", demand=1, repo="o/r", subject="8",
                           state=RUNNING, started_at=1_000_000)

    projection = tracer.live_projection([waiting_intake, running_build])

    numbers = [e["number"] for e in projection]
    assert 7 not in numbers, "waiting Intake must not appear on the live board"
    assert 8 in numbers, "running Build must appear on the live board"
    assert projection[0]["started_at"] != ""


# --- issue #205: screenshot evidence must be hosted from an immutable commit ----

class TestScreenshotHostingInstruction:
    """Every prompt that asks for screenshots must pin them to the immutable commit that
    added the file — never a mutable branch ref that GitHub repaints from the branch head."""

    def _build(self):
        return BUILD_PROMPT.format(
            repo="o/r", n=205, title="t", body="b", effort="medium",
            surfaces="agentflow/webui/src/")

    def _revise(self):
        return REVISE_PROMPT.format(
            repo="o/r", n=205, findings="- x", surfaces="agentflow/webui/src/")

    def _mockup(self):
        return PRODUCE_PROMPT.format(
            repo="o/r", n=205, title="t", body="b",
            branch="agentflow/claude/issue-205-x", surfaces="agentflow/webui/src/",
            scope_guidance=SCOPE_GUIDANCE[MockupScope.SURFACE],
            disclaimer=MOCKUP_DISCLAIMER)

    def test_build_prompt_pins_to_the_commit_and_namespaces_the_round(self):
        text = self._build()
        assert "raw/<commit-sha>" in text
        assert "refs/heads" in text                      # as the thing to never use
        assert "docs/screenshots/issue-205/<short-sha>/" in text

    def test_revise_prompt_pins_to_the_commit_and_namespaces_the_round(self):
        text = self._revise()
        assert "raw/<commit-sha>" in text
        assert "refs/heads" in text
        assert "docs/screenshots/issue-205/<short-sha>/" in text

    def test_mockup_prompt_pins_to_the_commit_and_drops_the_branch_ref(self):
        text = self._mockup()
        assert "raw/<commit-sha>" in text
        # the old mutable-ref host is gone entirely from the mockup image instruction
        assert "refs/heads" not in text
