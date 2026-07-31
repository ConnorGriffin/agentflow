"""Coordinator-only production dispatch (ADR 0030, issue #109).

Dispatch discovers stage inputs and submits their durable facts. It never starts a provider,
counts a permit, or consults the live-board projection for ownership. The session coordinator is
the single owner of reconciliation, admission, pacing, attempts, and provider launch.
"""

from __future__ import annotations

import os
import threading

from agentflow import coordinated_build, coordinated_respond, coordinated_review, loop, pipeline
from agentflow.balancer import pick_pair
from agentflow.labels import BUILDING, RESOLVING, TRIAGING, claim
from agentflow.coordinator.quota_poll import refresh_claude_quota
from agentflow.coordinator.store import default_store_path
from agentflow.runner import dispatch_preflight


def _spawn(fn) -> threading.Thread:
    thread = threading.Thread(target=fn, daemon=True)
    thread.start()
    return thread


def _run_and_log(cfg, label: str, fn, _log) -> None:
    try:
        _log(f"{cfg.repo}: {label}: {fn()}")
    except Exception as e:  # noqa: BLE001 — one stage/repo cannot sink the fleet cycle
        _log(f"{cfg.repo}: {label} error: {type(e).__name__}: {e}")


def _submit_coordinated_build(cfg, coordinator, _log) -> str:
    """Submit the oldest ready issue that can actually run.

    A candidate that is conclusively non-runnable — no complexity dial, or an idempotent
    resubmission that reused a terminal held record — is reported and passed over so the same
    pass still reaches later eligible work (#327). Unknown shared state (GitHub listings, pool
    headroom, a failed claim mutation) still ends the pass, and an exhausted Build is never
    auto-resumed: it needs an explicit maintainer `build <N>` (#245).
    """
    from agentflow.coordinator.record import WAITING

    reserved: set[int] = set()
    skipped: list[str] = []

    def _report(tail: str) -> str:
        return "; ".join([*skipped, tail])

    while True:
        issue = loop._next_ready_issue(cfg, reserved, _log=_log)
        if not issue:
            return _report("no further runnable ready-for-agent issues") if skipped \
                else "no ready-for-agent issues"
        number = issue["number"]
        builder, _reviewer, block_msg = pick_pair()
        if builder is None:
            return _report(f"#{number}: no pool has headroom ({block_msg}) — deferring")
        submission = coordinated_build.build_submission(cfg, issue, builder.tool)
        if submission is None:
            skipped.append(f"#{number}: skipped — no agentflow:complexity:* label (ADR 0018 gate)")
            reserved.add(number)
            continue
        identity = coordinator.submit_stage(submission)
        record = coordinator.stage_record(identity)
        # The resubmission reused a terminal `held` record and produced nothing to run, so claim
        # nothing, leave the hold untouched, and move on to the next candidate.
        if record is None or record.state != WAITING or record.hold_pending or record.retired:
            skipped.append(f"#{number}: Build held — awaiting a maintainer resume "
                           f"(`/agentflow pickup {number}` then `build`)")
            reserved.add(number)
            continue
        if not claim(cfg.repo, number, BUILDING):
            return _report(f"#{number}: could not claim Build — refusing coordinator submission")
        return _report(f"#{number}: submitted to coordinator → {builder.tool} (build)")


def _submit_coordinated_respond(cfg, coordinator, _log) -> str:
    pending = loop._next_pr_awaiting_reply(cfg)
    if not pending:
        return "no PRs awaiting reply"
    pr, branch, comment, target, baseline = pending
    # A reply that answers a parked review's recorded decision belongs to that review. It resumes
    # the parked exact-head review instead of opening a second lifecycle that competes with the
    # manual recovery for the same claim (#344); ordinary discussion still falls through to Respond.
    resumed = coordinated_review.resume_answered_review(
        cfg, coordinator, pr, comment=comment, target=target, baseline=baseline)
    if resumed is not None:
        return resumed
    submission = coordinated_respond.respond_submission(
        cfg, pr, branch, comment, target, baseline)
    if submission is None:
        return f"PR #{pr}: not a resolvable agentflow respond target — skipping"
    number = int(submission.subject)
    if number in pipeline.owned_issues(cfg, lane="building"):
        return (f"#{number}: prior change stage still owns the building claim — "
                "deferring Respond")
    if not claim(cfg.repo, number, BUILDING):
        return f"#{number}: could not claim Respond — refusing coordinator submission"
    coordinator.submit_stage(submission)
    return f"#{number}: submitted PR #{pr} to coordinator → {submission.pool} (respond)"


def _submit_coordinated_intake(cfg, coordinator, _log) -> str:
    from agentflow import coordinated_intake
    from agentflow.coordinator.record import WAITING

    reserved: set[int] = set()
    submitted = []
    while True:
        picked = loop._next_intake_candidate(cfg, reserved)
        if picked is None:
            break
        issue, extra = picked
        if issue["number"] in pipeline.owned_issues(cfg, lane=None):
            # A non-retired coordinator record in some downstream lane already owns this
            # issue — it is mid-pipeline, not a fresh intake candidate. Reserve it so the
            # picker doesn't return it again this cycle, then skip (#201).
            reserved.add(issue["number"])
            continue
        builder, _reviewer, block_msg = pick_pair()
        if builder is None:
            return ("; ".join(submitted) if submitted else
                    f"#{issue['number']}: no pool has headroom ({block_msg}) — deferring")
        submission = coordinated_intake.intake_submission(cfg, issue, extra, builder.tool)
        if submission is None:
            return f"#{issue['number']}: Intake source unreadable — deferring"
        identity = coordinator.submit_stage(submission)
        record = coordinator.stage_record(identity)
        # An idempotent resubmission whose stable identity already points at a terminal
        # (completed/held/retired) Intake record created nothing to run. Claiming would
        # stamp agentflow:triaging with no coordinator owner; orphan reconciliation would
        # strip it and the next cycle would recreate it forever (#308). Reserve and skip.
        if record is None or record.state != WAITING or record.hold_pending or record.retired:
            reserved.add(issue["number"])
            continue
        if not claim(cfg.repo, issue["number"], TRIAGING):
            # Runnable submission but the claim mutation failed: withdraw the never-started
            # WAITING record so no unowned Intake work survives, mirroring build_issue.
            coordinator.withdraw_stage(identity)
            return f"#{issue['number']}: Intake record saved; claim pending — deferring admission"
        reserved.add(issue["number"])
        submitted.append(f"#{issue['number']} → {builder.tool}")
    return "; ".join(submitted) if submitted else "no un-triaged issues"


def _submit_coordinated_research(cfg, coordinator, _log) -> str:
    from agentflow import coordinated_research

    ticket = loop._next_research_ticket(cfg, _log=_log)
    if not ticket:
        return "no wayfinder:research tickets to resolve"
    builder, _reviewer, block_msg = pick_pair()
    if builder is None:
        return f"#{ticket['number']}: no pool has headroom to research ({block_msg}) — deferring"
    map_context = coordinated_research.research_map_context(cfg.repo, ticket["number"])
    submission = coordinated_research.research_submission(
        cfg, ticket, builder.tool, map_context=map_context)
    if not claim(cfg.repo, ticket["number"], RESOLVING):
        return f"#{ticket['number']}: could not claim wayfinder:resolving — refusing submission"
    coordinator.submit_stage(submission)
    return f"#{ticket['number']}: submitted to coordinator → {builder.tool} (research)"


def _submit_repo(cfg, coordinator, _log) -> None:
    """Discover and durably submit each stage kind; no provider starts in this layer.

    A repository whose environment can no longer carry a session receives no cold work at all
    this pass (ADR 0050) — feeding agents into it produces sessions that die before they can say
    why. The gate stops *new* submissions only: records submitted in earlier passes still admit
    and launch through reconciliation, which is a bounded backlog that the reclamation sweep is
    meanwhile curing.
    """
    if not dispatch_preflight(cfg.repo, cfg.workdir, pipeline.owned_worktrees(cfg), _log=_log):
        return
    _run_and_log(cfg, "intake", lambda: _submit_coordinated_intake(cfg, coordinator, _log), _log)
    _run_and_log(cfg, "build", lambda: _submit_coordinated_build(cfg, coordinator, _log), _log)
    # ``needs-mockup`` is a human hold, not permission to spend a five-permit deep session.
    # A maintainer enters that phase through `/agentflow pickup N` (ADR 0019); the coordinator
    # only recovers Mockups that had already started before this gate was restored.
    _run_and_log(cfg, "respond", lambda: _submit_coordinated_respond(cfg, coordinator, _log), _log)
    _run_and_log(cfg, "research", lambda: _submit_coordinated_research(cfg, coordinator, _log), _log)


def _refresh_claude_quota(_log) -> None:
    """Keep Claude's five-hour dispatch authority fresh from the provider before this pass reads it
    (issue #309). `refresh_claude_quota` already owns its TTL and swallows every failure to a
    ``None``; this outer guard is defense-in-depth for the dispatch-critical path (the poll shells
    out via subprocess/urllib), so no unforeseen error there can ever sink a cycle."""
    try:
        refresh_claude_quota(default_store_path(), log=_log)
    except Exception as e:  # noqa: BLE001 — a quota poll must never break dispatch
        _log(f"claude quota poll error: {type(e).__name__}: {e}")


def run_cycle(repos, *, submit_new: bool = True, coordinator=None, _log=None) -> None:
    """Submit new work when active, then always reconcile durable records.

    ``submit_new=False`` is the operator pause/drain boundary: it launches no cold work but still
    observes running families, continues owned stages, finalizes outcomes/holds, and republishes
    the live board from durable running rows. There is no legacy or bypass mode.
    """
    _log = _log or (lambda _m: None)
    _refresh_claude_quota(_log)
    coord = coordinator if coordinator is not None else pipeline.build_coordinator(_log)
    if submit_new:
        threads = [_spawn(lambda cfg=cfg: _submit_repo(cfg, coord, _log)) for cfg in repos]
        for thread in threads:
            thread.join()
    else:
        _log("dispatch paused — reconciling coordinator-owned work only")
    pipeline.reconcile_and_project(coord, _log=_log)
    for cfg in repos:
        pipeline.reconcile_orphaned_claims(cfg, _log=_log)
