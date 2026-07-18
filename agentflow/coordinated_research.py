"""The unattended research stage wired into the daemon (ADR 0037).

An open, unblocked, unclaimed ``wayfinder:research`` planning ticket is resolved by an unattended
agent session that runs as one bounded coordinated ``research`` stage through the *existing* session
coordinator — a sibling of the six pipeline stages, modeled on ``converse`` (ADR 0037). This module
is the daemon-side glue, mirroring :mod:`agentflow.coordinated_converse`:

- **submission mapping** — one eligible ticket → one ``research`` :class:`Submission` with identity
  ``(repository, ticket number, research)``, so re-discovery of the same ticket is idempotent.
- **stage collaborators** — the findings-artifact ``verify``, the isolated-worktree ``prepare``, the
  single-writer ``resolve`` (post findings, close the ticket, append one map breadcrumb, release the
  shared claim), and the exhaustion ``release`` (drop the claim so the ticket is eligible again).
  These are the production wiring the daemon injects into :class:`ResearchStageAdapter`.

The dispatched session writes only into its isolated worktree — a findings artifact. It never writes
the ticket, the map, GitHub, or coordinator state; only the daemon-side finalizer resolves the ticket
(ADR 0037). Resolution is idempotent on the closed-ticket state: a crash-replay never posts a second
findings comment or appends a second map line.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from agentflow.coordinator import Submission

# The findings comment marker (per-ticket, stable across attempts and restarts) and the visible
# disclaimer that fronts it, so a replay recognizes its own prior comment and never posts a second.
_RESEARCH_DISCLAIMER = "> *agentflow research — resolved by an unattended session (AI).*"


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
for you: it posts your findings as a comment, closes the ticket, and leaves one line on the map.

The ticket:
---
{body}
---
{map_context}When you are done, write your findings — AND the decision they support — to this file,
creating parent directories as needed:

    {findings_path}

Writing that file is the sole durable outcome of this run. Keep it self-contained: state what you
investigated, what you found, and the concrete decision or recommendation it supports, in plain
prose the map's owner can read without re-deriving it. It becomes the ticket comment verbatim, so
write it as the answer, not as a note to yourself. If you exit without writing it, the run is
incomplete and will run again — never write it twice.
"""


# --- paths / artifacts ------------------------------------------------------------------

def research_worktree(workdir: str, pool: str, number: int) -> str:
    """The isolated worktree one research run reuses across attempts (resume context)."""
    return os.path.join(workdir, ".agentflow", "worktrees", pool, f"research-{number}")


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
    return read_findings(record) is not None


def _research_worktree_ready(record) -> bool:
    """Provision the run's isolated worktree before admission (ADR 0030): a detached checkout of
    ``origin/main`` the bounded session reads to investigate, and into which it writes its findings.

    An existing worktree is reused *exactly as it is* — a resumed run keeps the partial findings it
    already wrote — so it is never reset or cleaned. A research run owns no branch and pushes nothing,
    so the checkout is detached. Any git failure returns False, so admission is skipped with no permit
    and no attempt consumed — the run simply retries next cycle."""
    from agentflow.loop import _run
    from agentflow.runner import _worktree_is_registered
    src = record.source or ""
    if "/.agentflow/worktrees/" not in src:
        return False
    workdir, tail = src.split("/.agentflow/worktrees/", 1)
    if not tail.startswith(f"{record.pool}/research-"):
        return False
    wt = Path(src)
    if wt.exists():
        return _worktree_is_registered(workdir, wt)  # reuse as-is; never rebuild a resumed run
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    return _run(["git", "-C", workdir, "worktree", "add", "--detach", str(wt),
                 "origin/main"]).returncode == 0


# --- parent map + 'Decisions so far' breadcrumb (pure helpers + one GitHub read) ---------

_DECISIONS_HEADING = re.compile(r"^#{1,6}\s+Decisions so far\s*$", re.IGNORECASE)
_ANY_HEADING = re.compile(r"^#{1,6}\s+")


def decision_line(title: str, number: int) -> str:
    """One titled breadcrumb line for the map's 'Decisions so far' — the ticket's own title and a
    back-reference to the resolved ticket (GitHub auto-links ``#N`` in an issue body). The `#N`
    reference is the idempotency key, so a human can later curate the prose without risking a
    duplicate append."""
    clean = " ".join((title or "").split()) or f"research ticket #{number}"
    return f"- **{clean}** — resolved by unattended research (#{number})."


def _decisions_section(map_body: str) -> tuple[int, int, list[str]] | None:
    """The ('Decisions so far' heading index, section-end index, section lines) of a map body, or
    ``None`` when the section is absent. Section-end is the next heading or end of body."""
    lines = (map_body or "").splitlines()
    idx = next((i for i, ln in enumerate(lines) if _DECISIONS_HEADING.match(ln)), None)
    if idx is None:
        return None
    end = len(lines)
    for j in range(idx + 1, len(lines)):
        if _ANY_HEADING.match(lines[j]):
            end = j
            break
    return idx, end, lines[idx + 1:end]


def decision_present(map_body: str, number: int) -> bool:
    """Whether the map's 'Decisions so far' already contains this ticket's own breadcrumb entry —
    the exact shape decision_line() writes — not any incidental #N cross-reference elsewhere in
    the section. The idempotency guard that keeps a crash-replay from appending a second line."""
    section = _decisions_section(map_body)
    if section is None:
        return False
    return any(re.search(rf"resolved by unattended research \(#{number}\)\.", ln)
               for ln in section[2])


def with_decision(map_body: str, line: str) -> str:
    """Append ``line`` under the map's 'Decisions so far' — after the last existing entry, before any
    following heading. Creates the section at the end of the body when it is absent. Pure."""
    body = map_body or ""
    lines = body.splitlines()
    section = _decisions_section(body)
    if section is None:
        prefix = [*lines, ""] if lines else []
        return "\n".join([*prefix, "## Decisions so far", "", line]) + "\n"
    idx, end, _ = section
    insert_at = end
    while insert_at > idx + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1  # keep the new entry inside the section, above trailing blank lines
    return "\n".join([*lines[:insert_at], line, *lines[insert_at:]]) + "\n"


def _parent_map(repo: str, number: int) -> tuple[int, str] | None:
    """The ``(map number, map body)`` of a research ticket's parent Decision Map — its native GitHub
    parent that carries the ``wayfinder:map`` label (ADR 0036/0037). ``None`` when the parent cannot
    be read or is not a map, so the finalizer fails closed rather than editing the wrong issue."""
    from agentflow.loop import _run
    owner, _, name = repo.partition("/")
    query = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
             "{issue(number:$number){parent{number body labels(first:20){nodes{name}}}}}}")
    r = _run(["gh", "api", "graphql", "-f", f"query={query}", "-f", f"owner={owner}",
              "-f", f"name={name}", "-F", f"number={number}"])
    if r.returncode != 0:
        return None
    try:
        issue = (json.loads(r.stdout or "{}").get("data") or {}).get("repository", {}).get("issue")
    except (ValueError, AttributeError):
        return None
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


def _append_map_decision(repo: str, number: int, title: str) -> bool:
    """Append this ticket's one titled line to the parent map's 'Decisions so far' idempotently.
    Returns whether the breadcrumb is durably present. A missing/unreadable parent map fails closed
    (retry), so resolution never retires without the breadcrumb the map's owner reconciles from."""
    from agentflow.loop import _run
    found = _parent_map(repo, number)
    if found is None:
        return False
    map_number, map_body = found
    if decision_present(map_body, number):
        return True  # already recorded — idempotent
    new_body = with_decision(map_body, decision_line(title, number))
    edited = _run(["gh", "issue", "edit", str(map_number), "--repo", repo, "--body", new_body])
    if edited.returncode != 0:
        return False
    reread = _parent_map(repo, number)
    return reread is not None and decision_present(reread[1], number)


# --- resolution (the single daemon-side writer, ADR 0037) -------------------------------

def resolve(record) -> str | None:
    """Resolve the ticket in the stage finalizer — the *only* writer of the outcome (ADR 0037): post
    the findings comment, append the map breadcrumb, close the ticket, and release the shared claim.
    Every step is idempotent and ordered so a crash-replay never double-writes: the per-ticket comment
    marker gates the comment, the `#N` reference gates the map line, and a closed ticket / removed
    label are no-ops. Returns a durable proof (the ticket URL) once the ticket is closed with its
    findings comment, or ``None`` to retry next cycle rather than retiring over an incomplete
    resolution.

    On durable resolution the run's isolated worktree is removed so resolved runs do not accumulate
    on disk. Cleanup is best-effort and never blocks returning the proof."""
    from agentflow.loop import _release_resolving, _run
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    findings = read_findings(record)
    if findings is None:
        return None  # verify proved findings exist; if the artifact is gone, retry rather than retire
    repo = record.repo
    viewed = _run(["gh", "issue", "view", str(number), "--repo", repo,
                   "--json", "state,title,comments,url"])
    if viewed.returncode != 0:
        return None
    try:
        issue = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    marker = _findings_marker(number)
    if not any(marker in c.get("body", "") for c in issue.get("comments", [])):
        body = f"{_RESEARCH_DISCLAIMER}\n{marker}\n\n{findings}"
        if _run(["gh", "issue", "comment", str(number), "--repo", repo,
                 "--body", body]).returncode != 0:
            return None
    if not _append_map_decision(repo, number, issue.get("title", "")):
        return None
    if issue.get("state") != "CLOSED":
        if _run(["gh", "issue", "close", str(number), "--repo", repo]).returncode != 0:
            return None
    if not _release_resolving(repo, number):
        return None
    proved = _run(["gh", "issue", "view", str(number), "--repo", repo,
                   "--json", "state,comments,url"])
    if proved.returncode != 0:
        return None
    try:
        final = json.loads(proved.stdout or "{}")
    except json.JSONDecodeError:
        return None
    has_comment = any(marker in c.get("body", "") for c in final.get("comments", []))
    if final.get("state") != "CLOSED" or not has_comment:
        return None
    # Resolution is durable — remove the isolated worktree so resolved runs don't accumulate.
    src = record.source or ""
    if "/.agentflow/worktrees/" in src:
        workdir = src.split("/.agentflow/worktrees/", 1)[0]
        wt = Path(src)
        if wt.exists():
            _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    return final.get("url") or f"https://github.com/{repo}/issues/{number}"


def release(record) -> str | None:
    """Release the shared ``wayfinder:resolving`` claim on exhaustion so the ticket is eligible again
    next cycle (ADR 0037). Idempotent and crash-safe: a repeat re-proves the same release. Returns the
    ticket URL as durable proof, or ``None`` to retry when the claim could not be proved released.

    The run's isolated worktree is intentionally kept on disk — a resumed attempt reuses it to pick up
    partial findings rather than starting from scratch."""
    from agentflow.loop import _release_resolving, _run
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    if not _release_resolving(record.repo, number):
        return None
    viewed = _run(["gh", "issue", "view", str(number), "--repo", record.repo, "--json", "url"])
    if viewed.returncode == 0:
        try:
            url = json.loads(viewed.stdout or "{}").get("url")
        except json.JSONDecodeError:
            url = None
        if url:
            return url
    return f"https://github.com/{record.repo}/issues/{number}"
