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


def test_pr_state_reads_and_fails_closed(monkeypatch):
    _stub_json(monkeypatch, {"state": "MERGED"})
    assert github.pr_state(REPO, 9) == "MERGED"
    _stub(monkeypatch, returncode=1)
    assert github.pr_state(REPO, 9) is None


def test_pr_comments_are_typed_rows(monkeypatch):
    _stub_json(monkeypatch, {"comments": [
        {"body": "please rebase", "createdAt": "2026-07-19T00:00:00Z"}]})
    got = github.pr_comments(REPO, 9)
    assert got == [github.Comment(body="please rebase",
                                  created_at="2026-07-19T00:00:00Z")]


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
        {"number": 9, "headRefName": "feature/x", "headRefOid": "abc123"}])
    assert github.list_open_prs(REPO) == [
        github.PrRow(number=9, head_ref_name="feature/x", head_ref_oid="abc123")]


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


# --- writes report only what the command did ------------------------------------

@pytest.mark.parametrize("call", [
    lambda: github.add_label(REPO, 5, "agentflow:building"),
    lambda: github.remove_label(REPO, 5, "agentflow:building"),
    lambda: github.edit_title(REPO, 5, "new title"),
    lambda: github.edit_body(REPO, 5, "new body"),
    lambda: github.comment(REPO, 5, "hello"),
    lambda: github.pr_comment(REPO, 9, "hello"),
    lambda: github.close(REPO, 5),
    lambda: github.pr_ready(REPO, 9),
    lambda: github.create_label(REPO, "agentflow:building", "fbca04"),
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
