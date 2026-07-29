"""The one seam through which all of agentflow's GitHub access flows (ADR 0040).

Every stage used to hand-roll the same four steps at ~90 call sites: build a `gh`
argument vector, check the return code, parse the JSON, and decide what to do when
GitHub could not be reached. GitHub's own wire field names (`headRefOid`, `createdAt`,
`labels[].name`) leaked through a dozen modules as a result. This module owns that
shape once, so callers get typed values and never see raw GitHub JSON keys.

Two rules the merge machinery depends on:

- **A failed read returns ``None`` ("unknown"), never an empty value.** ``None`` means
  only that the read *failed* — `gh` errored, timed out, or returned something
  unparseable. A real subject with no labels returns an empty set; a real empty comment
  thread returns an empty list. "Couldn't check" must never be confused with "nothing
  there", or a stage could act on a fact it never confirmed.
- **Writes report only what the command did.** They return whether the `gh` command
  succeeded; they never re-read to prove the change landed. Durable proof-on-write is a
  separate later effort and is deliberately not built here.

Reads fetch only the fields they need — checking a label never pulls comment threads.
Anything genuinely exotic (a GraphQL comment edit, a REST blockers read, the auth token)
goes through the single, explicitly-named :func:`api` escape hatch so nothing anywhere
else shells out to `gh`.

This module is a purely additive keystone: it migrates no existing caller, so it cannot
change how the pipeline behaves.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from agentflow.runner import _run


# --- typed rows ----------------------------------------------------------------
# GitHub's field names stop here: callers see these, never the wire JSON.

@dataclass(frozen=True)
class IssueRow:
    """One issue as returned by a discovery listing."""
    number: int
    title: str
    body: str
    labels: frozenset[str]
    updated_at: str | None = None


@dataclass(frozen=True)
class PrRow:
    """One open pull request as returned by a discovery listing."""
    number: int
    head_ref_name: str
    head_ref_oid: str


@dataclass(frozen=True)
class SnapshotPrRow:
    """One PR row for the fleet snapshot — title and merge timestamp included."""
    number: int
    title: str
    head_ref_name: str
    merged_at: str | None


@dataclass(frozen=True)
class PipelinePrRow:
    """One PR row for the workspace pipeline join — full evidence fields included."""
    number: int
    title: str
    head_ref_name: str
    url: str
    merged_at: str | None
    merge_commit_oid: str | None
    review_decision: str | None
    ci_rollup: list


@dataclass(frozen=True)
class Comment:
    """One comment on an issue or PR."""
    body: str
    created_at: str
    id: str = ""


@dataclass(frozen=True)
class SearchHit:
    """One issue/PR matched by the cross-repo change search."""
    number: int
    updated_at: str


@dataclass(frozen=True)
class IssueMatch:
    """One issue matched by a free-text search within a repo, body included so the caller can
    confirm the hit really carries what it searched for."""
    number: int
    url: str
    body: str


@dataclass(frozen=True)
class IssueCreation:
    """The outcome of creating an issue: the new issue's ``url``, or a ``url`` of ``None`` when
    the create failed, with ``error`` carrying `gh`'s own failure text for the caller to word."""
    url: str | None = None
    error: str = ""


# --- internals -----------------------------------------------------------------

def _gh(args: list[str]) -> subprocess.CompletedProcess:
    return _run(["gh", *args])


def _read_json(args: list[str]) -> Any | None:
    """Run a `gh` read and parse its stdout as JSON, or ``None`` if the command failed
    or the output was unparseable. This is the one place the fail-closed read contract
    lives: every typed read below builds its typed value on top of a non-``None`` return."""
    r = _gh(args)
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "")
    except json.JSONDecodeError:
        return None


def _labels_of(node: dict) -> frozenset[str]:
    return frozenset(
        lbl["name"] for lbl in node.get("labels", [])
        if isinstance(lbl, dict) and lbl.get("name")
    )


def _comments_of(node: dict) -> list[Comment]:
    return [
        Comment(body=c.get("body", "") or "", created_at=c.get("createdAt", "") or "",
                id=c.get("id", "") or "")
        for c in node.get("comments", []) if isinstance(c, dict)
    ]


# --- reads (fail closed on None) -----------------------------------------------

def issue_labels(repo: str, issue: int) -> frozenset[str] | None:
    """The issue's labels, or ``None`` if they can't be read. An issue with no labels
    returns an empty set — a real fact, distinct from an unreadable one."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "labels"])
    if not isinstance(data, dict):
        return None
    return _labels_of(data)


def issue_body(repo: str, issue: int) -> str | None:
    """The issue's body text, or ``None`` if it can't be read. An empty body reads
    as ``""`` — distinct from unknown."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "body"])
    if not isinstance(data, dict):
        return None
    body = data.get("body")
    return body if isinstance(body, str) else ""


def issue_state(repo: str, issue: int) -> str | None:
    """The issue's state (``OPEN``/``CLOSED``), or ``None`` if it can't be read."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "state"])
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    return state if isinstance(state, str) and state else None


def pr_state(repo: str, pr: int) -> str | None:
    """The PR's state (``OPEN``/``MERGED``/``CLOSED``), or ``None`` if it can't be read."""
    data = _read_json(["pr", "view", str(pr), "--repo", repo, "--json", "state"])
    if not isinstance(data, dict):
        return None
    state = data.get("state")
    return state if isinstance(state, str) and state else None


def issue_comments(repo: str, issue: int) -> list[Comment] | None:
    """The issue's comments, or ``None`` if they can't be read. A real empty thread
    returns an empty list."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "comments"])
    if not isinstance(data, dict):
        return None
    return _comments_of(data)


def pr_comments(repo: str, pr: int) -> list[Comment] | None:
    """The PR's comments, or ``None`` if they can't be read. A real empty thread
    returns an empty list."""
    data = _read_json(["pr", "view", str(pr), "--repo", repo, "--json", "comments"])
    if not isinstance(data, dict):
        return None
    return _comments_of(data)


def list_issues(repo: str, *, label: str | None = None,
                limit: int = 100) -> list[IssueRow] | None:
    """The open issues in ``repo`` (optionally filtered to one ``label``) as typed rows,
    or ``None`` if the listing failed. An empty repo returns an empty list. This is a
    discovery collection: it reads number/title/body/labels/updatedAt in one call by design."""
    args = ["issue", "list", "--repo", repo, "--state", "open",
            "--json", "number,title,body,labels,updatedAt", "--limit", str(limit)]
    if label is not None:
        args += ["--label", label]
    data = _read_json(args)
    if not isinstance(data, list):
        return None
    return [
        IssueRow(number=row["number"], title=row.get("title", "") or "",
                 body=row.get("body", "") or "", labels=_labels_of(row),
                 updated_at=row.get("updatedAt") or None)
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


def list_open_prs(repo: str, *, head: str | None = None,
                  limit: int = 100) -> list[PrRow] | None:
    """The open PRs in ``repo`` (optionally only those whose head branch is ``head``) as
    typed rows, or ``None`` if the listing failed. No open PRs returns an empty list."""
    args = ["pr", "list", "--repo", repo, "--state", "open",
            "--json", "number,headRefName,headRefOid", "--limit", str(limit)]
    if head is not None:
        args += ["--head", head]
    data = _read_json(args)
    if not isinstance(data, list):
        return None
    return [
        PrRow(number=row["number"], head_ref_name=row.get("headRefName", "") or "",
              head_ref_oid=row.get("headRefOid", "") or "")
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


def list_prs(repo: str, state: str, *, limit: int = 30) -> list[SnapshotPrRow] | None:
    """PRs in ``repo`` with the given ``state`` as snapshot rows (number, title, branch,
    merge timestamp), or ``None`` if the listing failed. No PRs returns an empty list."""
    data = _read_json(["pr", "list", "--repo", repo, "--state", state,
                       "--json", "number,title,headRefName,mergedAt", "--limit", str(limit)])
    if not isinstance(data, list):
        return None
    return [
        SnapshotPrRow(number=row["number"], title=row.get("title", "") or "",
                      head_ref_name=row.get("headRefName", "") or "",
                      merged_at=row.get("mergedAt") or None)
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


def list_pipeline_prs(repo: str, state: str, *, limit: int = 50) -> list[PipelinePrRow] | None:
    """PRs in ``repo`` with the given ``state`` as pipeline rows — full evidence fields
    (review decision, CI rollup, merge commit) included for the workspace pipeline join
    (ADR 0033). Returns ``None`` if the listing failed; no PRs returns an empty list."""
    data = _read_json(["pr", "list", "--repo", repo, "--state", state, "--json",
                       "number,title,headRefName,url,mergedAt,mergeCommit,"
                       "reviewDecision,statusCheckRollup", "--limit", str(limit)])
    if not isinstance(data, list):
        return None
    return [
        PipelinePrRow(number=row["number"], title=row.get("title", "") or "",
                      head_ref_name=row.get("headRefName", "") or "",
                      url=row.get("url", "") or "",
                      merged_at=row.get("mergedAt") or None,
                      merge_commit_oid=(row.get("mergeCommit") or {}).get("oid") or None,
                      review_decision=row.get("reviewDecision") or None,
                      ci_rollup=row.get("statusCheckRollup") or [])
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


def search(repos: list[str], since: str, *, limit: int = 100) -> list[SearchHit] | None:
    """One cross-repo search for every issue/PR updated after ``since``, as typed rows, or
    ``None`` if the search itself failed. No matches returns an empty list — unknown is not
    'no change'."""
    args = ["search", "issues", "--include-prs", "--limit", str(limit),
            "--json", "number,updatedAt"]
    for repo in repos:
        args += ["--repo", repo]
    args += ["--updated", f">{since}"]
    data = _read_json(args)
    if not isinstance(data, list):
        return None
    return [
        SearchHit(number=row["number"], updated_at=row.get("updatedAt", "") or "")
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


def find_issues(repo: str, term: str, *, limit: int = 50) -> list[IssueMatch] | None:
    """Issues in ``repo`` in any state whose free-text search matches ``term``, as typed rows,
    or ``None`` if the search failed. No matches returns an empty list."""
    data = _read_json(["issue", "list", "--repo", repo, "--state", "all", "--search", term,
                       "--json", "number,url,body", "--limit", str(limit)])
    if not isinstance(data, list):
        return None
    return [
        IssueMatch(number=row["number"], url=row.get("url", "") or "",
                   body=row.get("body", "") or "")
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


# --- writes (report the command's result, not durable proof) -------------------

def create_issue(repo: str, title: str, body: str) -> IssueCreation:
    """Create an issue and report what the command did: the new issue's URL, or `gh`'s failure
    text. Like every write here it reports the command's result — it never re-reads to prove the
    issue landed."""
    r = _gh(["issue", "create", "--repo", repo, "--title", title, "--body", body])
    if r.returncode != 0:
        return IssueCreation(error=getattr(r, "stderr", "") or "")
    return IssueCreation(url=(r.stdout or "").strip().splitlines()[-1].strip() if r.stdout else "")



def add_label(repo: str, issue: int, label: str) -> bool:
    """Add ``label`` to the issue. Returns whether the command succeeded."""
    return _gh(["issue", "edit", str(issue), "--repo", repo,
                "--add-label", label]).returncode == 0


def remove_label(repo: str, issue: int, label: str) -> bool:
    """Remove ``label`` from the issue. Returns whether the command succeeded."""
    return _gh(["issue", "edit", str(issue), "--repo", repo,
                "--remove-label", label]).returncode == 0


def edit_title(repo: str, issue: int, title: str) -> bool:
    """Set the issue's title. Returns whether the command succeeded."""
    return _gh(["issue", "edit", str(issue), "--repo", repo,
                "--title", title]).returncode == 0


def edit_body(repo: str, issue: int, body: str) -> bool:
    """Set the issue's body. Returns whether the command succeeded."""
    return _gh(["issue", "edit", str(issue), "--repo", repo,
                "--body", body]).returncode == 0


def comment(repo: str, issue: int, body: str) -> bool:
    """Post a comment on the issue. Returns whether the command succeeded."""
    return _gh(["issue", "comment", str(issue), "--repo", repo,
                "--body", body]).returncode == 0


def pr_comment(repo: str, pr: int, body: str) -> bool:
    """Post a comment on the PR. Returns whether the command succeeded."""
    return _gh(["pr", "comment", str(pr), "--repo", repo,
                "--body", body]).returncode == 0


def edit_comment(comment_id: str, body: str) -> bool:
    """Replace one issue/PR comment by its opaque GraphQL node id."""
    if not comment_id:
        return False
    mutation = (
        "mutation($id:ID!,$body:String!){updateIssueComment(input:"
        "{id:$id,body:$body}){issueComment{id}}}")
    return _gh([
        "api", "graphql", "-f", f"query={mutation}", "-f", f"id={comment_id}",
        "-f", f"body={body}",
    ]).returncode == 0


def close(repo: str, issue: int) -> bool:
    """Close the issue. Returns whether the command succeeded."""
    return _gh(["issue", "close", str(issue), "--repo", repo]).returncode == 0


def pr_ready(repo: str, pr: int) -> bool:
    """Mark the PR ready for review (undraft it). Returns whether the command succeeded."""
    return _gh(["pr", "ready", str(pr), "--repo", repo]).returncode == 0


def create_label(repo: str, name: str, color: str) -> bool:
    """Create the label (idempotent via ``--force``). Returns whether the command succeeded."""
    return _gh(["label", "create", name, "--repo", repo,
                "--color", color, "--force"]).returncode == 0


# --- escape hatch --------------------------------------------------------------

def api(args: list[str], *, parse_json: bool = False) -> Any | None:
    """The one explicitly-marked escape hatch for GitHub calls that don't fit the typed
    surface — a GraphQL comment edit, a REST blockers read, the auth token. It runs
    ``gh <args>`` through the same fail-closed handling as every read: ``None`` means the
    command failed. Otherwise it returns the parsed JSON (when ``parse_json``) or the
    stripped stdout string. A caller reaching for this owns GitHub's shape for that one
    call — but nowhere else does anything shell out to `gh`."""
    if parse_json:
        return _read_json(args)
    r = _gh(args)
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()
