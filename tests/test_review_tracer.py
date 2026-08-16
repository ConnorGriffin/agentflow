"""Review as the second coordinated stage (issue #104), driven through the public
``submit_stage`` / ``cycle`` seam. Review binds to the exact PR head SHA, retains its writable
checkout on continuation, completes only on a durable verdict for the final SHA, may move pools
before starting when review safety allows it, and parks the PR on exhaustion — all asserted at the coordinator
interface, never by poking private transitions. The Build → Review claim transfer and its crash
boundaries are exercised here too, through a stage router that runs both live stages behind one
coordinator.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeSession, permits, record_of

from agentflow import (coordinated_build, coordinated_review, github, pipeline, pr_park)
from agentflow.gate import MAX_REVISES
from agentflow.coordinator import (BuildStageAdapter, ReviewStageAdapter, StageRouter, Submission,
                                    tracer)
from agentflow.coordinator.providers import (
    PROVIDER_INPUT_V1, ProviderCause, ProviderObservation,
    split_terminal_session_lead_contract)
from agentflow.coordinator.record import Record
from agentflow.review_policy import ReviewState
from agentflow.routing import routing


def _review(subject="7", *, pool="claude", target="sha-a", builder_lineage="codex",
            source="/wt/pr-7-x", transfer_from=None, review_tainted=False):
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool, complexity="deep",
                      target=target, source=source, builder_lineage=builder_lineage,
                      transfer_from=transfer_from,
                      review=ReviewState(
                          change_author_tool=builder_lineage, tainted=review_tainted))


def _review_adapter(fake, *, verdict, prep, handoff=None):
    """A Review adapter wired to test flags: ``verdict``/``prep`` are single-element lists so a
    test flips verdict durability and checkout readiness mid-flight; the fake plays observer."""
    return ReviewStageAdapter(verdict_ready=lambda r, o: verdict[0],
                              worktree_reset=lambda r: prep[0], observer=fake, handoff=handoff)


def _router(fake, *, pr, verdict, prep):
    """One coordinator owning both live stages, so the Build → Review transition is exercised
    end to end (ADR 0030)."""
    build = BuildStageAdapter(pr_exists=lambda r: pr[0], worktree_ready=lambda r: True,
                              observer=fake)
    review = _review_adapter(fake, verdict=verdict, prep=prep)
    return StageRouter({"build": build, "review": review})


def _records(coord):
    return list(coord._store.load().values())


class _CommitFault:
    """A connection proxy that kills submission immediately before or after SQLite COMMIT."""

    def __init__(self, connection, point):
        self._connection = connection
        self._point = point

    def execute(self, sql, parameters=()):
        if sql == "COMMIT" and self._point == "before":
            raise RuntimeError("daemon died immediately before successor commit")
        result = self._connection.execute(sql, parameters)
        if sql == "COMMIT" and self._point == "after":
            raise RuntimeError("daemon died immediately after successor commit")
        return result

    def close(self):
        self._connection.close()


# --- outcome-first verdict verification --------------------------------------------------

def test_review_completes_when_the_verdict_is_durable_even_after_a_bad_exit(make_coord):
    fake = FakeSession()
    verdict, prep = [True], [True]
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=verdict, prep=prep))
    ident = coord.submit_stage(_review())
    assert coord.cycle("claude") == []              # attempt running
    fake.end(ident, cause=ProviderCause.PROCESS)    # reviewer exited badly (non-zero)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]  # verdict present → done


def test_clean_exit_without_a_verdict_stays_incomplete(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    ident = coord.submit_stage(_review())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)       # clean exit, but no verdict
    assert coord.cycle("claude") == []              # not completed — bounded continuation instead
    rec = record_of(coord, ident)
    assert rec.state != "completed" and rec.continuation and rec.attempts == 2


def test_a_verdict_for_another_sha_does_not_complete_review():
    """The verdict must name the exact reviewed head SHA (ADR 0028). The production verifier is
    pure over the reviewer's captured final message and the record's immutable target."""
    record = Record(identity="o/r|7|review|sha-a", stage="review", pool="claude", demand=1,
                    repo="o/r", subject="7", target="sha-a")
    match = ProviderObservation(
        final_message='{"verdict": "PASS", "reviewed_sha": "sha-a", "findings": []}')
    other = ProviderObservation(
        final_message='{"verdict": "PASS", "reviewed_sha": "sha-b", "findings": []}')
    none = ProviderObservation(final_message="I looked but wrote no verdict object.")
    assert coordinated_review._verdict_ready(record, match)
    assert not coordinated_review._verdict_ready(record, other)   # a different head SHA
    assert not coordinated_review._verdict_ready(record, none)    # no verdict at all


# --- the exact PR head SHA is the review's identity --------------------------------------

def test_same_head_sha_is_one_review_a_new_head_sha_is_a_fresh_stage(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    first = coord.submit_stage(_review(target="sha-a"))
    again = coord.submit_stage(_review(target="sha-a"))
    assert first == again                            # same head SHA → the same review record
    coord.cycle("claude")
    assert record_of(coord, first).attempts == 1     # one running attempt so far

    newer = coord.submit_stage(_review(target="sha-b"))
    assert newer != first                            # a different head SHA → a new stage
    assert record_of(coord, newer).attempts == 0     # with a fresh, unused budget


# --- read-only checkout is recreated before admission ------------------------------------

def test_checkout_recreation_failure_consumes_no_permit_or_attempt_and_keeps_the_target(
        make_coord):
    fake = FakeSession()
    verdict, prep = [False], [False]                 # read-only checkout not ready yet
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=verdict, prep=prep))
    ident = coord.submit_stage(_review(target="sha-a"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0             # nothing reserved
    rec = record_of(coord, ident)
    assert rec.attempts == 0 and rec.state == "waiting"
    assert rec.target == "sha-a"                      # the durable target is never lost

    prep[0] = True                                    # checkout recreated → admits normally
    coord.cycle("claude")
    assert permits(coord, "claude") == 1
    assert record_of(coord, ident).attempts == 1


# --- a review continuation may move pools ------------------------------------------------

def test_writable_review_continuation_stays_on_its_reviewer_tool(make_coord):
    """Once Review has launched it may own partial fixes, so a continuation stays on that reviewer
    tool instead of silently handing writable work to the builder's pool (ADR 0047)."""
    fake = FakeSession()
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    # A cross-tool review of a claude-built PR, assigned to codex (demand 2). Cross-tool, so it
    # may auto-merge for now.
    r1 = coord.submit_stage(_review("1", pool="codex", builder_lineage="claude"))
    coord.cycle("codex", now=0)
    assert record_of(coord, r1).auto_merge_allowed is True
    fake.end(r1, cause=ProviderCause.CAPACITY, reset_at=100)   # codex paused this attempt
    coord.cycle("codex", now=0)                                # freed; not eligible until 100

    # Meanwhile codex fills with two other reviews (2 + 2), leaving no room for r1's demand-2.
    coord.submit_stage(_review("2", pool="codex", builder_lineage="claude"))
    coord.submit_stage(_review("3", pool="codex", builder_lineage="claude"))
    coord.cycle("codex", now=0)
    assert permits(coord, "codex") == 4

    # r1 becomes eligible but codex is full. It must wait there: moving to claude could hand
    # partially-authored review fixes to a different tool and erase cross-tool ownership.
    coord.cycle("claude", now=100)
    moved = record_of(coord, r1)
    assert moved.pool == "codex" and moved.state == "waiting"
    assert moved.lineage == "codex" and moved.auto_merge_allowed is True
    assert permits(coord, "claude") == 0


def test_a_code_writing_continuation_never_migrates(make_coord):
    """A code-writing stage stays on its builder lineage after launch even when its pool is full
    (ADR 0028). A revise pinned to codex is never offered to claude."""
    fake = FakeSession()
    coord = make_coord(fake)
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise", pool="codex",
                                           builder_lineage="codex", complexity="deep"))
    coord.cycle("codex")
    fake.end(revise, cause=ProviderCause.PROCESS)     # interrupted → continuation on codex
    coord.cycle("claude", now=0)                       # claude cycle must not adopt it
    assert record_of(coord, revise).pool == "codex"
    assert permits(coord, "claude") == 0


# --- a review waits for its selected independence tool -------------------------------------

def _gate_blocking(*pools):
    """An admission gate that refuses launches on the named pools (e.g. one whose weekly budget
    is spent) while its permit ledger is untouched — the launch-gate block that froze the
    home-depot #22/#23 reviews at zero attempts."""
    blocked = set(pools)
    return lambda record: record.pool not in blocked


def test_a_fresh_review_waits_when_its_selected_tool_lost_launch_capacity(make_coord):
    """Reviewer selection is the independence gate. A fresh cross-tool review whose selected pool
    later loses capacity waits there without consuming permits; the coordinator must not silently
    turn autonomous work into a same-tool review."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_gate_blocking("codex"),
                       adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    # A cross-tool review of a claude-built PR, assigned to codex while codex still had budget;
    # codex has since lost its launch capacity with an empty ledger.
    r = coord.submit_stage(_review("1", pool="codex", builder_lineage="claude"))
    coord.cycle("codex", now=0)                       # codex cannot launch it; ledger untouched
    frozen = record_of(coord, r)
    assert frozen.state == "waiting" and frozen.attempts == 0
    assert permits(coord, "codex") == 0

    coord.cycle("claude", now=0)                      # same-tool pool must not adopt it
    waiting = record_of(coord, r)
    assert waiting.pool == "codex" and waiting.state == "waiting"
    assert waiting.target == "sha-a" and waiting.auto_merge_allowed is True
    assert permits(coord, "claude") == 0


def test_a_review_stays_put_when_neither_pool_can_launch_it_then_lands_home_on_recovery(
        make_coord):
    """With both pools launch-blocked the review stays on its selected pool, then launches there
    once that pool recovers."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_gate_blocking("codex", "claude"),
                       adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    r = coord.submit_stage(_review("1", pool="codex", builder_lineage="claude"))
    for _ in range(3):                                # several cycles, both pools blocked
        coord.cycle("codex", now=0)
        coord.cycle("claude", now=0)
    parked = record_of(coord, r)
    assert parked.pool == "codex" and parked.state == "waiting" and parked.attempts == 0
    assert parked.demand == 2
    assert parked.auto_merge_allowed is True          # still cross-tool while it stays home
    assert permits(coord, "claude") == 0

    coord._gate = _gate_blocking("claude")            # codex regains its weekly budget
    coord.cycle("codex", now=0)                       # launches at home, not re-placed
    home = record_of(coord, r)
    assert home.pool == "codex" and home.state == "running"
    assert permits(coord, "codex") == 2


def test_a_gate_blocked_code_writing_stage_never_migrates(make_coord):
    """Weekly-budget pacing of codex code-writing work is intended: a waiting revise whose codex
    pool is launch-blocked stays on codex and is never offered to claude."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_gate_blocking("codex"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise", pool="codex",
                                           builder_lineage="codex", complexity="deep"))
    coord.cycle("codex", now=0)                        # gate-blocked; stays waiting on codex
    coord.cycle("claude", now=0)                       # claude cycle must not adopt it
    stayed = record_of(coord, revise)
    assert stayed.pool == "codex" and stayed.state == "waiting"
    assert permits(coord, "claude") == 0


# --- exhaustion parks the PR once --------------------------------------------------------

def test_exhaustion_parks_the_pr_once_with_one_handoff_and_notification(make_coord):
    fake = FakeSession()
    handoffs = []
    adapter = _review_adapter(
        fake, verdict=[False], prep=[True],
        handoff=lambda record: handoffs.append(record.identity) or "pr-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review())
    outcome = None
    for _ in range(6):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        # a restarted/waiting review keeps its claim while it still has budget
        assert record_of(coord, ident).claim is True
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held"
    assert outcome.handoff == "pr:parked"
    rec = record_of(coord, ident)
    # Review may retain partial fixes, so it gets the full initial + two continuation budget before
    # parking — still exactly one handoff and notification.
    assert rec.attempts == 3 and rec.handoffs == 1 and rec.notifications == 1
    assert rec.claim is False                          # claim released only at the park boundary
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]                          # restart cannot repeat the external handoff


# --- build → review claim transfer with no ownership gap ---------------------------------

def test_completed_build_opens_review_and_transfers_the_claim_before_retiring(make_coord):
    fake = FakeSession()
    pr, verdict, prep = [True], [False], [True]
    coord = make_coord(fake, adapter=_router(fake, pr=pr, verdict=verdict, prep=prep),
                       gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    assert [o.stage for o in coord.cycle("claude")] == ["build"]  # build completed
    assert record_of(coord, build).claim is True and record_of(coord, build).retired is False

    # The completed Build opens the review for the exact head SHA and hands off the claim.
    review = coord.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                        target="head-sha", transfer_from=build))
    assert record_of(coord, build).claim is False and record_of(coord, build).retired is True
    assert record_of(coord, review).claim is True
    # Exactly one record owns the issue's claim at every point — no gap, no double-claim.
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


def test_daemon_death_between_stages_keeps_ownership_and_transfers_once(make_coord):
    """Fault injection (ADR 0028): a daemon death before the claim transfer must leave the
    completed Build owning the claim (no gap); a death after the verdict is durable but before
    retirement must not duplicate the review or lose ownership."""
    fake = FakeSession()
    pr, verdict, prep = [True], [False], [True]
    adapter = _router(fake, pr=pr, verdict=verdict, prep=prep)
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="high", source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")                              # build completes, claim retained

    # Death before the transfer: a fresh coordinator still sees the Build owning its claim.
    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    assert record_of(restarted, build).claim is True and record_of(restarted, build).retired is False
    review = restarted.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                            target="head-sha", transfer_from=build))
    assert record_of(restarted, build).retired is True

    # The review runs and its verdict becomes durable; then the daemon dies before retirement.
    restarted.cycle("codex")
    verdict[0] = True
    fake.end(review, cause=ProviderCause.PROCESS)
    again = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    assert [o.status for o in again.cycle("codex")] == ["completed"]   # finalized exactly once
    assert record_of(again, review).claim is True                     # kept until the next stage
    assert record_of(again, review).attempts == 1                     # attempt not double-counted
    assert again.cycle("codex") == []                                 # no duplicate review work


def test_death_before_successor_commit_keeps_completed_predecessor_ownership(make_coord):
    fake = FakeSession()
    adapter = _router(fake, pr=[True], verdict=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build",
                                          pool="claude", complexity="deep", effort="high",
                                          source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")

    crashed = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    crashed._store._conn = _CommitFault(crashed._store._conn, "before")
    with pytest.raises(RuntimeError, match="before successor commit"):
        crashed.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                     target="head-sha", transfer_from=build))
    crashed._store.close()

    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    records = {r.identity: r for r in _records(restarted)}
    assert set(records) == {build}
    assert records[build].state == "completed"
    assert records[build].claim is True and records[build].retired is False


def test_death_after_successor_commit_leaves_one_owner_and_retry_is_idempotent(make_coord):
    fake = FakeSession()
    adapter = _router(fake, pr=[True], verdict=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build",
                                          pool="claude", complexity="deep", effort="high",
                                          source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    review_submission = _review("7", pool="codex", builder_lineage="claude",
                                target="head-sha", transfer_from=build)

    crashed = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    crashed._store._conn = _CommitFault(crashed._store._conn, "after")
    with pytest.raises(RuntimeError, match="after successor commit"):
        crashed.submit_stage(review_submission)
    crashed._store.close()

    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    records = {r.identity: r for r in _records(restarted)}
    review = "o/r|7|review|head-sha"
    assert set(records) == {build, review}
    assert records[build].retired is True and records[build].claim is False
    assert records[review].retired is False and records[review].claim is True
    assert tracer.owned_issues(list(records.values()), "o/r") == {7}
    assert restarted.submit_stage(review_submission) == review
    assert len(_records(restarted)) == 2


def test_successor_transfer_is_the_same_transition_for_review_to_revise(make_coord):
    fake = FakeSession()
    adapter = _router(fake, pr=[False], verdict=[True], prep=[True])
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    review_revision = "e" * 40
    review = coord.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                        target=review_revision))
    coord.cycle("codex")
    fake.end(review, cause=ProviderCause.PROCESS)
    coord.cycle("codex")

    revise = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="revise", pool="claude", target=review_revision,
        builder_lineage="claude", source="/wt/issue-7", transfer_from=review,
        subject_revision="f" * 40))
    records = {r.identity: r for r in _records(coord)}
    assert records[review].retired is True and records[review].claim is False
    assert records[revise].retired is False and records[revise].claim is True
    assert records[revise].subject_revision == "f" * 40
    review_route = (records[review].route_id, records[review].route_cell_digest,
                    records[review].launch_config_digest)
    revise_route = (records[revise].route_id, records[revise].route_cell_digest,
                    records[revise].launch_config_digest)
    assert all(review_route) and all(revise_route) and revise_route != review_route


def test_existing_successor_assumes_claim_durably_without_duplication(make_coord):
    fake = FakeSession()
    adapter = _router(fake, pr=[True], verdict=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build",
                                          pool="claude", complexity="deep", effort="high",
                                          source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    review_without_claim = _review("7", pool="codex", builder_lineage="claude",
                                   target="head-sha")
    review_without_claim = replace(review_without_claim, claim=False)
    review = coord.submit_stage(review_without_claim)

    coord.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                               target="head-sha", transfer_from=build))
    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    records = {r.identity: r for r in _records(restarted)}
    assert set(records) == {build, review}
    assert records[build].retired is True and records[build].claim is False
    assert records[review].claim is True


def test_store_failure_during_submission_preserves_predecessor_ownership(make_coord):
    fake = FakeSession()
    adapter = _router(fake, pr=[True], verdict=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build",
                                          pool="claude", complexity="deep", effort="high",
                                          source="/wt/issue-7"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    coord._store.close()

    with pytest.raises(RuntimeError):
        coord.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                   target="head-sha", transfer_from=build))

    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    records = {r.identity: r for r in _records(restarted)}
    assert set(records) == {build}
    assert records[build].claim is True and records[build].retired is False


def test_production_reconciliation_recovers_completed_build_handoff_after_restart(
        make_coord, monkeypatch):
    """Kill the daemon after Build completion, then recover only through the production pass."""
    fake = FakeSession()
    pr, verdict, prep = [True], [False], [True]
    adapter = _router(fake, pr=pr, verdict=verdict, prep=prep)
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="build", pool="claude", complexity="deep",
        effort="high", source="/work/.agentflow/worktrees/claude/issue-7-recover-handoff",
        input_ptr="Issue acceptance"))
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")

    # The process dies before production consumes that cycle's outcome. A fresh coordinator
    # must rediscover the durable Build instead of relying on an in-memory outcome.
    restarted = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    list_calls = [0]

    def list_open_prs(repo, *, head=None, limit=100):
        list_calls[0] += 1
        if list_calls[0] == 1:               # the first pass cannot reach GitHub
            return None
        return [github.PrRow(42, head or "", "head-a")]

    monkeypatch.setattr("agentflow.github.list_open_prs", list_open_prs)
    # The PR the opener resolves is an ordinary one: no depth proposal of its own, nothing
    # sensitive in its surface, so the review it opens gets the policy's default depth.
    monkeypatch.setattr("agentflow.github.pr_content",
                        lambda _repo, _pr: github.PrContent(
                            body="Fixed the thing.", paths=("agentflow/widget.py",), comments=[]))
    # The in-flight review's live head still sits on the SHA it was opened against, so the
    # diverged-review reconciler finds nothing to resettle and leaves the record alone.
    monkeypatch.setattr("agentflow.github.pr_facts",
                        lambda _repo, _pr: github.PrFacts(
                            head_ref_name="agentflow/claude/issue-7-recover-handoff",
                            head_ref_oid="head-a", state="OPEN", closing_issues=(7,)))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)
    # An unreadable PR fails closed: Build still owns the change and a later pass retries.
    pipeline.reconcile_and_project(restarted)
    assert record_of(restarted, build).claim is True
    assert record_of(restarted, build).retired is False

    pipeline.reconcile_and_project(restarted)
    review = "o/r|7|review|head-a"
    assert record_of(restarted, build).retired is True
    assert record_of(restarted, build).claim is False
    assert record_of(restarted, review).state == "waiting"
    assert record_of(restarted, review).claim is True

    # Repeated reconciliation/restart neither creates another record nor another provider.
    pipeline.reconcile_and_project(restarted)
    restarted_again = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    pipeline.reconcile_and_project(restarted_again)
    assert list(fake.family_of).count(review) == 1
    assert len([r for r in _records(restarted_again) if r.stage == "review"]) == 1
    # The Build → Review opener resolved the branch's PR exactly once (a second readable pass);
    # later passes only re-read the in-flight review's live head to detect a moved head (#208),
    # never re-firing the opener.
    assert list_calls[0] == 2


# --- the production Review adapter wiring (issue #120) -----------------------------------

class _ReviewerObserver:
    """The injected provider edge: the reviewer's captured final message, exactly what the
    production observer reconstructs from the attempt's durable session artifacts."""

    def __init__(self):
        self.final_message = ""

    def observe(self, record):
        return ProviderObservation(cause=ProviderCause.PROCESS,
                                   final_message=self.final_message)


def test_production_verdict_wiring_completes_only_on_the_exact_reviewed_sha(make_coord):
    """The PRODUCTION verdict edge (issue #120): ``coordinated_review._verdict_ready`` wired as the
    Review adapter's verifier and driven through ``submit_stage``/``cycle`` — not a direct private
    call. A durable verdict naming another SHA keeps the review incomplete; the exact reviewed SHA
    completes it even on a bad provider exit (ADR 0028 outcome-first)."""
    fake = FakeSession()
    reviewer = _ReviewerObserver()
    adapter = ReviewStageAdapter(verdict_ready=coordinated_review._verdict_ready,
                                 worktree_reset=lambda r: True, observer=reviewer)
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review(target="sha-a"))
    coord.cycle("claude")
    reviewer.final_message = '{"verdict": "PASS", "reviewed_sha": "sha-b", "findings": []}'
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert coord.cycle("claude") == []                # a verdict for another SHA is not this one's
    assert record_of(coord, ident).continuation

    reviewer.final_message = '{"verdict": "PASS", "reviewed_sha": "sha-a", "findings": []}'
    fake.end(ident, cause=ProviderCause.PROCESS)      # bad exit; the exact-SHA verdict is durable
    assert [o.status for o in coord.cycle("claude")] == ["completed"]


def _git(cwd, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, text=True,
                          capture_output=True).stdout.strip()


def _repo_with_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "agentflow@example.com")
    _git(repo, "config", "user.name", "agentflow test")
    (repo / "README.md").write_text("start\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "start")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def test_production_checkout_continuation_preserves_review_fixes_at_the_exact_sha(make_coord,
                                                                                   tmp_path):
    """The PRODUCTION checkout edge (issue #120): ``coordinated_review._review_worktree_reset``
    wired as the Review adapter's prepare and driven through admission over a real git repo. It
    creates a detached writable checkout at the record's immutable target SHA, and a continuation
    preserves local review work even after the branch moved."""
    repo = _repo_with_origin(tmp_path)
    reviewed_sha = _git(repo, "rev-parse", "HEAD")
    wt = repo / ".agentflow" / "worktrees" / "codex-review" / "pr-42-x"
    fake = FakeSession()
    adapter = ReviewStageAdapter(verdict_ready=lambda r, o: False,
                                 worktree_reset=coordinated_review._review_worktree_reset,
                                 observer=fake)
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review(target=reviewed_sha, source=str(wt)))
    coord.cycle("claude")                              # admission runs the production prepare
    assert record_of(coord, ident).state == "running"
    assert _git(wt, "rev-parse", "HEAD") == reviewed_sha   # the exact reviewed SHA
    assert _git(wt, "branch", "--show-current") == ""      # detached — review holds no branch

    # The reviewer leaves a partial fix and the branch moves on; continuation preparation keeps the
    # partial fix and same starting SHA rather than resetting its work away.
    (wt / "stale.txt").write_text("leftover")
    (repo / "README.md").write_text("moved\n")
    _git(repo, "commit", "-am", "branch moves on")
    _git(repo, "push", "origin", "main")
    fake.end(ident, cause=ProviderCause.PROCESS)       # interrupted with no verdict → continuation
    coord.cycle("claude")                              # re-admission re-runs the production prepare
    assert record_of(coord, ident).attempts == 2
    assert _git(wt, "rev-parse", "HEAD") == reviewed_sha
    assert (wt / "stale.txt").exists()                 # partial review work survives continuation


def test_production_reset_self_heals_an_orphaned_review_checkout_dir(tmp_path):
    """Issue #171: a review checkout dir that exists on disk but whose git metadata is gone
    (orphaned — e.g. a daemon killed mid-prepare) must not stall admission forever. Driven
    through ``_review_worktree_reset``, the reset discards the orphaned dir and rebuilds a valid
    detached checkout at the immutable target SHA, returning True instead of False. Fails against
    main, where the unchecked ``worktree remove`` leaves the orphan and the rebuild raises."""
    repo = _repo_with_origin(tmp_path)
    reviewed_sha = _git(repo, "rev-parse", "HEAD")
    wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-42-x"
    _git(repo, "worktree", "add", "--detach", str(wt), reviewed_sha)
    shutil.rmtree(repo / ".git" / "worktrees" / "pr-42-x")  # orphan: metadata gone, dir remains
    assert wt.exists() and not _worktree_registered(repo, wt)

    record = SimpleNamespace(repo="o/r", source=str(wt), target=reviewed_sha, pool="claude")
    assert coordinated_review._review_worktree_reset(record)
    assert _worktree_registered(repo, wt)
    assert _git(wt, "rev-parse", "HEAD") == reviewed_sha
    assert _git(wt, "branch", "--show-current") == ""  # detached — review holds no branch


def test_production_reset_ignores_a_leftover_other_tool_checkout_of_the_same_pr(tmp_path):
    """Issue #171: the codex and claude review checkouts share the ``pr-<n>-<slug>`` basename, so a
    leftover other-tool checkout must not block creating this tool's. The reset builds the claude
    checkout while the registered codex one for the same PR is left untouched."""
    repo = _repo_with_origin(tmp_path)
    reviewed_sha = _git(repo, "rev-parse", "HEAD")
    codex_wt = repo / ".agentflow" / "worktrees" / "codex-review" / "pr-42-x"
    _git(repo, "worktree", "add", "--detach", str(codex_wt), reviewed_sha)  # leftover other tool
    claude_wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-42-x"

    record = SimpleNamespace(repo="o/r", source=str(claude_wt), target=reviewed_sha, pool="claude")
    assert coordinated_review._review_worktree_reset(record)
    assert _git(claude_wt, "rev-parse", "HEAD") == reviewed_sha
    assert codex_wt.exists() and _worktree_registered(repo, codex_wt)  # other tool untouched


def _stuck_review_coord(make_coord, wt, target, logs):
    """A coordinator whose Review stage prepares through the real production checkout code, so
    the breadcrumb cadence and the checkout's own refusal are exercised as one piece."""
    fake = FakeSession()
    review = ReviewStageAdapter(verdict_ready=lambda r, o: False,
                                worktree_reset=coordinated_review._review_worktree_reset,
                                observer=fake)
    coord = make_coord(fake, adapter=StageRouter({"review": review}),
                       gate=tracer.build_review_revise_gate, log=logs.append)
    coord.submit_stage(_review(subject="99", target=target, source=str(wt)))
    return coord


def test_a_review_checkout_that_keeps_failing_names_the_check_and_the_path(
        tmp_path, monkeypatch, make_coord):
    """Issue #171/#405: a genuinely stuck review (one whose checkout never succeeds) must become
    visible rather than no-op'ing admission silently every cycle — and it must say *what* refused.
    The first miss can be transient and stays quiet; a repeat surfaces once naming the failing
    check and the offending checkout path, then re-reminds periodically. The old line said only
    that admission was stuck, which is what sent #397/#399 to a human with nothing to go on."""
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-99-x"
    # A target that still exists: the checkout, not the reviewed head, is what is broken.
    live = _git(repo, "rev-parse", "HEAD")
    monkeypatch.setattr(
        "agentflow.runner.ClaudeRunner.prepare_worktree_detached",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["git", "worktree", "add"])))
    logs: list[str] = []
    coord = _stuck_review_coord(make_coord, wt, live, logs)

    def breadcrumbs():
        return [line for line in logs if "unprepared for" in line]

    coord.cycle("claude")
    assert breadcrumbs() == []                              # a single miss can be transient
    coord.cycle("claude")
    assert len(breadcrumbs()) == 1                          # the repeat is surfaced
    assert "checkout-failed" in breadcrumbs()[0]            # the check that refused, by name
    assert str(wt) in breadcrumbs()[0]                      # and the checkout it refused on
    assert "admission is stuck" not in breadcrumbs()[0]
    for _ in range(9):  # misses 3..11 stay quiet — one breadcrumb, not one per cycle
        coord.cycle("claude")
    assert len(breadcrumbs()) == 1
    coord.cycle("claude")                                   # the 12th re-reminds
    assert len(breadcrumbs()) == 2

    published = tracer.refusal_projection(_records(coord))
    assert len(published) == 1 and published[0]["refusal"].startswith("checkout-failed: ")
    assert published[0]["expected"] is False


def test_a_review_checkout_held_by_a_live_sibling_session_is_contention_not_stuck(
        tmp_path, make_coord):
    """A checkout occupied by a live session — the superseded review still finishing while its
    successor record waits its turn — is ordinary contention, not a stuck checkout. Admission
    skips quietly every cycle and succeeds as soon as the sibling releases the worktree; no
    breadcrumb ever pages a human after a checkout that is working exactly as intended. The
    contention is still published, marked expected, so the board can show it without alarm."""
    repo = _repo_with_origin(tmp_path)
    live = _git(repo, "rev-parse", "HEAD")
    wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-97-x"
    _git(repo, "worktree", "add", "--detach", str(wt), live)
    marker = Path(_git(wt, "rev-parse", "--git-path", "agentflow-active"))
    marker = marker if marker.is_absolute() else wt / marker
    marker.write_text(str(os.getpid()))               # a live sibling session holds the checkout
    logs: list[str] = []
    coord = _stuck_review_coord(make_coord, wt, live, logs)

    for _ in range(12):  # far past every surfacing threshold — contention never pages
        coord.cycle("claude")
    assert [line for line in logs if "unprepared for" in line] == []
    published = tracer.refusal_projection(_records(coord))
    assert len(published) == 1 and published[0]["expected"] is True
    assert published[0]["refusal"].startswith("sibling-active: ")

    marker.unlink()                                   # the sibling session ends
    assert coordinated_review._review_worktree_reset(
        SimpleNamespace(repo="o/r", source=str(wt), target=live, pool="claude"))
    assert _git(wt, "rev-parse", "HEAD") == live


def test_a_review_whose_head_was_rebased_away_reads_as_awaiting_retarget_not_stuck(
        tmp_path, monkeypatch, make_coord):
    """A reviewed head that has been rebased or amended away leaves a record no human can clear —
    the diverged-review reconciler supersedes it at the live head once a reviewer pool has
    headroom. Reporting that as a stuck checkout sends someone after a checkout that is fine."""
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-98-x"
    gone = "0" * 40
    monkeypatch.setattr(
        "agentflow.runner.ClaudeRunner.prepare_worktree_detached",
        lambda *a, **k: (_ for _ in ()).throw(
            subprocess.CalledProcessError(1, ["git", "reset"])))
    logs: list[str] = []
    coord = _stuck_review_coord(make_coord, wt, gone, logs)

    coord.cycle("claude")
    coord.cycle("claude")
    breadcrumbs = [line for line in logs if "unprepared for" in line]
    assert len(breadcrumbs) == 1
    assert "reviewed-head-gone" in breadcrumbs[0]
    assert "awaiting retarget" in breadcrumbs[0] and gone[:12] in breadcrumbs[0]
    assert "admission is stuck" not in breadcrumbs[0]


def _worktree_registered(repo: Path, wt: Path) -> bool:
    listing = _git(repo, "worktree", "list", "--porcelain")
    target = wt.resolve()
    return any(Path(line.removeprefix("worktree ")).resolve() == target
               for line in listing.splitlines() if line.startswith("worktree "))


def test_production_park_resolves_the_pr_from_the_review_worktree_and_parks_once(make_coord,
                                                                                 monkeypatch):
    """The PRODUCTION park edge (issue #120): ``pr_park.park_pr`` wired as the Review
    adapter's handoff and driven to exhaustion through ``cycle``. The PR number comes from the
    review worktree path (no GitHub lookup); the park comment is the durable proof, so a restart
    re-observes it and never parks or notifies twice."""
    parked, notified, pr_comments = [], [], []

    def _park(repo, pr, verdict, *, reason, missing_outcome, context, proof_marker):
        parked.append((repo, pr))
        pr_comments.append({
            "body": f"> *agentflow: parked for human review.*\n<!-- {proof_marker} -->"})

    monkeypatch.setattr("agentflow.gate.park", _park)
    monkeypatch.setattr("agentflow.github.pr_comments",
                        lambda repo, pr: [github.Comment(body=c["body"], created_at="")
                                          for c in pr_comments])
    monkeypatch.setattr("agentflow.notify.notify", lambda *a, **k: notified.append(a))

    fake = FakeSession()
    adapter = ReviewStageAdapter(verdict_ready=lambda r, o: False, worktree_reset=lambda r: True,
                                 observer=fake, handoff=pr_park.park_pr)
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review(source="/w/.agentflow/worktrees/codex-review/pr-42-x"))
    outcome = None
    for _ in range(8):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held" and outcome.handoff == "pr:parked"
    assert parked == [("o/r", 42)] and len(notified) == 1   # parked and notified exactly once
    rec = record_of(coord, ident)
    assert rec.state == "held" and rec.claim is False and rec.handoffs == 1

    # Idempotent across a restart: the durable park comment is the proof — no second park/notify.
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert parked == [("o/r", 42)] and len(notified) == 1


def test_park_refuses_when_the_pr_thread_is_unreadable(monkeypatch):
    """Fail-closed (ADR 0040/0042): a comments read that could not reach GitHub stays *unknown*, so
    the park refuses to act — it neither posts the marker, proves it, nor pings — rather than reading
    the silence as an empty thread and parking blind. Exercises ``_park_pr`` through the shared
    :class:`DurableHandoff` envelope."""
    record = _completed_review_record()
    parked, notified = [], []
    monkeypatch.setattr("agentflow.github.pr_comments", lambda _repo, _pr: None)
    monkeypatch.setattr("agentflow.gate.park", lambda *a, **k: parked.append(a))
    monkeypatch.setattr("agentflow.notify.notify", lambda *a, **k: notified.append(a) or True)

    assert pr_park.park_pr(record) is None
    assert parked == [] and notified == []               # unreadable stays unknown — no blind park


# --- all production stages share the one gate --------------------------------------------

def test_all_stages_use_the_same_gate_and_pool_budget(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_router(fake, pr=[False], verdict=[False], prep=[True]),
                       gate=tracer.build_review_revise_gate)
    # The PR-bound review (1 permit) and revise (3) drain first (ADR 0039) and fill four permits.
    # Build is enabled by the same gate, but its low-effort demand (4) waits because the immutable
    # five-permit pool budget is full.
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="low", source="/wt/issue-7"))
    review = coord.submit_stage(_review("8", pool="claude", builder_lineage="codex"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise", pool="claude",
                                           builder_lineage="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, review).state == "running"
    assert record_of(coord, revise).state == "running"
    build_rec = record_of(coord, build)
    assert build_rec.state == "waiting" and build_rec.attempts == 0


def _completed_review_record(*, profile="reviewed"):
    return Record(
        identity=f"o/r|7|review|sha-a|{profile}", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="sha-a", builder_lineage="claude",
        change_author_tool="claude",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix", state="completed",
        auto_merge_allowed=True)


def test_clean_reviewed_settlement_posts_one_summary_and_returns_durable_proof(monkeypatch):
    from agentflow.reviewer import Verdict

    record = _completed_review_record()
    comments = []
    summarized = []
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "reviewed")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: list(comments))
    monkeypatch.setattr("agentflow.github.pr_comments",
                        lambda _repo, _pr: [github.Comment(body=c["body"], created_at="")
                                            for c in comments])

    monkeypatch.setattr(
        "agentflow.gate.post_clean_review_summary",
        lambda repo, pr, verdict, head: summarized.append((repo, pr, head)) or True)
    monkeypatch.setattr("agentflow.coordinated_review._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: github.HeadChecks(sha=sha))

    proof = coordinated_review._settle_review(record)
    assert proof == "https://github.com/o/r/pull/42"
    assert summarized == [("o/r", 42, "sha-a")]


def test_clean_taint_clearing_autonomous_review_reenters_full_merge_gate(monkeypatch):
    from agentflow.reviewer import Verdict

    record = _completed_review_record(profile="autonomous")
    record.review_tainted = True
    record.review_taint_cleared = True
    merged, finished, label_edits = [], [], []
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.gate.ci_is_green", lambda _repo, _pr, **_kwargs: True)
    monkeypatch.setattr("agentflow.gate.ui_evidence_gap", lambda *_args: False)
    monkeypatch.setattr("agentflow.gate.reply_pending", lambda _comments: False)
    monkeypatch.setattr("agentflow.gate.squash_merge",
                        lambda _repo, pr: merged.append(pr) or True)
    monkeypatch.setattr("agentflow.coordinated_review._finish_review",
                        lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr("agentflow.github.remove_label",
                        lambda repo, issue, label: label_edits.append((issue, label)) or True)
    reads = iter((frozenset({"agentflow:building"}), frozenset()))
    monkeypatch.setattr("agentflow.github.issue_labels", lambda *_args: next(reads))
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: github.HeadChecks(sha=sha))
    coordinated_review._REVIEW_CI_OBSERVED[record.identity] = True

    assert coordinated_review._settle_review(record) == "https://github.com/o/r/pull/42"
    assert merged == [42] and len(finished) == 1
    assert label_edits == [(7, "agentflow:building"), ("7", "ready-for-agent")]


def test_already_merged_review_settlement_releases_build_claim(monkeypatch):
    from agentflow.reviewer import Verdict

    record = _completed_review_record()
    label_edits, finished = [], []
    reads = iter((frozenset({"agentflow:building"}), frozenset()))
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "merged-sha", "state": "MERGED"})
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.github.issue_labels", lambda *_args: next(reads))
    monkeypatch.setattr("agentflow.github.remove_label",
                        lambda _repo, issue, label: label_edits.append((issue, label)) or True)
    monkeypatch.setattr("agentflow.coordinated_review._finish_review",
                        lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)

    assert coordinated_review._settle_review(record) == "https://github.com/o/r/pull/42"
    assert label_edits == [(7, "agentflow:building"), ("7", "ready-for-agent")]
    assert len(finished) == 1


def test_merged_clean_review_summary_is_stamped_with_its_reviewed_head(monkeypatch):
    from agentflow.reviewer import Verdict

    record = _completed_review_record()
    summarized = []
    reads = iter((frozenset({"agentflow:building"}), frozenset()))
    monkeypatch.setattr(coordinated_review, "_review_verdict",
                        lambda _r: Verdict(clean=True, change_author_tool="claude"))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "merged-sha", "state": "MERGED"})
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.github.issue_labels", lambda *_args: next(reads))
    monkeypatch.setattr("agentflow.github.remove_label", lambda *_args: True)
    monkeypatch.setattr("agentflow.gate.post_clean_review_summary",
                        lambda repo, pr, verdict, head: summarized.append((repo, pr, head)) or True)
    monkeypatch.setattr("agentflow.coordinated_review._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)

    assert coordinated_review._settle_review(record) == "https://github.com/o/r/pull/42"
    assert summarized == [("o/r", 42, "sha-a")]


@pytest.mark.parametrize("pr_state", ["MERGED", "OPEN"])
def test_merged_review_settlement_stays_unsettled_when_build_claim_release_fails(
        monkeypatch, pr_state):
    from agentflow.reviewer import Verdict

    record = _completed_review_record(profile="autonomous")
    finished, ready_removals = [], []
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": pr_state})
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile",
                        lambda _workdir: "autonomous")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.coordinated_review._finish_review",
                        lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    monkeypatch.setattr(coordinated_review, "release", lambda *_args: False)
    monkeypatch.setattr("agentflow.github.remove_label",
                        lambda _repo, issue, label: ready_removals.append((issue, label)) or True)

    if pr_state == "OPEN":
        monkeypatch.setattr("agentflow.gate.ci_is_green", lambda *args, **kwargs: True)
        monkeypatch.setattr("agentflow.gate.ui_evidence_gap", lambda *_args: False)
        monkeypatch.setattr("agentflow.gate.reply_pending", lambda _comments: False)
        monkeypatch.setattr("agentflow.gate.squash_merge", lambda *_args: True)
        monkeypatch.setattr("agentflow.github.commit_head_checks",
                            lambda _repo, sha: github.HeadChecks(sha=sha))
        coordinated_review._REVIEW_CI_OBSERVED[record.identity] = True

    try:
        assert coordinated_review._settle_review(record) is None
    finally:
        coordinated_review._REVIEW_CI_OBSERVED.pop(record.identity, None)
    assert finished == [] and ready_removals == []


def test_review_authored_fix_settles_only_at_the_final_reviewed_head(monkeypatch):
    """A reviewer may start at sha-a, push sha-b, and merge only after re-reviewing sha-b. The
    immutable stage target still proves the starting diff; ``final_sha`` is the merge boundary."""
    from agentflow.reviewer import Verdict

    record = _completed_review_record(profile="autonomous")
    verdict = Verdict(
        clean=True, reviewed_sha="sha-a", final_sha="sha-b", pushed_sha="sha-b",
        fixes=("Removed the stale helper",))
    merged = []
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: verdict)
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-b", "state": "OPEN"})
    monkeypatch.setattr(coordinated_review, "_review_pr_head", lambda _r: "sha-b")
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.coordinated_review._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.gate.ci_is_green", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentflow.gate.ui_evidence_gap", lambda *_args: False)
    monkeypatch.setattr("agentflow.gate.reply_pending", lambda _comments: False)
    monkeypatch.setattr("agentflow.gate.squash_merge",
                        lambda _repo, pr: merged.append(pr) or True)
    monkeypatch.setattr("agentflow.github.remove_label", lambda *_args: True)
    monkeypatch.setattr("agentflow.github.issue_labels", lambda *_args: frozenset())
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: github.HeadChecks(sha=sha))
    coordinated_review._REVIEW_CI_OBSERVED[record.identity] = True

    assert coordinated_review._settle_review(record) == "https://github.com/o/r/pull/42"
    assert merged == [42]


def _settle_autonomous_clean_review(monkeypatch, *, surfaces, content, comments, ci_green=True,
                                    merges=False):
    """Settle one clean, exact-head, autonomous PR and report what the merge gate was asked.

    Everything the settlement reads is fixed except the two facts under test — what the PR's diff
    and attachments say about screenshots, and whether a maintainer question is outstanding — so a
    park here is the merge gate's own answer, not a second copy of the rule in the caller.

    ``content`` is the PR's reviewable content as the gate reads it (:class:`github.PrContent`):
    the paths it changes, what its body says, and the comments already on it. Pass ``merges`` for
    the case where nothing should block — the merge is then let through and reported, rather than
    failing the test the way an unexpected merge must.
    """
    from agentflow import gate
    from agentflow.review_policy import UIVerification
    from agentflow.reviewer import Verdict

    record = _completed_review_record(profile="autonomous")
    asked, parked, posted, merged = [], [], [], []
    decide = gate.decide_merge

    def _spy(**kwargs):
        asked.append(kwargs)
        return decide(**kwargs)

    def _park(_repo, _pr, _verdict, *, reason, proof_marker, **_kwargs):
        parked.append(reason)
        posted.append(f"> *agentflow: parked for human review.*\n<!-- {proof_marker} -->")

    monkeypatch.setattr(
        coordinated_review, "_review_verdict",
        lambda _r: Verdict(
            clean=True,
            ui_verification=(UIVerification.PASSED if surfaces
                             else UIVerification.NOT_REQUIRED)))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: list(surfaces))
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: list(comments))
    monkeypatch.setattr("agentflow.github.pr_content", lambda _repo, _pr: content)
    monkeypatch.setattr("agentflow.github.pr_comments",
                        lambda _repo, _pr: [github.Comment(body=body, created_at="")
                                            for body in posted])
    monkeypatch.setattr("agentflow.gate.decide_merge", _spy)
    monkeypatch.setattr("agentflow.gate.park", _park)
    monkeypatch.setattr("agentflow.gate.squash_merge",
                        (lambda _repo, pr: merged.append(pr) or True) if merges else
                        (lambda *_args, **_kwargs: pytest.fail("a blocked PR must never merge")))
    if merges:
        monkeypatch.setattr("agentflow.gate.ci_is_green", lambda *args, **kwargs: True)
        monkeypatch.setattr("agentflow.github.remove_label", lambda *_args: True)
        monkeypatch.setattr("agentflow.github.issue_labels", lambda *_args: frozenset())
    monkeypatch.setattr("agentflow.coordinated_review._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: github.HeadChecks(sha=sha))
    coordinated_review._REVIEW_CI_OBSERVED[record.identity] = ci_green

    proof = coordinated_review._settle_review(record)
    coordinated_review._REVIEW_CI_OBSERVED.pop(record.identity, None)
    return SimpleNamespace(proof=proof, asked=asked, parked=parked, merged=merged)


def test_screenshotless_ui_change_is_blocked_by_the_gate_it_is_reported_to(monkeypatch):
    """A clean autonomous PR that touches a declared user-facing surface with no before/after
    screenshot must not merge — and the merge gate must be the thing that says so, so the fact
    reaches it instead of being ruled on before the question is asked (ADR 0018)."""
    from agentflow.prompts import UI_GAP_REASON

    settled = _settle_autonomous_clean_review(
        monkeypatch,
        surfaces=["agentflow/webui/src/"],
        content=github.PrContent(body="Renamed the queue column.",
                                 paths=("agentflow/webui/src/App.svelte",), comments=[]),
        comments=[])

    assert settled.asked and settled.asked[0]["ui_evidence_missing"] is True
    assert settled.parked == [UI_GAP_REASON]
    assert settled.proof == "https://github.com/o/r/pull/42"


def test_a_ui_change_that_carries_its_screenshot_is_not_reported_as_a_gap(monkeypatch):
    """The other half of the same rule: the identical PR, with the before/after screenshot
    committed alongside the change, reaches the merge gate with no gap to report. Without this
    the screenshot case and the screenshotless one are indistinguishable."""
    settled = _settle_autonomous_clean_review(
        monkeypatch,
        surfaces=["agentflow/webui/src/"],
        content=github.PrContent(body="Renamed the queue column.",
                                 paths=("agentflow/webui/src/App.svelte",
                                        "docs/screenshots/queue-column-after.png"), comments=[]),
        comments=[], merges=True)

    assert settled.asked and settled.asked[0]["ui_evidence_missing"] is False
    assert settled.parked == [] and settled.merged == [42]


def test_unanswered_maintainer_question_is_blocked_by_the_gate_it_is_reported_to(monkeypatch):
    """Nothing merges while a maintainer question hangs on the PR (issue #18) — and, as with the
    screenshot gap, the merge gate is asked the question rather than told the answer."""
    settled = _settle_autonomous_clean_review(
        monkeypatch,
        surfaces=[],
        content=github.PrContent(body="", paths=(), comments=[]),
        comments=[{"id": "9001", "body": "Why did this drop the retry?"}])

    assert settled.asked and settled.asked[0]["reply_pending"] is True
    assert settled.parked == ["could not be auto-merged after review"]


def test_an_unanswered_question_outranks_red_ci_in_the_park_notice(monkeypatch):
    """A maintainer whose question is still hanging must not be told the build failed. Both are
    true, but only one of them is theirs to act on, and the park comment is where they read it."""
    settled = _settle_autonomous_clean_review(
        monkeypatch,
        surfaces=[],
        content=github.PrContent(body="", paths=(), comments=[]),
        comments=[{"id": "9002", "body": "Should this keep the old default?"}],
        ci_green=False)

    assert settled.parked == ["could not be auto-merged after review"]


def test_review_settlement_releases_claim_through_public_coordinator_seam(make_coord, monkeypatch):
    from agentflow.reviewer import Verdict

    fake = FakeSession()
    comments = []
    summarized = []
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "reviewed")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.coordinated_review._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: list(comments))
    monkeypatch.setattr("agentflow.github.pr_comments",
                        lambda _repo, _pr: [github.Comment(body=c["body"], created_at="")
                                            for c in comments])
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: github.HeadChecks(sha=sha))

    monkeypatch.setattr(
        "agentflow.gate.post_clean_review_summary",
        lambda repo, pr, verdict, head: summarized.append((repo, pr, head)) or True)
    adapter = ReviewStageAdapter(
        verdict_ready=lambda _record, _obs: True, worktree_reset=lambda _record: True,
        observer=fake, settle=coordinated_review._settle_review,
        prepare_settle=coordinated_review._prepare_review_settlement)
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review(
        target="sha-a", source="/work/.agentflow/worktrees/codex-review/pr-42-fix"))
    coord.cycle("claude")
    fake.end(ident, success=True, cause=ProviderCause.PROCESS)
    assert [outcome.status for outcome in coord.cycle("claude")] == ["completed"]
    assert record_of(coord, ident).claim is True

    coord.cycle("claude")
    settled = record_of(coord, ident)
    assert settled.retired is True and settled.claim is False
    assert summarized == [("o/r", 42, "sha-a")]
    make_coord(fake, adapter=adapter).cycle("claude")
    assert summarized == [("o/r", 42, "sha-a")]


def test_completed_product_review_keeps_its_verdict_when_provider_artifacts_disappear(
        make_coord, monkeypatch):
    """A verified Product pass must carry its exact verdict durably into the private Standards
    handoff. Provider session artifacts are observation inputs, not the completed-stage outcome:
    if they disappear after completion, reconciliation must neither reinterpret the pass as a
    legacy blocking review nor park a PR whose revise budget is already spent."""
    from agentflow.review_policy import ReviewAssignment, ReviewAxis, ReviewDepth

    payload = json.dumps({
        "verdict": "PASS", "depth": "full", "depth_reason": "shared behavior",
        "axis": "product", "change_author_tool": "claude", "reviewed_sha": "sha-a",
        "final_sha": "sha-a", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["product behavior checked"], "decision": "", "findings": [],
        "uncertainty": None,
    })

    class CompletedArtifact:
        def observe(self, _record):
            return ProviderObservation(
                final_message=payload, cause=ProviderCause.NONE, has_end_fact=True)

    class MissingArtifact:
        def observe(self, _record):
            return ProviderObservation()

    fake = FakeSession()
    parked = []
    adapter = ReviewStageAdapter(
        verdict_ready=coordinated_review._verdict_ready,
        worktree_reset=lambda _record: True,
        observer=CompletedArtifact(),
        handoff=lambda record: parked.append(record.identity) or f"proof:{record.identity}",
        settle=coordinated_review._settle_review,
        prepare_settle=coordinated_review._prepare_review_settlement)
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="review", pool="codex", complexity="deep",
        target="sha-a", builder_lineage="claude", round=MAX_REVISES,
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        input_ptr="Review PR #42",
        review=ReviewState(
            assignment=ReviewAssignment(
                ReviewDepth.FULL, "shared behavior", ReviewAxis.PRODUCT),
            change_author_tool="claude")))
    coord.cycle("codex")
    fake.end(ident, success=True, cause=ProviderCause.NONE)
    assert [outcome.status for outcome in coord.cycle("codex")] == ["completed"]

    # The provider's session can no longer supply the terminal message after completion.
    monkeypatch.setattr("agentflow.coordinator.providers.ProviderObserver", MissingArtifact)
    monkeypatch.setattr(
        coordinated_review, "_review_pr_facts",
        lambda _record: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "reviewed")

    pipeline.reconcile_and_project(coord)

    completed = record_of(coord, ident)
    assert completed.outcome == payload
    assert completed.retired is True and completed.claim is False
    successors = [
        record for record in _records(coord)
        if record.stage == "review" and not record.retired
    ]
    assert len(successors) == 1
    assert successors[0].review_axis == "standards" and successors[0].claim is True
    assert parked == []


def test_completed_conflict_decision_transfers_to_revise_before_settlement(
        make_coord, monkeypatch):
    """A grounded private conflict decision must return to the original builder for application.
    It is not a clean Review settlement: reconciliation transfers the claim to conflict Revise
    before any summary, park, or merge policy can consume the decision pass."""
    from agentflow.review_policy import ReviewAssignment, ReviewAxis, ReviewDepth

    payload = json.dumps({
        "verdict": "PASS", "depth": "full",
        "depth_reason": "competing product behaviors in a conflict",
        "axis": "decision", "change_author_tool": "claude", "reviewed_sha": "sha-a",
        "final_sha": "sha-a", "pushed_sha": "", "fixes": [], "follow_ups": [],
        "checks": ["competing behaviors traced"],
        "decision": "keep main: the shared rule owns ties", "findings": [],
        "uncertainty": None,
    })

    class CompletedArtifact:
        def observe(self, _record):
            return ProviderObservation(
                final_message=payload, cause=ProviderCause.NONE, has_end_fact=True)

    fake = FakeSession()
    parked, summarized = [], []
    adapter = ReviewStageAdapter(
        verdict_ready=coordinated_review._verdict_ready,
        worktree_reset=lambda _record: True,
        observer=CompletedArtifact(),
        handoff=lambda record: parked.append(record.identity) or f"proof:{record.identity}",
        settle=coordinated_review._settle_review,
        prepare_settle=coordinated_review._prepare_review_settlement)
    coord = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="review", pool="codex", complexity="deep",
        target="sha-a", builder_lineage="claude", builder_complexity="deep",
        conflict_round=1,
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        input_ptr="Resolve the private conflict decision for PR #42",
        review=ReviewState(
            assignment=ReviewAssignment(
                ReviewDepth.FULL, "competing product behaviors in a conflict",
                ReviewAxis.DECISION),
            change_author_tool="claude", uncertainty_handoffs=1)))
    coord.cycle("codex")
    fake.end(ident, success=True, cause=ProviderCause.NONE)
    assert [outcome.status for outcome in coord.cycle("codex")] == ["completed"]

    monkeypatch.setattr(
        coordinated_review, "_review_pr_facts",
        lambda _record: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "reviewed")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.coordinated_review._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "agentflow.gate.post_clean_review_summary",
        lambda repo, pr, verdict, head: summarized.append((repo, pr, head)) or True)
    monkeypatch.setattr(
        pipeline, "pick_session_lead",
            lambda **_kwargs: (SimpleNamespace(tool="claude"), None, ""))

    pipeline.reconcile_and_project(coord)

    completed = record_of(coord, ident)
    assert completed.retired is True and completed.claim is False
    successors = [
        record for record in _records(coord)
        if record.stage == "revise" and not record.retired
    ]
    assert len(successors) == 1
    assert successors[0].pool == "claude" and successors[0].conflict_round == 1
    assert successors[0].claim is True
    assert "shared rule owns ties" in successors[0].input_ptr
    assert summarized == [] and parked == []


def test_forced_same_tool_autonomous_review_posts_summary_without_waiting_for_ci(
        make_coord, monkeypatch):
    from agentflow.reviewer import Verdict

    fake = FakeSession()
    comments, summarized = [], []
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_review, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.coordinated_review._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.github.pr_comment_rows", lambda _repo, _pr: list(comments))
    monkeypatch.setattr("agentflow.github.pr_comments",
                        lambda _repo, _pr: [github.Comment(body=c["body"], created_at="")
                                            for c in comments])
    monkeypatch.setattr("agentflow.gate.ci_is_green",
                        lambda *args, **kwargs: pytest.fail("forced same-tool review skips CI merge"))
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)
    monkeypatch.setattr("agentflow.github.commit_head_checks",
                        lambda _repo, sha: github.HeadChecks(sha=sha))

    monkeypatch.setattr(
        "agentflow.gate.post_clean_review_summary",
        lambda repo, pr, verdict, head: summarized.append((repo, pr, head)) or True)
    adapter = ReviewStageAdapter(
        verdict_ready=lambda _record, _obs: True, worktree_reset=lambda _record: True,
        observer=fake, settle=coordinated_review._settle_review,
        prepare_settle=coordinated_review._prepare_review_settlement)
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review(
        pool="claude", builder_lineage="claude", target="sha-a",
        source="/work/.agentflow/worktrees/claude-review/pr-42-fix",
        review_tainted=True))
    coord.cycle("claude")
    fake.end(ident, success=True, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    coord.cycle("claude")

    settled = record_of(coord, ident)
    assert settled.auto_merge_allowed is False
    assert settled.retired is True and settled.claim is False
    assert summarized == [("o/r", 42, "sha-a")]


# --- pure Build → Review submission mapping ----------------------------------------------

def test_review_submission_binds_to_the_head_sha_and_assumes_the_build_claim():
    build = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5, repo="o/r",
                   subject="7", source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing",
                   capability_context="{", change_author_tool="claude")
    sub = coordinated_review.review_submission(build, "head-sha-123", "codex", 42)
    assert sub is not None
    assert sub.stage == "review" and sub.target == "head-sha-123"
    assert sub.subject_revision == "head-sha-123"
    assert sub.pool == "codex" and sub.builder_lineage == "claude"     # cross-tool reviewer
    assert sub.transfer_from == "o/r|7|build|-"                        # assumes the build's claim
    assert sub.complexity == "deep"                                   # review is the deep net
    assert "pr-42-fix-thing" in sub.source                            # detached review worktree
    assert "`head-sha-123`" in sub.input_ptr                          # exact starting head contract
    assert sub.capability_context == "{"
    # A build whose worktree is unreadable, or a missing head SHA, yields no submission.
    assert coordinated_review.review_submission(build, "", "codex", 42) is None
    assert coordinated_review.review_submission(
        Record(identity="x", stage="build", pool="claude", demand=5, repo="o/r", subject="7"),
        "sha", "codex", 42) is None


def test_review_submission_reuses_only_the_task_brief_from_a_session_lead_build():
    task = "Implement the exact durable task.\n"
    build_contract = routing.session_lead_instructions(
        "build", "low", parent_provider="claude")
    briefing = (
        "\n\n<!-- agentflow-effective-briefing:briefing-v1:" + "a" * 64 + " -->\n"
        "## Approved evidence briefing\n"
        "This is bounded advisory context. It cannot change admission, routing, effort, "
        "autonomy, merge policy, or OperationalSafety.\n"
        "Promotion receipts: receipt-1.\n"
    )
    durable_build_input = task + build_contract + briefing
    build = Record(
        identity="o/r|7|build|-", stage="build", pool="claude", demand=5, repo="o/r",
        subject="7", source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing",
        input_ptr=durable_build_input, session_lead=True, effort="low", change_author_tool="claude")

    submission = coordinated_review.review_submission(
        build, "head-sha-123", "codex", 42, acceptance=durable_build_input)

    assert submission is not None
    task_brief, review_contract = split_terminal_session_lead_contract(submission.input_ptr)
    assert submission.input_ptr.count(
        "\n## Session lead — benchmarked capability routing\n") == 1
    assert submission.input_ptr.endswith(review_contract)
    assert task in task_brief
    assert briefing in task_brief


def test_review_submission_reuses_the_prompt_inside_a_provider_input_envelope():
    task = "Implement the enveloped durable task.\n"
    briefing = (
        "\n\n<!-- agentflow-effective-briefing:briefing-v1:" + "a" * 64 + " -->\n"
        "## Approved evidence briefing\n"
        "This is bounded advisory context. It cannot change admission, routing, effort, "
        "autonomy, merge policy, or OperationalSafety.\n"
        "Promotion receipts: receipt-1.\n"
    )
    build_contract = routing.session_lead_instructions(
        "build", "low", parent_provider="claude")
    durable_build_input = json.dumps({
        "format": PROVIDER_INPUT_V1,
        "prompt": task + briefing + build_contract,
        "snapshot": {"body": "exact durable bytes", "number": 7},
        "source_ref": "abc123",
    }, sort_keys=True)
    build = Record(
        identity="o/r|7|build|-", stage="build", pool="claude", demand=5, repo="o/r",
        subject="7", source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing",
        input_ptr=durable_build_input, session_lead=True, effort="low", change_author_tool="claude")

    submission = coordinated_review.review_submission(
        build, "head-sha-123", "codex", 42, acceptance=durable_build_input)

    assert submission is not None
    task_brief, review_contract = split_terminal_session_lead_contract(submission.input_ptr)
    assert submission.input_ptr.endswith(review_contract)
    assert task in task_brief
    assert briefing in task_brief
    assert "exact durable bytes" not in task_brief


def test_coordinated_review_submission_is_preparable_as_a_session_lead(make_coord):
    """The Review opener records ownership of the generated lead contract before admission."""
    fake = FakeSession()
    coord = make_coord(
        fake, adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    build = Record(
        identity="o/r|7|build|-", stage="build", pool="claude", demand=5, repo="o/r",
        subject="7", source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing",
        change_author_tool="claude")
    submission = coordinated_review.review_submission(build, "head-sha-123", "codex", 42)

    assert submission.session_lead is True
    identity = coord.submit_stage(replace(submission, transfer_from=None))

    assert coord.cycle("codex") == []
    assert record_of(coord, identity).state == "running"


def test_survivor_review_has_no_synthetic_predecessor(monkeypatch):
    monkeypatch.setattr("agentflow.coordinated_review.ui_surfaces", lambda _workdir: [])
    cfg = SimpleNamespace(repo="o/r", workdir="/work")

    sub = coordinated_review.survivor_review_submission(
        cfg, issue=7, slug="fix", builder_tool="claude", head_sha="head-a",
        reviewer_tool="codex", pr_number=42, acceptance="Issue acceptance",
        review=ReviewState(change_author_tool="claude"))

    assert sub is not None and sub.stage == "review"
    assert sub.transfer_from is None
    assert sub.builder_lineage == "claude" and sub.target == "head-a"
    assert "Issue acceptance" in sub.input_ptr
    assert sub.builder_complexity is None  # a blocking survivor review parks; it never revises


def test_review_identity_is_idempotent_per_exact_head(make_coord):
    coord = make_coord()
    same = coord.submit_stage(_review(target="head-a"))
    assert coord.submit_stage(_review(target="head-a")) == same
    changed = coord.submit_stage(_review(target="head-b"))
    assert changed != same
    assert {r.target for r in _records(coord) if r.stage == "review"} == {"head-a", "head-b"}


# --- a PR head that moves off the immutable review target (issue #208) --------------------
#
# A Review's target SHA is immutable and verify demands a verdict for exactly that SHA. When the
# head moves for any reason other than an auto-revise — a maintainer rebase, a manual push, a
# conflict fix — the in-flight review can never verify: it re-reviews a dead head, burns its budget,
# and finally parks "budget exhausted" even on a merged PR. The reconcile pass detects the
# divergence before an attempt is charged, driven here through the production
# ``reconcile_and_project`` seam with GitHub reads faked.


def _diverged_review(*, target, subject="7", pool="codex", builder_lineage="claude", round=0):
    """A cold Review holding its claim, at a review worktree whose path encodes PR #26 —
    the shape ``reconcile_and_project`` needs to recover the PR number for its live-head read."""
    return Submission(
        repo="o/r", subject=subject, stage="review", pool=pool, complexity="deep", target=target,
        builder_lineage=builder_lineage, builder_complexity="deep", builder_effort="high",
        round=round, input_ptr="Review PR #26",
        source="/work/.agentflow/worktrees/codex-review/pr-26-home-depot-probe")


def _gh_pr(state, head):
    """A faked GitHub edge: the PR's live head and state come back as the module's typed PR
    facts, the way the diverged-review reconciler reads them — never a gh shellout."""
    from agentflow import github

    def pr_facts(repo, pr):
        return github.PrFacts(head_ref_name="", head_ref_oid=head, state=state,
                              closing_issues=())
    return pr_facts


def test_a_merged_pr_retires_the_stranded_review_silently(make_coord, monkeypatch):
    """Reproduces home-depot PR #26: a Review stranded at a rebased-away head on a PR the maintainer
    already merged must retire silently — no park comment, no notification, no attempt charged. Fails
    before the fix, where nothing detects the merge and the review parks the merged PR."""
    fake = FakeSession()
    fake.gate_open = False  # isolate the divergence pass from admission
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="stale-sha"))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("MERGED", "merged-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    rec = record_of(coord, ident)
    assert rec.retired is True and rec.claim is False
    assert rec.attempts == 0                            # the divergence charges no attempt
    assert rec.handoffs == 0 and rec.notifications == 0  # nothing parked, nobody notified
    assert rec.hold_pending is False


def test_a_moved_head_retires_the_stale_review_and_opens_a_bounded_successor(make_coord,
                                                                             monkeypatch):
    """An open PR whose head moved off the review target retires the stranded record and opens one
    successor Review at the live head, at the same auto-revise round; the stranded attempt is never
    charged. Re-running reconciliation opens nothing new (idempotent)."""
    fake = FakeSession()
    fake.gate_open = False
    coord = make_coord(fake)
    stale = coord.submit_stage(_diverged_review(target="stale-sha", round=0))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("OPEN", "live-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "autonomous")
    choices = []
    monkeypatch.setattr(
        coordinated_review, "pick_reviewer",
        lambda tool, **kwargs: choices.append((tool, kwargs)) or "codex")

    pipeline.reconcile_and_project(coord)

    stale_rec = record_of(coord, stale)
    assert stale_rec.retired is True and stale_rec.claim is False
    assert stale_rec.attempts == 0                      # the stranded attempt is never charged
    records = {r.identity: r for r in _records(coord)}
    assert "o/r|7|review|live-sha" in records
    successor = records["o/r|7|review|live-sha"]
    assert successor.state == "waiting" and successor.claim is True
    assert successor.target == "live-sha" and successor.round == 0
    assert successor.builder_lineage == "claude"        # lineage carried forward
    assert successor.builder_complexity == "deep" and successor.builder_effort == "high"
    assert successor.effort is None
    assert successor.handoffs == 0                      # no human park
    assert choices == [("claude", {"allow_same_tool": False})]

    pipeline.reconcile_and_project(coord)       # idempotent re-drive
    live = [r.identity for r in _records(coord) if r.stage == "review" and not r.retired]
    assert live == ["o/r|7|review|live-sha"]


def test_a_legacy_session_led_moved_head_successor_normalizes_provenance(make_coord,
                                                                         monkeypatch):
    from agentflow.coordinator.providers import validate_session_lead_input

    fake = FakeSession()
    fake.gate_open = False
    coord = make_coord(fake)
    build = Record(
        identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
        repo="o/r", subject="7", change_author_tool="claude",
        source="/work/.agentflow/worktrees/claude/issue-7-home-depot-probe")
    opening = coordinated_review.review_submission(build, "stale-sha", "codex", 26)
    contract = opening.input_ptr[opening.input_ptr.index(
        "\n## Session lead — benchmarked capability routing\n"):]
    stale = coord.submit_stage(replace(opening, transfer_from=None, session_lead=False))
    legacy = record_of(coord, stale)
    legacy.native_helpers_marker = "codex-cli 0.144.0\n"
    coord._store.upsert(legacy)
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("OPEN", "live-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)
    monkeypatch.setattr("agentflow.coordinated_review.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr(
        coordinated_review, "pick_reviewer", lambda _tool, **_kwargs: "codex")

    pipeline.reconcile_and_project(coord)

    successor = record_of(coord, "o/r|7|review|live-sha")
    assert successor.session_lead is True
    assert successor.input_ptr.count("<!-- agentflow-review-assignment:start -->") == 1
    assert successor.input_ptr.count(
        "\n## Session lead — benchmarked capability routing\n") == 1
    assert successor.input_ptr.endswith(contract)
    validate_session_lead_input(successor)


def test_a_running_moved_head_review_terminates_before_opening_its_successor(make_coord,
                                                                               monkeypatch):
    """A live review is stopped before its moved-head successor takes the claim (#220)."""
    fake = FakeSession()
    coord = make_coord(fake)
    stale = coord.submit_stage(_diverged_review(target="stale-sha", round=0))
    coord.cycle("codex")
    assert record_of(coord, stale).state == "running"

    events = []
    submit_stage = coord.submit_stage
    monkeypatch.setattr(coordinated_review, "_kill_running_family",
                        lambda rec: events.append(("kill", rec.identity)))
    monkeypatch.setattr(coord, "submit_stage",
                        lambda submission: events.append(("submit", submission.target))
                        or submit_stage(submission))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("OPEN", "live-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)
    monkeypatch.setattr(coordinated_review, "pick_reviewer", lambda tool, **kwargs: "codex")

    pipeline.reconcile_and_project(coord)

    assert events == [("kill", stale), ("submit", "live-sha")]
    assert record_of(coord, stale).retired is True
    assert record_of(coord, "o/r|7|review|live-sha").claim is True


def test_a_running_review_is_not_killed_for_its_own_clean_push(make_coord, monkeypatch):
    """A live-head move owned by Review's clean detached checkout is its bounded fix, not an
    external supersession. Let it finish and emit the final-head verdict."""
    fake = FakeSession()
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="start-sha", round=0))
    coord.cycle("codex")
    killed = []
    monkeypatch.setattr(coordinated_review, "_review_checkout_owns_head",
                        lambda _record, head: head == "fixed-sha")
    monkeypatch.setattr(coordinated_review, "_kill_running_family",
                        lambda rec: killed.append(rec.identity))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("OPEN", "fixed-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    assert killed == []
    assert record_of(coord, ident).state == "running"
    assert not [r for r in _records(coord) if r.target == "fixed-sha"]


def test_a_moved_head_parks_once_when_the_revise_rounds_are_spent(make_coord, monkeypatch):
    """When the auto-revise rounds are already spent, a moved head has no bounded successor to open,
    so the open PR parks once through the existing Review exhaustion handoff."""
    fake = FakeSession()
    fake.gate_open = False
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="stale-sha", round=MAX_REVISES))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("OPEN", "live-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    rec = record_of(coord, ident)
    assert rec.state == "held" and rec.handoffs == 1 and rec.notifications == 1
    assert rec.claim is False
    assert not [r for r in _records(coord) if r.target == "live-sha"]  # no successor opened

    pipeline.reconcile_and_project(coord)       # the park is idempotent
    assert record_of(coord, ident).handoffs == 1


def test_an_unmoved_head_is_left_to_the_normal_review_flow(make_coord, monkeypatch):
    """The live head still equals the review target: the reconcile pass leaves the record entirely
    alone so the ordinary verify/settle flow is unchanged (no retire, no successor, no park)."""
    fake = FakeSession()
    fake.gate_open = False
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="live-sha", round=0))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("OPEN", "live-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    rec = record_of(coord, ident)
    assert rec.retired is False and rec.claim is True and rec.state == "waiting"
    assert rec.handoffs == 0
    assert [r.identity for r in _records(coord) if r.stage == "review"] == [ident]


# --- terminating a RUNNING family before the retire/park (issue #220) ----------------------
#
# When the PR head moves (or the PR is gone) and the reconciler retires or parks the stranded
# review, it must first terminate the provider family if one is still running. Without the fix the
# orphaned session keeps burning tokens on a head that can never complete. The kill is fail-open:
# a family already gone, or an os.kill that raises, must never block the retire or park.


def test_a_running_diverged_review_terminates_its_family_before_retiring(make_coord, monkeypatch):
    """A diverged review that is actively RUNNING has its provider family terminated before the
    record is retired, so the orphaned process does not burn tokens on a superseded head (#220).
    Fails before the fix because _kill_running_family does not exist and no kill is attempted."""
    fake = FakeSession()
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="stale-sha"))
    coord.cycle("codex")                             # admit and start → RUNNING with live family
    assert record_of(coord, ident).state == "running"

    killed = []
    monkeypatch.setattr(coordinated_review, "_kill_running_family",
                        lambda rec: killed.append(rec.identity))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("MERGED", "merged-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    assert ident in killed                           # kill hook was invoked before retire
    assert record_of(coord, ident).retired is True   # retire still completed


def test_kill_failure_does_not_block_the_retire(make_coord, monkeypatch):
    """An os.kill that raises (family already gone, permission denied) is swallowed; the retire
    completes regardless — fail-open on termination (#220)."""
    import signal
    fake = FakeSession()
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="stale-sha"))
    coord.cycle("codex")

    monkeypatch.setattr("agentflow.coordinator.launcher.pid_family_alive", lambda _f: True)
    kill_attempts = []

    def raise_on_sigterm(pid, sig):
        kill_attempts.append((pid, sig))
        if sig == signal.SIGTERM:
            raise OSError("no such process")

    monkeypatch.setattr(coordinated_review.os, "kill", raise_on_sigterm)
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("MERGED", "merged-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    assert any(sig == signal.SIGTERM for _pid, sig in kill_attempts)  # kill was attempted
    assert record_of(coord, ident).retired is True                    # retire still completed


def test_a_waiting_diverged_review_does_not_attempt_a_kill(make_coord, monkeypatch):
    """A diverged review that never started (WAITING) has no provider family; the reconciler must
    not attempt a kill — only RUNNING records carry a family to terminate (#220)."""
    fake = FakeSession()
    fake.gate_open = False                           # keep the review WAITING (never admitted)
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="stale-sha"))
    assert record_of(coord, ident).state == "waiting"

    killed = []
    monkeypatch.setattr(coordinated_review, "_kill_running_family",
                        lambda rec: killed.append(rec.identity))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("MERGED", "merged-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    assert killed == []                              # no kill attempted for a WAITING record
    assert record_of(coord, ident).retired is True   # retire still happens normally


def test_a_completed_diverged_review_does_not_attempt_a_kill(make_coord, monkeypatch):
    """A completed review has no live provider family, even when its PR is subsequently merged."""
    from agentflow.reviewer import Verdict

    fake = FakeSession()
    coord = make_coord(fake, adapter=_review_adapter(fake, verdict=[True], prep=[True]))
    ident = coord.submit_stage(_diverged_review(target="stale-sha"))
    coord.cycle("codex")
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [outcome.status for outcome in coord.cycle("codex")] == ["completed"]

    killed = []
    monkeypatch.setattr(coordinated_review, "_kill_running_family",
                        lambda rec: killed.append(rec.identity))
    monkeypatch.setattr(coordinated_review, "_review_verdict", lambda _rec: Verdict(clean=True))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("MERGED", "merged-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    assert killed == []
    assert record_of(coord, ident).retired is False  # normal completed-review settlement owns it


def test_a_running_diverged_review_terminates_its_family_before_parking(make_coord, monkeypatch):
    """The budget-exhausted park path stops its running provider before handing off (#220)."""
    fake = FakeSession()
    coord = make_coord(fake)
    ident = coord.submit_stage(_diverged_review(target="stale-sha", round=MAX_REVISES))
    coord.cycle("codex")

    killed = []
    monkeypatch.setattr(coordinated_review, "_kill_running_family",
                        lambda rec: killed.append(rec.identity))
    monkeypatch.setattr("agentflow.github.pr_facts", _gh_pr("OPEN", "live-sha"))
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)

    pipeline.reconcile_and_project(coord)

    assert killed == [ident]
    assert record_of(coord, ident).state == "held"


def test_a_rejected_verdict_shape_earns_one_repair_turn_naming_the_exact_error():
    """A review that reached a conclusion but stated it in a rejected shape must not spend its
    continuation budget re-reviewing. It gets one repair turn carrying the parser's own error and
    an instruction to restate only the outcome."""
    from agentflow.coordinator.recovery import PROGRESS, REPAIR

    record = Record(
        identity="o/r|9|review|head-x|a1", stage="review", pool="codex", demand=2,
        repo="o/r", subject="9", target="head-x", change_author_tool="claude",
        review_depth="targeted", depth_reason="contained journey", review_axis="combined",
        source="/wt/pr-9-reverify", attempts=2)
    adapter = ReviewStageAdapter(verdict_ready=lambda r, o: False)

    rejected = json.dumps({
        "verdict": "PASS", "reviewed_sha": "head-x", "final_sha": "head-x", "pushed_sha": "",
        "fixes": ["a fix an earlier attempt already pushed"], "checks": ["re-verified"],
        "follow_ups": [], "findings": [], "depth": "targeted",
        "depth_reason": "contained journey", "axis": "combined",
        "change_author_tool": "claude"})
    repair = adapter.recover(record, SimpleNamespace(final_message=rejected))

    assert repair.kind == REPAIR
    assert "shipped fixes have no push provenance" in repair.envelope
    assert "do not redo it" in repair.envelope
    assert "further GitHub calls" in repair.envelope

    # A review that produced no verdict at all is unfinished work, not a misstatement.
    unfinished = adapter.recover(record, SimpleNamespace(final_message="ran out of time"))
    assert unfinished.kind == PROGRESS


# --- recovering a parked review by hand (#344) --------------------------------------------

def test_manual_review_recovers_a_parked_claimless_exact_head_review(make_coord, monkeypatch):
    """A parked review is left `held`, deliberately unretired, and claimless, so it owns nothing to
    hand over. Treating it as an ownership-transfer predecessor is what made the maintainer's own
    `/agentflow review <PR>` recovery fail outright on the very PR the park asked them to decide.

    Driven end to end over one durable store: park the review through ``cycle``, then run the
    maintainer's recovery against that same durable state.
    """
    from agentflow import loop
    from agentflow.loop import RepoConfig
    from agentflow.review_policy import ReviewAssignment

    pr_comments = []

    def _park(repo, pr, verdict, *, reason, missing_outcome, context, proof_marker):
        pr_comments.append({"body": "> *agentflow: parked for human review.*\n"
                                    f"<!-- {proof_marker} -->"})

    monkeypatch.setattr("agentflow.gate.park", _park)
    monkeypatch.setattr("agentflow.github.pr_comments",
                        lambda repo, pr: [github.Comment(body=c["body"], created_at="")
                                          for c in pr_comments])
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)

    fake = FakeSession()
    adapter = ReviewStageAdapter(verdict_ready=lambda r, o: False, worktree_reset=lambda r: True,
                                 observer=fake, handoff=pr_park.park_pr)
    coord = make_coord(fake, adapter=adapter)
    parked_id = coord.submit_stage(
        _review(source="/w/.agentflow/worktrees/codex-review/pr-42-x"))
    for _ in range(8):
        if coord.cycle("claude"):
            break
        fake.end(parked_id, cause=ProviderCause.PROCESS)
    parked = record_of(coord, parked_id)
    assert parked.state == "held" and parked.retired is False and parked.claim is False

    monkeypatch.setattr(loop.github, "pr_facts", lambda repo, pr: loop.github.PrFacts(
        head_ref_name="agentflow/codex/issue-7-x", head_ref_oid="sha-a",
        state="OPEN", closing_issues=(7,)))
    monkeypatch.setattr(loop, "_issue_acceptance", lambda cfg, issue: "acceptance")
    monkeypatch.setattr(loop, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(coordinated_review, "_review_assignment_facts",
                        lambda *args, **kwargs: (ReviewAssignment(reason="one journey"), ()))
    monkeypatch.setattr(loop, "pick_reviewer", lambda author, **kwargs: "claude")
    claimed = []
    monkeypatch.setattr(loop, "claim", lambda repo, issue, _label: claimed.append(issue) or True)
    monkeypatch.setattr(pipeline, "build_coordinator", lambda **_kwargs: coord)
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda _coord: None)

    assert loop.review_pr(RepoConfig("o/r", "/w"), 42) == "review submitted"

    assert claimed == [7]                                  # a fresh claim, not an invalid transfer
    reviews = [r for r in _records(coord) if r.stage == "review"]
    assert len(reviews) == 2
    assert record_of(coord, parked_id).claim is False      # the park is not revived or re-owned
    resumed = next(r for r in reviews if r.identity != parked_id)
    assert resumed.target == "sha-a" and resumed.claim is True
    assert resumed.review_sequence == 1 and resumed.builder_lineage == "codex"

    # Run the command a second time while that recovery is live. The park is still unretired and
    # still sorts first, so asking "is the first unretired record running?" would miss the review
    # that is — and hand the same issue a second owner.
    coord.cycle("claude")
    assert record_of(coord, resumed.identity).state == "running"

    assert loop.review_pr(RepoConfig("o/r", "/w"), 42) == \
        "exact-head review is already running; it was not preempted"
    assert claimed == [7]                                  # the issue is never claimed twice
    assert len([r for r in _records(coord) if r.stage == "review"]) == 2


def test_manual_review_resume_keeps_the_three_pass_ceiling_and_park_truth(
        make_coord, monkeypatch):
    """Replay the PR #538 boundary through the durable coordinator and manual review command.

    Two earlier reviewer pushes are carried on the parked record, whose own parsed PASS pushed the
    third repair. The park must describe those three judgments, and a manual resume at that moved
    head may buy a fresh judgment but not another repair push.
    """
    from agentflow import loop
    from agentflow.loop import RepoConfig
    from agentflow.review_policy import ReviewAssignment

    payload = json.dumps({
        "verdict": "PASS", "depth": "targeted", "depth_reason": "one journey",
        "axis": "combined", "change_author_tool": "claude", "reviewed_sha": "sha-a",
        "final_sha": "sha-b", "pushed_sha": "sha-b", "fixes": ["repaired the review flow"],
        "follow_ups": [], "checks": ["review tracer passed"], "findings": [],
        "uncertainty": None, "decision": "",
    })

    class CompletedArtifact:
        def observe(self, _record):
            return ProviderObservation(
                final_message=payload, cause=ProviderCause.NONE, has_end_fact=True)

    posted = []
    monkeypatch.setattr(
        github, "pr_comments",
        lambda _repo, _pr: [github.Comment(body=body, created_at="") for body in posted])
    monkeypatch.setattr(
        github, "pr_comment",
        lambda _repo, _pr, body: bool(posted.append(body)) or True)
    monkeypatch.setattr("agentflow.notify.notify", lambda *_args, **_kwargs: True)

    fake = FakeSession()
    adapter = ReviewStageAdapter(
        verdict_ready=coordinated_review._verdict_ready,
        worktree_reset=lambda _record: True,
        observer=CompletedArtifact(),
        handoff=pr_park.park_pr)
    coord = make_coord(fake, adapter=adapter)
    parked_id = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="review", pool="codex", complexity="deep",
        target="sha-a", builder_lineage="claude",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        input_ptr="Review PR #42",
        review=ReviewState(
            assignment=ReviewAssignment(reason="one journey"),
            change_author_tool="claude", passes=2)))
    coord.cycle("codex")
    fake.end(parked_id, success=True, cause=ProviderCause.NONE)
    assert [outcome.status for outcome in coord.cycle("codex")] == ["completed"]
    assert coord.park_completed(parked_id) is not None

    monkeypatch.setattr(loop.github, "pr_facts", lambda _repo, _pr: loop.github.PrFacts(
        head_ref_name="agentflow/claude/issue-7-fix", head_ref_oid="sha-b",
        state="OPEN", closing_issues=(7,)))
    monkeypatch.setattr(loop, "_issue_acceptance", lambda _cfg, _issue: "acceptance")
    monkeypatch.setattr(loop, "repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr(
        coordinated_review, "_review_assignment_facts",
        lambda *_args, **_kwargs: (ReviewAssignment(reason="one journey"), ()))
    monkeypatch.setattr(loop, "pick_reviewer", lambda _author, **_kwargs: "codex")
    monkeypatch.setattr(loop, "claim", lambda *_args: True)
    monkeypatch.setattr(pipeline, "build_coordinator", lambda **_kwargs: coord)
    monkeypatch.setattr(pipeline, "reconcile_and_project", lambda _coord: None)

    assert loop.review_pr(RepoConfig("o/r", "/work"), 42) == "review submitted"

    resumed = next(record for record in _records(coord) if record.target == "sha-b")
    assert resumed.review_passes == 3 and resumed.resume == 1
    assert resumed.identity != parked_id and resumed.claim is True
    body = posted[0]
    assert "3 review passes recorded a verdict" in body
    for false_claim in (
        "No review verdict was recorded for this exact head",
        "the review executions failed",
        "nothing has judged this change yet",
        "the review this change never got",
    ):
        assert false_claim not in body


def test_manual_resume_identity_never_reuses_a_daemon_retarget_record(make_coord, monkeypatch):
    """A manual and daemon resume can race toward the same new head with the same pass ledger.

    The daemon keeps resume zero; the maintainer command carries its own positive resume dimension,
    so submitting it after the daemon record retired cannot revive that record or take its claim.
    """
    from agentflow.loop import RepoConfig
    from agentflow.review_policy import ReviewAssignment

    monkeypatch.setattr(coordinated_review, "repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr(
        coordinated_review, "pick_reviewer", lambda _author, **_kwargs: "claude")
    coord = make_coord()
    stranded_id = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="review", pool="codex", target="sha-a",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix",
        builder_lineage="claude", input_ptr="Review sha-a",
        review=ReviewState(
            assignment=ReviewAssignment(reason="one journey"),
            change_author_tool="claude", passes=2)))
    stranded = record_of(coord, stranded_id)
    stranded.capability_context = "{"
    daemon = coordinated_review._moved_head_review_submission(stranded, "sha-b")
    assert daemon is not None and daemon.resume == 0 and daemon.review.passes == 2
    assert daemon.capability_context == "{"
    manual = coordinated_review.survivor_review_submission(
        RepoConfig("o/r", "/work"), issue=7, slug="fix", builder_tool="claude",
        head_sha="sha-b", reviewer_tool="codex", pr_number=42, acceptance="acceptance",
        review=ReviewState(
            assignment=ReviewAssignment(reason="one journey"),
            change_author_tool="claude", reviewed_from_sha="sha-b", passes=2),
        resume=1)
    assert manual is not None

    daemon_id = coord.submit_stage(daemon)
    later = replace(daemon, target="sha-c", transfer_from=daemon_id, supersede=True)
    coord.submit_stage(later)
    assert record_of(coord, daemon_id).retired is True
    assert record_of(coord, daemon_id).claim is False

    manual_id = coord.submit_stage(manual)

    assert manual_id != daemon_id
    assert record_of(coord, daemon_id).retired is True
    assert record_of(coord, daemon_id).claim is False
    assert record_of(coord, manual_id).claim is True


# --- a prior attempt's pushed fix must not park the continuation review (#346 class) ------

def test_a_continuation_verdict_over_a_prior_attempts_pushed_fix_verifies(monkeypatch):
    """The park factory this class of fix targets: an earlier attempt of the same logical review
    pushed the fixes, the continuation honestly reports ``pushed_sha: ""``, and verification
    rejected the honest verdict every attempt until the PR parked. When the retained checkout
    proves the moved head, the verdict verifies and the proof is persisted for settlement."""
    record = Record(identity="o/r|7|review|sha-a|afix", stage="review", pool="claude", demand=1,
                    repo="o/r", subject="7", target="sha-a", review_axis="fix",
                    source="/wt/pr-7-x")
    payload = json.dumps({"verdict": "PASS", "reviewed_sha": "sha-a",
                          "final_sha": "sha-b", "pushed_sha": "", "findings": []})
    monkeypatch.setattr(coordinated_review, "_review_checkout_owns_head",
                        lambda rec, head: head == "sha-b")

    result = coordinated_review._verdict_ready(
        record, ProviderObservation(final_message=payload))

    assert result
    assert record.review_prior_push == "sha-b"      # durable proof for settlement


def test_an_unowned_moved_head_still_fails_and_names_the_check(monkeypatch):
    """A third-party push must not be excused: with no checkout ownership the verdict stays
    rejected — and the miss now names the exact failed check instead of a silent False."""
    record = Record(identity="o/r|7|review|sha-a|afix", stage="review", pool="claude", demand=1,
                    repo="o/r", subject="7", target="sha-a", review_axis="fix",
                    source="/wt/pr-7-x")
    payload = json.dumps({"verdict": "PASS", "reviewed_sha": "sha-a",
                          "final_sha": "sha-b", "pushed_sha": "", "findings": []})
    monkeypatch.setattr(coordinated_review, "_review_checkout_owns_head",
                        lambda rec, head: False)

    result = coordinated_review._verdict_ready(
        record, ProviderObservation(final_message=payload))

    assert not result
    assert result.check == "verdict-parse" and "provenance" in result.detail
    assert record.review_prior_push is None


def test_settlement_reparses_with_the_recorded_prior_push_proof():
    """Settlement must accept the same verdict verification accepted: it re-parses the captured
    payload against the durable ``review_prior_push`` fact, never a checkout that may be gone."""
    payload = json.dumps({"verdict": "PASS", "reviewed_sha": "sha-a",
                          "final_sha": "sha-b", "pushed_sha": "", "findings": []})
    record = Record(identity="o/r|7|review|sha-a|afix", stage="review", pool="claude", demand=1,
                    repo="o/r", subject="7", target="sha-a", review_axis="fix",
                    outcome=payload, review_prior_push="sha-b")

    verdict = coordinated_review._review_verdict(record)

    assert verdict.parsed and verdict.clean and verdict.final_sha == "sha-b"


def test_park_comment_names_the_last_unverified_check():
    """The park comment prints the recorded miss, so the human reading the park sees what
    actually stopped the machine instead of a generic budget line."""
    record = Record(identity="o/r|7|review|sha-a", stage="review", pool="claude", demand=1,
                    repo="o/r", subject="7", target="sha-a", source="/wt/pr-7-x",
                    verify_miss="fix-push: the fix-axis review recorded FIX findings")
    ctx = pr_park.park_context(
        record, None, reason="exhausted its review budget without a durable verdict",
        missing="No review verdict was recorded for this exact head.")

    assert any(c.startswith("Last unverified check: fix-push") for c in ctx.checks)
