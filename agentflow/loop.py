"""The M0 loop — one ready-for-agent issue through the whole pipeline, serially.

Ephemeral hands, single issue (ADR 0011); the persistent daemon and real two-pool
balancing are M1. For M0 the pair is fixed — Claude builds, Codex reviews — so
cross-tool independence holds and swapping in the headroom balancer is the M1 change.
Every ready issue must carry an `agentflow:complexity:*` label (the hard gate,
ADR 0018) — intake stamps it; the loop reads it and skips an issue that has none.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agentflow import (coordinated_build, coordinated_review, coordinated_revise, github,
                       pipeline, ratchet)
from agentflow.balancer import pick_pair, pick_reviewer
from agentflow.coordinator.record import WAITING
from agentflow.coordinator.store import StoreUnavailable
from agentflow.gate import (conflict_revises_used, maintainer_comment, maintainer_comment_id,
                            park, reply_pending, review_resume_passes,
                            supersede_clean_review)
from agentflow.intake import (INTAKE_MARK, _strip_quoted_lines, awaiting_recheck,
                              replies_since_intake)
from agentflow.labels import (AWAITING_DISPOSITION, BUILDING, DRAWING, HELD_LABELS,
                              RESEARCH_PARKED, RESEARCH_TICKET, RESOLVING, TRIAGE_SKIP, TRIAGING,
                              WAYFINDER_NON_RESEARCH, claim)
from agentflow.notify import notify
from agentflow.prompts import CONFLICT_REASON
from agentflow.repo_facts import intake_allowlist, repo_profile
from agentflow.review_policy import ReviewState
from agentflow.reviewer import Verdict
from agentflow.runner import (_commit_is_on_origin, _run, _worktree_is_disposable,
                              _worktree_is_registered, remove_worktree_if_safe, resettable_head,
                              retain_stranded_commit, worktree_session)
from agentflow.worktree_ref import BUILD_BRANCH_RE, WorktreeRef, issue_of_branch


@dataclass(frozen=True)
class RepoConfig:
    repo: str        # "owner/name" on GitHub
    workdir: str     # local main checkout


def _row_dict(row: github.IssueRow) -> dict:
    """Adapt a typed issue row back to the raw mapping the coordinator's build/mockup submission
    builders and the pure dispatch predicates still read (they consume GitHub's own
    `labels[].name` shape). The discovery listings return typed rows now; this is the single
    bridge to the not-yet-migrated consumers, so the wire shape is reassembled once here rather
    than at every call site."""
    return {"number": row.number, "title": row.title, "body": row.body,
            # Sorted only so the shape a caller sees is stable from run to run — a typed row holds
            # its labels as a set. Nothing may depend on the order: a duplicate dial is settled by
            # rank in the decoders themselves (:mod:`agentflow.labels`), not by who comes first.
            "labels": [{"name": name} for name in sorted(row.labels)]}


def _issues_in_flight(cfg: RepoConfig) -> set[int] | None:
    """Issues that already have an OPEN agentflow PR — an agent is on them, so don't
    re-dispatch a duplicate. Dispatch dedup, distinct from ADR 0009's merge-time floor:
    an issue stays `ready-for-agent` while its PR is in review, so without this the loop
    would re-build it every cycle (a second PR on a different tool).

    Returns None when the listing itself failed — unknown is NOT empty. Treating a `gh`
    blip as "nothing in flight" would re-dispatch every in-review issue; callers fail closed
    (skip, retry next cycle)."""
    prs = github.list_open_prs(cfg.repo, limit=100)
    if prs is None:
        return None
    in_flight: set[int] = set()
    for pr in prs:
        # A PR's declared closing-issue reference marks that issue in-flight regardless of
        # how its head branch is named — a hand-driven build on an off-convention branch
        # (e.g. `codex/40-foo`) still dedups. The reference is same-repo scoped, so it can't
        # be fooled by a `Closes #N` meaning another repo.
        in_flight.update(pr.closing_issues)
        # Fallback: recognize the conventional branch even if no closing reference is declared.
        if (n := issue_of_branch(pr.head_ref_name)) is not None:
            in_flight.add(n)
    return in_flight


_BLOCKED_BY_RE = re.compile(r"^Blocked by #(\d+)\s*$", re.MULTILINE)


def _native_blockers(cfg: RepoConfig, number: int) -> set[int] | None:
    """Same-repo issue numbers from an issue's native GitHub `blocked_by` edges.

    A second recognized source of blockers alongside `Blocked by #N` body prose (ADR 0024):
    planning tools express dependencies as native GitHub relationships. Returns None when the
    edge set cannot be read (a `gh` failure or malformed response) so the caller can fail
    closed — an unreadable dependency graph is never mistaken for "no blockers". Cross-repo
    edges are ignored: only blockers in `cfg.repo` join the same-repo gate."""
    # A REST dependency read — one of the escape hatch's named uses (ADR 0040).
    edges = github.api(["api", f"repos/{cfg.repo}/issues/{number}/dependencies/blocked_by"],
                       parse_json=True)
    if not isinstance(edges, list):
        return None
    numbers: set[int] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            return None
        if (edge.get("repository") or {}).get("full_name") == cfg.repo:
            numbers.add(int(edge["number"]))
    return numbers


def _free_to_dispatch(cfg: RepoConfig, issue: dict, in_flight: set[int], _log=None) -> bool:
    """A ready issue is free only if no agent owns it and every declared blocker is closed.

    The blocker set is the union of the issue's native GitHub `blocked_by` edges and its
    `Blocked by #N` body prose (ADR 0024). Blocker state is read from the same repository on
    every selection pass. Unknown is not closed: a missing issue, `gh` failure, or malformed
    response — from either source — skips the dependent until a later pass can verify it
    safely. Shared by queue selection and by-hand builds."""
    if (issue["number"] in in_flight
            or BUILDING in {lbl["name"] for lbl in issue.get("labels", [])}):
        return False

    native = _native_blockers(cfg, issue["number"])
    if native is None:
        if _log:
            _log(f"{cfg.repo}: #{issue['number']}: skipped — native blocked-by edges "
                 "could not be determined")
        return False

    prose = (int(n) for n in _BLOCKED_BY_RE.findall(issue.get("body") or ""))
    blockers = dict.fromkeys([*prose, *sorted(native)])
    for blocker in blockers:
        state = github.issue_state(cfg.repo, blocker)
        if state != "CLOSED":
            if _log:
                reason = f"open blocker #{blocker}" if state == "OPEN" \
                    else f"blocker #{blocker} whose state could not be determined"
                _log(f"{cfg.repo}: #{issue['number']}: skipped — {reason}")
            return False
    return True


def _next_ready_issue(cfg: RepoConfig, reserved: set[int] = frozenset(),
                      _log=None) -> dict | None:
    """The oldest ready issue free to dispatch. `reserved` skips candidates this pass has
    already found conclusively undispatchable, so one bad queue head cannot starve later
    runnable work (#327)."""
    rows = github.list_issues(cfg.repo, label="ready-for-agent", limit=50)
    if rows is None:
        return None
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return None   # can't see what's in flight — fail closed, dispatch next cycle
    issues = sorted((_row_dict(r) for r in rows), key=lambda i: i["number"])
    return next((i for i in issues if i["number"] not in reserved
                 and _free_to_dispatch(cfg, i, in_flight, _log)), None)


def _untriaged(issue: dict) -> bool:
    """An issue is in the intake queue only if it is not a wayfinder planning artifact and
    nothing has resolved or claimed it — none of intake's state labels and no
    `agentflow:triaging` claim (set before the grounding session, closing intake's
    no-label-yet window, symmetric to `_free_to_dispatch`). Pure (test surface)."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if any(label.startswith("wayfinder:") for label in labels):
        return False
    return not (labels & TRIAGE_SKIP)


def _next_untriaged_issue(cfg: RepoConfig, reserved: set[int] = frozenset()) -> dict | None:
    """The oldest open issue in the intake queue — excluding wayfinder planning artifacts,
    with none of intake's state labels and unclaimed by a live grounding session (ADR 0016).
    `reserved` skips issues a concurrent triage fan-out already claimed this cycle, before
    their `agentflow:triaging` label is visible."""
    rows = github.list_issues(cfg.repo, limit=50)
    if rows is None:
        return None
    untriaged = [i for i in (_row_dict(r) for r in rows)
                 if _untriaged(i) and i["number"] not in reserved]
    return min(untriaged, key=lambda i: i["number"]) if untriaged else None


def build_issue(cfg: RepoConfig, n: int, *, floodgates: bool = False) -> str:
    """By-hand build of a *specific* ready issue (ADR 0022's `build <N>`). Fetches issue N,
    **refuses and redirects** anything that isn't `ready-for-agent` (a held issue → `pickup`;
    an un-triaged one → `triage`/`scope`), refuses one already claimed or in flight, then
    submits the same durable Build record as the daemon. Provider launch, review, continuation,
    and permits remain behind the coordinator. ``floodgates=True`` passes an operator's
    per-dispatch floodgates override (ADR 0025 amendment) through to both the pool pick and the
    submitted record, so a later admission recheck still honors it."""
    # By-hand, one issue at a time: the whole-issue read is affordable here (the queue pass uses
    # the lean discovery listing), and it answers the state and the build fields together.
    view = github.issue_view(cfg.repo, n)
    if view is None:
        return f"#{n}: not found in {cfg.repo}"
    if view.state != "OPEN":
        return f"#{n}: closed — nothing to build"
    issue = _row_dict(github.IssueRow(number=n, title=view.title, body=view.body,
                                      labels=view.labels))
    labels = view.labels
    if "ready-for-agent" not in labels:
        held = labels & HELD_LABELS
        if held:
            return f"#{n}: held — resume it with `/agentflow pickup {n}`, not build"
        return f"#{n}: not ready — run `/agentflow triage {n}` (or `scope {n}`) first"
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return f"#{n}: can't see what's in flight (gh error) — refusing to risk a duplicate; retry"
    if not _free_to_dispatch(cfg, issue, in_flight):
        return f"#{n}: not dispatchable — already claimed, in flight, or waiting on a blocker"
    builder, _reviewer, block_msg = pick_pair(operator=True, floodgates=floodgates)
    if builder is None:
        return f"#{n}: no pool has headroom ({block_msg}) — deferring"
    submission = coordinated_build.build_submission(cfg, issue, builder.tool, floodgates=floodgates)
    if submission is None:
        return f"#{n}: skipped — no agentflow:complexity:* label (ADR 0018 hard gate)"
    # A `build <N>` on an issue whose latest Build exhausted its budget and `held` is the explicit,
    # durable maintainer resume (#245): open a fresh bounded execution at the next resume identity
    # instead of silently reusing the terminal held record.
    records = pipeline.tracer.load_records()
    resumed = coordinated_build.resume_if_held(submission, records)
    coordinator = pipeline.build_coordinator()
    identity = coordinator.submit_stage(resumed)
    record = coordinator.stage_record(identity)
    # Claim the issue and report a launch only when admission actually produced runnable work. An
    # ordinary resubmission that reused a terminal (held/completed) record leaves nothing to run —
    # never stamp `agentflow:building` on it or claim success; redirect the maintainer to `pickup`.
    if record is None or record.state != WAITING or record.hold_pending or record.retired:
        if coordinated_build.resume_in_flight(resumed, records):
            return f"#{n}: a resume is already running — let it finish or reply what's missing"
        return f"#{n}: still held — resume it with `/agentflow pickup {n}`, reply what's missing"
    if not claim(cfg.repo, n, BUILDING):
        # We submitted a runnable record but never established the GitHub claim: withdraw it so no
        # orphaned WAITING build is left for a later cycle to launch unguarded (#245).
        coordinator.withdraw_stage(identity)
        return f"#{n}: could not claim Build — withdrew the coordinator submission"
    pipeline.reconcile_and_project(coordinator)
    verb = "resumed" if resumed.resume else "submitted"
    return f"#{n}: {verb} to coordinator → {resumed.pool} (build)"


# --- research stage: dispatch an unattended session for an AFK-able planning ticket (ADR 0037) ---
# Build intake still walls out the whole `wayfinder:*` namespace (`_untriaged`); this is the *only*
# path that lets the daemon see and run a planning ticket, and it may run `wayfinder:research` and
# nothing else. The shared `wayfinder:resolving` label is the claim — set before the session, so a
# human session and the daemon never both grab the same ticket.

def _research_eligible(issue: dict) -> bool:
    """Pure (test surface). A ticket is dispatchable research only if it carries the
    `wayfinder:research` type label, is not already claimed by `wayfinder:resolving`, has not already
    been settled by research (awaiting disposition, or parked because an unattended run could not
    rule on it — ADR 362), and carries no other `wayfinder:*` type label the daemon must never run
    (ADR 0037). Its blocker state is verified separately against the live dependency graph."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if (RESEARCH_TICKET not in labels or RESOLVING in labels
            or AWAITING_DISPOSITION in labels or RESEARCH_PARKED in labels):
        return False
    return not (labels & WAYFINDER_NON_RESEARCH)


def _research_unblocked(cfg: RepoConfig, number: int, _log=None) -> bool:
    """Whether every native `blockedBy` edge of a research ticket is closed (ADR 0037). Unreadable
    edges (`_native_blockers` returns None) or a blocker whose state cannot be read fail closed — an
    unknown dependency graph is never mistaken for 'unblocked', the same discipline the build queue
    uses."""
    native = _native_blockers(cfg, number)
    if native is None:
        if _log:
            _log(f"{cfg.repo}: #{number}: skipped research — native blocked-by edges "
                 "could not be determined")
        return False
    for blocker in sorted(native):
        state = github.issue_state(cfg.repo, blocker)
        if state != "CLOSED":
            if _log:
                _log(f"{cfg.repo}: #{number}: skipped research — blocker #{blocker} not closed")
            return False
    return True


def _next_research_ticket(cfg: RepoConfig, _log=None) -> dict | None:
    """The oldest open, unblocked, unclaimed `wayfinder:research` ticket the daemon may resolve
    unattended (ADR 0037). Selection is by the research type label, so no other `wayfinder:*` ticket
    is ever admitted; eligibility and blocker state are re-read on every pass and fail closed. None
    on a `gh` blip (retry next cycle)."""
    rows = github.list_issues(cfg.repo, label=RESEARCH_TICKET, limit=50)
    if rows is None:
        return None
    for issue in sorted((_row_dict(r) for r in rows), key=lambda i: i["number"]):
        if _research_eligible(issue) and _research_unblocked(cfg, issue["number"], _log):
            return issue
    return None


def _next_resumable_issue(cfg: RepoConfig,
                          reserved: set[int] = frozenset()) -> tuple[dict, str] | None:
    """A `needs-grilling` or `needs-mockup` issue whose latest comment is the maintainer's
    reply — return it with their answer text so intake can resolve it (ADR 0019). A waiver
    reply on a mockup-held issue ("skip the mockup, build it") promotes to ready; a locked-spec
    reply ("here is the visual spec") does the same. Use `/agentflow pickup <N>` to drive
    either interactively instead."""
    issues: list[dict] = []
    for label in ("agentflow:needs-grilling", "agentflow:needs-mockup"):
        rows = github.list_issues(cfg.repo, label=label, limit=50)
        if rows is None:
            return None
        issues.extend(_row_dict(r) for r in rows)
    seen: set[int] = set()
    deduped = []
    for issue in sorted(issues, key=lambda i: i["number"]):
        if issue["number"] not in seen:
            seen.add(issue["number"])
            deduped.append(issue)
    for issue in deduped:
        if issue["number"] in reserved:
            continue
        claims = {lbl["name"] for lbl in issue["labels"]}
        if TRIAGING in claims or DRAWING in claims:
            continue   # Intake or the current Mockup round already owns this held issue
        comments = github.issue_comment_rows(cfg.repo, issue["number"])
        if comments is None:
            continue
        allowlist = intake_allowlist(cfg.repo, cfg.workdir)
        if awaiting_recheck(comments, allowlist):
            qualifying = [c for c in comments
                          if c.get("author", {}).get("login", "") in allowlist
                          and INTAKE_MARK not in _strip_quoted_lines(c.get("body", ""))]
            if qualifying:
                latest = qualifying[-1]
                issue["_intake_target"] = str(latest.get("id") or latest.get("createdAt") or "")
            return issue, replies_since_intake(comments, allowlist)
    return None


def _next_intake_candidate(cfg: RepoConfig,
                           reserved: set[int] = frozenset()) -> tuple[dict, str] | None:
    """The next issue to triage — a held issue the maintainer just answered (resume, ADR
    0019) or the oldest un-triaged one (ADR 0016) — with its resume text. Skips issues a
    concurrent fan-out already claimed this cycle (`reserved`). None when the queue is empty."""
    resumable = _next_resumable_issue(cfg, reserved)
    if resumable:
        return resumable
    issue = _next_untriaged_issue(cfg, reserved)
    return (issue, "") if issue else None


def _next_pr_awaiting_reply(cfg: RepoConfig) -> tuple[int, str, str, str, str] | None:
    """The next open agentflow PR with an unanswered maintainer comment.

    Returns ``(pr_number, head_branch, comment, comment_target, baseline_head)`` for the oldest
    unanswered target. A target-aware reply removes only that comment, so later comments become
    fresh Respond stages instead of being collapsed into one run. Generic legacy markers retain
    their old run-level meaning, and the responder never wakes on its own comments (#18/#107)."""
    prs = github.list_open_prs(cfg.repo, limit=100)
    if prs is None:
        return None
    for pr in sorted(prs, key=lambda p: p.number):
        branch = pr.head_ref_name
        baseline = pr.head_ref_oid
        if issue_of_branch(branch) is None or not baseline:
            continue   # not an agentflow PR — a human's own branch
        comments = github.pr_comment_rows(cfg.repo, pr.number)
        if comments is None:
            continue
        if reply_pending(comments):
            return (pr.number, branch, maintainer_comment(comments),
                    maintainer_comment_id(comments), baseline)
    return None


def _checkout_pr_branch(cfg: RepoConfig, branch: str, wt: Path) -> bool:
    """Put the PR branch in a worktree so a responder can push fixes to it. Reuses the
    builder's worktree when it's still there (freshened to the PR head), else cuts a fresh
    one tracking `origin/<branch>`. Returns success. Live orchestration, not unit-tested.

    Reuse is gated on what the freshening actually overwrites: tracked content. A build session
    routinely leaves untracked files behind (a screenshot config, a draft PR body), and a reset
    leaves those untouched — so they must not veto the reuse. A commit that never reached the
    remote is anchored under a recovery ref before the reset, exactly as a review checkout's is."""
    if _run(["git", "-C", cfg.workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    if wt.exists():
        head = resettable_head(cfg.workdir, wt)
        if not head:
            return False
        if not _commit_is_on_origin(cfg.workdir, head) \
                and not retain_stranded_commit(cfg.workdir, wt, head):
            return False
        return _run(["git", "-C", str(wt), "reset", "--hard", f"origin/{branch}"]).returncode == 0
    wt.parent.mkdir(parents=True, exist_ok=True)
    return _run(["git", "-C", cfg.workdir, "worktree", "add", "-B", branch,
                 str(wt), f"origin/{branch}"]).returncode == 0


# --- ADR 0009 merge-time floor: re-rebase survivors after main advances (issue #45) ---
# When one PR merges, `main` moves and every other open agentflow PR that was parked
# clean can silently go CONFLICTING. This pass re-rebases those survivors each cycle so a
# conflicted one is never discovered by hand at merge time with no signal on the PR.

# Our conflict notice on a survivor carries the PR marker so it reads as ours (not a
# maintainer question, issue #18) and lets us ping a conflicted survivor once, not every
# cycle — see `conflict_already_flagged`.
_CONFLICT_MARK = "agentflow: parked — conflicts after main advanced"


class RebaseResult(str, Enum):
    CLEAN = "clean"        # re-rebased onto main and force-pushed to the same branch
    NOOP = "noop"          # rebase replayed nothing — no force-push
    CONFLICT = "conflict"  # rebase hit conflicts and was aborted; branch untouched
    ERROR = "error"        # checkout / rebase / push plumbing failed


def base_advanced(main_tip: str, merge_base: str) -> bool:
    """Pure. True when `origin/main` has moved past the point the branch last rebased onto
    — the merge-base is no longer main's tip, so the branch must re-rebase. False when the
    merge-base already IS the tip (the branch contains current main; rebasing would be a
    needless force-push) or when either SHA is missing (a git blip → don't churn). This is
    ADR 0009's 'base advanced since last rebase' check, kept pure so a survivor whose base
    hasn't moved is left untouched."""
    return bool(main_tip) and bool(merge_base) and main_tip != merge_base


def conflict_already_flagged(comments: list[dict]) -> bool:
    """Pure. True when our conflict notice is the most recent comment on the PR — so a
    still-conflicting survivor is pinged once, not re-pinged every cycle while its base
    stays behind. A newer comment (a maintainer engaging) flips this False; from there the
    comment responder owns the reply (issue #18)."""
    for c in reversed(comments):
        body = c.get("body", "")
        if _CONFLICT_MARK in body:
            return True
        if body.strip():
            return False
    return False


def _open_agentflow_prs(cfg: RepoConfig) -> list[tuple[int, str]] | None:
    """Open agentflow PRs as (number, head_branch), oldest first. None on a `gh` blip —
    unknown is not empty, so a listing failure defers the whole pass rather than reading as
    'no survivors to re-rebase'."""
    rows = github.list_open_prs(cfg.repo, limit=100)
    if rows is None:
        return None
    prs = [(row.number, row.head_ref_name)
           for row in rows
           if issue_of_branch(row.head_ref_name) is not None]
    return sorted(prs, key=lambda p: p[0])


def _base_advanced_for(workdir: str, branch: str) -> bool | None:
    """Live: has `origin/main` advanced past this branch's last rebase? Compares the
    merge-base against main's tip (both read from the just-fetched remote refs). None when a
    git command fails — caller skips rather than blind-rebases."""
    tip = _run(["git", "-C", workdir, "rev-parse", "origin/main"])
    mb = _run(["git", "-C", workdir, "merge-base", "origin/main", f"origin/{branch}"])
    if tip.returncode != 0 or mb.returncode != 0:
        return None
    return base_advanced(tip.stdout.strip(), mb.stdout.strip())


def _rebase_branch(cfg: RepoConfig, branch: str, wt: Path) -> RebaseResult:
    """Re-rebase the PR branch onto `origin/main` in its worktree and force-push it back
    (same branch, never a new one). A rebase that replays nothing is a no-op, not a
    force-push; a conflicting rebase is aborted so the branch is left exactly as it was.
    Live orchestration, not unit-tested (mirrors `_checkout_pr_branch`)."""
    if not _checkout_pr_branch(cfg, branch, wt):
        return RebaseResult.ERROR
    # Claim the worktree session only AFTER the checkout has run: `_checkout_pr_branch` reuses an
    # existing worktree only when its own idle/disposability guard passes, and that guard rejects
    # an *active* worktree. Marking the session before the checkout makes the guard see our own
    # mark and refuse — every survivor with a live builder worktree then fails plumbing forever.
    # Held across rebase+push so a concurrent cleanup can't remove the worktree mid-rebase.
    with worktree_session(wt):
        before = _run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
        if _run(["git", "-C", str(wt), "rebase", "origin/main"]).returncode != 0:
            _run(["git", "-C", str(wt), "rebase", "--abort"])
            return RebaseResult.CONFLICT
        after = _run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
        if after and after == before:
            return RebaseResult.NOOP
        if _run(["git", "-C", str(wt), "push", "--force-with-lease", "origin", branch]).returncode != 0:
            return RebaseResult.ERROR
        return RebaseResult.CLEAN


def _park_conflicted_survivor(cfg: RepoConfig, pr: int, n: int) -> None:
    """A survivor that no longer rebases clean: post one conflict notice (carrying our
    marker) and ping, so a conflicted survivor is never silent."""
    body = (
        f"> *{_CONFLICT_MARK}.*\n\n"
        "## Maintainer decision needed\n\n"
        "Affected behavior: the PR and current `main` cannot be combined automatically.\n\n"
        "Options:\n"
        "- Clarify how the two intended behaviors coexist and resume conflict resolution.\n"
        "- Close the PR and retain current `main` behavior only.\n\n"
        "Consequences: resolving may ship both compatible outcomes; closing drops the PR outcome.\n\n"
        "Recommendation: state the intended behavior at the conflict, then resume this PR.\n\n"
        "## Agent handoff\n\n"
        f"Code locations: PR #{pr}'s conflicting diff against current `main`.\n\n"
        f"Conflicting changes or unresolved facts: {CONFLICT_REASON}\n\n"
        "Checks: the rebase was attempted and aborted after Git reported conflicts; no conflicted "
        "state was pushed.\n\n"
        f"Retained work: issue #{n}'s existing PR branch remains unchanged.\n\n"
        "Exact next action: record the intended behavior, then resume conflict resolution on this "
        "same PR.")
    github.pr_comment(cfg.repo, pr, body)
    ratchet.record(cfg.repo, "parked")
    notify("agentflow needs you",
           f"{cfg.repo} #{n}: PR #{pr} conflicts after main advanced — rebase by hand",
           github.pr_url(cfg.repo, pr))


def _issue_acceptance(cfg: RepoConfig, number: int) -> str | None:
    """The current issue body anchoring a survivor re-review; unreadable fails closed."""
    return github.issue_body(cfg.repo, number)


_SAME_TOOL_REVIEW_WARNING = (
    "Warning: same-tool review is not independent. This PR will be human-merge-only and marked "
    "tainted until the other tool reviews the exact open head. Re-run with maintainer_confirmed=True "
    "only after the maintainer explicitly confirms this trade-off.")


def review_pr(cfg: RepoConfig, pr: int, *, force_same_tool: bool = False,
              maintainer_confirmed: bool = False) -> str:
    """Submit `/agentflow review <pr>` through the durable coordinator.

    A forced same-tool review is never implicit: the first call returns the warning, and only an
    explicit confirmed call submits the human-merge-only tainted review. Existing running review
    work is never preempted; a waiting exact-head review that still owns the claim transfers it
    atomically, while a parked one — left unretired but claimless — is recovered on a fresh claim.
    """
    if force_same_tool and not maintainer_confirmed:
        return _SAME_TOOL_REVIEW_WARNING
    facts = github.pr_facts(cfg.repo, pr)
    if facts is None or facts.state != "OPEN":
        return "open PR facts unreadable"
    branch, head = facts.head_ref_name, facts.head_ref_oid
    match = BUILD_BRANCH_RE.match(branch)
    if match is None or not head:
        return "PR is not on a recognized agentflow issue branch"
    builder_tool, branch_issue, slug = match.group(1), int(match.group(2)), match.group(3)
    if builder_tool not in {"claude", "codex"}:
        return "PR builder tool is unreadable"
    issue = facts.closing_issues[0] if facts.closing_issues else branch_issue
    acceptance = _issue_acceptance(cfg, issue)
    if acceptance is None:
        return "issue acceptance unreadable"
    try:
        records = pipeline.tracer.load_records()
    except StoreUnavailable:
        return "coordinator state unreadable"
    same_head = [
        record for record in records
        if record.stage == "review" and record.repo == cfg.repo
        and str(record.subject) == str(issue) and record.target == head
    ]
    latest = max(
        same_head,
        key=lambda record: (
            record.review_sequence, record.review_passes, record.created_at, record.identity),
        default=None)
    # Branch naming is durable builder lineage, not current authorship. A reviewer that pushed a
    # fix became the author of the exact open head; manual re-review must preserve that provenance.
    current_author = (
        latest.change_author_tool if latest and latest.change_author_tool else builder_tool)
    profile = repo_profile(cfg.workdir)
    if force_same_tool:
        reviewer_tool = current_author
    else:
        reviewer_tool = pick_reviewer(
            current_author, allow_same_tool=profile != "autonomous")
        if reviewer_tool is None:
            return "no eligible reviewer pool available — deferring"
    assignment, _changed_files = coordinated_review._review_assignment_facts(
        cfg.repo, pr, profile=profile)
    # A parked review stays unretired, so "the first unretired record" is no longer the live one:
    # each question is asked of the record that actually answers it. Running work is never
    # preempted whichever order the store holds, and only a record that still owns the visible
    # claim has one to hand over — a parked review is left unretired but claimless on purpose, so
    # recovering it is a fresh claim (#344).
    unretired = [record for record in same_head if not record.retired]
    if any(record.state == "running" for record in unretired):
        return "exact-head review is already running; it was not preempted"
    predecessor = next((record for record in unretired if record.claim), None)
    sequence = max((record.review_sequence for record in same_head), default=-1) + 1
    resume = max((record.resume for record in same_head), default=0) + 1
    review = ReviewState(
        assignment=assignment, change_author_tool=current_author,
        reviewed_from_sha=head, passes=review_resume_passes(records, cfg.repo, issue),
        sequence=sequence, tainted=force_same_tool)
    submission = coordinated_review.survivor_review_submission(
        cfg, issue=issue, slug=slug, builder_tool=builder_tool, head_sha=head,
        reviewer_tool=reviewer_tool, pr_number=pr, acceptance=acceptance,
        review=review, transfer_from=predecessor.identity if predecessor else None,
        supersede=predecessor is not None, resume=resume)
    if submission is None:
        return "review submission unavailable"
    if predecessor is None and not claim(cfg.repo, issue, BUILDING):
        return "could not claim PR review"
    coordinator = pipeline.build_coordinator()
    coordinator.submit_stage(submission)
    pipeline.reconcile_and_project(coordinator)
    status = "same-tool review submitted; maintainer merge required" if force_same_tool \
        else "review submitted"
    return status


def _merge_autonomous_survivor(cfg: RepoConfig, pr: int, n: int, sl: str,
                               branch_tool: str, branch: str, comments: list[dict]) -> str:
    """Submit the rebased exact head as a fresh durable Review; never launch one directly.

    Submitting one takes the PR back from the maintainer, so the summary the earlier — now
    tainted — review left behind is retired with it: an unmergeable PR under active re-review
    must not keep sitting at the top of the operator's merge queue. A successful rebase
    force-pushes the branch onto current `main`, so the PR normally stops being eligible next
    cycle and there is no later retry — a retirement that failed is said out loud in this
    cycle's status instead of leaving a stale hand-off nobody can see."""
    head = _run(["git", "-C", cfg.workdir, "rev-parse", f"origin/{branch}"])
    if head.returncode != 0 or not head.stdout.strip():
        return "review head unreadable"
    # Route the re-review through the same reviewer choice the autonomous openers use: require the
    # cross-tool reviewer and defer while it cannot launch. Same-tool fallback would create a
    # result that the autonomous profile is forbidden to merge.
    reviewer_tool = pick_reviewer(branch_tool, allow_same_tool=False)
    if reviewer_tool is None:
        return "no reviewer pool available — deferring"
    acceptance = _issue_acceptance(cfg, n)
    if acceptance is None:
        return "issue acceptance unreadable"
    submission = coordinated_review.survivor_review_submission(
        cfg, issue=n, slug=sl, builder_tool=branch_tool, head_sha=head.stdout.strip(),
        reviewer_tool=reviewer_tool, pr_number=pr, acceptance=acceptance)
    if submission is None:
        return "review submission unavailable"
    if not claim(cfg.repo, n, BUILDING):
        return "could not claim survivor Review"
    coordinator = pipeline.build_coordinator()
    coordinator.submit_stage(submission)
    pipeline.reconcile_and_project(coordinator)
    if not supersede_clean_review(comments):
        return "review submitted, but its merge hand-off could not be retired"
    return "review submitted"


def _conflict_revise_owns_head(cfg: RepoConfig, n: int, branch: str) -> bool:
    """Is a conflict Revise already resolving this branch's exact current head?

    Ownership is what the coordinator itself means by it: a conflict Revise record targeting the
    head we can read *right now*, not retired, and still holding its claim. Retired or claimless
    records are deliberately excluded — a PR head can sit still while `main` moves again, and a
    Revise that has let go of the work must not block the fresh rebase that would now apply.
    An unreadable head or coordinator store proves nothing, so it answers ``False`` and the caller
    takes the ordinary rebase path rather than skipping it on a guess."""
    head = _run(["git", "-C", cfg.workdir, "rev-parse", f"origin/{branch}"])
    if head.returncode != 0 or not head.stdout.strip():
        return False
    head_sha = head.stdout.strip()
    try:
        records = pipeline.tracer.load_records()
    except StoreUnavailable:
        return False
    return any(r.target == head_sha and not r.retired and r.claim
               for r in conflict_revises_used(records, cfg.repo, n))


def _conflict_revise_survivor(cfg: RepoConfig, pr: int, n: int, sl: str, tool: str,
                              branch: str, comments: list[dict]) -> str | None:
    """A survivor's re-rebase no longer applies: open a conflict Revise on the builder's own lineage
    to resolve it (ADR 0038) instead of parking. The Revise adopts the retained PR-branch worktree,
    is bound to the conflicting head SHA it must supersede, and is admitted ahead of cold build work.
    Returns a status string once a conflict Revise is opened (or is already open for this head).
    There is no PR-lifetime conflict cap: each genuinely new conflicting head gets its own bounded
    stage attempts. ``None`` is reserved for an unreconstructable submission, where the caller uses
    the human fallback. Never parks or force-merges here.

    Opening one takes the PR back from the maintainer, so any clean-review summary it carries is
    retired with it — a conflicting PR being resolved is not the maintainer's to merge."""
    head = _run(["git", "-C", cfg.workdir, "rev-parse", f"origin/{branch}"])
    if head.returncode != 0 or not head.stdout.strip():
        return f"#{pr}: conflict — head unreadable, retry next cycle"
    head_sha = head.stdout.strip()
    try:
        records = pipeline.tracer.load_records()
    except StoreUnavailable:
        return f"#{pr}: conflict — coordinator state unreadable, retry next cycle"
    priors = conflict_revises_used(records, cfg.repo, n)
    if any(r.target == head_sha for r in priors):
        return f"#{pr}: conflict — revise already open"   # idempotent under re-reconcile
    conflict_round = len(priors) + 1
    submission = coordinated_revise.survivor_conflict_revise_submission(
        cfg, issue=n, slug=sl, builder_tool=tool, head_sha=head_sha, pr_number=pr,
        conflict_round=conflict_round)
    if submission is None:
        return None                                       # unreconstructable → caller parks
    if not claim(cfg.repo, n, BUILDING):
        return f"#{pr}: conflict — could not claim conflict revise"
    coordinator = pipeline.build_coordinator()
    coordinator.submit_stage(submission)
    pipeline.reconcile_and_project(coordinator)
    supersede_clean_review(comments)   # the Revise is durably open either way; retried next cycle
    return f"#{pr}: conflict — revise round {conflict_round} opened"


def _rebase_survivor(cfg: RepoConfig, pr: int, branch: str, profile: str,
                     comments: list[dict]) -> str:
    """Re-rebase one survivor and route the outcome by the repo's profile. On conflict, open a
    conflict Revise to resolve it (ADR 0038); park only when that stage cannot be reconstructed or
    genuinely fails its bounded attempts. On a clean re-rebase: `autonomous` reruns the merge gate
    and lands one; `reviewed`/`guarded` just leave the PR mergeable again for the human — never a
    merge (ADR 0002).

    A conflict Revise that is already resolving this exact head has adopted the PR-branch worktree,
    so re-entering it would only fail plumbing. That case is recognized *before* any checkout, and
    the one thing still worth doing there is retrying the merge hand-off's retirement — the durable
    same-head Revise is itself the proof the engine already took this PR back."""
    m = BUILD_BRANCH_RE.match(branch)
    if not m:
        return f"#{pr}: unrecognized branch {branch}"
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    if _conflict_revise_owns_head(cfg, n, branch):
        if not supersede_clean_review(comments):
            return f"#{pr}: conflict — revise already open, merge hand-off could not be retired"
        return f"#{pr}: conflict — revise already open"
    wt = Path(WorktreeRef.for_build(cfg.workdir, tool, n, sl).path)
    result = _rebase_branch(cfg, branch, wt)
    if result is RebaseResult.CONFLICT:
        revised = _conflict_revise_survivor(cfg, pr, n, sl, tool, branch, comments)
        if revised is not None:
            return revised
        _park_conflicted_survivor(cfg, pr, n)
        comments = github.pr_comment_rows(cfg.repo, pr)
        if comments is not None and any(_CONFLICT_MARK in c.get("body", "") for c in comments):
            remove_worktree_if_safe(cfg.workdir, wt)
        return f"#{pr}: conflict — parked for human"
    if result is RebaseResult.ERROR:
        return f"#{pr}: rebase plumbing failed — retry next cycle"
    if result is RebaseResult.NOOP:
        remove_worktree_if_safe(cfg.workdir, wt)
        return f"#{pr}: nothing to replay"
    remove_worktree_if_safe(cfg.workdir, wt)
    if profile != "autonomous":
        return f"#{pr}: re-rebased clean — mergeable for the human"
    return f"#{pr}: {_merge_autonomous_survivor(cfg, pr, n, sl, tool, branch, comments)}"


def recheck_once(cfg: RepoConfig) -> str:
    """Re-rebase every open agentflow PR whose base advanced since a sibling merged, and
    reroute by profile (ADR 0009 merge-time floor; issue #45). Merges serialize — at most
    one lands per cycle, so surviving siblings re-rebase against the new `main` before the
    next is eligible. Skips a survivor whose base hasn't moved (no needless force-push) and
    one already flagged / awaiting a maintainer reply (no re-ping, no fighting the responder
    — issue #18). Never opens a new PR; never merges on a `reviewed`/`guarded` repo."""
    prs = _open_agentflow_prs(cfg)
    if prs is None:
        return "couldn't list open PRs — deferring"
    if _run(["git", "-C", cfg.workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return "couldn't fetch origin — deferring"
    profile = repo_profile(cfg.workdir)
    results: list[str] = []
    for pr, branch in prs:
        if not _base_advanced_for(cfg.workdir, branch):
            continue   # False or None — base hasn't moved (or unknown): leave it untouched
        comments = github.pr_comment_rows(cfg.repo, pr)
        if comments is None:
            continue
        if conflict_already_flagged(comments) or reply_pending(comments):
            continue   # already pinged, or a maintainer question the responder owns
        out = _rebase_survivor(cfg, pr, branch, profile, comments)
        results.append(out)
        if out.endswith(": merged"):
            break   # one merge per cycle — survivors re-rebase against the new main first
    return "; ".join(results) if results else "no survivors to re-rebase"
