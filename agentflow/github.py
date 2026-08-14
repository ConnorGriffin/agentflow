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

import base64
import binascii
import json
import shlex
import subprocess
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from agentflow.runner import _run


def command_creates_issue(command: str) -> bool:
    """Whether a captured shell command invokes GitHub's issue-creation verb."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False
    segment: list[str] = []
    for token in (*tokens, ";"):
        if token not in {";", "&&", "||", "|"}:
            segment.append(token)
            continue
        while segment and "=" in segment[0] and not segment[0].startswith("="):
            segment.pop(0)
        if segment[:1] == ["env"]:
            segment.pop(0)
            while segment and (segment[0].startswith("-") or "=" in segment[0]):
                segment.pop(0)
        if segment[:1] == ["command"]:
            segment.pop(0)
        if segment[:1] in (["sh"], ["bash"], ["zsh"]) and "-c" in segment:
            script = segment[segment.index("-c") + 1:]
            if script and command_creates_issue(script[0]):
                return True
        if len(segment) >= 3 and segment[:3] == ["gh", "issue", "create"]:
            return True
        segment = []
    return False


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
    id: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ClaimedIssue:
    """One issue carrying a claim label, with the last-touched time reconciliation needs."""
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
class PromotionAuthorityRead:
    """One exact merged-PR authority snapshot, including the checked-in artifact bytes."""
    repository: str
    pull_number: int
    merged: bool
    merge_commit: str
    head_commit: str
    tree: str
    artifact_path: str
    artifact_revision: str
    artifact_bytes: bytes
    linked_issue_closed: bool
    linked_issue_completed: bool
    merged_by: str
    merged_by_permission: str


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
    candidate until its marker, label namespace, and repository are verified (ADR 0036).
    ``id`` is GitHub's own node ID: the only identifier that is unique across repositories,
    and so the only safe key to deduplicate accepted candidates by. A candidate that arrives
    without one is never typed at all — an unidentifiable edge cannot be verified."""
    id: str
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
    # GitHub's own count of this child's outgoing `blocking` edges. More than were returned
    # means a handoff could be hiding behind the bound — handoff completeness only, never
    # frontier verification, which reads `blockedBy` (ADR 0036).
    handoff_edges_total: int


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
    signal reads (ADR 0036). ``error`` is set — and ``maps`` empty — exactly when the read
    failed, carrying GitHub's own diagnostic so the projection can say *why* rather than
    only *that* (ADR 374)."""
    maps: tuple[MapRow, ...]
    total_count: int
    cost: int | None
    remaining: int | None
    error: str | None = None


@dataclass(frozen=True)
class MapQueryRead:
    """One Decision Map GraphQL call exactly as GitHub answered it: the whole parsed response
    kept for evidence, its ``data`` payload for typing, the point cost/remaining it reported,
    and a human-safe diagnostic when the call failed (ADR 374). Only the two Decision Map
    queries are read this way — every other typed read keeps the plain "``None`` means
    unreadable" contract."""
    raw: Any | None
    payload: dict | None
    cost: int | None
    remaining: int | None
    error: str | None


@dataclass(frozen=True)
class HandoffLinksRead:
    """The typed handoff-link join plus the accounting and diagnostic from its map query."""
    links: dict[int, HandoffLinkRow]
    cost: int | None
    remaining: int | None
    error: str | None = None


@dataclass(frozen=True)
class HandoffAttemptRow:
    """One pull request that closed — or tried to close — a handed-off Build Issue, as its own
    native closing reference reports it. Carries landed state itself rather than only a join
    key, so a pull request that merged before the console's PR listing window still reads as
    landed (ADR 0036: closing references are the authority, never branch names)."""
    number: int
    url: str
    state: str
    merged_at: str | None


@dataclass(frozen=True)
class HandoffLinkRow:
    """A verified handoff Build Issue's native closing-PR references: every attempt GitHub
    returned within the bound, plus its own total so a repeated hand-off says how many times
    it was attempted."""
    number: int
    attempts: tuple[HandoffAttemptRow, ...]
    attempt_count: int


@dataclass(frozen=True)
class Comment:
    """One comment on an issue or PR."""
    body: str
    created_at: str
    id: str = ""
    updated_at: str = ""
    url: str = ""


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


def _connected_labels_of(node: dict) -> frozenset[str]:
    """Labels as GraphQL nests them — ``labels(first:N){nodes{name}}`` — rather than the flat
    list `gh`'s porcelain returns. Kept separate from :func:`_labels_of` on purpose: only the
    Decision Map query asks for a label *connection*, and one parser guessing which of the two
    shapes it was handed would silently return an empty set for whichever it guessed wrong."""
    labels = node.get("labels")
    nodes = labels.get("nodes") if isinstance(labels, dict) else None
    return frozenset(
        lbl["name"] for lbl in (nodes or [])
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
                id=c.get("id", "") or "", updated_at=c.get("updatedAt", "") or "",
                url=c.get("url", "") or "")
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
                       "--json", "id,title,body,state,url,updatedAt,labels,comments"])
    if not isinstance(data, dict):
        return None
    return IssueView(title=str(data.get("title") or ""), body=str(data.get("body") or ""),
                     state=str(data.get("state") or ""), url=str(data.get("url") or ""),
                     labels=_labels_of(data), comments=_comments_of(data),
                     id=str(data.get("id") or ""), updated_at=str(data.get("updatedAt") or ""))


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


_PROMOTION_AUTHORITY_QUERY = (
    "query($owner:String!,$name:String!,$number:Int!){"
    "repository(owner:$owner,name:$name){pullRequest(number:$number){"
    "number state merged mergedAt headRefOid mergedBy{login} "
    "mergeCommit{oid tree{oid}} "
    "closingIssuesReferences(first:100){totalCount nodes{state stateReason}}"
    "}}}")


def promotion_authority_read(repository: str, pull_number: int, artifact_path: str,
                             revision: str) -> PromotionAuthorityRead | None:
    """Read one exact promotion authority, or ``None`` when any fact is unavailable.

    GitHub wire shapes and artifact decoding stay inside this module. The artifact is read at
    ``revision`` through the Contents API, never from the working tree or current default branch.
    """
    try:
        owner, separator, name = repository.partition("/")
        if (not separator or not owner or not name or "/" in name
                or isinstance(pull_number, bool) or not isinstance(pull_number, int)
                or pull_number < 1 or not artifact_path or not revision):
            return None
        data = _read_json([
            "api", "graphql", "-f", f"query={_PROMOTION_AUTHORITY_QUERY}",
            "-f", f"owner={owner}", "-f", f"name={name}", "-F", f"number={pull_number}",
        ])
        if not isinstance(data, dict) or data.get("errors"):
            return None
        pull = (((data.get("data") or {}).get("repository") or {}).get("pullRequest"))
        if (not isinstance(pull, dict) or isinstance(pull.get("number"), bool)
                or pull.get("number") != pull_number):
            return None
        merge = pull.get("mergeCommit")
        actor = pull.get("mergedBy")
        tree = merge.get("tree") if isinstance(merge, dict) else None
        if not isinstance(merge, dict) or not isinstance(tree, dict) or not isinstance(actor, dict):
            return None
        merged_by = actor.get("login")
        if not isinstance(merged_by, str) or not merged_by:
            return None
        permission = _read_json([
            "api", f"repos/{repository}/collaborators/{quote(merged_by, safe='')}/permission",
        ])
        encoded_path = quote(artifact_path, safe="/")
        encoded_revision = quote(revision, safe="")
        artifact = _read_json([
            "api", f"repos/{repository}/contents/{encoded_path}?ref={encoded_revision}",
        ])
        if (not isinstance(permission, dict) or not isinstance(artifact, dict)
                or artifact.get("type") != "file" or artifact.get("encoding") != "base64"
                or not isinstance(artifact.get("content"), str)):
            return None
        encoded = "".join(artifact["content"].splitlines())
        artifact_bytes = base64.b64decode(encoded, validate=True)
        if (isinstance(artifact.get("size"), bool) or not isinstance(artifact.get("size"), int)
                or artifact["size"] != len(artifact_bytes)):
            return None
        references = pull.get("closingIssuesReferences")
        nodes = references.get("nodes") if isinstance(references, dict) else None
        total = references.get("totalCount") if isinstance(references, dict) else None
        if (not isinstance(nodes, list) or isinstance(total, bool) or not isinstance(total, int)
                or total != len(nodes)
                or any(not isinstance(node, dict) for node in nodes)):
            return None
        completed = any(node.get("state") == "CLOSED" and node.get("stateReason") == "COMPLETED"
                        for node in nodes)
        return PromotionAuthorityRead(
            repository=repository,
            pull_number=pull_number,
            merged=pull.get("merged") is True and pull.get("state") == "MERGED"
            and bool(pull.get("mergedAt")),
            merge_commit=str(merge.get("oid") or ""),
            head_commit=str(pull.get("headRefOid") or ""),
            tree=str(tree.get("oid") or ""),
            artifact_path=artifact_path,
            artifact_revision=revision,
            artifact_bytes=artifact_bytes,
            linked_issue_closed=completed,
            linked_issue_completed=completed,
            merged_by=merged_by,
            merged_by_permission=str(permission.get("permission") or ""),
        )
    except (AttributeError, binascii.Error, TypeError, ValueError):
        return None


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
    """Issues in any state carrying ``label``, with the last-touched time claim reconciliation
    needs, or ``None`` when the listing could not be read. No claims returns an empty list.

    This is a REST read on purpose. The equivalent ``gh issue list --label`` is answered by
    GitHub's *search*, whose ceiling is about 30 requests a minute — far below what one
    reconciliation pass costs across a fleet (four lanes per repo, every cycle), so the lane
    starved permanently once the fleet grew. The REST issues endpoint filters by label from the
    ordinary hourly budget instead. It also returns pull requests, which carry numbers from the
    same sequence as issues, so those are dropped — a PR must never be read as an issue's claim.
    """
    listed = _read_json(
        ["api", f"repos/{repo}/issues?state=all&labels={quote(label)}&per_page=100"])
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


def create_pr(repo: str, *, head: str, title: str, body: str) -> IssueCreation:
    """Open a pull request from `head` against the repo's default branch, reporting what the
    command did — the new PR's URL, or `gh`'s failure text. Same write contract as
    `create_issue`: it reports the command's result, it never re-reads to prove the PR landed."""
    r = _gh(["pr", "create", "--repo", repo, "--head", head, "--title", title, "--body", body])
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
# Raw GraphQL, kept inside the seam like the check-rollup escape hatch above: `gh`'s porcelain
# has no notion of sub-issues or dependency edges. The map read asks a cheap counting question
# first and only pays for detail where maps exist — GitHub bills GraphQL on the page sizes a
# query *requests*, not on what comes back, so one fixed-size query charged a repository with no
# maps the same as one with three (#497). Per-map detail is still a single nested call rather
# than the five separate ones ADR 0036 budgets for. The handoff read is exactly the ADR's "one
# handoff/pipeline batch query": it asks only for each verified handoff's closing-PR numbers,
# aliased per issue number since there is no batch-by-number porcelain; full pipeline evidence
# (title, merge commit, review, CI) is then resolved from the PR listings the daemon already
# reads (:func:`list_pipeline_prs`), not re-fetched here.
#
# These reads are also the only ones that keep GitHub's own words when they fail (ADR 374): the
# projection publishes a per-repository reason an operator can act on, so "that repository does
# not exist" must not arrive as the same four words as "the hourly budget is gone".

_MAP_LABEL = "wayfinder:map"

# Discovery: how many active maps does this repository have? Selects no issue fields at all
# beyond the count, so it requests no child or edge pages and costs a single point.
_MAPS_DISCOVERY_QUERY = (
    "query($owner:String!,$name:String!,$label:[String!]){"
    "rateLimit{cost remaining}"
    "repository(owner:$owner,name:$name){"
    "issues(states:[OPEN],labels:$label,first:1){totalCount}"
    "}}")

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
    "blocking(first:$edgesFirst){totalCount nodes{"
    "id number title url body labels(first:20){nodes{name}} repository{nameWithOwner}"
    "}}"
    "}}"
    "}}"
    "}}")


def _handoff_candidate_row(node: dict, repo: str) -> HandoffCandidateRow:
    # Labels arrive as a connection here — this is the only read that asks for them that way.
    return HandoffCandidateRow(
        id=node["id"],
        number=node["number"], title=node.get("title", "") or "", url=node.get("url", "") or "",
        body=node.get("body", "") or "", labels=_connected_labels_of(node),
        repo=((node.get("repository") or {}).get("nameWithOwner")) or repo)


def _map_child_row(node: dict, repo: str) -> MapChildRow:
    blocked = node.get("blockedBy") or {}
    blocked_nodes = [n for n in (blocked.get("nodes") or []) if isinstance(n, dict)]
    open_count = sum(1 for n in blocked_nodes if (n.get("state") or "").upper() != "CLOSED")
    closed_count = len(blocked_nodes) - open_count
    blocking = node.get("blocking") or {}
    blocking_nodes = [n for n in (blocking.get("nodes") or [])
                      if isinstance(n, dict) and isinstance(n.get("number"), int) and n.get("id")]
    candidates = tuple(_handoff_candidate_row(n, repo) for n in blocking_nodes)
    return MapChildRow(
        number=node["number"], title=node.get("title", "") or "", url=node.get("url", "") or "",
        state=node.get("state", "") or "",
        assigned=bool((node.get("assignees") or {}).get("totalCount")),
        blocked_by_open=open_count, blocked_by_closed=closed_count,
        blocked_by_total=blocked.get("totalCount") if isinstance(blocked.get("totalCount"), int)
        else len(blocked_nodes),
        handoff_candidates=candidates,
        handoff_edges_total=blocking.get("totalCount")
        if isinstance(blocking.get("totalCount"), int) else len(blocking_nodes))


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


_READ_FAILED = "the map read failed"
_DIAGNOSTIC_CHARS = 300


def _diagnostic(messages: list[str]) -> str:
    """GitHub's own words about a failed map read, made safe to publish: whitespace collapsed to
    one line and bounded, since this text lands in the operator's snapshot."""
    text = " · ".join(" ".join(m.split()) for m in messages if m.strip())
    if len(text) > _DIAGNOSTIC_CHARS:
        text = text[:_DIAGNOSTIC_CHARS - 1].rstrip() + "…"
    return text or _READ_FAILED


def _map_query(args: list[str]) -> MapQueryRead:
    """The read path every Decision Map query shares. Unlike :func:`_read_json`, a failure here
    keeps GitHub's own explanation — `gh`'s stderr and any GraphQL ``errors[].message`` — rather
    than flattening everything to ``None`` (ADR 374). Deliberately narrow: the map projection is
    the only read with somewhere honest to publish a reason, and every other typed read keeps
    the fail-closed contract :func:`_read_json` owns."""
    r = _gh(["api", "graphql", *args])
    try:
        raw = json.loads(r.stdout or "")
    except json.JSONDecodeError:
        raw = None
    payload = raw.get("data") if isinstance(raw, dict) else None
    messages = [str(e.get("message")) for e in ((raw or {}).get("errors") or [])
                if isinstance(raw, dict) and isinstance(e, dict) and e.get("message")]
    if (r.stderr or "").strip():
        messages.append(r.stderr)
    rate = (payload or {}).get("rateLimit") or {}
    failed = r.returncode != 0 or not isinstance(payload, dict) or bool(messages)
    return MapQueryRead(
        raw=raw, payload=payload if isinstance(payload, dict) else None,
        cost=rate.get("cost") if isinstance(rate.get("cost"), int) else None,
        remaining=rate.get("remaining") if isinstance(rate.get("remaining"), int) else None,
        error=_diagnostic(messages) if failed else None)


def _summed(*costs: int | None) -> int | None:
    """What a multi-phase read spent, or ``None`` when GitHub reported nothing at all."""
    reported = [c for c in costs if c is not None]
    return sum(reported) if reported else None


def _handoff_numbers(numbers: list[int]) -> list[int]:
    """The issue numbers the closing-PR batch query actually asks about — deduplicated and
    ordered, so one alias exists per issue and the argv is stable run to run."""
    return sorted({n for n in numbers if isinstance(n, int)})


def _handoff_links_argv(repo: str, numbers: list[int]) -> list[str]:
    owner, _, name = repo.partition("/")
    fields = "".join(
        f'i{n}:issue(number:{n}){{closedByPullRequestsReferences'
        f'(first:5,includeClosedPrs:true){{totalCount nodes{{number url state mergedAt}}}}}}'
        for n in _handoff_numbers(numbers))
    query = (f"query($owner:String!,$name:String!){{rateLimit{{cost remaining}} "
             f"repository(owner:$owner,name:$name){{{fields}}}}}")
    return ["-f", f"query={query}", "-f", f"owner={owner}", "-f", f"name={name}"]


def decision_maps_with_evidence(
    repo: str, *, limit: int = 5, children_limit: int = 50, edges_limit: int = 10,
) -> tuple[MapsRead, tuple[MapQueryRead, ...]]:
    """The map read *and* every raw response it made along the way, for the read-only probe to
    print as evidence. :func:`decision_maps` is the front door every ordinary caller wants."""
    owner, _, name = repo.partition("/")
    identity = ["-f", f"owner={owner}", "-f", f"name={name}", "-f", f"label[]={_MAP_LABEL}"]

    discovery = _map_query(["-f", f"query={_MAPS_DISCOVERY_QUERY}", *identity])
    counted = (((discovery.payload or {}).get("repository") or {}).get("issues")
               or {}).get("totalCount")
    if discovery.error or not isinstance(counted, int):
        return MapsRead(maps=(), total_count=0, cost=discovery.cost,
                        remaining=discovery.remaining,
                        error=discovery.error or _READ_FAILED), (discovery,)
    if counted <= 0:
        return MapsRead(maps=(), total_count=0, cost=discovery.cost,
                        remaining=discovery.remaining), (discovery,)

    detail = _map_query(
        ["-f", f"query={_MAPS_QUERY}", *identity,
         "-F", f"mapsFirst={min(counted, limit)}", "-F", f"childrenFirst={children_limit}",
         "-F", f"edgesFirst={edges_limit}"])
    issues = ((detail.payload or {}).get("repository") or {}).get("issues") or {}
    nodes = issues.get("nodes")
    spent = _summed(discovery.cost, detail.cost)
    if detail.error or not isinstance(nodes, list):
        return MapsRead(maps=(), total_count=0, cost=spent, remaining=detail.remaining,
                        error=detail.error or _READ_FAILED), (discovery, detail)
    maps = tuple(_map_row(n, repo) for n in nodes if isinstance(n, dict))
    return MapsRead(
        maps=maps,
        total_count=issues.get("totalCount") if isinstance(issues.get("totalCount"), int)
        else len(maps),
        cost=spent, remaining=detail.remaining), (discovery, detail)


def decision_maps(repo: str, *, limit: int = 5, children_limit: int = 50,
                  edges_limit: int = 10) -> MapsRead:
    """The repository's active Decision Maps (open issues labeled ``wayfinder:map``), newest
    first, each with its bounded decision-child set and just enough dependency-graph data to
    classify the frontier and discover handoff candidates. A failed read comes back with no maps
    and an ``error`` carrying GitHub's own words; an enrolled repository with no maps comes back
    empty with no error, which is a different fact (ADR 0036).

    Asks how many maps the repository has before asking what is in them, and pays for detail
    only up to that count (#497) — a repository with none is completely answered by the count.
    Both phases are one read behind one front door: either the caller gets the whole answer
    with both phases' costs summed, or — if *either* call fails — nothing at all, since an
    empty map set published for a repository that has three would be a confident falsehood."""
    read, _evidence = decision_maps_with_evidence(
        repo, limit=limit, children_limit=children_limit, edges_limit=edges_limit)
    return read


def handoff_pr_links_response(repo: str, numbers: list[int]) -> MapQueryRead:
    """GitHub's own answer to the handoff closing-PR batch query, before any typing — the last
    of the map reads, exposed for the same probe evidence."""
    return _map_query(_handoff_links_argv(repo, numbers))


def handoff_pr_links_read(repo: str, numbers: list[int]) -> HandoffLinksRead:
    """The native closing-PR numbers for each verified handoff Build Issue in ``numbers``
    (ADR 0036's join key), together with this query's diagnostic and point accounting. An empty
    ``numbers`` makes no call and returns an empty successful read. Batched as one query with one
    alias per issue — there is no porcelain for "these N issues' closing PRs" in a single call."""
    wanted = _handoff_numbers(numbers)
    if not wanted:
        return HandoffLinksRead(links={}, cost=0, remaining=None)
    response = handoff_pr_links_response(repo, wanted)
    repo_node = (response.payload or {}).get("repository")
    if response.error or not isinstance(repo_node, dict):
        return HandoffLinksRead(links={}, cost=response.cost, remaining=response.remaining,
                                error=response.error or _READ_FAILED)
    out: dict[int, HandoffLinkRow] = {}
    for n in wanted:
        node = repo_node.get(f"i{n}") or {}
        refs = node.get("closedByPullRequestsReferences") or {}
        pr_nodes = [p for p in (refs.get("nodes") or [])
                    if isinstance(p, dict) and isinstance(p.get("number"), int)]
        attempts = tuple(
            HandoffAttemptRow(number=p["number"], url=p.get("url", "") or "",
                              state=p.get("state", "") or "", merged_at=p.get("mergedAt"))
            for p in pr_nodes)
        out[n] = HandoffLinkRow(
            number=n, attempts=attempts,
            attempt_count=refs.get("totalCount") if isinstance(refs.get("totalCount"), int)
            else len(attempts))
    return HandoffLinksRead(links=out, cost=response.cost, remaining=response.remaining)


# --- escape hatch --------------------------------------------------------------

def api(args: list[str], *, parse_json: bool = False) -> Any | None:
    """The one explicitly-marked escape hatch for GitHub calls that don't fit the typed
    surface. Four callers use it, and the census is enforced by test, not trust: the REST
    blocker read, the auth token, the combined add-and-remove label edit, and the parent-map
    GraphQL lookup. It runs ``gh <args>`` through the same fail-closed handling as every read:
    ``None`` means the command failed. Otherwise it returns the parsed JSON (when
    ``parse_json``) or the stripped stdout string. A caller reaching for this owns GitHub's
    shape for that one call — but nowhere else does anything shell out to `gh`."""
    if parse_json:
        return _read_json(args)
    r = _gh(args)
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip()
