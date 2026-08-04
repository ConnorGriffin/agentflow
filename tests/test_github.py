"""Tests for the one GitHub-access module (ADR 0040).

These state facts about what GitHub returns ("issue 5 has labels {ready-for-agent}",
"the label read failed") and assert the typed result. They deliberately do NOT match
`gh` command-line arguments: the stub ignores the argv entirely and returns only the
stated outcome, so the tests exercise the module purely through its public interface.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from agentflow import github


def _stub(monkeypatch, *, returncode: int = 0, stdout: str = ""):
    """Make every `gh` call in the module return this one stated outcome, regardless of
    which command was built — the tests describe GitHub's answer, not the argv."""
    def fake_run(cmd, cwd=None, timeout=None):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
    monkeypatch.setattr(github, "_run", fake_run)


def _stub_json(monkeypatch, payload, *, returncode: int = 0):
    _stub(monkeypatch, returncode=returncode, stdout=json.dumps(payload))


REPO = "owner/repo"


# --- the failure-vs-empty distinction (the correctness the repo depends on) -----

def test_issue_labels_reports_a_real_empty_set(monkeypatch):
    _stub_json(monkeypatch, {"labels": []})
    assert github.issue_labels(REPO, 5) == frozenset()


def test_issue_labels_reports_the_labels_present(monkeypatch):
    _stub_json(monkeypatch, {"labels": [{"name": "ready-for-agent"}, {"name": "bug"}]})
    assert github.issue_labels(REPO, 5) == frozenset({"ready-for-agent", "bug"})


def test_a_failed_label_read_is_unknown_not_empty(monkeypatch):
    # gh could not be reached: the read must report None (unknown), never an empty set —
    # a stage keys "couldn't check" apart from "has no labels" on exactly this.
    _stub(monkeypatch, returncode=1, stdout="")
    assert github.issue_labels(REPO, 5) is None


def test_unparseable_label_output_is_unknown(monkeypatch):
    _stub(monkeypatch, returncode=0, stdout="not json at all")
    assert github.issue_labels(REPO, 5) is None


# --- the other single-fact reads -----------------------------------------------

def test_issue_body_reads_text_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"body": "the description"})
    assert github.issue_body(REPO, 5) == "the description"
    _stub_json(monkeypatch, {"body": ""})
    assert github.issue_body(REPO, 5) == ""          # real empty body, distinct from...
    _stub(monkeypatch, returncode=1)
    assert github.issue_body(REPO, 5) is None          # ...an unreadable one


def test_issue_state_reads_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"state": "OPEN"})
    assert github.issue_state(REPO, 5) == "OPEN"
    _stub(monkeypatch, returncode=1)
    assert github.issue_state(REPO, 5) is None


def test_issue_standing_pairs_labels_with_state_and_fails_closed(monkeypatch):
    # Closing an issue does not strip its labels, so a claim proof must see both facts in one
    # snapshot (#438) — and an unreadable pair is unknown, never "unlabelled and open".
    _stub_json(monkeypatch, {"labels": [{"name": "agentflow:triaging"}], "state": "CLOSED"})
    assert github.issue_standing(REPO, 5) == github.IssueStanding(
        labels=frozenset({"agentflow:triaging"}), state="CLOSED")
    _stub(monkeypatch, returncode=1)
    assert github.issue_standing(REPO, 5) is None


def test_pr_state_reads_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"state": "MERGED"})
    assert github.pr_state(REPO, 9) == "MERGED"
    _stub(monkeypatch, returncode=1)
    assert github.pr_state(REPO, 9) is None


def test_pr_comments_are_typed_rows(monkeypatch):
    _stub_json(monkeypatch, {"comments": [
        {"body": "please rebase", "createdAt": "2026-07-19T00:00:00Z",
         "id": "IC_kwDO"}]})
    got = github.pr_comments(REPO, 9)
    assert got == [github.Comment(body="please rebase",
                                  created_at="2026-07-19T00:00:00Z", id="IC_kwDO")]


def test_pr_comments_real_empty_thread_is_a_list(monkeypatch):
    _stub_json(monkeypatch, {"comments": []})
    assert github.pr_comments(REPO, 9) == []


def test_pr_comments_failure_is_unknown(monkeypatch):
    _stub(monkeypatch, returncode=1)
    assert github.pr_comments(REPO, 9) is None


def test_issue_comments_typed_and_fail_closed(monkeypatch):
    _stub_json(monkeypatch, {"comments": [
        {"body": "note", "createdAt": "2026-07-19T01:00:00Z"}]})
    assert github.issue_comments(REPO, 5) == [
        github.Comment(body="note", created_at="2026-07-19T01:00:00Z")]
    _stub(monkeypatch, returncode=1)
    assert github.issue_comments(REPO, 5) is None


# --- discovery collections ------------------------------------------------------

def test_list_issues_returns_typed_rows(monkeypatch):
    _stub_json(monkeypatch, [
        {"number": 5, "title": "t", "body": "b",
         "labels": [{"name": "ready-for-agent"}]}])
    rows = github.list_issues(REPO, label="ready-for-agent")
    assert rows == [github.IssueRow(number=5, title="t", body="b",
                                    labels=frozenset({"ready-for-agent"}))]


def test_list_issues_empty_repo_vs_failed_listing(monkeypatch):
    _stub_json(monkeypatch, [])
    assert github.list_issues(REPO) == []            # really nothing open
    _stub(monkeypatch, returncode=1)
    assert github.list_issues(REPO) is None            # couldn't list


def test_list_open_prs_returns_typed_rows(monkeypatch):
    _stub_json(monkeypatch, [
        {"number": 9, "headRefName": "feature/x", "headRefOid": "abc123",
         "closingIssuesReferences": [{"number": 40}]}])
    assert github.list_open_prs(REPO) == [
        github.PrRow(number=9, head_ref_name="feature/x", head_ref_oid="abc123",
                     closing_issues=(40,))]


def test_an_open_pr_declaring_no_closing_issue_reads_as_none_declared(monkeypatch):
    _stub_json(monkeypatch, [{"number": 9, "headRefName": "feature/x", "headRefOid": "abc"}])
    assert github.list_open_prs(REPO)[0].closing_issues == ()


def test_prs_for_branch_spans_every_state_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, [
        {"number": 9, "state": "MERGED", "headRefName": "feature/x", "url": "u"}])
    assert github.prs_for_branch(REPO, "feature/x") == [
        github.BranchPrRow(number=9, state="MERGED", head_ref_name="feature/x", url="u")]
    _stub_json(monkeypatch, [])
    assert github.prs_for_branch(REPO, "feature/x") == []   # the branch never had a PR
    _stub(monkeypatch, returncode=1)
    assert github.prs_for_branch(REPO, "feature/x") is None  # ...distinct from unreadable


def test_claimed_issues_drops_pull_requests_sharing_the_number_sequence(monkeypatch):
    _stub_json(monkeypatch, [
        {"number": 7, "updated_at": "2020-01-01T00:00:00Z"},
        {"number": 9, "updated_at": "2020-01-01T00:00:00Z",
         "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/9"}}])
    assert github.claimed_issues(REPO, "agentflow:building") == [
        github.ClaimedIssue(number=7, updated_at="2020-01-01T00:00:00Z")]
    _stub_json(monkeypatch, [])
    assert github.claimed_issues(REPO, "agentflow:building") == []   # no claims out there
    _stub(monkeypatch, returncode=1)
    assert github.claimed_issues(REPO, "agentflow:building") is None  # ...vs unreadable


def test_claimed_issues_asks_the_rest_endpoint_and_never_the_search_api(monkeypatch):
    """The one read in this file whose *argv* is the point, against the module's usual rule.

    Which endpoint answers the claim listing is not an implementation detail. The obvious
    spelling — `gh issue list --label` — is answered by GitHub's search, whose ceiling is about
    thirty requests a minute; a fleet-wide reconciliation pass (four lanes per repo, every cycle)
    goes through that in seconds, and when it did, the lane starved for every repo at once. The
    REST issues endpoint filters by label out of the ordinary hourly budget instead.
    """
    asked = []

    def recording_run(cmd, cwd=None, timeout=None):
        asked.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

    monkeypatch.setattr(github, "_run", recording_run)

    assert github.claimed_issues(REPO, "agentflow:building") == []

    assert len(asked) == 1
    assert asked[0][:2] == ["gh", "api"]
    assert asked[0][2].startswith(f"repos/{REPO}/issues?")
    assert "state=all" in asked[0][2]
    assert "labels=agentflow%3Abuilding" in asked[0][2]
    assert asked[0][1:3] != ["issue", "list"]   # never the search-backed listing
    assert "search" not in asked[0]


def test_list_open_prs_failure_is_unknown(monkeypatch):
    _stub(monkeypatch, returncode=1)
    assert github.list_open_prs(REPO, head="feature/x") is None


def test_search_returns_typed_hits(monkeypatch):
    _stub_json(monkeypatch, [{"number": 5, "updatedAt": "2026-07-19T00:00:00Z"}])
    assert github.search([REPO], "2026-07-18T00:00:00Z") == [
        github.SearchHit(number=5, updated_at="2026-07-19T00:00:00Z")]


def test_search_failure_is_unknown_not_no_change(monkeypatch):
    _stub(monkeypatch, returncode=1)
    assert github.search([REPO], "2026-07-18T00:00:00Z") is None


# --- the combined reads the stages weigh as one snapshot ------------------------

def test_issue_headline_pairs_title_with_labels_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"title": "Scoped", "labels": [{"name": "ready-for-agent"}]})
    assert github.issue_headline(REPO, 5) == github.IssueHeadline(
        title="Scoped", labels=frozenset({"ready-for-agent"}))
    _stub(monkeypatch, returncode=1)
    assert github.issue_headline(REPO, 5) is None


def test_issue_settlement_pairs_labels_with_the_url_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"labels": [], "url": "https://github.com/owner/repo/issues/5"})
    settled = github.issue_settlement(REPO, 5)
    assert settled == github.IssueSettlement(
        labels=frozenset(), url="https://github.com/owner/repo/issues/5")
    _stub(monkeypatch, returncode=1)
    assert github.issue_settlement(REPO, 5) is None


def test_issue_view_reads_the_whole_issue_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {
        "title": "t", "body": "b", "state": "CLOSED", "url": "u",
        "labels": [{"name": "wayfinder:research"}],
        "comments": [{"body": "findings", "createdAt": "2026-07-19T01:00:00Z"}]})
    assert github.issue_view(REPO, 5) == github.IssueView(
        title="t", body="b", state="CLOSED", url="u",
        labels=frozenset({"wayfinder:research"}),
        comments=[github.Comment(body="findings", created_at="2026-07-19T01:00:00Z")])
    _stub(monkeypatch, returncode=1)
    assert github.issue_view(REPO, 5) is None


def test_issue_url_reads_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"url": "https://github.com/owner/repo/issues/5"})
    assert github.issue_url(REPO, 5) == "https://github.com/owner/repo/issues/5"
    _stub(monkeypatch, returncode=1)
    assert github.issue_url(REPO, 5) is None


@pytest.mark.parametrize("payload", [{}, {"isDraft": "true"}])
def test_a_draft_answer_that_is_not_a_yes_or_no_is_unknown(monkeypatch, payload):
    # A read missing the field, or carrying a non-boolean, leaves the draft state unknown —
    # the merge gate must be able to tell that apart from a confirmed "not a draft".
    _stub_json(monkeypatch, payload)
    assert github.pr_is_draft(REPO, 9) is None


def test_pr_is_draft_reads_both_answers_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"isDraft": True})
    assert github.pr_is_draft(REPO, 9) is True
    _stub_json(monkeypatch, {"isDraft": False})
    assert github.pr_is_draft(REPO, 9) is False
    _stub(monkeypatch, returncode=1)
    assert github.pr_is_draft(REPO, 9) is None


def test_pr_facts_reads_head_state_and_declared_closing_issues(monkeypatch):
    _stub_json(monkeypatch, {
        "headRefName": "agentflow/claude/issue-7-fix", "headRefOid": "abc123",
        "state": "OPEN", "closingIssuesReferences": [{"number": 7}, {"number": "bad"}]})
    assert github.pr_facts(REPO, 9) == github.PrFacts(
        head_ref_name="agentflow/claude/issue-7-fix", head_ref_oid="abc123",
        state="OPEN", closing_issues=(7,))
    _stub(monkeypatch, returncode=1)
    assert github.pr_facts(REPO, 9) is None


def test_pr_content_reads_body_paths_and_thread_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {
        "body": "what changed", "files": [{"path": "webui/app.svelte"}, {"path": ""}],
        "comments": [{"body": "a note", "createdAt": "2026-07-19T01:00:00Z"}]})
    assert github.pr_content(REPO, 9) == github.PrContent(
        body="what changed", paths=("webui/app.svelte",),
        comments=[github.Comment(body="a note", created_at="2026-07-19T01:00:00Z")])
    _stub(monkeypatch, returncode=1)
    assert github.pr_content(REPO, 9) is None


def test_checks_that_could_not_be_confirmed_are_not_passed(monkeypatch):
    # Unlike the reads, this has no unknown: `gh pr checks` exits non-zero while any check is
    # pending, failed or unreadable, and the merge gate wants exactly that fail-safe answer.
    _stub(monkeypatch, returncode=0)
    assert github.pr_checks_passed(REPO, 9) is True
    _stub(monkeypatch, returncode=1)
    assert github.pr_checks_passed(REPO, 9) is False


# --- writes report only what the command did ------------------------------------

@pytest.mark.parametrize("call", [
    lambda: github.add_label(REPO, 5, "agentflow:building"),
    lambda: github.remove_label(REPO, 5, "agentflow:building"),
    lambda: github.edit_title(REPO, 5, "new title"),
    lambda: github.edit_body(REPO, 5, "new body"),
    lambda: github.comment(REPO, 5, "hello"),
    lambda: github.pr_comment(REPO, 9, "hello"),
    lambda: github.edit_comment("IC_kwDO", "updated"),
    lambda: github.close(REPO, 5),
    lambda: github.pr_ready(REPO, 9),
    lambda: github.create_label(REPO, "agentflow:building", "fbca04"),
    lambda: github.create_label(REPO, "agentflow:building", "fbca04", "the change claim"),
    lambda: github.merge_pr(REPO, 9),
])
def test_writes_report_success_and_failure(monkeypatch, call):
    _stub(monkeypatch, returncode=0)
    assert call() is True
    _stub(monkeypatch, returncode=1)
    assert call() is False


def test_a_write_does_not_re_read_to_prove_it_landed(monkeypatch):
    # A mutation runs exactly one command and trusts its return code — proving the
    # change stuck is a separate, later effort and must not be built here.
    calls = []

    def fake_run(cmd, cwd=None, timeout=None):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
    monkeypatch.setattr(github, "_run", fake_run)

    assert github.add_label(REPO, 5, "agentflow:building") is True
    assert len(calls) == 1


# --- the escape hatch -----------------------------------------------------------

def test_api_returns_stripped_stdout(monkeypatch):
    _stub(monkeypatch, returncode=0, stdout="  gho_token_value\n")
    assert github.api(["auth", "token"]) == "gho_token_value"


def test_api_parses_json_when_asked(monkeypatch):
    _stub_json(monkeypatch, [{"number": 7}])
    assert github.api(["api", "repos/o/r/issues/5/dependencies/blocked_by"],
                      parse_json=True) == [{"number": 7}]


def test_api_reports_failure_as_none(monkeypatch):
    _stub(monkeypatch, returncode=1)
    assert github.api(["auth", "token"]) is None
    assert github.api(["api", "x"], parse_json=True) is None


# --- GraphQL query constants -----------------------------------------------------

@pytest.mark.parametrize("query", [github._ROLLUP_QUERY, github._MAPS_QUERY,
                                   github._MAPS_DISCOVERY_QUERY],
                          ids=["_ROLLUP_QUERY", "_MAPS_QUERY", "_MAPS_DISCOVERY_QUERY"])
def test_graphql_query_constants_have_balanced_braces(query):
    assert query.count("{") == query.count("}")


# --- the Decision Map read pays for what a repository actually has (#497) ---------
#
# The other argv-recording test in this file (`claimed_issues`) states the rule: argv is
# asserted only where *which* question is asked is the behavior, not an implementation
# detail. It is here — GitHub bills GraphQL on the page sizes a query requests, so asking a
# repository with no maps for five maps' worth of children and dependency edges is the whole
# cost of the briefing for seven of nine enrolled repositories.

def _recording_graphql(monkeypatch, payloads):
    """Answer successive `gh api graphql` calls from ``payloads``, recording each argv."""
    asked = []

    def recording_run(cmd, cwd=None, timeout=None):
        asked.append(list(cmd))
        payload = payloads[len(asked) - 1]
        if payload is None:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="boom")
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(github, "_run", recording_run)
    return asked


def _discovery(count, *, cost=1, remaining=4999):
    return {"data": {"rateLimit": {"cost": cost, "remaining": remaining},
                     "repository": {"issues": {"totalCount": count}}}}


def _detail(numbers, *, cost=20, remaining=4979, total_count=None):
    nodes = [{"number": n, "title": f"Map {n}", "url": f"https://github.com/o/r/issues/{n}",
              "updatedAt": "2026-08-03T00:00:00Z", "body": "",
              "subIssues": {"totalCount": 0, "nodes": []}} for n in numbers]
    return {"data": {"rateLimit": {"cost": cost, "remaining": remaining},
                     "repository": {"issues": {
                         "totalCount": total_count if total_count is not None else len(nodes),
                         "nodes": nodes}}}}


def test_a_repository_with_no_maps_is_answered_by_one_cheap_question(monkeypatch):
    asked = _recording_graphql(monkeypatch, [_discovery(0)])

    read = github.decision_maps(REPO, limit=5, children_limit=50, edges_limit=10)

    assert read == github.MapsRead(maps=(), total_count=0, cost=1, remaining=4999)
    assert len(asked) == 1, "no detail query is sent for a repository with no maps"
    query = asked[0][asked[0].index("-f") + 1]
    assert "childrenFirst" not in " ".join(asked[0]), "no child page size is requested"
    assert "edgesFirst" not in " ".join(asked[0]), "no dependency-edge page size is requested"
    assert "subIssues" not in query
    assert "nodes" not in query, "the count is the whole question — no issue fields are selected"


def test_a_repository_with_maps_asks_for_exactly_as_many_as_it_has(monkeypatch):
    asked = _recording_graphql(monkeypatch, [_discovery(3), _detail([1, 2, 3], cost=20)])

    read = github.decision_maps(REPO, limit=5, children_limit=50, edges_limit=10)

    assert len(asked) == 2
    assert "mapsFirst=3" in asked[1], "the detail page size matches what discovery counted"
    assert [m.number for m in read.maps] == [1, 2, 3]
    assert read.cost == 21, "both phases' reported costs are charged to the budget"
    assert read.remaining == 4979


def test_a_repository_with_more_maps_than_the_bound_still_asks_only_for_the_bound(monkeypatch):
    asked = _recording_graphql(monkeypatch, [_discovery(9), _detail([1, 2, 3, 4, 5],
                                                                   total_count=9)])

    read = github.decision_maps(REPO, limit=5, children_limit=50, edges_limit=10)

    assert "mapsFirst=5" in asked[1]
    assert read.total_count == 9, "GitHub's own count still drives overflow reporting"


def test_a_failed_detail_read_is_a_read_failure_not_an_empty_repository(monkeypatch):
    _recording_graphql(monkeypatch, [_discovery(3), None])
    read = github.decision_maps(REPO)
    assert read.maps == ()
    assert read.error == "boom", "the map read carries what GitHub said, not just that it failed"


def test_a_failed_discovery_read_is_a_read_failure(monkeypatch):
    _recording_graphql(monkeypatch, [None])
    read = github.decision_maps(REPO)
    assert read.maps == () and read.error == "boom"


def test_a_detail_read_that_shrank_publishes_what_it_returned(monkeypatch):
    # The last map can close between the two calls; what came back is the honest answer.
    _recording_graphql(monkeypatch, [_discovery(2), _detail([], cost=13)])
    read = github.decision_maps(REPO)
    assert read.maps == ()
    assert read.total_count == 0
    assert read.cost == 14
