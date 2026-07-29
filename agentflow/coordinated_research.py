"""The unattended research stage wired into the daemon (ADR 0037).

An open, unblocked, unclaimed ``wayfinder:research`` planning ticket is resolved by an unattended
agent session that runs as one bounded coordinated ``research`` stage through the *existing* session
coordinator — a sibling of the six pipeline stages, modeled on ``converse`` (ADR 0037). This module
is the daemon-side glue, mirroring :mod:`agentflow.coordinated_converse`:

- **submission mapping** — one eligible ticket → one ``research`` :class:`Submission` with identity
  ``(repository, ticket number, research)``, so re-discovery of the same ticket is idempotent.
- **stage collaborators** — the disposition-aware findings ``verify``, isolated-worktree ``prepare``,
  single-writer ``resolve`` (close an explicit decision or park a handoff for operator judgment),
  and exhaustion ``release`` (drop the claim so an incomplete ticket is eligible again).
  These are the production wiring the daemon injects into :class:`ResearchStageAdapter`.

The dispatched session writes only into its isolated worktree — a findings artifact. It never writes
the ticket, the map, GitHub, or coordinator state; only the daemon-side finalizer records the result
(ADR 0037). Finalization is idempotent across both closed and intentionally pending outcomes: a
crash-replay never posts a second findings comment or appends a second map line.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from agentflow import github
from agentflow.coordinator import Submission
from agentflow.labels import AWAITING_DISPOSITION, RESOLVING, release as release_claim
from agentflow.runner import _run
from agentflow.shell_crib import SHELL_CRIB
from agentflow.worktree_ref import WorktreeKind, WorktreeRef

# The findings comment marker (per-ticket, stable across attempts and restarts) and the visible
# disclaimer that fronts it, so a replay recognizes its own prior comment and never posts a second.
_RESEARCH_DISCLAIMER = "> *agentflow research — completed by an unattended session (AI).*"


def _findings_marker(number: int) -> str:
    return f"<!-- agentflow-research-findings:#{number} -->"


# The prompt an unattended research run executes. The run's isolated worktree is its only durable
# write path; it must land its answer at exactly this findings artifact, which is the outcome the
# stage adapter verifies and the finalizer resolves from (ADR 0037 outcome-first).
RESEARCH_PROMPT = """\
You are agentflow resolving one AFK-able research ticket for {repo}: #{n} — {title}

This is a `wayfinder:research` planning ticket — a bounded investigation an unattended session can
finish alone. Answer the question it poses: audit the repository and read the relevant code, docs,
and history as needed.

Do NOT open a pull request, push a branch, edit any GitHub issue or label, edit the decision map,
or change any durable project state — this is research, not a build. The daemon records your outcome
for you. It closes a ticket only for a durable no-build ruling or concrete defer; a handoff result
stays open for an operator to disposition.

The ticket:
---
{body}
---
{map_context}When you are done, write your findings — AND the decision they support — to this file,
creating parent directories as needed:

    {findings_path}

Writing that file is the sole durable outcome of this run. Keep it self-contained: state what you
investigated and what you found in plain prose the map's owner can read without re-deriving it. End
with exactly one top-level ``## Disposition`` section containing only one fenced ``json`` object in
one of these exact shapes:

    {{"disposition":"no_build","summary":"Why no implementation should be filed."}}
    {{"disposition":"deferred","summary":"What is deferred and why.",
     "trigger":"The named observable event that reopens the decision.",
     "verification":"How the operator will verify that the event occurred."}}
    {{"disposition":"handoff_required","summary":"Why operator disposition is required.",
     "candidates":[{{"title":"One independently shippable build",
                    "build":"The concrete behavior that build would deliver."}}]}}

For ``handoff_required``, list every independently shippable candidate separately; never combine
several builds into an umbrella candidate. For ``deferred``, name an observable trigger and a
distinct verification condition — “maybe later”, “when ready”, and similar placeholders are
invalid. The file becomes the ticket comment verbatim. Missing, malformed, multiple, or vague
dispositions are incomplete and continue within this run's recovery budget.
""" + SHELL_CRIB


# --- paths / artifacts ------------------------------------------------------------------

def research_worktree(workdir: str, pool: str, number: int) -> str:
    """The isolated worktree one research run reuses across attempts (resume context)."""
    return WorktreeRef.for_research(workdir, pool, number).path


def findings_path(record) -> str:
    """The findings artifact path inside the run's worktree. Per-*ticket* so a stray file left by
    another run on a reused worktree can never falsely complete this one."""
    return os.path.join(record.source or "", ".agentflow", f"research-findings-{record.subject}.md")


def read_findings(record) -> str | None:
    """The durable findings the session wrote for this ticket, or ``None`` if absent/empty."""
    try:
        text = Path(findings_path(record)).read_text().strip()
    except OSError:
        return None
    return text or None


@dataclass(frozen=True)
class ResearchDisposition:
    """The one machine-checkable ruling carried by a completed findings artifact."""

    kind: str
    summary: str
    trigger: str | None = None
    verification: str | None = None
    candidates: tuple[tuple[str, str], ...] = ()


_DISPOSITION_HEADING = re.compile(r"^##\s+Disposition\s*$", re.IGNORECASE | re.MULTILINE)
_DISPOSITION_JSON = re.compile(r"^```json\s*\n(?P<body>.+)\n```\s*$",
                               re.IGNORECASE | re.DOTALL)
_VAGUE = re.compile(
    r"^(?:maybe(?:\s+later)?|later|someday|tbd|unknown|when\s+(?:ready|needed|appropriate))"
    r"[.!]?$",
    re.IGNORECASE,
)
_NON_OBSERVABLE_CONDITION = re.compile(
    r"\b(?:maybe|later|someday|tbd|unknown|somehow|when\s+(?:ready|needed|appropriate)|"
    r"if\s+needed|priorities?\s+allow|time\s+permits|team\s+(?:decides|wants|feels)|"
    r"ask\s+the\s+team)\b",
    re.IGNORECASE,
)


def _specific_text(value) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if len(text) < 12 or _VAGUE.fullmatch(text):
        return None
    return text


def _observable_condition(value) -> str | None:
    text = _specific_text(value)
    if text is None or _NON_OBSERVABLE_CONDITION.search(text):
        return None
    return text


def parse_disposition(findings: str) -> ResearchDisposition | None:
    """Parse the artifact's single final structured disposition, failing closed on any drift."""
    headings = list(_DISPOSITION_HEADING.finditer(findings or ""))
    if len(headings) != 1:
        return None
    fenced = _DISPOSITION_JSON.fullmatch(findings[headings[0].end():].strip())
    if fenced is None:
        return None
    try:
        payload = json.loads(fenced.group("body"))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    kind = payload.get("disposition")
    summary = _specific_text(payload.get("summary"))
    if summary is None:
        return None
    if kind == "no_build":
        if set(payload) != {"disposition", "summary"}:
            return None
        return ResearchDisposition(kind=kind, summary=summary)
    if kind == "deferred":
        if set(payload) != {"disposition", "summary", "trigger", "verification"}:
            return None
        trigger = _observable_condition(payload.get("trigger"))
        verification = _observable_condition(payload.get("verification"))
        if trigger is None or verification is None or trigger.casefold() == verification.casefold():
            return None
        return ResearchDisposition(kind=kind, summary=summary, trigger=trigger,
                                   verification=verification)
    if kind != "handoff_required" or set(payload) != {
        "disposition", "summary", "candidates",
    }:
        return None
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return None
    candidates = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"title", "build"}:
            return None
        title = _specific_text(candidate.get("title"))
        build = _specific_text(candidate.get("build"))
        if title is None or build is None:
            return None
        candidates.append((title, build))
    return ResearchDisposition(kind=kind, summary=summary, candidates=tuple(candidates))


# --- submission mapping (pure over the ticket + supplied map context) --------------------

def research_submission(cfg, ticket: dict, tool: str, *, map_context: str = ""):
    """Map one eligible ticket and its chosen tool into a single ``research`` stage submission — the
    minimal facts the coordinator needs (ADR 0030). The stable identity is ``(repo, number,
    research)``, so repeated discovery returns the same record. ``map_context`` is the parent map
    excerpt dispatch resolves and injects (kept out of this mapping so it stays pure); the durable
    prompt reconstructs the exact same research job on a recovered attempt."""
    n = int(ticket["number"])
    source = research_worktree(cfg.workdir, tool, n)
    block = f"Its parent decision map, for context:\n---\n{map_context}\n---\n\n" if map_context else ""
    prompt = RESEARCH_PROMPT.format(
        repo=cfg.repo, n=n, title=ticket.get("title", ""), body=ticket.get("body") or "",
        map_context=block,
        findings_path=os.path.join(source, ".agentflow", f"research-findings-{n}.md"))
    return Submission(repo=cfg.repo, subject=str(n), stage="research", pool=tool, complexity="deep",
                      source=source, claim=True, input_ptr=prompt)


# --- stage collaborators (injected into ResearchStageAdapter) ---------------------------

def _findings_ready(record, obs) -> bool:
    """The Research outcome is a durable findings artifact for this ticket (ADR 0037 outcome-first),
    independent of provider exit: a bad exit that recorded findings completes; a clean exit that
    recorded nothing does not, and the run continues within budget."""
    findings = read_findings(record)
    return findings is not None and parse_disposition(findings) is not None


def _research_worktree_ready(record) -> bool:
    """Provision the run's isolated worktree before admission (ADR 0030): a detached checkout of
    ``origin/main`` the bounded session reads to investigate, and into which it writes its findings.

    An existing worktree is reused *exactly as it is* — a resumed run keeps the partial findings it
    already wrote — so it is never reset or cleaned. A research run owns no branch and pushes nothing,
    so the checkout is detached. Any git failure returns False, so admission is skipped with no permit
    and no attempt consumed — the run simply retries next cycle."""
    from agentflow.runner import _worktree_is_registered
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.kind is not WorktreeKind.RESEARCH or ref.tool != record.pool:
        return False
    workdir = ref.workdir
    wt = Path(ref.path)
    if wt.exists():
        return _worktree_is_registered(workdir, wt)  # reuse as-is; never rebuild a resumed run
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    return _run(["git", "-C", workdir, "worktree", "add", "--detach", str(wt),
                 "origin/main"]).returncode == 0


# --- parent map + 'Decisions so far' breadcrumb (pure helpers + one GitHub read) ---------

_DECISIONS_HEADING = re.compile(r"^#{1,6}\s+Decisions so far\s*$", re.IGNORECASE)
_AWAITING_HEADING = re.compile(r"^#{1,6}\s+Awaiting disposition\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,6}\s+")


def decision_line(title: str, number: int, disposition: ResearchDisposition) -> str:
    """One titled, explicit no-build or deferred ruling for the settled map ledger."""
    clean = " ".join((title or "").split()) or f"research ticket #{number}"
    if disposition.kind == "no_build":
        return f"- **{clean}** — no build: {disposition.summary} (#{number})."
    return (f"- **{clean}** — deferred: {disposition.summary} Trigger: {disposition.trigger} "
            f"Verification: {disposition.verification} (#{number}).")


def _map_section(map_body: str, heading: re.Pattern) -> tuple[int, int, list[str]] | None:
    lines = (map_body or "").splitlines()
    idx = next((i for i, ln in enumerate(lines) if heading.match(ln)), None)
    if idx is None:
        return None
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if _ANY_HEADING.match(lines[j]):
            end = j
            break
    return idx, end, lines[idx + 1:end]


def _decisions_section(map_body: str) -> tuple[int, int, list[str]] | None:
    """The map's 'Decisions so far' section, if present."""
    return _map_section(map_body, _DECISIONS_HEADING)


def _awaiting_section(map_body: str) -> tuple[int, int, list[str]] | None:
    """The map's 'Awaiting disposition' section, if present."""
    return _map_section(map_body, _AWAITING_HEADING)


def decision_present(map_body: str, number: int, expected: str | None = None) -> bool:
    """Whether the map's 'Decisions so far' already contains this ticket's own breadcrumb entry —
    the exact shape decision_line() writes — not any incidental #N cross-reference elsewhere in
    the section. The idempotency guard that keeps a crash-replay from appending a second line."""
    section = _decisions_section(map_body)
    if section is None:
        return False
    if expected is not None:
        return expected in section[2]
    return any(
        re.search(rf"— (?:no build:|deferred:).+\(#{number}\)\.$", ln)
        for ln in section[2]
    )


def _with_map_entry(map_body: str, heading: str, section, line: str) -> str:
    body = map_body or ""
    lines = body.splitlines()
    if section is None:
        prefix = [*lines, ""] if lines else []
        return "\n".join([*prefix, f"## {heading}", "", line]) + "\n"
    idx, end, _ = section
    insert_at = end
    while insert_at > idx + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # keep the new entry inside the section, above trailing blank lines
    return "\n".join([*lines[:insert_at], line, *lines[insert_at:]]) + "\n"


def with_decision(map_body: str, line: str) -> str:
    """Append ``line`` under the map's 'Decisions so far' idempotency section. Pure."""
    return _with_map_entry(map_body, "Decisions so far", _decisions_section(map_body), line)


def awaiting_disposition_line(title: str, number: int) -> str:
    clean = " ".join((title or "").split()) or f"research ticket #{number}"
    return f"- **{clean}** — awaiting operator disposition (#{number})."


def awaiting_disposition_present(map_body: str, number: int) -> bool:
    section = _awaiting_section(map_body)
    if section is None:
        return False
    return any(re.search(rf"awaiting operator disposition \(#{number}\)\.", line)
               for line in section[2])


def with_awaiting_disposition(map_body: str, line: str) -> str:
    """Append one pending research entry outside the settled decisions ledger. Pure."""
    return _with_map_entry(
        map_body, "Awaiting disposition", _awaiting_section(map_body), line)


def _parent_map(repo: str, number: int) -> tuple[int, str] | None:
    """The ``(map number, map body)`` of a research ticket's parent Decision Map — its native GitHub
    parent that carries the ``wayfinder:map`` label (ADR 0036/0037). ``None`` when the parent cannot
    be read or is not a map, so the finalizer fails closed rather than editing the wrong issue."""
    owner, _, name = repo.partition("/")
    query = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
             "{issue(number:$number){parent{number body labels(first:20){nodes{name}}}}}}")
    # A parent lookup is a GraphQL shape the typed surface doesn't cover, so it goes through the
    # module's escape hatch; None (an unreachable read) fails closed to the caller.
    data = github.api(["api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
                       "-f", f"name={name}", "-F", f"number={number}"], parse_json=True)
    if not isinstance(data, dict):
        return None
    issue = ((data.get("data") or {}).get("repository") or {}).get("issue")
    parent = issue.get("parent") if isinstance(issue, dict) else None
    if not isinstance(parent, dict) or not isinstance(parent.get("number"), int):
        return None
    labels = {n.get("name") for n in ((parent.get("labels") or {}).get("nodes") or [])
              if isinstance(n, dict)}
    if "wayfinder:map" not in labels:
        return None
    return parent["number"], parent.get("body") or ""


def research_map_context(repo: str, number: int, *, limit: int = 4000) -> str:
    """A bounded excerpt of the parent map body dispatch injects into the run's prompt, or ``""`` when
    no readable parent map exists. Context only — the authoritative breadcrumb is written by the
    finalizer, so an unreadable map here never blocks dispatch."""
    found = _parent_map(repo, number)
    if found is None:
        return ""
    body = found[1].strip()
    return body[:limit] if body else ""


def _append_map_decision(repo: str, number: int, title: str,
                         disposition: ResearchDisposition) -> bool:
    """Append this ticket's one titled line to the parent map's 'Decisions so far' idempotently.
    Returns whether the breadcrumb is durably present. A missing/unreadable parent map fails closed
    (retry), so resolution never retires without the breadcrumb the map's owner reconciles from."""
    found = _parent_map(repo, number)
    if found is None:
        return False
    map_number, map_body = found
    line = decision_line(title, number, disposition)
    if decision_present(map_body, number):
        return decision_present(map_body, number, line)
    new_body = with_decision(map_body, line)
    if not github.edit_body(repo, map_number, new_body):
        return False
    reread = _parent_map(repo, number)
    return reread is not None and decision_present(reread[1], number, line)


def _append_map_awaiting(repo: str, number: int, title: str) -> bool:
    """Record one visibly pending entry without making the map claim a settled decision."""
    found = _parent_map(repo, number)
    if found is None:
        return False
    map_number, map_body = found
    if awaiting_disposition_present(map_body, number):
        return True
    new_body = with_awaiting_disposition(
        map_body, awaiting_disposition_line(title, number))
    if not github.edit_body(repo, map_number, new_body):
        return False
    reread = _parent_map(repo, number)
    return reread is not None and awaiting_disposition_present(reread[1], number)


def _cleanup_worktree(record) -> None:
    ref = WorktreeRef.parse(record.source)
    if ref is not None:
        wt = Path(ref.path)
        if wt.exists():
            _run(["git", "-C", ref.workdir, "worktree", "remove", "--force", str(wt)])


def _findings_comment_present(comments, marker: str, findings: str) -> bool:
    return any(marker in comment.body and findings in comment.body for comment in comments)


def _await_disposition(repo: str, number: int) -> bool:
    """Durably mark completed research as waiting for operator judgment."""
    if not github.create_label(
        repo, AWAITING_DISPOSITION, "d4c5f9",
        "Completed research awaiting operator disposition",
    ):
        return False
    labels = github.issue_labels(repo, number)
    if labels is None:
        return False
    if AWAITING_DISPOSITION not in labels:
        if not github.add_label(repo, number, AWAITING_DISPOSITION):
            return False
    proved = github.issue_labels(repo, number)
    return proved is not None and AWAITING_DISPOSITION in proved


# --- resolution (the single daemon-side writer, ADR 0037) -------------------------------

def resolve(record) -> str | None:
    """Record one disposition as the stage's only GitHub writer (ADR 0037).

    Explicit no-build and deferred rulings close; handoff-required findings become an open pending
    ticket whose map entry, state label, released claim, and findings comment are all re-proved.
    Every write is idempotent. ``None`` withholds retirement until the durable route converges.
    Finished-run worktree cleanup is best-effort and never blocks proof.
    """
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    findings = read_findings(record)
    if findings is None:
        return None  # verify proved findings exist; if the artifact is gone, retry rather than retire
    disposition = parse_disposition(findings)
    if disposition is None:
        return None
    repo = record.repo
    # One whole-issue snapshot carries the idempotency facts together; an unreadable read retries.
    issue = github.issue_view(repo, number)
    if issue is None:
        return None
    marker = _findings_marker(number)
    marker_present = any(marker in comment.body for comment in issue.comments)
    if not marker_present:
        body = f"{_RESEARCH_DISCLAIMER}\n{marker}\n\n{findings}"
        if not github.comment(repo, number, body):
            return None
    elif not _findings_comment_present(issue.comments, marker, findings):
        return None
    if disposition.kind == "handoff_required":
        if not _append_map_awaiting(repo, number, issue.title):
            return None
        if not _await_disposition(repo, number):
            return None
        if not release_claim(repo, number, RESOLVING):
            return None
        final = github.issue_view(repo, number)
        if final is None:
            return None
        has_comment = _findings_comment_present(final.comments, marker, findings)
        found = _parent_map(repo, number)
        has_pending = found is not None and awaiting_disposition_present(found[1], number)
        if (final.state != "OPEN" or not has_comment or not has_pending
                or AWAITING_DISPOSITION not in final.labels or RESOLVING in final.labels):
            return None
        _cleanup_worktree(record)
        return final.url or f"https://github.com/{repo}/issues/{number}"
    if not _append_map_decision(repo, number, issue.title, disposition):
        return None
    if issue.state != "CLOSED":
        if not github.close(repo, number):
            return None
    if not release_claim(repo, number, RESOLVING):
        return None
    final = github.issue_view(repo, number)
    if final is None:
        return None
    has_comment = _findings_comment_present(final.comments, marker, findings)
    if final.state != "CLOSED" or not has_comment:
        return None
    # Resolution is durable — remove the isolated worktree so resolved runs don't accumulate.
    _cleanup_worktree(record)
    return final.url or f"https://github.com/{repo}/issues/{number}"


def release(record) -> str | None:
    """Release the shared ``wayfinder:resolving`` claim on exhaustion so the ticket is eligible again
    next cycle (ADR 0037). Idempotent and crash-safe: a repeat re-proves the same release. Returns the
    ticket URL as durable proof, or ``None`` to retry when the claim could not be proved released.

    The run's isolated worktree is intentionally kept on disk — a resumed attempt reuses it to pick up
    partial findings rather than starting from scratch."""
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    if not release_claim(record.repo, number, RESOLVING):
        return None
    return (github.issue_url(record.repo, number)
            or f"https://github.com/{record.repo}/issues/{number}")
