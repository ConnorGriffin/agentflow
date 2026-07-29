"""Worktree lifecycle for unattended research runs (issue #193).

A resolved research run's isolated worktree must be removed when resolution is durable; a run
that drops its claim without resolving (e.g. out-of-budget) must leave its worktree intact so
the next attempt resumes from partial findings.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

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


class _FakeGitHub:
    """A stateful stand-in for the ticket, its parent Decision Map, and the shared claim. The
    finalizer's GitHub reads/writes are stated through the GitHub module's helpers (ADR 0040) —
    never by matching a ``gh`` argument vector. ``run`` stands in for the loop-owned ``_run`` so
    the git worktree cleanup can be observed."""

    def __init__(self, number=7, *, map_body="# Map\n\n## Decisions so far\n\n"):
        self.number = number
        self.state = "OPEN"
        self.title = "Audit the widget path"
        self.comments: list[dict] = []
        self.labels = ["wayfinder:resolving"]
        self.map_body = map_body
        self.git_calls: list[list[str]] = []

    # --- GitHub module seam (ADR 0040) ------------------------------------------------
    def api(self, args, *, parse_json=False):
        # Routed by leading verb — the GraphQL parent-map read vs. an issue snapshot — never by
        # matching the read's field vector.
        if args[0] == "api":                       # GraphQL parent-map read
            return {"data": {"repository": {"issue": {"parent": {
                "number": 4, "body": self.map_body,
                "labels": {"nodes": [{"name": "wayfinder:map"}]}}}}}}
        return {"state": self.state, "title": self.title, "comments": list(self.comments),
                "url": f"https://github.com/o/r/issues/{self.number}"}

    def comment(self, repo, number, body):
        self.comments.append({"body": body})
        return True

    def close(self, repo, number):
        self.state = "CLOSED"
        return True

    def edit_body(self, repo, number, body):       # the parent map's breadcrumb edit
        self.map_body = body
        return True

    def release(self, repo, number, _label):       # stands in for coordinated_research.release_claim
        if "wayfinder:resolving" in self.labels:
            self.labels.remove("wayfinder:resolving")
        return True

    def run(self, argv):                           # coordinated_research._run: only the git worktree cleanup remains
        assert argv and argv[0] == "git", f"unexpected non-git coordinated_research._run call: {argv}"
        self.git_calls.append(list(argv))
        if "worktree" in argv and "remove" in argv:
            shutil.rmtree(argv[-1], ignore_errors=True)
        return _R(0)

    @property
    def worktree_removals(self):
        return [c for c in self.git_calls if "worktree" in c and "remove" in c]

    def install(self, monkeypatch):
        from agentflow import github, loop
        monkeypatch.setattr(github, "api", self.api)
        monkeypatch.setattr(github, "comment", self.comment)
        monkeypatch.setattr(github, "close", self.close)
        monkeypatch.setattr(github, "edit_body", self.edit_body)
        monkeypatch.setattr(coordinated_research, "release_claim", self.release)
        monkeypatch.setattr(coordinated_research, "_run", self.run)


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

    gh = _FakeGitHub()
    gh.install(monkeypatch)

    proof = coordinated_research.resolve(record)

    assert proof is not None, "resolve() must return a proof URL on durable resolution"
    assert not wt.exists(), "worktree must be removed after durable resolution"
    assert gh.worktree_removals, "git worktree remove must have been called"


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

    gh = _FakeGitHub()
    gh.install(monkeypatch)

    proof = coordinated_research.resolve(record)

    assert proof is not None, "resolve() must succeed even if worktree is already gone"
    assert not gh.worktree_removals, "git worktree remove must be skipped when directory is absent"


def test_release_does_not_remove_the_worktree(tmp_path, monkeypatch):
    """release() (out-of-budget exhaustion path) must leave the worktree intact so the next
    attempt resumes from partial findings rather than starting from scratch."""
    record = _make_record(tmp_path)
    wt = Path(record.source)
    assert wt.exists()

    gh = _FakeGitHub()
    gh.install(monkeypatch)

    proof = coordinated_research.release(record)

    assert proof is not None, "release() must return a proof URL"
    assert wt.exists(), "worktree must survive a non-resolving exit so the next attempt resumes"
    assert not gh.worktree_removals, "git worktree remove must NOT be called on exhaustion"
