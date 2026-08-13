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

from dataclasses import replace
from datetime import datetime
from pathlib import Path

from agentflow import github, worktree_ref
from agentflow.intake import held_build_result
from agentflow.labels import BUILDING, complexity_from_labels, effort_from_labels
from agentflow.prompts import stage_prompt_spec
from agentflow.pool_control import POOLS, pool_paused
from agentflow.repo_facts import surface_declaration, surfaces_phrase
from agentflow.routing import routing
from agentflow.runner import _run, codex_spent_at_render
from agentflow.worktree_ref import WorktreeRef, source_facts


def build_submission(cfg, issue: dict, *, parent_pool: str = "claude", floodgates: bool = False):
    """Translate one ready issue into a single session-led Build stage submission — the
    minimal facts the coordinator needs (ADR 0030). The durable input pointer is the full build
    brief the provider session runs, so a recovered attempt rebuilds the same prompt. Pure: the
    issue→submission mapping is the test surface. Returns ``None`` when the issue lacks the
    complexity gate a build requires (ADR 0018), so a mis-labelled issue never becomes an
    attempt. ``floodgates`` carries a by-hand dispatch's per-record floodgates override (ADR
    0025 amendment) onto the record, so a later admission recheck still honors it."""
    from agentflow.coordinator import Submission
    n = issue["number"]
    labels = [lbl["name"] for lbl in issue.get("labels", [])]
    complexity = complexity_from_labels(labels)
    if complexity is None:
        return None
    sl = worktree_ref.slug(issue["title"])
    effort = effort_from_labels(labels).value
    brief = stage_prompt_spec("build").render(
        repo=cfg.repo, n=n, title=issue.get("title", ""), body=issue.get("body") or "",
        effort=effort,
        surfaces=surfaces_phrase(surface_declaration(cfg.workdir)))
    brief += routing.session_lead_instructions(
        "build", effort, parent_provider=parent_pool, codex_spent=codex_spent_at_render(),
        unavailable_providers=frozenset(pool for pool in POOLS if pool_paused(pool)))
    return Submission(
        repo=cfg.repo, subject=str(n), stage="build", pool=parent_pool,
        complexity=complexity.value, effort=effort,
        source=WorktreeRef.for_build(cfg.workdir, parent_pool, n, sl).path, claim=True, input_ptr=brief,
        builder_lineage=parent_pool, branch_lineage=parent_pool, session_lead=True,
        floodgates=floodgates, capability_root=cfg.workdir,
        capability_context={"ui": bool(surface_declaration(cfg.workdir).surfaces)})


def resume_if_held(submission, records):
    """Turn a deliberate maintainer `build <N>` into an explicit, durable resume when the issue's
    latest Build is a budget-exhausted ``held`` record (#245).

    A ``held`` Build is terminal but never retired, so its stable identity (``repo|issue|build|-``)
    stays live: an ordinary resubmission reuses it unwritten and no provider ever launches. When the
    latest Build for this issue is that held record, this bumps the submission to the next resume
    dimension, whose fresh identity opens a genuinely new bounded execution (a fresh
    ``ATTEMPT_BUDGET``) that reuses the same issue and retained worktree ``source`` while adopting
    the current session-lead brief. That source is a path, not a promise the directory is still
    there: a held
    Build is no longer protected from reclamation, so a long-idle one may have been archived to a
    recovery ref and the resume then rebuilds the checkout from the branch tip (ADR 0050).
    Otherwise the submission is returned unchanged, so an ordinary duplicate
    stays idempotent and a repeated resume — whose successor is already live — never opens a second
    concurrent Build. Pure: the resume decision is the test surface."""
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
    # Reuse the retained branch/worktree the stage adapter left on disk, but launch the resumed
    # work under today's fixed Claude session lead and today's lead brief. A pre-#498 held Codex
    # build therefore keeps its checkout without reviving the retired single-agent dispatch.
    return replace(
        submission, resume=next_resume, source=latest.source,
        branch_lineage=latest.branch_lineage or latest.pool)


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
    # A PR opened for this branch in *any* state (open/closed/merged) is the Build outcome. An
    # unreadable read stays unknown — raise rather than mistake it for an absent PR (ADR 0040).
    prs = github.prs_for_branch(record.repo, branch, limit=1)
    if prs is None:
        raise RuntimeError(f"cannot verify Build PR outcome for {record.repo}:{branch}")
    return any(pr.head_ref_name == branch for pr in prs)


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


# What the held-build comment says stopped this build, chosen from the persisted hold reason so
# a restarted daemon recomposes the same words. Until #386 every non-collision hold read as
# "continuation budget exhausted" — true only for a build that really did run out of tries, and
# actively misleading for a build that never reached the work at all.
_EXHAUSTED_STATUS = "continuation budget exhausted"
_TURN_CAP_STATUS = ("used up its tries with the last coding session cut off at its per-stage turn "
                    "ceiling — it was stopped mid-work rather than running out of things to try")
_COLLISION_STATUS = ("could not rebase past a collision with newer changes on the main branch "
                     "and stopped without resolving it")
_ENVIRONMENT_STATUS = ("the machine couldn't give the coding agent a working command line — "
                       "usually too many leftover session checkouts in this repository — so it "
                       "never reached the work, and no attempt was spent")
# One phrase per kind of permanent provider condition, mirroring the diagnosis intake already
# gives (issue #342); a build held for one of these is not a build that ran out of tries.
_PERMANENT_STATUS = {
    "access": ("the coding agent's provider refused the session outright — an expired sign-in, "
               "a billing or plan limit, or a permission problem — so it never reached the work"),
    "rejected-request": ("the coding agent's provider rejected the request itself, so it never "
                         "reached the work"),
    "spend": ("the coding agent's provider stopped the run at its configured spending cap "
              "before it reached the work"),
    "unspecified": ("the coding agent's provider ended the session permanently without saying "
                    "which condition it was, so it never reached the work"),
}


def _marker_status(reason: str | None) -> str:
    """The status string this record's post-once marker is derived from — deliberately frozen.

    The marker is a hash of the record identity plus a status string, and a ``held`` record
    recomposes it on every later cycle. So the words a hold *displays* cannot be the words it
    keys on: changing them would make an issue that is already held look unheld and post a
    second comment on the next deploy. This returns exactly the two strings the marker has
    always been keyed on, for every reason, so no existing hold's marker moves — while
    ``_hold_status`` is free to say something truer. Pure (test surface)."""
    return _COLLISION_STATUS if reason == "integration collision" else _EXHAUSTED_STATUS


def _hold_status(reason: str | None) -> tuple[str, str]:
    """The maintainer-facing status phrase and notification headline for one persisted hold
    reason. A collision, an environment that couldn't carry a session, and each kind of
    permanent provider condition read as themselves; a budget spent on sessions the turn ceiling
    kept cutting off says so, because that is fixed by raising the ceiling and not by re-running
    the same build; every other reason — a genuinely spent budget, a replay that would have been
    identical, a completed stage with no successor — keeps the exhaustion wording it has always
    had. Pure (test surface)."""
    from agentflow.coordinator.coordinator import (PERMANENT_HOLD_REASON, ended_at_turn_cap,
                                                   parse_permanent_hold_reason)
    from agentflow.coordinator.providers import EndingReason

    if reason == "integration collision":
        return _COLLISION_STATUS, "Build hit an integration collision"
    if reason and reason.startswith(PERMANENT_HOLD_REASON):
        permanent = parse_permanent_hold_reason(reason)
        if permanent is EndingReason.ENVIRONMENT:
            return _ENVIRONMENT_STATUS, "Build never got a working session"
        return (_PERMANENT_STATUS.get(permanent.value, _PERMANENT_STATUS["unspecified"]),
                "Build's coding agent could not run")
    if ended_at_turn_cap(reason):
        return _TURN_CAP_STATUS, "Build was cut off at its turn ceiling"
    return _EXHAUSTED_STATUS, "Build continuation budget exhausted"


def _hold_build(record) -> str | None:
    """Create and prove Build's exhaustion handoff without touching its worktree.

    The crash-safe post-once → prove → notify recipe is the shared
    :class:`~agentflow.handoff.DurableHandoff` envelope (ADR 0042). The marker is a hidden tag
    derived from this record and a frozen name for why it stopped, carried at the end of the
    held-build comment: the comment's own text names only which of a few fixed stoppages
    happened, so a resumed build reusing the same retained worktree would compose the identical
    words and read as already handed off. What the comment *says* is chosen separately from what
    the marker keys on, so the wording can improve without un-holding an issue already held.

    Releasing the visible building claim and proving the resulting held state are stage
    bookkeeping that run once the handoff confirms. A projection interrupted after its comment
    landed is finished here — **once** — and the hold then ends either way: the alternative is a
    maintainer who reads the hold and re-queues the issue having their labels re-stamped on every
    later cycle, which is not a repair but a revert.
    """
    from agentflow.handoff import (DurableHandoff, Notification, Subject, marked_body,
                                   proof_marker)
    from agentflow.intake import apply_intake

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    status, headline = _hold_status(record.hold_reason)
    marker_status = _marker_status(record.hold_reason)
    # The worktree is retained for the human — but no longer forever: a held source that goes a
    # day untouched may be archived to a recovery ref and reclaimed (ADR 0050), so the comment
    # that sends a maintainer to that directory must also say how to find the work if it is gone.
    where = (f"the retained worktree `{record.source}` (if it has since been reclaimed, its "
             f"uncommitted work is on a recovery ref — `git for-each-ref "
             f"refs/agentflow/stranded/{Path(record.source or '').name}/`)")
    result = held_build_result(status, where)
    # A hold posted before this record carried its own marker is still proof of itself, so an
    # issue already held when the daemon deploys is never commented on twice. Both proofs are
    # composed from the *frozen* marker status, never the displayed one, so a hold that landed
    # under the old wording still recognizes itself. The marker goes *between* the disclaimer and
    # the ask rather than at the end, so that a marked comment does not itself contain the old
    # whole-body text and re-answer for a different record's hold.
    legacy_marker = held_build_result(marker_status, where).body.strip()
    marker = proof_marker(record.identity, marker_status, tag="build-hold")
    result = replace(result, body=marked_body(result.body, marker))

    def project() -> bool:
        # A read that couldn't reach GitHub leaves the hold unprojected, so the envelope proves
        # no marker and retries next cycle rather than holding over an empty read.
        live = github.issue_headline(record.repo, number)
        if live is None:
            return False
        apply_intake(record.repo, number, live.title, sorted(live.labels), result)
        return True

    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="build-hold",
        marker=marker,
        action=project,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: {headline}"),
        also_proven_by=legacy_marker)
    if url is None:
        return None
    labels = github.issue_labels(record.repo, number)
    if labels is None:
        return None
    if "agentflow:needs-grilling" not in labels and not project():
        return None   # GitHub was unreachable, so nothing was written — retry instead
    github.remove_label(record.repo, number, BUILDING)
    labels = github.issue_labels(record.repo, number)
    if labels is None or BUILDING in labels:
        return None   # the claim is still visible — retry the release, and only the release
    return url
