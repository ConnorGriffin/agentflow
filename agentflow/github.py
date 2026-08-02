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

Reads fetch only the fields they need — checking a label never pulls comment threads. Where a
stage must weigh several facts as one snapshot (a route's title *and* labels, a PR's head *and*
state), one typed read answers them together, so the caller fails closed on the pair rather than
acting on the half it happened to get.

Anything genuinely exotic — a GraphQL comment edit, a GraphQL parent-map lookup, a REST blockers
read, the auth token, a single add-and-remove label edit — goes through the explicitly-named
:func:`api` escape hatch, so nothing anywhere else shells out to `gh` *or* speaks GitHub's field
names. Both halves of that seam are enforced by architectural tests in ``tests/test_dispatch.py``.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

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
class IssueHeadline:
    """An issue's live title and labels — the pair a route projection is written against."""
    title: str
    labels: frozenset[str]


@dataclass(frozen=True)
class IssueSettlement:
    """An issue's live labels and URL — the pair a stage settlement proves itself against: the
    claim label is gone, and here is where the maintainer can see it."""
    labels: frozenset[str]
    url: str


@dataclass(frozen=True)
class IssueStanding:
    """An issue's live labels and open/closed state — the pair a claim proof weighs together:
    the claim label is still there, *and* the issue is still open to act on."""
    labels: frozenset[str]
    state: str


@dataclass(frozen=True)
class IssueView:
    """One issue read whole, for the cold paths that must weigh several facts at once."""
    title: str
    body: str
    state: str
    url: str
    labels: frozenset[str]
    comments: list[Comment]


@dataclass(frozen=True)
class ClaimedIssue:
    """One open issue carrying a claim label, with the last-touched time reconciliation needs."""
    number: int
    updated_at: str


@dataclass(frozen=True)
class PrRow:
    """One open pull request as returned by a discovery listing."""
    number: int
    head_ref_name: str
    head_ref_oid: str
    closing_issues: tuple[int, ...] = ()


@dataclass(frozen=True)
class BranchPrRow:
    """One PR opened for a branch in any state — the all-state counterpart to :class:`PrRow`."""
    number: int
    state: str
    head_ref_name: str
    url: str


@dataclass(frozen=True)
class PrFacts:
    """A PR's identity: the branch it is on, the exact commit at its head, its state, and the
    issues it declares it closes."""
    head_ref_name: str
    head_ref_oid: str
    state: str
    closing_issues: tuple[int, ...]


@dataclass(frozen=True)
class PrContent:
    """A PR's reviewable content: what it says, what it changes, and what has been said on it."""
    body: str
    paths: tuple[str, ...]
    comments: list[Comment]


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
class HandoffCandidateRow:
    """One issue a Decision Map child ``blocking``-links to — a handed-off Build Issue
    candidate until its marker, label namespace, and repository are verified (ADR 0036)."""
    number: int
    title: str
    url: str
    body: str
    labels: frozenset[str]
    repo: str


@dataclass(frozen=True)
class MapChildRow:
    """One Decision Map decision-child issue, with just enough of its dependency graph to
    classify the frontier and discover handoff candidates (ADR 0036)."""
    number: int
    title: str
    url: str
    state: str
    assigned: bool
    blocked_by_open: int
    blocked_by_closed: int
    blocked_by_total: int
    handoff_candidates: tuple[HandoffCandidateRow, ...]


@dataclass(frozen=True)
class MapRow:
    """One Decision Map issue (``wayfinder:map``) with its bounded decision set, in GitHub's
    native ``subIssues`` order (ADR 0036)."""
    number: int
    title: str
    url: str
    updated_at: str
    body: str
    children: tuple[MapChildRow, ...]
    children_total: int


@dataclass(frozen=True)
class MapsRead:
    """One repository's bounded Decision Map read: the active maps found, GitHub's own
    ``totalCount`` for overflow, and the point cost/remaining budget the daemon's stop
    signal reads (ADR 0036)."""
    maps: tuple[MapRow, ...]
    total_count: int
    cost: int | None
    remaining: int | None


@dataclass(frozen=True)
class HandoffLinkRow:
    """A verified handoff Build Issue's native closing-PR references — just the join key;
    the console resolves full pipeline evidence from the PR listings it already reads."""
    number: int
    pr_numbers: tuple[int, ...]
    attempt_count: int


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


def _closing_of(node: dict) -> tuple[int, ...]:
    return tuple(
        item["number"] for item in node.get("closingIssuesReferences") or []
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    )


def _comments_of(node: dict) -> list[Comment]:
    return [
        Comment(body=c.get("body", "") or "", created_at=c.get("createdAt", "") or "",
                id=c.get("id", "") or "")
        for c in node.get("comments", []) if isinstance(c, dict)
    ]


# --- URL forms (pure) ----------------------------------------------------------

def pr_url(repo: str, pr: int) -> str:
    """Where a human goes to look at this PR."""
    return f"https://github.com/{repo}/pull/{pr}"


def pr_number(url: str) -> int:
    """The PR number a pull-request URL names — :func:`pr_url` read back."""
    return int(url.rstrip("/").rsplit("/", 1)[-1])


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


def issue_url(repo: str, issue: int) -> str | None:
    """The issue's canonical URL, or ``None`` if it can't be read."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "url"])
    if not isinstance(data, dict):
        return None
    url = data.get("url")
    return url if isinstance(url, str) and url else None


def issue_headline(repo: str, issue: int) -> IssueHeadline | None:
    """The issue's live title and labels, or ``None`` if they can't be read. The two travel
    together because a route projection rewrites the title *against* the labels already there —
    reading one without the other would let a projection act on half a snapshot."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "title,labels"])
    if not isinstance(data, dict):
        return None
    return IssueHeadline(title=str(data.get("title") or ""), labels=_labels_of(data))


def issue_standing(repo: str, issue: int) -> IssueStanding | None:
    """The issue's live labels and state, or ``None`` if they can't be read. The two travel
    together because closing an issue does not strip its labels: a claim proof that read them
    apart could see the claim on an issue that closed between the reads and admit a session
    against a subject nothing can act on (#438)."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "labels,state"])
    if not isinstance(data, dict):
        return None
    return IssueStanding(labels=_labels_of(data), state=str(data.get("state") or ""))


def issue_settlement(repo: str, issue: int) -> IssueSettlement | None:
    """The issue's live labels and URL, or ``None`` if they can't be read — the one snapshot a
    stage settlement needs: proof the claim label is gone, and the link it hands back."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "labels,url"])
    if not isinstance(data, dict):
        return None
    return IssueSettlement(labels=_labels_of(data), url=str(data.get("url") or ""))


def issue_view(repo: str, issue: int) -> IssueView | None:
    """One issue read whole — title, body, state, URL, labels and comment thread in a single
    snapshot — or ``None`` if it can't be read.

    This is the deliberately cold read. Intake's verification and the research finalizer must
    prove one decision against several facts at once, and a proof assembled from separate reads
    is no proof. Every warm path (claim checks, projections, settlements) uses the single-fact
    reads above, so nothing on the hot path ever drags a comment thread it doesn't need."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo,
                       "--json", "title,body,state,url,labels,comments"])
    if not isinstance(data, dict):
        return None
    return IssueView(title=str(data.get("title") or ""), body=str(data.get("body") or ""),
                     state=str(data.get("state") or ""), url=str(data.get("url") or ""),
                     labels=_labels_of(data), comments=_comments_of(data))


def pr_is_draft(repo: str, pr: int) -> bool | None:
    """Whether the PR is still a draft, or ``None`` if that can't be read."""
    data = _read_json(["pr", "view", str(pr), "--repo", repo, "--json", "isDraft"])
    if not isinstance(data, dict):
        return None
    draft = data.get("isDraft")
    return draft if isinstance(draft, bool) else None


def pr_facts(repo: str, pr: int) -> PrFacts | None:
    """The PR's identity facts in one snapshot — branch, head commit, state, and the issues it
    declares it closes — or ``None`` if they can't be read.

    They travel together because every caller decides from the pair: a head SHA is only
    meaningful alongside the state it was read with, and acting on one while the other was
    unreadable is exactly the half-confirmed fact this module exists to prevent."""
    data = _read_json(["pr", "view", str(pr), "--repo", repo, "--json",
                       "headRefName,headRefOid,state,closingIssuesReferences"])
    if not isinstance(data, dict):
        return None
    return PrFacts(head_ref_name=str(data.get("headRefName") or ""),
                   head_ref_oid=str(data.get("headRefOid") or ""),
                   state=str(data.get("state") or ""),
                   closing_issues=_closing_of(data))


def pr_content(repo: str, pr: int) -> PrContent | None:
    """The PR's body, the paths it changes and its comment thread in one snapshot, or ``None``
    if it can't be read. Body, files and comments are one evidence question — the UI-evidence
    gate and the review-depth read both weigh them together — so they fail closed together."""
    data = _read_json(["pr", "view", str(pr), "--repo", repo, "--json", "body,files,comments"])
    if not isinstance(data, dict):
        return None
    return PrContent(
        body=str(data.get("body") or ""),
        paths=tuple(str(item["path"]) for item in data.get("files") or []
                    if isinstance(item, dict) and item.get("path")),
        comments=_comments_of(data))


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


def issue_comment_rows(repo: str, issue: int) -> list[dict] | None:
    """The issue's comments as GitHub's own rows, or ``None`` if they can't be read.

    The typed :func:`issue_comments` above is the shape callers should want. These rows exist
    for the gate/intake predicates that still read GitHub's own `author` key, which the typed
    comment does not carry — so the raw shape stays available here rather than every such
    caller reaching for the escape hatch. Same fail-closed contract: a real empty thread
    returns an empty list."""
    data = _read_json(["issue", "view", str(issue), "--repo", repo, "--json", "comments"])
    if not isinstance(data, dict):
        return None
    return data.get("comments", [])


def pr_comment_rows(repo: str, pr: int) -> list[dict] | None:
    """The PR's comments as GitHub's own rows, or ``None`` if they can't be read — the raw
    counterpart to :func:`pr_comments`, for the predicates that read `author` as well as
    `body`. A real empty thread returns an empty list."""
    data = _read_json(["pr", "view", str(pr), "--repo", repo, "--json", "comments"])
    if not isinstance(data, dict):
        return None
    return data.get("comments", [])


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
    args = ["pr", "list", "--repo", repo, "--state", "open", "--json",
            "number,headRefName,headRefOid,closingIssuesReferences", "--limit", str(limit)]
    if head is not None:
        args += ["--head", head]
    data = _read_json(args)
    if not isinstance(data, list):
        return None
    return [
        PrRow(number=row["number"], head_ref_name=row.get("headRefName", "") or "",
              head_ref_oid=row.get("headRefOid", "") or "",
              closing_issues=_closing_of(row))
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


def prs_for_branch(repo: str, branch: str, *, limit: int = 30) -> list[BranchPrRow] | None:
    """Every PR ever opened for ``branch`` — open, closed or merged — newest first, or ``None``
    if the listing failed. A branch that never had a PR returns an empty list.

    The all-state counterpart to :func:`list_open_prs`. A Build's outcome, a Revise's park
    target and a branch's latest PR state all turn on PRs that may already be closed or merged,
    so the open-only listing cannot answer them."""
    data = _read_json(["pr", "list", "--repo", repo, "--head", branch, "--state", "all",
                       "--json", "number,state,headRefName,url", "--limit", str(limit)])
    if not isinstance(data, list):
        return None
    return [
        BranchPrRow(number=row["number"], state=row.get("state", "") or "",
                    head_ref_name=row.get("headRefName", "") or "",
                    url=row.get("url", "") or "")
        for row in data if isinstance(row, dict) and isinstance(row.get("number"), int)
    ]


def open_pr_for_branch(repo: str, branch: str) -> PrRow | None:
    """The one open PR for the owned branch — its number and head SHA — or ``None`` when there is
    none or the read fails. The shared lookup behind every claim-transfer opener; a ``None`` leaves
    the completed record still claimed, so the next reconcile pass retries the transfer rather than
    stranding it."""
    prs = list_open_prs(repo, head=branch)
    if not prs:
        return None
    return prs[0]


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


def claimed_issues(repo: str, label: str) -> list[ClaimedIssue] | None:
    """Open issues carrying ``label``, with the last-touched time claim reconciliation needs,
    or ``None`` when the listing could not be read. No claims returns an empty list.

    This is a REST read on purpose. The equivalent ``gh issue list --label`` is answered by
    GitHub's *search*, whose ceiling is about 30 requests a minute — far below what one
    reconciliation pass costs across a fleet (four lanes per repo, every cycle), so the lane
    starved permanently once the fleet grew. The REST issues endpoint filters by label from the
    ordinary hourly budget instead. It also returns pull requests, which carry numbers from the
    same sequence as issues, so those are dropped — a PR must never be read as an issue's claim.
    """
    listed = _read_json(
        ["api", f"repos/{repo}/issues?state=open&labels={quote(label)}&per_page=100"])
    if not isinstance(listed, list):
        return None
    return [
        ClaimedIssue(number=item["number"], updated_at=str(item.get("updated_at", "") or ""))
        for item in listed
        if isinstance(item, dict) and "pull_request" not in item
        and isinstance(item.get("number"), int)
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


def create_label(repo: str, name: str, color: str, description: str = "") -> bool:
    """Create the label (idempotent via ``--force``), optionally with the description that
    explains it to a maintainer. Returns whether the command succeeded."""
    args = ["label", "create", name, "--repo", repo, "--color", color]
    if description:
        args += ["--description", description]
    return _gh([*args, "--force"]).returncode == 0


def pr_checks_passed(repo: str, pr: int) -> bool:
    """Whether every required check on the PR has completed successfully.

    Unlike the reads above this has no ``None``: `gh pr checks` exits non-zero while any check
    is pending, failed, or unreadable, so a check the command could not confirm is simply not
    passed — which is the fail-safe answer a merge gate wants anyway."""
    return _gh(["pr", "checks", str(pr), "--repo", repo]).returncode == 0


# The head check gate's state mapping (ADR 417), across both vocabularies GitHub returns in one
# rollup context list: check runs speak `conclusion`, legacy statuses speak `state`. `cancelled`
# and `stale` are deliberately not red — a cancelled run recorded no verdict, and parking a PR on
# one is a false human interrupt. An outcome named by neither table changes nothing: a missing
# entry never invents a disposition.
_CHECK_RUN_RED = frozenset({"FAILURE", "TIMED_OUT", "ACTION_REQUIRED"})
_STATUS_RED = frozenset({"FAILURE", "ERROR"})
_STATUS_PENDING = frozenset({"PENDING", "EXPECTED"})


@dataclass(frozen=True)
class HeadChecks:
    """The checks reported on one exact commit — the head check gate's whole answer (ADR 417).

    ``failing`` carries the red contexts' names (empty means not red), because the revise finding
    and the park body both must name the check, and a second read to fetch names would defeat the
    first. ``pending`` reports whether anything is still running — it never blocks a settlement,
    only distinguishes "green" from "not finished" for whoever logs it. A commit with no checks at
    all reads as neither red nor pending: absent checks settle exactly as today."""
    sha: str
    failing: tuple[str, ...] = ()
    pending: bool = False
    action_required: bool = False


def head_checks_from_rollup(nodes: list[dict], sha: str) -> HeadChecks:
    """Map one status-check-rollup context list onto the gate's typed answer. Pure (test
    surface); the two-vocabulary mapping above is the whole behavior."""
    failing: list[str] = []
    pending = False
    action_required = False
    for node in nodes:
        kind = node.get("__typename", "")
        if kind == "CheckRun":
            if (node.get("status") or "").upper() != "COMPLETED":
                pending = True
                continue
            conclusion = (node.get("conclusion") or "").upper()
            if conclusion in _CHECK_RUN_RED:
                failing.append(node.get("name") or "unnamed check")
                action_required = action_required or conclusion == "ACTION_REQUIRED"
        elif kind == "StatusContext":
            state = (node.get("state") or "").upper()
            if state in _STATUS_RED:
                failing.append(node.get("context") or "unnamed status")
            elif state in _STATUS_PENDING:
                pending = True
    return HeadChecks(sha=sha, failing=tuple(failing), pending=pending,
                      action_required=action_required)


_ROLLUP_QUERY = (
    "query($owner:String!,$name:String!,$oid:GitObjectID!){"
    "repository(owner:$owner,name:$name){object(oid:$oid){... on Commit{"
    "statusCheckRollup{contexts(first:100){nodes{__typename "
    "... on CheckRun{name status conclusion} "
    "... on StatusContext{context state}}}}}}}}")


def commit_head_checks(repo: str, sha: str) -> HeadChecks | None:
    """The status-check rollup for one exact commit, or ``None`` when GitHub is unreadable.

    The commit's rollup is the one read that returns check runs *and* legacy status contexts in a
    single list, which is what makes the two-vocabulary mapping tractable; there is no `gh`
    porcelain for an arbitrary commit's rollup, so this is a named GraphQL escape-hatch call.
    A missing commit (rewritten away by a force-push) is unreadable, not green: the gate must
    defer, never guess. A commit whose rollup is null simply has no checks."""
    owner, _, name = repo.partition("/")
    data = api(["api", "graphql",
                "-f", f"query={_ROLLUP_QUERY}",
                "-f", f"owner={owner}", "-f", f"name={name}", "-f", f"oid={sha}"],
               parse_json=True)
    if not isinstance(data, dict):
        return None
    commit = ((data.get("data") or {}).get("repository") or {}).get("object")
    if not isinstance(commit, dict):
        return None
    rollup = commit.get("statusCheckRollup") or {}
    nodes = (rollup.get("contexts") or {}).get("nodes") or []
    return head_checks_from_rollup([n for n in nodes if isinstance(n, dict)], sha)


def merge_pr(repo: str, pr: int) -> bool:
    """Squash-merge the PR and delete its head branch. Returns whether the command succeeded."""
    return _gh(["pr", "merge", str(pr), "--repo", repo,
                "--squash", "--delete-branch"]).returncode == 0


# --- Decision Map projection (ADR 0036) -----------------------------------------
#
# Two raw GraphQL calls, kept inside the seam like the check-rollup escape hatch above: `gh`'s
# porcelain has no notion of sub-issues or dependency edges. Discovery and per-map detail are
# combined into one nested query rather than the five separate detail calls ADR 0036 budgets
# for — GraphQL's nesting answers "give me each active map's children and their blockers" in
# one round trip, which is fewer requests than the ceiling allows, never more. The second call
# is exactly the ADR's "one handoff/pipeline batch query": it asks only for each verified
# handoff's closing-PR numbers, aliased per issue number since there is no batch-by-number
# porcelain; full pipeline evidence (title, merge commit, review, CI) is then resolved from the
# PR listings the daemon already reads (:func:`list_pipeline_prs`), not re-fetched here.

_MAP_LABEL = "wayfinder:map"

_MAPS_QUERY = (
    "query($owner:String!,$name:String!,$label:[String!],$mapsFirst:Int!,"
    "$childrenFirst:Int!,$edgesFirst:Int!){"
    "rateLimit{cost remaining}"
    "repository(owner:$owner,name:$name){"
    "issues(states:[OPEN],labels:$label,first:$mapsFirst,"
    "orderBy:{field:UPDATED_AT,direction:DESC}){"
    "totalCount nodes{number title url updatedAt body "
    "subIssues(first:$childrenFirst){totalCount nodes{"
    "number title url state "
    "assignees(first:1){totalCount} "
    "blockedBy(first:$edgesFirst){totalCount nodes{number state}} "
    "blocking(first:$edgesFirst){nodes{"
    "number title url body labels(first:20){nodes{name}} repository{nameWithOwner}"
    "}}"
    "}}"
    "}}"
    "}}"
    "}")


def _handoff_candidate_row(node: dict, repo: str) -> HandoffCandidateRow:
    return HandoffCandidateRow(
        number=node["number"], title=node.get("title", "") or "", url=node.get("url", "") or "",
        body=node.get("body", "") or "", labels=_labels_of(node),
        repo=((node.get("repository") or {}).get("nameWithOwner")) or repo)


def _map_child_row(node: dict, repo: str) -> MapChildRow:
    blocked = node.get("blockedBy") or {}
    blocked_nodes = [n for n in (blocked.get("nodes") or []) if isinstance(n, dict)]
    open_count = sum(1 for n in blocked_nodes if (n.get("state") or "").upper() != "CLOSED")
    closed_count = len(blocked_nodes) - open_count
    blocking = node.get("blocking") or {}
    candidates = tuple(
        _handoff_candidate_row(n, repo) for n in (blocking.get("nodes") or [])
        if isinstance(n, dict) and isinstance(n.get("number"), int))
    return MapChildRow(
        number=node["number"], title=node.get("title", "") or "", url=node.get("url", "") or "",
        state=node.get("state", "") or "",
        assigned=bool((node.get("assignees") or {}).get("totalCount")),
        blocked_by_open=open_count, blocked_by_closed=closed_count,
        blocked_by_total=blocked.get("totalCount") if isinstance(blocked.get("totalCount"), int)
        else len(blocked_nodes),
        handoff_candidates=candidates)


def _map_row(node: dict, repo: str) -> MapRow:
    sub = node.get("subIssues") or {}
    children_nodes = [c for c in (sub.get("nodes") or []) if isinstance(c, dict)]
    children = tuple(_map_child_row(c, repo) for c in children_nodes)
    return MapRow(
        number=node["number"], title=node.get("title", "") or "", url=node.get("url", "") or "",
        updated_at=node.get("updatedAt", "") or "", body=node.get("body", "") or "",
        children=children,
        children_total=sub.get("totalCount") if isinstance(sub.get("totalCount"), int)
        else len(children))


def decision_maps(repo: str, *, limit: int = 5, children_limit: int = 50,
                  edges_limit: int = 10) -> MapsRead | None:
    """The repository's active Decision Maps (open issues labeled ``wayfinder:map``), newest
    first, each with its bounded decision-child set and just enough dependency-graph data to
    classify the frontier and discover handoff candidates — or ``None`` when the read failed.
    An enrolled repository with no maps returns an empty read, not ``None`` (ADR 0036)."""
    owner, _, name = repo.partition("/")
    data = api(["api", "graphql", "-f", f"query={_MAPS_QUERY}",
               "-f", f"owner={owner}", "-f", f"name={name}",
               "-f", f"label[]={_MAP_LABEL}",
               "-F", f"mapsFirst={limit}", "-F", f"childrenFirst={children_limit}",
               "-F", f"edgesFirst={edges_limit}"],
              parse_json=True)
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    rate = payload.get("rateLimit") or {}
    issues = ((payload.get("repository") or {}).get("issues")) or {}
    nodes = issues.get("nodes")
    if not isinstance(nodes, list):
        return None
    maps = tuple(_map_row(n, repo) for n in nodes if isinstance(n, dict))
    return MapsRead(
        maps=maps,
        total_count=issues.get("totalCount") if isinstance(issues.get("totalCount"), int)
        else len(maps),
        cost=rate.get("cost") if isinstance(rate.get("cost"), int) else None,
        remaining=rate.get("remaining") if isinstance(rate.get("remaining"), int) else None)


def handoff_pr_links(repo: str, numbers: list[int]) -> dict[int, HandoffLinkRow] | None:
    """The native closing-PR numbers for each verified handoff Build Issue in ``numbers``
    (ADR 0036's join key), or ``None`` when the read failed. An empty ``numbers`` makes no
    call and returns ``{}``. Batched as one query with one alias per issue — there is no
    porcelain for "these N issues' closing PRs" in a single call."""
    wanted = sorted({n for n in numbers if isinstance(n, int)})
    if not wanted:
        return {}
    owner, _, name = repo.partition("/")
    fields = "".join(
        f'i{n}:issue(number:{n}){{closedByPullRequestsReferences'
        f'(first:5,includeClosedPrs:true){{totalCount nodes{{number}}}}}}'
        for n in wanted)
    query = f"query($owner:String!,$name:String!){{repository(owner:$owner,name:$name){{{fields}}}}}"
    data = api(["api", "graphql", "-f", f"query={query}",
               "-f", f"owner={owner}", "-f", f"name={name}"], parse_json=True)
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    repo_node = payload.get("repository")
    if not isinstance(repo_node, dict):
        return None
    out: dict[int, HandoffLinkRow] = {}
    for n in wanted:
        node = repo_node.get(f"i{n}") or {}
        refs = node.get("closedByPullRequestsReferences") or {}
        pr_nodes = [p for p in (refs.get("nodes") or []) if isinstance(p, dict)]
        pr_numbers = tuple(p["number"] for p in pr_nodes if isinstance(p.get("number"), int))
        out[n] = HandoffLinkRow(
            number=n, pr_numbers=pr_numbers,
            attempt_count=refs.get("totalCount") if isinstance(refs.get("totalCount"), int)
            else len(pr_numbers))
    return out


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
