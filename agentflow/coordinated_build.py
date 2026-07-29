"""The Build stage behind the durable session coordinator (issues #103–#108).

One ready issue becomes one durable ``build`` submission, which holds the change claim until a
completed Build transfers it to Review. This module is the daemon-side glue, mirroring its stage
siblings (:mod:`agentflow.coordinated_review`, :mod:`agentflow.coordinated_revise`,
:mod:`agentflow.coordinated_respond`, :mod:`agentflow.coordinated_mockup`):

- **submission mapping** — one ready issue → one ``build`` :class:`Submission` carrying the full
  build brief, plus the deliberate maintainer resume of a budget-exhausted held Build (#245).
- **stage collaborators** — the open-PR outcome check, the integration-collision report and the
  ``origin/main`` head it is measured against, and the exhaustion handoff. These are the
  production wiring :mod:`agentflow.pipeline` injects into :class:`BuildStageAdapter`; the
  branch/worktree preparation Build shares with Revise, Respond and Mockup is
  :mod:`agentflow.stage_worktree`.

The mappings are pure and exercised directly; the live GitHub reads are exercised through the
adapter seam (ADR 0020).
"""

from __future__ import annotations

from datetime import datetime

from agentflow import github, worktree_ref
from agentflow.intake import held_build_result
from agentflow.labels import BUILDING, complexity_from_labels, effort_from_labels
from agentflow.prompts import BUILD_PROMPT
from agentflow.repo_facts import surface_declaration, surfaces_phrase
from agentflow.runner import _run
from agentflow.worktree_ref import WorktreeRef, source_facts


def build_submission(cfg, issue: dict, tool: str):
    """Translate one ready issue and its chosen tool into a single Build stage submission — the
    minimal facts the coordinator needs (ADR 0030). The durable input pointer is the full build
    brief the provider session runs, so a recovered attempt rebuilds the same prompt. Pure: the
    issue→submission mapping is the test surface. Returns ``None`` when the issue lacks the
    complexity gate a build requires (ADR 0018), so a mis-labelled issue never becomes an
    attempt."""
    from agentflow.coordinator import Submission
    n = issue["number"]
    labels = [lbl["name"] for lbl in issue.get("labels", [])]
    complexity = complexity_from_labels(labels)
    if complexity is None:
        return None
    sl = worktree_ref.slug(issue["title"])
    brief = BUILD_PROMPT.format(
        repo=cfg.repo, n=n, title=issue.get("title", ""), body=issue.get("body") or "",
        effort=effort_from_labels(labels).value,
        surfaces=surfaces_phrase(surface_declaration(cfg.workdir)))
    return Submission(
        repo=cfg.repo, subject=str(n), stage="build", pool=tool,
        complexity=complexity.value, effort=effort_from_labels(labels).value,
        source=WorktreeRef.for_build(cfg.workdir, tool, n, sl).path, claim=True, input_ptr=brief)


def resume_if_held(submission, records):
    """Turn a deliberate maintainer `build <N>` into an explicit, durable resume when the issue's
    latest Build is a budget-exhausted ``held`` record (#245).

    A ``held`` Build is terminal but never retired, so its stable identity (``repo|issue|build|-``)
    stays live: an ordinary resubmission reuses it unwritten and no provider ever launches. When the
    latest Build for this issue is that held record, this bumps the submission to the next resume
    dimension, whose fresh identity opens a genuinely new bounded execution (a fresh
    ``ATTEMPT_BUDGET``) that still reuses the same issue, brief, builder lineage, and retained
    worktree ``source``. Otherwise the submission is returned unchanged, so an ordinary duplicate
    stays idempotent and a repeated resume — whose successor is already live — never opens a second
    concurrent Build. Pure: the resume decision is the test surface."""
    from dataclasses import replace
    from agentflow.coordinator.record import HELD

    builds = [r for r in records
              if r.repo == submission.repo and str(r.subject) == str(submission.subject)
              and r.stage == "build"]
    live = [r for r in builds if not r.retired]
    if not live:
        return submission                       # no live Build — an ordinary cold submission
    latest = max(live, key=lambda r: r.resume)
    if latest.state != HELD:
        return submission                       # a live or completed Build — nothing to resume
    # The next resume dimension is one past *every* Build ever opened for this issue — retired
    # successors included — so a resume can never collide with a prior successor's identity.
    next_resume = max(r.resume for r in builds) + 1
    # Reuse the held builder's pinned pool, retained worktree, and durable brief so the resume
    # *recovers* the same branch/worktree the stage adapter left on disk — and re-runs the same
    # build brief — rather than re-deriving a fresh path from a possibly re-picked tool (#245).
    return replace(submission, resume=next_resume, pool=latest.pool,
                   source=latest.source, builder_lineage=latest.builder_lineage,
                   input_ptr=latest.input_ptr)


def resume_in_flight(submission, records) -> bool:
    """True when a resume of this issue's Build is already live — a non-retired successor at a resume
    dimension past the original held record, still running or queued (#245). A repeated maintainer
    `build <N>` while that resume runs is correctly non-duplicating (``resume_if_held`` leaves it
    unchanged, so it idempotently reuses the terminal held record), but the caller should acknowledge
    the running resume rather than report the record as merely 'still held'. Pure."""
    from agentflow.coordinator.record import HELD

    return any(r.repo == submission.repo and str(r.subject) == str(submission.subject)
               and r.stage == "build" and not r.retired and r.resume >= 1 and r.state != HELD
               for r in records)


def _pr_exists(record) -> bool:
    """Whether the expected PR is open for the record's owned branch (the Build outcome)."""
    parsed = source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, _wt = parsed
    # A PR opened for this branch in *any* state (open/closed/merged) is the Build outcome, which
    # the typed open-only listing cannot express, so this goes through the module's escape hatch. An
    # unreadable read stays unknown — raise rather than mistake it for an absent PR (ADR 0040).
    data = github.api(["pr", "list", "--repo", record.repo, "--head", branch,
                       "--state", "all", "--json", "headRefName,url", "--limit", "1"],
                      parse_json=True)
    if not isinstance(data, list):
        raise RuntimeError(f"cannot verify Build PR outcome for {record.repo}:{branch}")
    return any(pr.get("headRefName") == branch for pr in data)


_COLLISION_MARK = "INTEGRATION-COLLISION"


def _main_head(record) -> str | None:
    """The current `origin/main` head SHA in the record's checkout, or None if unreadable. The
    coordinator compares it to the head a collision was recorded against to tell an identical
    retry (defer) from a main that has moved (one retry is warranted — issue #209)."""
    parsed = source_facts(record)
    if parsed is None:
        return None
    workdir, _branch, _wt = parsed
    _run(["git", "-C", workdir, "fetch", "--quiet", "origin", "main"])
    head = _run(["git", "-C", workdir, "rev-parse", "origin/main"])
    return head.stdout.strip() if head.returncode == 0 else None


def _integration_collision(record) -> str | None:
    """The `origin/main` head this Build reported an integration collision against this attempt, or
    None. The builder rebases onto `origin/main` before opening a PR and, on conflict, stops without
    resolving and posts a comment prefixed ``INTEGRATION-COLLISION`` (ADR 0009). A comment carrying
    that marker at its start and created after this attempt was admitted is the durable outcome; the
    recorded main head is what makes a subsequent identical retry detectable (issue #209)."""
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    comments = github.issue_comments(record.repo, number)
    if comments is None:
        return None
    if not any(_collision_comment(comment, record.started_at) for comment in comments):
        return None
    return _main_head(record)


def _collision_comment(comment: github.Comment, admitted_at: int) -> bool:
    """Whether one issue comment is this attempt's integration-collision report: its body starts
    with the ``INTEGRATION-COLLISION`` marker and it was created after the attempt was admitted, so
    a collision comment from an earlier attempt can never stand in for this one. A record from
    before admission times were stamped carries ``started_at == 0`` and keeps the unanchored
    behavior; an unparseable timestamp cannot be proven fresh and fails closed."""
    if not (comment.body or "").lstrip().startswith(_COLLISION_MARK):
        return False
    if not admitted_at:
        return True
    try:
        created = datetime.fromisoformat(
            (comment.created_at or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return created > admitted_at


def _hold_build(record) -> str | None:
    """Create and prove Build's exhaustion handoff without touching its worktree.

    The held-route comment is the durable marker, so the crash-safe post-once → prove →
    notify-once recipe is the shared :class:`~agentflow.handoff.DurableHandoff` envelope
    (ADR 0042): a daemon that dies between posting that comment and pinging the operator
    observes the same comment on restart and does not ping again — this handoff previously
    had no notification key at all and could double-ping. Releasing the visible building claim
    and proving the resulting held state are stage bookkeeping that run once the handoff
    confirms; an interrupted projection is finished on the way out so a partial write is never
    stranded.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject
    from agentflow.intake import apply_intake

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    status = ("could not rebase past a collision with newer changes on the main branch and stopped "
              "without resolving it" if record.hold_reason == "integration collision"
              else "continuation budget exhausted")
    result = held_build_result(status, f"the retained worktree `{record.source}`")

    def project() -> None:
        # Title+labels in one live read through the named escape hatch (ADR 0040); a read that
        # couldn't reach GitHub leaves the hold unprojected, so the envelope proves no marker
        # and retries next cycle rather than holding over an empty read.
        issue = github.api(["issue", "view", str(number), "--repo", record.repo,
                            "--json", "title,labels"], parse_json=True)
        if not isinstance(issue, dict):
            return
        apply_intake(record.repo, number, issue.get("title", ""),
                     [label.get("name", "") for label in issue.get("labels", [])], result)

    headline = ("Build hit an integration collision" if record.hold_reason == "integration collision"
                else "Build continuation budget exhausted")
    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="build-hold",
        marker=result.body.strip(),
        action=project,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: {headline}"))
    if url is None:
        return None
    github.remove_label(record.repo, number, BUILDING)
    labels = github.issue_labels(record.repo, number)
    if labels is None:
        return None
    if BUILDING in labels or "agentflow:needs-grilling" not in labels:
        project()   # the projection was interrupted after its comment landed — finish it
        return None
    return url
