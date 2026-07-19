"""Coordinator-only production dispatch (ADR 0030, issue #109).

Dispatch discovers stage inputs and submits their durable facts. It never starts a provider,
counts a permit, or consults the live-board projection for ownership. The session coordinator is
the single owner of reconciliation, admission, pacing, attempts, and provider launch.
"""

from __future__ import annotations

import os
import threading

from agentflow import coordinated_build, loop
from agentflow.balancer import pick_pair

# Production limits are named here for the coordinator's one composed admission gate. They are
# not counters: durable running rows are the only concurrency/permit ledger.
MACHINE_CEILING = int(os.environ.get("AGENTFLOW_MAX_SESSIONS", "4"))
TRIAGE_CONCURRENCY = int(os.environ.get("AGENTFLOW_TRIAGE_CONCURRENCY", "3"))
BUILD_CONCURRENCY = int(os.environ.get("AGENTFLOW_BUILD_CONCURRENCY", "2"))
STAGE_CAPS = {
    "triage": TRIAGE_CONCURRENCY,
    "build": BUILD_CONCURRENCY,
    "mockup": 1,
    "respond": 1,
    "research": 1,
}


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
    issue = loop._next_ready_issue(cfg, _log=_log)
    if not issue:
        return "no ready-for-agent issues"
    builder, _reviewer, block_msg = pick_pair()
    if builder is None:
        return f"#{issue['number']}: no pool has headroom ({block_msg}) — deferring"
    submission = coordinated_build.build_submission(cfg, issue, builder.tool)
    if submission is None:
        return f"#{issue['number']}: skipped — no agentflow:complexity:* label (ADR 0018 gate)"
    from agentflow.coordinator.record import WAITING
    identity = coordinator.submit_stage(submission)
    record = coordinator.stage_record(identity)
    # The daemon never auto-resumes an exhausted Build — that needs an explicit maintainer
    # `build <N>` (#245). An ordinary resubmission that reused a terminal `held` record produced
    # nothing to run, so claim nothing and report the held state rather than a false launch.
    if record is None or record.state != WAITING or record.hold_pending or record.retired:
        return (f"#{issue['number']}: Build held — awaiting a maintainer resume "
                f"(`/agentflow pickup {issue['number']}` then `build`)")
    if not loop._claim(cfg.repo, issue["number"]):
        return f"#{issue['number']}: could not claim Build — refusing coordinator submission"
    return f"#{issue['number']}: submitted to coordinator → {builder.tool} (build)"


def _submit_coordinated_respond(cfg, coordinator, _log) -> str:
    pending = loop._next_pr_awaiting_reply(cfg)
    if not pending:
        return "no PRs awaiting reply"
    pr, branch, comment, target, baseline = pending
    submission = coordinated_build.respond_submission(
        cfg, pr, branch, comment, target, baseline)
    if submission is None:
        return f"PR #{pr}: not a resolvable agentflow respond target — skipping"
    number = int(submission.subject)
    if number in coordinated_build.owned_issues(cfg, lane="building"):
        return (f"#{number}: prior change stage still owns the building claim — "
                "deferring Respond")
    if not loop._claim(cfg.repo, number):
        return f"#{number}: could not claim Respond — refusing coordinator submission"
    coordinator.submit_stage(submission)
    return f"#{number}: submitted PR #{pr} to coordinator → {submission.pool} (respond)"


def _submit_coordinated_mockup(cfg, coordinator, _log) -> str:
    issue = loop._next_mockup_issue(cfg)
    if not issue:
        return "no needs-mockup issues to draw"
    builder, _reviewer, block_msg = pick_pair()
    if builder is None:
        return f"#{issue['number']}: no pool has headroom to draw mockups ({block_msg}) — deferring"
    submission = coordinated_build.mockup_submission(cfg, issue, builder.tool)
    coordinator.submit_stage(submission)
    if not loop._claim_mockup(cfg.repo, issue["number"]):
        return f"#{issue['number']}: Mockup record saved; drawing claim pending"
    return f"#{issue['number']}: submitted to coordinator → {builder.tool} (mockup)"


def _submit_coordinated_intake(cfg, coordinator, _log) -> str:
    from agentflow import coordinated_intake

    reserved: set[int] = set()
    submitted = []
    while True:
        picked = loop._next_intake_candidate(cfg, reserved)
        if picked is None:
            break
        issue, extra = picked
        if issue["number"] in coordinated_build.owned_issues(cfg, lane=None):
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
        coordinator.submit_stage(submission)
        if not loop._claim_triage(cfg.repo, issue["number"]):
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
    if not loop._claim_resolving(cfg.repo, ticket["number"]):
        return f"#{ticket['number']}: could not claim wayfinder:resolving — refusing submission"
    coordinator.submit_stage(submission)
    return f"#{ticket['number']}: submitted to coordinator → {builder.tool} (research)"


def _submit_repo(cfg, coordinator, _log) -> None:
    """Discover and durably submit each stage kind; no provider starts in this layer."""
    _run_and_log(cfg, "intake", lambda: _submit_coordinated_intake(cfg, coordinator, _log), _log)
    _run_and_log(cfg, "build", lambda: _submit_coordinated_build(cfg, coordinator, _log), _log)
    _run_and_log(cfg, "mockup", lambda: _submit_coordinated_mockup(cfg, coordinator, _log), _log)
    _run_and_log(cfg, "respond", lambda: _submit_coordinated_respond(cfg, coordinator, _log), _log)
    _run_and_log(cfg, "research", lambda: _submit_coordinated_research(cfg, coordinator, _log), _log)


def run_cycle(repos, *, submit_new: bool = True, coordinator=None, _log=None) -> None:
    """Submit new work when active, then always reconcile durable records.

    ``submit_new=False`` is the operator pause/drain boundary: it launches no cold work but still
    observes running families, continues owned stages, finalizes outcomes/holds, and republishes
    the live board from durable running rows. There is no legacy or bypass mode.
    """
    _log = _log or (lambda _m: None)
    coord = coordinator if coordinator is not None else coordinated_build.build_coordinator(_log)
    if submit_new:
        threads = [_spawn(lambda cfg=cfg: _submit_repo(cfg, coord, _log)) for cfg in repos]
        for thread in threads:
            thread.join()
    else:
        _log("dispatch paused — reconciling coordinator-owned work only")
    coordinated_build.reconcile_and_project(coord, _log=_log)
    for cfg in repos:
        coordinated_build.reconcile_orphaned_claims(cfg, _log=_log)
