"""The unattended research stage wired into the daemon (ADR 0037).

An open, unblocked, unclaimed ``wayfinder:research`` planning ticket is resolved by an unattended
agent session that runs as one bounded coordinated ``research`` stage through the *existing* session
coordinator — a sibling of the six pipeline stages, modeled on ``converse`` (ADR 0037). This module
is the daemon-side glue, mirroring :mod:`agentflow.coordinated_converse`:

- **submission mapping** — one eligible ticket → one ``research`` :class:`Submission` with identity
  ``(repository, ticket number, research)``, so re-discovery of the same ticket is idempotent.
- **stage collaborators** — the disposition-aware findings ``verify``, isolated-worktree ``prepare``,
  single-writer ``resolve`` (close an explicit decision or park a handoff for operator judgment),
  and hold-time ``park`` (name what stopped the run and hand the ticket back).
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
from agentflow.coordinator.verification import PREPARED, unprepared
from agentflow.handoff import DurableHandoff, Notification, Subject
from agentflow.labels import (AWAITING_DISPOSITION, RESEARCH_PARKED, RESOLVING,
                              release as release_claim)
from agentflow.prompts import stage_prompt_spec
from agentflow.repo_facts import surface_declaration
from agentflow.runner import _run
from agentflow.shell_crib import SHELL_CRIB
from agentflow.worktree_ref import WorktreeKind, WorktreeRef, capture_subject_revision

# The findings comment marker (per-ticket, stable across attempts and restarts) and the visible
# disclaimer that fronts it, so a replay recognizes its own prior comment and never posts a second.
_RESEARCH_DISCLAIMER = "> *agentflow research — completed by an unattended session (AI).*"
# The park's own disclaimer and marker. A park is a different statement than a finding — the run
# produced no ruling — so it dedups on its own marker and never collides with a findings comment.
_PARK_DISCLAIMER = "> *agentflow research — parked by an unattended session (AI).*"


def _findings_marker(number: int) -> str:
    return f"<!-- agentflow-research-findings:#{number} -->"


def _park_marker(number: int) -> str:
    return f"<!-- agentflow-research-park:#{number} -->"


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
_GENERIC_DISPOSITION_WORDS = frozenset({
    "a", "an", "analysis", "answer", "any", "are", "because", "be", "been", "being",
    "broad", "build", "builds", "change", "changes", "code", "current", "currently",
    "decision", "defer", "deferred", "deferral", "direction", "disposition", "do", "does",
    "finding", "findings", "for", "general", "handoff", "handoffs", "implementation",
    "implementations", "implement", "is", "issue", "it", "justified", "later", "made",
    "must", "necessary", "need", "needed", "needs", "no", "not", "nothing", "now", "one",
    "operator", "outcome", "overall", "project", "reason", "required", "require", "requires",
    "research", "result", "should", "since", "so", "some", "that", "the", "thing", "this",
    "ticket", "to", "until", "warranted", "was", "work", "yet",
})
_GENERIC_CONDITION_WORDS = _GENERIC_DISPOSITION_WORDS | frozenset({
    "check", "condition", "confirm", "confirmed", "concrete", "event", "future", "happen",
    "happened", "happens", "meaningful", "named", "observable", "observe", "observed",
    "occur", "occurred", "occurs", "then", "trigger", "verification", "verify", "when",
})


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
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    return text if len(words - _GENERIC_CONDITION_WORDS) >= 3 else None


def _specific_summary(value) -> str | None:
    text = _specific_text(value)
    if text is None:
        return None
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    return text if len(words - _GENERIC_DISPOSITION_WORDS) >= 4 else None


def _specific_candidate(value) -> str | None:
    text = _specific_text(value)
    if text is None:
        return None
    words = set(re.findall(r"[a-z0-9]+", text.casefold()))
    return text if len(words - _GENERIC_DISPOSITION_WORDS) >= 2 else None


def _key_set_reason(kind: str, payload: dict, expected: set[str]) -> str:
    want = ", ".join(f"`{k}`" for k in sorted(expected))
    got = ", ".join(f"`{k}`" for k in sorted(payload)) or "no keys at all"
    return f"a `{kind}` ruling must carry exactly {want}, and this one carried {got}"


def _checked_disposition(findings: str) -> ResearchDisposition | str:
    """The artifact's single final structured disposition, or — when it has none — the one
    plain-language reason it was rejected.

    Every rejection in the contract names itself right here, at the check that fails, so the park
    comment an operator reads and the parser the daemon trusts can never disagree about why an
    artifact was refused. The checks and their order are the contract; adding a reason to each
    admits nothing new and refuses nothing extra."""
    headings = list(_DISPOSITION_HEADING.finditer(findings or ""))
    if len(headings) != 1:
        return ("the findings carried no `## Disposition` section" if not headings else
                f"the findings carried {len(headings)} `## Disposition` sections, "
                "and exactly one is required")
    fenced = _DISPOSITION_JSON.fullmatch(findings[headings[0].end():].strip())
    if fenced is None:
        return ("the `## Disposition` section was not exactly one fenced `json` block "
                "and nothing else")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate disposition field")
            result[key] = value
        return result

    try:
        payload = json.loads(fenced.group("body"), object_pairs_hook=unique_object)
    except (json.JSONDecodeError, TypeError):
        return "the fenced disposition block was not valid JSON"
    except ValueError:
        # unique_object's own refusal — a repeated field, which JSON itself would silently
        # collapse to the last one written.
        return "the fenced disposition block named the same field more than once"
    if not isinstance(payload, dict):
        return "the fenced disposition block was not a JSON object"
    kind = payload.get("disposition")
    summary = _specific_summary(payload.get("summary"))
    if summary is None:
        return ("the ruling's summary was missing, shorter than 12 characters, or said too "
                "little of its own — it must carry at least four words specific to this ticket")
    if kind == "no_build":
        if set(payload) != {"disposition", "summary"}:
            return _key_set_reason(kind, payload, {"disposition", "summary"})
        return ResearchDisposition(kind=kind, summary=summary)
    if kind == "deferred":
        expected = {"disposition", "summary", "trigger", "verification"}
        if set(payload) != expected:
            return _key_set_reason(kind, payload, expected)
        trigger = _observable_condition(payload.get("trigger"))
        verification = _observable_condition(payload.get("verification"))
        if trigger is None:
            return "the deferred ruling's trigger did not name an observable event"
        if verification is None:
            return "the deferred ruling's verification did not name an observable condition"
        if trigger.casefold() == verification.casefold():
            return ("the deferred ruling's trigger and verification were the same condition, so "
                    "nothing distinct would prove the trigger had occurred")
        return ResearchDisposition(kind=kind, summary=summary, trigger=trigger,
                                   verification=verification)
    if kind != "handoff_required":
        named = f"`{kind}`" if isinstance(kind, str) and kind else "nothing recognizable"
        return (f"the ruling named {named} — it must be one of `no_build`, `deferred`, or "
                "`handoff_required`")
    expected = {"disposition", "summary", "candidates"}
    if set(payload) != expected:
        return _key_set_reason(kind, payload, expected)
    raw_candidates = payload.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return "the handoff ruling listed no candidate builds"
    candidates = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict) or set(candidate) != {"title", "build"}:
            return "a handoff candidate was not an object carrying exactly `title` and `build`"
        title = _specific_candidate(candidate.get("title"))
        build = _specific_candidate(candidate.get("build"))
        if title is None or build is None:
            return ("a handoff candidate's title or build description was missing, shorter than "
                    "12 characters, or too generic to act on")
        candidates.append((title, build))
    return ResearchDisposition(kind=kind, summary=summary, candidates=tuple(candidates))


def parse_disposition(findings: str) -> ResearchDisposition | None:
    """Parse the artifact's single final structured disposition, failing closed on any drift."""
    checked = _checked_disposition(findings)
    return checked if isinstance(checked, ResearchDisposition) else None


def rejection_reason(findings: str | None) -> str | None:
    """Why this artifact carries no ruling the daemon can act on, in one plain sentence an operator
    can act on — or ``None`` when it does carry a usable one. An absent or empty artifact is its own
    reason: the run recorded nothing."""
    if not (findings or "").strip():
        return "the run recorded no findings at all"
    checked = _checked_disposition(findings)
    return checked if isinstance(checked, str) else None


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
    prompt = stage_prompt_spec("research").render(prompt=RESEARCH_PROMPT.format(
        repo=cfg.repo, n=n, title=ticket.get("title", ""), body=ticket.get("body") or "",
        map_context=block,
        findings_path=os.path.join(source, ".agentflow", f"research-findings-{n}.md")))
    return Submission(repo=cfg.repo, subject=str(n), stage="research", pool=tool, complexity="deep",
                      subject_revision=capture_subject_revision(cfg.workdir),
                      source=source, claim=True, input_ptr=prompt, capability_root=cfg.workdir,
                      capability_context={"ui": bool(surface_declaration(cfg.workdir).surfaces)})


# --- stage collaborators (injected into ResearchStageAdapter) ---------------------------

def _findings_ready(record, obs) -> bool:
    """The Research outcome is a durable findings artifact for this ticket (ADR 0037 outcome-first),
    independent of provider exit: a bad exit that recorded findings completes; a clean exit that
    recorded nothing does not, and the run continues within budget."""
    findings = read_findings(record)
    return findings is not None and parse_disposition(findings) is not None


def _research_worktree_ready(record):
    """Provision the run's isolated worktree before admission (ADR 0030): a detached checkout of
    ``origin/main`` the bounded session reads to investigate, and into which it writes its findings.

    An existing worktree is reused *exactly as it is* — a resumed run keeps the partial findings it
    already wrote — so it is never reset or cleaned. A research run owns no branch and pushes nothing,
    so the checkout is detached. Any git failure refuses by name (#405), so admission is skipped with
    no permit and no attempt consumed — the run simply retries next cycle, and the operator can see
    which step it is retrying."""
    from agentflow.runner import _worktree_is_registered
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.kind is not WorktreeKind.RESEARCH or ref.tool != record.pool:
        return unprepared("source-unreadable",
                          f"the run's checkout pointer does not parse as this pool's own "
                          f"research worktree: {record.source!r}")
    workdir = ref.workdir
    wt = Path(ref.path)
    if wt.exists():
        if not _worktree_is_registered(workdir, wt):  # reuse as-is; never rebuild a resumed run
            return unprepared("worktree-unregistered",
                              f"{wt} exists on disk but {workdir} does not list it as a worktree")
        return PREPARED
    wt.parent.mkdir(parents=True, exist_ok=True)
    fetch = _run(["git", "-C", workdir, "fetch", "origin", "--quiet"])
    if fetch.returncode != 0:
        return unprepared("fetch-failed",
                          f"`git -C {workdir} fetch origin` exited {fetch.returncode}")
    added = _run(["git", "-C", workdir, "worktree", "add", "--detach", str(wt),
                  record.subject_revision])
    if added.returncode != 0:
        return unprepared("worktree-add-failed",
                          f"`git worktree add --detach` at {wt} exited {added.returncode}")
    from agentflow.worktree_ownership import mark_worktree_owned
    mark_worktree_owned(wt, disposable=False)
    return PREPARED


# --- parent map + 'Decisions so far' breadcrumb (pure helpers + one GitHub read) ---------

_DECISIONS_HEADING = re.compile(r"^#{1,6}\s+Decisions so far\s*$", re.IGNORECASE)
_AWAITING_HEADING = re.compile(r"^#{1,6}\s+Awaiting disposition\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,6}\s+")
_AWAITING_ENTRY = re.compile(
    r"^\s*-\s+.*awaiting operator disposition \(#(?P<number>\d+)\)\.?\s*$",
    re.IGNORECASE,
)


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


def _is_awaiting_entry(line: str, number: int) -> bool:
    match = _AWAITING_ENTRY.fullmatch(line)
    return match is not None and int(match.group("number")) == number


def awaiting_disposition_present(map_body: str, number: int, expected: str) -> bool:
    """Whether the map carries exactly this ticket's one expected pending entry — a stale or
    duplicate pending line for the same ticket means *not* present, so the finalizer reconciles
    it rather than leaving the map with two answers for one child."""
    section = _awaiting_section(map_body)
    if section is None:
        return False
    return [line for line in section[2] if _is_awaiting_entry(line, number)] == [expected]


def with_awaiting_disposition(map_body: str, line: str, number: int) -> str:
    """Create or replace one pending entry outside the settled decisions ledger. Pure."""
    section = _awaiting_section(map_body)
    if section is not None:
        idx, end, _ = section
        lines = map_body.splitlines()
        lines = [
            existing for offset, existing in enumerate(lines)
            if not (idx < offset < end and _is_awaiting_entry(existing, number))
        ]
        map_body = "\n".join(lines)
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
    line = awaiting_disposition_line(title, number)
    if awaiting_disposition_present(map_body, number, line):
        return True
    new_body = with_awaiting_disposition(map_body, line, number)
    if not github.edit_body(repo, map_number, new_body):
        return False
    reread = _parent_map(repo, number)
    return reread is not None and awaiting_disposition_present(reread[1], number, line)


def _cleanup_worktree(record) -> None:
    ref = WorktreeRef.parse(record.source)
    if ref is not None:
        wt = Path(ref.path)
        if wt.exists():
            _run(["git", "-C", ref.workdir, "worktree", "remove", "--force", str(wt)])


def _findings_comment_present(comments, marker: str, findings: str) -> bool:
    return any(marker in comment.body and findings in comment.body for comment in comments)


# The two outcome states research leaves on a ticket it does not close, as GitHub shows them to a
# human looking at the issue. Both take the ticket out of unattended selection; they differ in what
# they say happened — one ruling is waiting to be chosen from, the other never arrived.
_STATE_LABELS = {
    AWAITING_DISPOSITION: ("d4c5f9", "Completed research awaiting operator disposition"),
    RESEARCH_PARKED: ("b60205", "Unattended research could not rule on this ticket — needs you"),
}


def _mark_state(repo: str, number: int, label: str) -> bool:
    """Durably stamp one research outcome state and re-read it as proof."""
    colour, description = _STATE_LABELS[label]
    if not github.create_label(repo, label, colour, description):
        return False
    labels = github.issue_labels(repo, number)
    if labels is None:
        return False
    if label not in labels:
        if not github.add_label(repo, number, label):
            return False
    proved = github.issue_labels(repo, number)
    return proved is not None and label in proved


def _await_disposition(repo: str, number: int) -> bool:
    """Durably mark completed research as waiting for operator judgment."""
    return _mark_state(repo, number, AWAITING_DISPOSITION)


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
    if marker_present and not _findings_comment_present(issue.comments, marker, findings):
        return None
    body = f"{_RESEARCH_DISCLAIMER}\n{marker}\n\n{findings}"
    if disposition.kind == "handoff_required":
        url = DurableHandoff().hand_off(
            Subject(repo=repo, number=number, kind="issue"),
            identity=record.identity, stage="research-findings",
            marker=marker,
            action=lambda: github.comment(repo, number, body),
            notification=Notification(
                "agentflow needs you",
                f"{repo} #{number}: Research findings await disposition"),
        )
        if url is None:
            return None
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
        pending_line = awaiting_disposition_line(final.title, number)
        has_pending = (
            found is not None
            and awaiting_disposition_present(found[1], number, pending_line)
        )
        if (final.state != "OPEN" or not has_comment or not has_pending
                or AWAITING_DISPOSITION not in final.labels or RESOLVING in final.labels):
            return None
        _cleanup_worktree(record)
        return final.url or url
    if not marker_present and not github.comment(repo, number, body):
        return None
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


# How a park names a permanent provider condition, keyed by *which* one fired. A provider that
# refuses a session and a spend ceiling that stops one need different remediations, so each gets its
# own diagnosis rather than re-authenticate advice for a healthy sign-in (issue #342).
_PROVIDER_PARK_COPY = {
    "access": ("refused the session outright — an expired sign-in, a billing or plan limit, or a "
               "permission problem. Re-authenticate the coding agent, or check its billing, plan, "
               "and permissions"),
    "rejected-request": ("rejected the request itself — too large for the model, an unrecognized "
                         "model, or a request it would not accept. The coding agent's sign-in is "
                         "fine; what it was asked to send is what needs a look"),
    "spend": ("stopped the run at its configured spending cap. The coding agent's sign-in is fine; "
              "raise or reset the cap for this work"),
    "unspecified": ("ended the session permanently without saying which condition it was. The "
                    "coding agent's health needs a look before it can run anything again"),
}


def _park_story(record, findings: str | None) -> tuple[str, str, str, str]:
    """What this park is: the sentence that opens the comment, the label on its reason line, the
    reason itself, and what the operator does next.

    The record's durable hold reason picks the story, exactly as Intake's hold picks its body
    (issues #328, #342). A run a permanent provider condition killed never read the question, so
    telling its operator the machine spent a budget failing to answer — and to go rewrite the
    question — would send them rewriting something no session ever saw. Only the story differs:
    the comment, the park label, the released claim, and the re-proof are identical for every
    reason, because the consequence is the same either way. Nothing here judges the question."""
    from agentflow.coordinator.coordinator import (PERMANENT_HOLD_REASON,
                                                   parse_permanent_hold_reason)
    hold_reason = record.hold_reason or ""
    if hold_reason.startswith(PERMANENT_HOLD_REASON):
        which = parse_permanent_hold_reason(hold_reason).value
        return ("An unattended research session could not get far enough to rule on this ticket, "
                "so the ticket is parked for you.",
                "Why there is no ruling",
                "the coding agent " + _PROVIDER_PARK_COPY.get(
                    which, _PROVIDER_PARK_COPY["unspecified"]),
                "Nothing here says anything about the question itself — the session never got to "
                "read it. Unattended research will not try this ticket again: once the coding "
                "agent is healthy, file a fresh research ticket for the same question, or answer "
                "it in a wayfinder session.")
    reason = rejection_reason(findings)
    if reason is None:
        # A parseable ruling belongs to resolve(), which the completed path runs before any hold.
        # Reaching the park with one means the artifact on disk changed after the run was verified
        # as incomplete, so the honest story is a ruling written but never recorded — never a
        # rejected check, which would name a check that did not fail.
        return ("An unattended research session wrote a ruling for this ticket but was held before "
                "the daemon could record it, so the ticket is parked for you.",
                "Why it was not recorded",
                "the run was held before the daemon could record the ruling it wrote",
                "The ruling it wrote is below, unrecorded — the decision map does not carry it. "
                "Unattended research will not try this ticket again: settle it in a wayfinder "
                "session, or file a fresh research ticket.")
    return ("An unattended research session ended without producing a ruling the daemon is allowed "
            "to record, so the ticket is parked for you.",
            "Why the ruling was refused", reason,
            "This says nothing about whether the question is a good one — only that the machine "
            "could not answer it in the shape the decision map requires. Unattended research will "
            "not try this ticket again. Rewrite the question so a bounded session can answer it, "
            "or answer it yourself in a wayfinder session.")


def _park_comment(number: int, story: tuple[str, str, str, str], findings: str | None) -> str:
    """The one comment a parked run leaves behind: what stopped it, why, what it did manage to
    record, and who owns the ticket now. It states a machine limit, never a judgment on the
    question — the daemon may not close, re-file, or re-word anything (ADR 0037)."""
    opening, reason_label, reason, next_step = story
    recorded = (f"\n\nWhat the run did record, so the work is not lost:\n\n---\n\n{findings}"
                if findings else "")
    return (
        f"{_PARK_DISCLAIMER}\n{_park_marker(number)}\n\n"
        f"{opening}\n\n"
        f"**{reason_label}:** {reason}.\n\n"
        f"{next_step}{recorded}"
    )


def park(record) -> str | None:
    """Hand a held research ticket back to the operator, visibly (ADR 362).

    This is the research stage's own operator-facing handoff, replacing the silent claim drop it
    used to do: one comment saying what stopped the run and why, carrying whatever the run did
    record, one durable park label that takes the ticket out of unattended selection, and the
    shared claim released. The ticket stays open and is never closed, re-filed, or judged.

    Every hold reaches here, not only exhaustion, so the comment's story comes from the record's
    durable hold reason (see :func:`_park_story`) — the ordinary case is a run that spent its
    budget without a ruling the contract accepts, which is total and repeatable, but a permanent
    provider condition parks the same ticket for an entirely different reason and must say so.

    Idempotent and crash-safe, exactly as ``resolve`` is: a repeat posts no second comment and
    re-proves the same park. ``None`` withholds proof until comment, label, and released claim can
    all be re-read as durable, so an interrupted park replays instead of half-parking."""
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    repo = record.repo
    findings = read_findings(record)
    story = _park_story(record, findings)
    marker = _park_marker(number)
    url = DurableHandoff().hand_off(
        Subject(repo=repo, number=number, kind="issue"),
        identity=record.identity, stage="research-park",
        marker=marker,
        action=lambda: github.comment(repo, number, _park_comment(number, story, findings)),
        notification=Notification(
            "agentflow needs you", f"{repo} #{number}: Research parked — {story[2]}"),
    )
    if url is None:
        return None
    if not _mark_state(repo, number, RESEARCH_PARKED):
        return None
    if not release_claim(repo, number, RESOLVING):
        return None
    final = github.issue_view(repo, number)
    if final is None:
        return None
    if (final.state != "OPEN"
            or not any(marker in comment.body for comment in final.comments)
            or RESEARCH_PARKED not in final.labels
            or RESOLVING in final.labels):
        return None
    # Nothing will ever resume this run, so the worktree it would have reused is just a leak.
    _cleanup_worktree(record)
    return final.url or url
