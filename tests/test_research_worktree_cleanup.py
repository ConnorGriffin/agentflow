"""Worktree lifecycle for unattended research runs (issue #193).

A resolved research run's isolated worktree must be removed when resolution is durable; a run
that drops its claim without resolving (e.g. out-of-budget) must leave its worktree intact so
the next attempt resumes from partial findings.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import coordinated_research


def _R(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _make_record(tmp_path, number=7):
    """A minimal record whose source points at a real directory inside tmp_path."""
    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / f"research-{number}"
    wt.mkdir(parents=True)
    return SimpleNamespace(
        repo="o/r",
        subject=str(number),
        pool="claude",
        source=str(wt),
    )


def _write_findings(record):
    """Plant the findings artifact the session would have written."""
    findings = Path(coordinated_research.findings_path(record))
    findings.parent.mkdir(parents=True, exist_ok=True)
    findings.write_text("The answer is 42.")


def _make_github_run(number=7, *, map_body="# Map\n\n## Decisions so far\n\n"):
    """A minimal stateful fake for the gh calls resolve() makes."""
    state = {"gh_state": "OPEN", "comments": [], "labels": ["wayfinder:resolving"],
             "map_body": map_body}

    def run(argv):
        head = argv[1:3]
        if argv[1] == "api" and argv[2] == "graphql":
            data = {"data": {"repository": {"issue": {"parent": {
                "number": 4, "body": state["map_body"],
                "labels": {"nodes": [{"name": "wayfinder:map"}]}}}}}}
            return _R(0, json.dumps(data))
        if head == ["issue", "view"]:
            fields = argv[argv.index("--json") + 1].split(",")
            out: dict = {}
            if "state" in fields:
                out["state"] = state["gh_state"]
            if "title" in fields:
                out["title"] = "Audit the widget path"
            if "comments" in fields:
                out["comments"] = list(state["comments"])
            if "labels" in fields:
                out["labels"] = [{"name": n} for n in state["labels"]]
            if "url" in fields:
                out["url"] = f"https://github.com/o/r/issues/{number}"
            return _R(0, json.dumps(out))
        if head == ["issue", "comment"]:
            body = argv[argv.index("--body") + 1]
            state["comments"].append({"body": body})
            return _R(0)
        if head == ["issue", "close"]:
            state["gh_state"] = "CLOSED"
            return _R(0)
        if head == ["issue", "edit"]:
            if "--body" in argv:
                state["map_body"] = argv[argv.index("--body") + 1]
            if "--remove-label" in argv:
                lbl = argv[argv.index("--remove-label") + 1]
                if lbl in state["labels"]:
                    state["labels"].remove(lbl)
            if "--add-label" in argv:
                state["labels"].append(argv[argv.index("--add-label") + 1])
            return _R(0)
        if head == ["label", "create"]:
            return _R(0)
        raise AssertionError(f"unexpected gh call: {argv}")

    return run


# --- worktree cleanup on durable resolution -------------------------------------

def test_resolve_removes_the_isolated_worktree_on_durable_resolution(tmp_path, monkeypatch):
    """resolve() must deregister and remove the run's worktree once the ticket is closed with
    findings and the claim is released — the resource-leak fix from issue #193.

    This test would have failed before the fix: the worktree directory would still exist after
    resolve() returned a proof."""
    record = _make_record(tmp_path)
    _write_findings(record)
    wt = Path(record.source)
    assert wt.exists()

    gh_run = _make_github_run()
    git_calls: list[list[str]] = []

    def fake_run(argv):
        if argv[0] == "git":
            git_calls.append(list(argv))
            # argv = ["git", "-C", workdir, "worktree", "remove", "--force", path]
            if "worktree" in argv and "remove" in argv:
                shutil.rmtree(argv[-1], ignore_errors=True)
            return _R(0)
        return gh_run(argv)

    monkeypatch.setattr("agentflow.loop._run", fake_run)

    proof = coordinated_research.resolve(record)

    assert proof is not None, "resolve() must return a proof URL on durable resolution"
    assert not wt.exists(), "worktree must be removed after durable resolution"
    worktree_removals = [c for c in git_calls if "worktree" in c and "remove" in c]
    assert worktree_removals, "git worktree remove must have been called"


def test_resolve_tolerates_already_missing_worktree(tmp_path, monkeypatch):
    """resolve() must succeed even when the worktree directory is already absent — cleanup
    must be skipped gracefully rather than blocking the proof."""
    record = _make_record(tmp_path)
    _write_findings(record)
    wt = Path(record.source)

    # Read the findings content before removing the directory, then stub read_findings.
    findings_text = "The answer is 42."
    shutil.rmtree(wt)
    assert not wt.exists()

    monkeypatch.setattr(coordinated_research, "read_findings", lambda _r: findings_text)

    git_calls: list[list[str]] = []
    gh_run = _make_github_run()

    def fake_run(argv):
        if argv[0] == "git":
            git_calls.append(list(argv))
            return _R(0)
        return gh_run(argv)

    monkeypatch.setattr("agentflow.loop._run", fake_run)

    proof = coordinated_research.resolve(record)

    assert proof is not None, "resolve() must succeed even if worktree is already gone"
    worktree_removals = [c for c in git_calls if "worktree" in c and "remove" in c]
    assert not worktree_removals, "git worktree remove must be skipped when directory is absent"


def test_release_does_not_remove_the_worktree(tmp_path, monkeypatch):
    """release() (out-of-budget exhaustion path) must leave the worktree intact so the next
    attempt resumes from partial findings rather than starting from scratch."""
    record = _make_record(tmp_path)
    wt = Path(record.source)
    assert wt.exists()

    git_calls: list[list[str]] = []
    gh_state = {"labels": ["wayfinder:resolving"]}

    def fake_run(argv):
        if argv[0] == "git":
            git_calls.append(list(argv))
            return _R(0)
        head = argv[1:3]
        if head == ["issue", "view"]:
            fields = argv[argv.index("--json") + 1].split(",")
            out: dict = {}
            if "labels" in fields:
                out["labels"] = [{"name": n} for n in gh_state["labels"]]
            if "url" in fields:
                out["url"] = "https://github.com/o/r/issues/7"
            return _R(0, json.dumps(out))
        if head == ["issue", "edit"] and "--remove-label" in argv:
            lbl = argv[argv.index("--remove-label") + 1]
            if lbl in gh_state["labels"]:
                gh_state["labels"].remove(lbl)
            return _R(0)
        if head == ["label", "create"]:
            return _R(0)
        raise AssertionError(f"unexpected call in release test: {argv}")

    monkeypatch.setattr("agentflow.loop._run", fake_run)

    proof = coordinated_research.release(record)

    assert proof is not None, "release() must return a proof URL"
    assert wt.exists(), "worktree must survive a non-resolving exit so the next attempt resumes"
    worktree_removals = [c for c in git_calls if "worktree" in c and "remove" in c]
    assert not worktree_removals, "git worktree remove must NOT be called on exhaustion"
