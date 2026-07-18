"""Review as the second coordinated stage (issue #104), driven through the public
``submit_stage`` / ``cycle`` seam. Review binds to the exact PR head SHA, recreates its read-only
checkout on continuation, completes only on a durable verdict for that SHA, may move pools when
review safety allows it, and parks the PR on exhaustion — all asserted at the coordinator
interface, never by poking private transitions. The Build → Review claim transfer and its crash
boundaries are exercised here too, through a stage router that runs both live stages behind one
coordinator.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_build
from agentflow.coordinator import (BuildStageAdapter, ReviewStageAdapter, StageRouter, Submission,
                                    tracer)
from agentflow.coordinator.providers import ProviderCause, ProviderObservation
from agentflow.coordinator.record import Record


def _review(subject="7", *, pool="claude", target="sha-a", builder_lineage="codex",
            source="/wt/pr-7-x", transfer_from=None):
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool, complexity="deep",
                      target=target, source=source, builder_lineage=builder_lineage,
                      transfer_from=transfer_from)


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
    assert coordinated_build._verdict_ready(record, match) is True
    assert coordinated_build._verdict_ready(record, other) is False   # a different head SHA
    assert coordinated_build._verdict_ready(record, none) is False    # no verdict at all


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

def test_review_continuation_moves_to_an_available_pool_and_cannot_same_tool_auto_merge(
        make_coord):
    """A read-only review whose home pool cannot fit it moves to the available pool, recomputing
    its admission demand there; landing on the builder's own tool strips auto-merge (ADR 0028)."""
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

    # r1 becomes eligible but codex is full, so it moves to claude — recomputing demand (1) and,
    # because claude is the builder's tool, it can no longer auto-merge.
    coord.cycle("claude", now=100)
    moved = record_of(coord, r1)
    assert moved.pool == "claude" and moved.state == "running"
    assert moved.demand == 1                          # recomputed for the destination pool
    assert moved.auto_merge_allowed is False          # same-tool review cannot auto-merge
    assert permits(coord, "claude") == 1


def test_a_code_writing_continuation_never_migrates(make_coord):
    """Only a read-only review moves pools; a code-writing stage stays on its builder lineage even
    when its pool is full (ADR 0028). A revise pinned to codex is never offered to claude."""
    fake = FakeSession()
    coord = make_coord(fake)
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise", pool="codex",
                                           builder_lineage="codex", complexity="deep"))
    coord.cycle("codex")
    fake.end(revise, cause=ProviderCause.PROCESS)     # interrupted → continuation on codex
    coord.cycle("claude", now=0)                       # claude cycle must not adopt it
    assert record_of(coord, revise).pool == "codex"
    assert permits(coord, "claude") == 0


# --- a review re-places when its home pool loses launch capacity (issue #202) -------------

def _gate_blocking(*pools):
    """An admission gate that refuses launches on the named pools (e.g. one whose weekly budget
    is spent) while its permit ledger is untouched — the launch-gate block that froze the
    home-depot #22/#23 reviews at zero attempts."""
    blocked = set(pools)
    return lambda record: record.pool not in blocked


def test_a_frozen_fresh_review_migrates_when_its_pool_lost_launch_capacity(make_coord):
    """A fresh review (never launched, zero attempts) whose home pool later loses launch capacity
    — permit ledger empty but the launch gate now blocks it (weekly budget spent) — is re-placed
    onto a pool that can launch it, keeping its immutable target and losing auto-merge when it
    lands on the builder's own tool. Reproduces home-depot #22/#23; before the fix the record
    freezes on codex forever because migration required a continuation and a full permit ledger."""
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

    coord.cycle("claude", now=0)                      # re-placed onto claude, which can launch it
    moved = record_of(coord, r)
    assert moved.pool == "claude" and moved.state == "running"
    assert moved.target == "sha-a"                    # immutable target unchanged
    assert moved.auto_merge_allowed is False          # same-tool review cannot auto-merge
    assert permits(coord, "claude") == 1


def test_a_review_stays_put_when_neither_pool_can_launch_it_then_lands_home_on_recovery(
        make_coord):
    """No flapping: with both pools launch-blocked the review reverts cleanly to its home pool
    each cycle (never a half-move), and once the home pool regains budget it launches at home
    rather than being re-placed."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_gate_blocking("codex", "claude"),
                       adapter=_review_adapter(fake, verdict=[False], prep=[True]))
    r = coord.submit_stage(_review("1", pool="codex", builder_lineage="claude"))
    for _ in range(3):                                # several cycles, both pools blocked
        coord.cycle("codex", now=0)
        coord.cycle("claude", now=0)
    parked = record_of(coord, r)
    assert parked.pool == "codex" and parked.state == "waiting" and parked.attempts == 0
    assert parked.demand == 2                         # migration fields reverted — no half-move
    assert parked.auto_merge_allowed is True          # still cross-tool while it stays home
    assert permits(coord, "claude") == 0

    coord._gate = _gate_blocking("claude")            # codex regains its weekly budget
    coord.cycle("codex", now=0)                       # launches at home, not re-placed
    home = record_of(coord, r)
    assert home.pool == "codex" and home.state == "running"
    assert permits(coord, "codex") == 2


def test_a_gate_blocked_code_writing_stage_never_migrates(make_coord):
    """Weekly-budget pacing of codex code-writing work is intended: a waiting revise whose codex
    pool is launch-blocked stays on codex and is never offered to claude (only reviews move)."""
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
    review = coord.submit_stage(_review("7", pool="codex", builder_lineage="claude",
                                        target="head-sha"))
    coord.cycle("codex")
    fake.end(review, cause=ProviderCause.PROCESS)
    coord.cycle("codex")

    revise = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="revise", pool="claude", target="head-sha",
        builder_lineage="claude", source="/wt/issue-7", transfer_from=review))
    records = {r.identity: r for r in _records(coord)}
    assert records[review].retired is True and records[review].claim is False
    assert records[revise].retired is False and records[revise].claim is True


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
    calls = []

    def gh(cmd):
        calls.append(cmd)
        if len(calls) == 1:
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout='[{"number":42,"headRefOid":"head-a"}]')

    monkeypatch.setattr("agentflow.loop._run", gh)
    monkeypatch.setattr("agentflow.live.replace_projection", lambda *a, **k: None)
    # An unreadable PR fails closed: Build still owns the change and a later pass retries.
    coordinated_build.reconcile_and_project(restarted)
    assert record_of(restarted, build).claim is True
    assert record_of(restarted, build).retired is False

    coordinated_build.reconcile_and_project(restarted)
    review = "o/r|7|review|head-a"
    assert record_of(restarted, build).retired is True
    assert record_of(restarted, build).claim is False
    assert record_of(restarted, review).state == "waiting"
    assert record_of(restarted, review).claim is True

    # Repeated reconciliation/restart neither creates another record nor another provider.
    coordinated_build.reconcile_and_project(restarted)
    restarted_again = make_coord(fake, adapter=adapter, gate=tracer.build_review_revise_gate)
    coordinated_build.reconcile_and_project(restarted_again)
    assert list(fake.family_of).count(review) == 1
    assert len([r for r in _records(restarted_again) if r.stage == "review"]) == 1
    assert len(calls) == 2


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
    """The PRODUCTION verdict edge (issue #120): ``coordinated_build._verdict_ready`` wired as the
    Review adapter's verifier and driven through ``submit_stage``/``cycle`` — not a direct private
    call. A durable verdict naming another SHA keeps the review incomplete; the exact reviewed SHA
    completes it even on a bad provider exit (ADR 0028 outcome-first)."""
    fake = FakeSession()
    reviewer = _ReviewerObserver()
    adapter = ReviewStageAdapter(verdict_ready=coordinated_build._verdict_ready,
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


def test_production_checkout_recreation_rebuilds_read_only_at_the_exact_sha(make_coord, tmp_path):
    """The PRODUCTION checkout edge (issue #120): ``coordinated_build._review_worktree_reset``
    wired as the Review adapter's prepare and driven through admission over a real git repo. It
    creates the read-only checkout detached at the record's immutable target SHA, and a
    continuation discards stale state and rebuilds at the SAME SHA even after the branch moved."""
    repo = _repo_with_origin(tmp_path)
    reviewed_sha = _git(repo, "rev-parse", "HEAD")
    wt = repo / ".agentflow" / "worktrees" / "codex-review" / "pr-42-x"
    fake = FakeSession()
    adapter = ReviewStageAdapter(verdict_ready=lambda r, o: False,
                                 worktree_reset=coordinated_build._review_worktree_reset,
                                 observer=fake)
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review(target=reviewed_sha, source=str(wt)))
    coord.cycle("claude")                              # admission runs the production prepare
    assert record_of(coord, ident).state == "running"
    assert _git(wt, "rev-parse", "HEAD") == reviewed_sha   # the exact reviewed SHA
    assert _git(wt, "branch", "--show-current") == ""      # detached — review holds no branch

    # The checkout goes stale and the branch moves on; the continuation's prepare discards the
    # leftover state and rebuilds at the same immutable target SHA — never the moved head.
    (wt / "stale.txt").write_text("leftover")
    (repo / "README.md").write_text("moved\n")
    _git(repo, "commit", "-am", "branch moves on")
    _git(repo, "push", "origin", "main")
    fake.end(ident, cause=ProviderCause.PROCESS)       # interrupted with no verdict → continuation
    coord.cycle("claude")                              # re-admission re-runs the production prepare
    assert record_of(coord, ident).attempts == 2
    assert _git(wt, "rev-parse", "HEAD") == reviewed_sha
    assert not (wt / "stale.txt").exists()             # stale state was discarded, not kept


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
    assert coordinated_build._review_worktree_reset(record) is True
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
    assert coordinated_build._review_worktree_reset(record) is True
    assert _git(claude_wt, "rev-parse", "HEAD") == reviewed_sha
    assert codex_wt.exists() and _worktree_registered(repo, codex_wt)  # other tool untouched


def test_a_review_checkout_that_keeps_failing_surfaces_in_the_log(tmp_path):
    """Issue #171: a genuinely stuck review (one whose checkout never succeeds) must become
    visible rather than no-op'ing admission silently every cycle. The first miss can be transient
    and stays quiet; a repeat surfaces once, then re-reminds periodically so a long-stuck review
    keeps a breadcrumb instead of a single line lost to scrollback."""
    repo = _repo_with_origin(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-99-x"
    record = SimpleNamespace(repo="o/r", source=str(wt), target="0" * 40, pool="claude")
    coordinated_build._REVIEW_PREPARE_FAILURES.pop(record.source, None)
    logs: list[str] = []

    assert coordinated_build._review_worktree_reset(record, _log=logs.append) is False
    assert logs == []  # a single miss can be transient
    assert coordinated_build._review_worktree_reset(record, _log=logs.append) is False
    assert len(logs) == 1 and "o/r" in logs[0]  # the repeat is surfaced
    for _ in range(9):  # failures 3..11 stay quiet — one breadcrumb, not one per cycle
        coordinated_build._review_worktree_reset(record, _log=logs.append)
    assert len(logs) == 1
    coordinated_build._review_worktree_reset(record, _log=logs.append)  # the 12th re-reminds
    assert len(logs) == 2
    coordinated_build._REVIEW_PREPARE_FAILURES.pop(record.source, None)


def _worktree_registered(repo: Path, wt: Path) -> bool:
    listing = _git(repo, "worktree", "list", "--porcelain")
    target = wt.resolve()
    return any(Path(line.removeprefix("worktree ")).resolve() == target
               for line in listing.splitlines() if line.startswith("worktree "))


def test_production_park_resolves_the_pr_from_the_review_worktree_and_parks_once(make_coord,
                                                                                 monkeypatch):
    """The PRODUCTION park edge (issue #120): ``coordinated_build._park_pr`` wired as the Review
    adapter's handoff and driven to exhaustion through ``cycle``. The PR number comes from the
    review worktree path (no GitHub lookup); the park comment is the durable proof, so a restart
    re-observes it and never parks or notifies twice."""
    parked, notified, pr_comments = [], [], []

    def _park(repo, pr, verdict, *, reason):
        parked.append((repo, pr))
        pr_comments.append({"body": "> *agentflow: parked for human review.*"})

    monkeypatch.setattr("agentflow.gate.park", _park)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: list(pr_comments))
    monkeypatch.setattr("agentflow.notify.notify", lambda *a, **k: notified.append(a))

    fake = FakeSession()
    adapter = ReviewStageAdapter(verdict_ready=lambda r, o: False, worktree_reset=lambda r: True,
                                 observer=fake, handoff=coordinated_build._park_pr)
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


# --- all production stages share the one gate --------------------------------------------

def test_all_stages_use_the_same_gate_and_pool_budget(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_router(fake, pr=[False], verdict=[False], prep=[True]),
                       gate=tracer.build_review_revise_gate)
    # A low-effort build (4 permits) leaves room for the review (1). Revise is enabled by the
    # same gate, but waits because the immutable five-permit pool budget is full.
    build = coord.submit_stage(Submission(repo="o/r", subject="7", stage="build", pool="claude",
                                          complexity="deep", effort="low", source="/wt/issue-7"))
    review = coord.submit_stage(_review("8", pool="claude", builder_lineage="codex"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise", pool="claude",
                                           builder_lineage="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, build).state == "running"
    assert record_of(coord, review).state == "running"
    revise_rec = record_of(coord, revise)
    assert revise_rec.state == "waiting" and revise_rec.attempts == 0


def _completed_review_record(*, profile="reviewed"):
    return Record(
        identity=f"o/r|7|review|sha-a|{profile}", stage="review", pool="codex", demand=2,
        repo="o/r", subject="7", target="sha-a", builder_lineage="claude",
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix", state="completed",
        auto_merge_allowed=True)


def test_clean_reviewed_settlement_parks_once_and_returns_durable_proof(monkeypatch):
    from agentflow.reviewer import Verdict

    record = _completed_review_record()
    comments = []
    parked, notified = [], []
    monkeypatch.setattr(coordinated_build, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_build, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.loop.repo_profile", lambda _workdir: "reviewed")
    monkeypatch.setattr("agentflow.loop.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda _repo, _pr: list(comments))

    def park(_repo, pr, _verdict, *, reason):
        parked.append((pr, reason))
        comments.append({"body": "> *agentflow: parked for human review.*"})

    monkeypatch.setattr("agentflow.gate.park", park)
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args, **kwargs: notified.append((args, kwargs)) or True)

    proof = coordinated_build._settle_review(record)
    assert proof == "https://github.com/o/r/pull/42"
    assert len(parked) == 1 and len(notified) == 1
    assert coordinated_build._settle_review(record) == proof
    assert len(parked) == 1 and len(notified) == 1


def test_clean_autonomous_settlement_uses_full_merge_gate(monkeypatch):
    from agentflow.reviewer import Verdict

    record = _completed_review_record(profile="autonomous")
    merged, finished, issue_edits = [], [], []
    monkeypatch.setattr(coordinated_build, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_build, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.loop.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr("agentflow.loop.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda _repo, _pr: [])
    monkeypatch.setattr("agentflow.gate.ci_is_green", lambda _repo, _pr, **_kwargs: True)
    monkeypatch.setattr("agentflow.gate.ui_evidence_gap", lambda *_args: False)
    monkeypatch.setattr("agentflow.gate.reply_pending", lambda _comments: False)
    monkeypatch.setattr("agentflow.gate.squash_merge",
                        lambda _repo, pr: merged.append(pr) or True)
    monkeypatch.setattr("agentflow.loop._finish_review",
                        lambda *args, **kwargs: finished.append((args, kwargs)))
    monkeypatch.setattr("agentflow.loop._run",
                        lambda argv: issue_edits.append(argv) or SimpleNamespace(returncode=0))
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    coordinated_build._REVIEW_CI_OBSERVED[record.identity] = True

    assert coordinated_build._settle_review(record) == "https://github.com/o/r/pull/42"
    assert merged == [42] and len(finished) == 1
    assert any(argv[1:3] == ["issue", "edit"] for argv in issue_edits)


def test_review_settlement_releases_claim_through_public_coordinator_seam(make_coord, monkeypatch):
    from agentflow.reviewer import Verdict

    fake = FakeSession()
    comments = []
    parked = []
    monkeypatch.setattr(coordinated_build, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_build, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.loop.repo_profile", lambda _workdir: "reviewed")
    monkeypatch.setattr("agentflow.loop.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.loop._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)

    def park(_repo, pr, _verdict, *, reason):
        parked.append((pr, reason))
        comments.append({"body": "> *agentflow: parked for human review.*"})

    monkeypatch.setattr("agentflow.gate.park", park)
    adapter = ReviewStageAdapter(
        verdict_ready=lambda _record, _obs: True, worktree_reset=lambda _record: True,
        observer=fake, settle=coordinated_build._settle_review,
        prepare_settle=coordinated_build._prepare_review_settlement)
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
    assert len(parked) == 1
    make_coord(fake, adapter=adapter).cycle("claude")
    assert len(parked) == 1


def test_same_tool_autonomous_review_settles_to_park_without_waiting_for_ci(
        make_coord, monkeypatch):
    from agentflow.reviewer import Verdict

    fake = FakeSession()
    comments, parked = [], []
    monkeypatch.setattr(coordinated_build, "_review_verdict", lambda _r: Verdict(clean=True))
    monkeypatch.setattr(coordinated_build, "_review_pr_facts",
                        lambda _r: {"head": "sha-a", "state": "OPEN"})
    monkeypatch.setattr("agentflow.loop.repo_profile", lambda _workdir: "autonomous")
    monkeypatch.setattr("agentflow.loop.ui_surfaces", lambda _workdir: [])
    monkeypatch.setattr("agentflow.loop._finish_review", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda _repo, _pr: list(comments))
    monkeypatch.setattr("agentflow.gate.ci_is_green",
                        lambda *args, **kwargs: pytest.fail("same-tool review must park before CI"))
    monkeypatch.setattr("agentflow.ratchet.record_once", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentflow.notify.notify", lambda *args, **kwargs: True)

    def park(_repo, pr, _verdict, *, reason):
        parked.append((pr, reason))
        comments.append({"body": "> *agentflow: parked for human review.*"})

    monkeypatch.setattr("agentflow.gate.park", park)
    adapter = ReviewStageAdapter(
        verdict_ready=lambda _record, _obs: True, worktree_reset=lambda _record: True,
        observer=fake, settle=coordinated_build._settle_review,
        prepare_settle=coordinated_build._prepare_review_settlement)
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_review(
        pool="claude", builder_lineage="claude", target="sha-a",
        source="/work/.agentflow/worktrees/claude-review/pr-42-fix"))
    coord.cycle("claude")
    fake.end(ident, success=True, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    coord.cycle("claude")

    settled = record_of(coord, ident)
    assert settled.auto_merge_allowed is False
    assert settled.retired is True and settled.claim is False
    assert len(parked) == 1


# --- pure Build → Review submission mapping ----------------------------------------------

def test_review_submission_binds_to_the_head_sha_and_assumes_the_build_claim():
    build = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5, repo="o/r",
                   subject="7", source="/home/w/.agentflow/worktrees/claude/issue-7-fix-thing")
    sub = coordinated_build.review_submission(build, "head-sha-123", "codex", 42)
    assert sub is not None
    assert sub.stage == "review" and sub.target == "head-sha-123"
    assert sub.pool == "codex" and sub.builder_lineage == "claude"     # cross-tool reviewer
    assert sub.transfer_from == "o/r|7|build|-"                        # assumes the build's claim
    assert sub.complexity == "deep"                                   # review is the deep net
    assert "pr-42-fix-thing" in sub.source                            # read-only review worktree
    # A build whose worktree is unreadable, or a missing head SHA, yields no submission.
    assert coordinated_build.review_submission(build, "", "codex", 42) is None
    assert coordinated_build.review_submission(
        Record(identity="x", stage="build", pool="claude", demand=5, repo="o/r", subject="7"),
        "sha", "codex", 42) is None


def test_survivor_review_has_no_synthetic_predecessor(monkeypatch):
    monkeypatch.setattr("agentflow.loop.ui_surfaces", lambda _workdir: [])
    cfg = SimpleNamespace(repo="o/r", workdir="/work")

    sub = coordinated_build.survivor_review_submission(
        cfg, issue=7, slug="fix", builder_tool="claude", head_sha="head-a",
        reviewer_tool="codex", pr_number=42, acceptance="Issue acceptance")

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
