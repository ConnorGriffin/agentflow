"""Build behind the session coordinator (issue #103), driven through the public
``submit_stage`` / ``cycle`` seam. The Build stage adapter's outcome-first PR verification,
worktree/lineage reuse on continuation, the build-only admission gate, the live-session
projection, the coordinator-owned claim guard, and the ADR 0028 log shapes are all asserted at
the coordinator interface — never by poking private transitions.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import replace

import pytest

from conftest import FakeSession, permits, record_of, starts_until_held

from agentflow import coordinated_build
from agentflow.coordinator import BuildStageAdapter, Submission
from agentflow.coordinator import tracer
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import Record
from agentflow.loop import RepoConfig


def _weekly_clear():
    """A fresh Claude seven-day window well under its released weekly allowance, so a stubbed
    clear pool also passes the paced weekly gate the launch admission now enforces (#315)."""
    from agentflow.balancer import RateLimitWindow
    now = time.time()
    return RateLimitWindow(used_percent=1.0, window_minutes=10080,
                           resets_at=now + 10080 * 60 - 3600)


def test_operator_pacing_is_charged_only_after_a_confirmed_start(make_coord, monkeypatch):
    from agentflow.balancer import PoolStatus
    from agentflow.coordinator.launcher import NOT_STARTED, StartResult
    gate = coordinated_build._production_gate()
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, **_: PoolStatus(tool, True, 10.0, active=True,
                                                     windows=(_weekly_clear(),)))
    fake = FakeSession()

    class FirstLaunchMisses:
        calls = 0
        def start(self, record, store):
            self.calls += 1
            return StartResult(NOT_STARTED) if self.calls == 1 else fake.start(record, store)
        def is_alive(self, family):
            return fake.is_alive(family)

    launcher = FirstLaunchMisses()
    coord = make_coord(fake, launcher=launcher, gate=gate)
    for subject in ("1", "2", "3"):
        coord.submit_stage(Submission(repo="o/r", subject=subject, stage="intake",
                                      pool="claude", source="/readonly"))

    coord.cycle("claude")

    assert launcher.calls == 2  # miss is free, one confirmed start spends active pacing


def _converse(subject="c1", *, pool="claude", source="/wt/ask"):
    """An operator-present Ask turn — interactive, so admission is real-time (issue #161)."""
    return Submission(repo="o/r", subject=subject, stage="converse", target="1", pool=pool,
                      complexity="deep", source=source, interactive=True)


def test_interactive_turn_admits_under_a_not_clear_pool(make_coord, monkeypatch):
    # An operator-present Ask turn is a real-time conversation: it launches on the next cycle even
    # when the pool is not clear (here the recent-session cooldown at 47% of an 85% ceiling —
    # nowhere near a real limit), because a permit is freely available (issue #161). Background
    # work on the same pool stays deferred by the not-clear pool. Fails against pre-#161 code,
    # where the interactive turn was gated exactly like background and sat waiting.
    from agentflow.balancer import PoolStatus
    gate = coordinated_build._production_gate()
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    monkeypatch.setattr(
        "agentflow.balancer._query_pool",
        lambda tool, **_: PoolStatus(tool, False, 47.0, reason="a session was active in the last 10m"))
    fake = FakeSession()
    coord = make_coord(fake, gate=gate)
    ask = coord.submit_stage(_converse())
    background = coord.submit_stage(_build())

    coord.cycle("claude")

    assert record_of(coord, ask).state == "running"        # launched despite the not-clear pool
    assert record_of(coord, background).state == "waiting"  # background still deferred


def test_interactive_turn_defers_only_when_no_permit_fits(make_coord, monkeypatch):
    # True zero capacity — no permit obtainable on the reservation ledger — is the ONLY thing that
    # may still defer an interactive turn (issue #161). A background build claims all five of the
    # pool's permits; the Ask turn then cannot reserve its demand and stays waiting.
    from agentflow.balancer import PoolStatus
    gate = coordinated_build._production_gate()
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, **_: PoolStatus(tool, True, 10.0, windows=(_weekly_clear(),)))
    fake = FakeSession()
    coord = make_coord(fake, gate=gate)
    background = coord.submit_stage(_build())  # demand 5 — the whole pool
    coord.cycle("claude")
    assert record_of(coord, background).state == "running" and permits(coord, "claude") == 5

    ask = coord.submit_stage(_converse())
    coord.cycle("claude")
    assert record_of(coord, ask).state == "waiting"  # no permit fits — the sole admissible block


def test_interactive_start_leaves_the_background_pace_slot_intact(monkeypatch):
    # An interactive start must not consume the background active-pacing budget, even on a pool a
    # background record already marked active this cycle (issue #161). With ACTIVE_PACE == 1, an
    # Ask turn admits and charges nothing, so the pool's single background pace slot survives for a
    # background start — which then spends it, deferring the next background record.
    from agentflow.balancer import PoolStatus
    from agentflow.coordinator.record import Record
    gate = coordinated_build._production_gate()
    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, **_: PoolStatus(tool, True, 10.0, active=True,
                                                     windows=(_weekly_clear(),)))
    ask = Record(identity="ask", stage="converse", pool="claude", demand=2, repo="o/r",
                 subject="c1", interactive=True)
    background_a = Record(identity="a", stage="build", pool="claude", demand=5, repo="o/r",
                          subject="7")
    background_b = Record(identity="b", stage="build", pool="claude", demand=5, repo="o/r",
                          subject="8")

    assert gate(ask) is True
    gate.started(ask)                       # interactive — charges no pace budget
    assert gate(background_a) is True       # the one background pace slot is still open
    gate.started(background_a)              # background start spends it
    assert gate(background_b) is False      # now paced out for the cycle


def test_interactive_flag_never_admits_a_disabled_stage(monkeypatch):
    # Stage enablement still holds: an unknown stage marked interactive is refused by the stage
    # gate, before the interactive carve-out is ever consulted (issue #161). _query_pool would
    # raise if reached, proving the refusal short-circuits on stage enablement.
    from agentflow.coordinator.record import Record
    gate = coordinated_build._production_gate()

    def _boom(tool, **_):
        raise AssertionError("stage gate must refuse before the pool is queried")

    monkeypatch.setattr("agentflow.balancer._query_pool", _boom)
    unknown = Record(identity="x", stage="frobnicate", pool="claude", demand=1, repo="o/r",
                     subject="9", interactive=True)
    assert gate(unknown) is False


def test_codex_launch_honors_weekly_unattended_budget(monkeypatch):
    # A codex stage queued while weekly headroom existed must not LAUNCH after that weekly
    # unattended budget is exhausted. Raw `_query_pool` weighs only the short-window ceiling, so
    # a codex pool one hour into the week is "clear" there — the launch gate must additionally
    # apply the same `_codex_dispatch_status` pacing `pick_pair` uses at submission, or a queued
    # build fires on codex despite intake having deferred new work for lack of weekly headroom.
    from agentflow.balancer import PoolStatus, RateLimitWindow
    from agentflow.coordinator.record import Record
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])

    now = 1_000_000.0
    monkeypatch.setattr(coordinated_build.time, "time", lambda: now)
    short = RateLimitWindow(used_percent=10.0, window_minutes=300, resets_at=now + 3600)
    # One hour into the week: only 11.4% of the 80% weekly cap is released for unattended day 0.
    weekly_at = lambda used: RateLimitWindow(used_percent=used, window_minutes=10080,
                                             resets_at=now + 10080 * 60 - 3600)
    codex = Record(identity="392", stage="build", pool="codex", lineage="codex",
                   demand=5, repo="o/r", subject="392")

    # Short window clear, but weekly at 29% is over the 11.4% released — launch defers.
    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, **_: PoolStatus(tool, True, 10.0, windows=(short, weekly_at(29.0))))
    assert coordinated_build._production_gate()(codex) is False

    # Weekly under the released budget — the same queued build launches.
    monkeypatch.setattr("agentflow.balancer._query_pool",
                        lambda tool, **_: PoolStatus(tool, True, 10.0, windows=(short, weekly_at(5.0))))
    assert coordinated_build._production_gate()(codex) is True


def _build(subject="7", *, pool="claude", source="/wt/issue-7", effort="high"):
    return Submission(repo="o/r", subject=subject, stage="build", pool=pool,
                      complexity="deep", effort=effort, source=source)


def _adapter(fake, *, pr, prep, handoff=None, collision=None, main=None):
    """A Build adapter wired to test flags: ``pr``/``prep`` are single-element lists so a test
    flips PR existence and worktree readiness mid-flight; the fake plays observer + launcher.
    ``collision`` scripts the `origin/main` head a reported integration collision names (or None
    for no collision), and ``main`` the current `origin/main` head the deferral gate reads."""
    return BuildStageAdapter(
        pr_exists=lambda r: pr[0],
        worktree_ready=lambda r: prep[0], observer=fake,
        handoff=handoff,
        integration_collision=(lambda r: collision[0]) if collision is not None else None,
        main_head=(lambda r: main[0]) if main is not None else None)


def _records(coord):
    return list(coord._store.load().values())


# --- outcome-first PR verification -------------------------------------------------------

def test_build_completes_when_pr_exists_even_after_a_bad_exit(make_coord):
    fake = FakeSession()
    pr, prep = [True], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    assert coord.cycle("claude") == []            # attempt running
    fake.end(ident, cause=ProviderCause.PROCESS)  # provider exited badly (non-zero)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]  # PR present → done


def test_clean_exit_without_the_pr_stays_incomplete(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)     # clean exit, but no PR
    assert coord.cycle("claude") == []            # not completed — bounded continuation instead
    rec = record_of(coord, ident)
    assert rec.state != "completed" and rec.continuation and rec.attempts == 2


# --- interruption keeps the worktree, lineage, branch, and claim -------------------------

def test_interrupted_build_continues_in_a_fresh_session_keeping_ownership(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)  # interrupted after local changes

    coord.cycle("claude")  # reconciles to waiting, then continues in a fresh session same cycle
    rec = record_of(coord, ident)
    assert rec.attempts == 2                       # a second attempt was consumed
    assert rec.source == "/wt/issue-7"             # same retained worktree
    assert rec.lineage == "claude"                 # pinned tool lineage held
    assert rec.claim is True                        # claim retained across the interruption
    assert rec.state == "running"

    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]


# --- preparation happens before admission ------------------------------------------------

def test_preparation_failure_consumes_no_permit_or_attempt(make_coord):
    fake = FakeSession()
    pr, prep = [False], [False]                    # worktree not ready yet
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0           # nothing reserved
    assert record_of(coord, ident).attempts == 0   # no attempt consumed
    assert record_of(coord, ident).state == "waiting"

    prep[0] = True                                  # worktree recovered → admits normally
    coord.cycle("claude")
    assert permits(coord, "claude") == 5
    assert record_of(coord, ident).attempts == 1


# --- exhaustion ---------------------------------------------------------------------------

def test_exhaustion_holds_once_with_one_handoff_and_notification(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    handoffs = []
    adapter = _adapter(
        fake, pr=pr, prep=prep,
        handoff=lambda record: handoffs.append(record.identity) or "issue-proof",
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_build())
    outcome = None
    for _ in range(6):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held"
    assert outcome.handoff == "issue:needs-grilling"
    rec = record_of(coord, ident)
    assert rec.attempts == 3                        # initial + two continuations, no more
    assert rec.handoffs == 1 and rec.notifications == 1
    assert rec.source == "/wt/issue-7"              # worktree left untouched for human re-entry
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]                       # restart cannot repeat the external handoff


def test_failed_exhaustion_handoff_retries_on_a_later_cycle(make_coord):
    fake = FakeSession()
    proofs = iter((None, "issue-proof"))
    calls = []

    def handoff(record):
        calls.append(record.identity)
        return next(proofs)

    coord = make_coord(
        fake,
        adapter=_adapter(fake, pr=[False], prep=[True], handoff=handoff),
    )
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    for _ in range(2):
        fake.end(ident, cause=ProviderCause.PROCESS)
        assert coord.cycle("claude") == []           # reconcile, then start continuation
    fake.end(ident, cause=ProviderCause.PROCESS)

    assert coord.cycle("claude") == []               # GitHub handoff failed; keep pending
    assert record_of(coord, ident).hold_pending is True
    settled = coord.cycle("claude")                   # next daemon cycle retries finalization

    assert [outcome.status for outcome in settled] == ["held"]
    assert calls == [ident, ident]


# --- integration collision is a durable outcome, not a retryable failure (issue #209) ----

def test_collision_defers_the_retry_while_main_is_unchanged(make_coord):
    # A build that rebases into an integration collision and stops (clean exit, no PR) must not
    # be relaunched into the identical conflict while `origin/main` has not moved: the record
    # defers, charges no attempt, and keeps its claim — instead of burning its budget to a hold.
    fake = FakeSession()
    pr, prep, collision, main = [False], [True], ["mainA"], ["mainA"]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep,
                                              collision=collision, main=main))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")                          # attempt 1 running
    fake.end(ident, cause=ProviderCause.NONE)      # clean exit, no PR, collision comment posted
    assert coord.cycle("claude") == []             # no outcome — deferred, neither held nor relaunched
    rec = record_of(coord, ident)
    assert rec.attempts == 0                        # the doomed attempt was refunded
    assert rec.state == "waiting" and rec.claim is True
    assert rec.collision_main_sha == "mainA"
    assert permits(coord, "claude") == 0            # nothing relaunched
    # Main still unchanged across further cycles → the retry stays deferred, not charged.
    assert coord.cycle("claude") == []
    assert record_of(coord, ident).attempts == 0 and permits(coord, "claude") == 0


def test_a_moved_main_grants_exactly_one_retry(make_coord):
    fake = FakeSession()
    pr, prep, collision, main = [False], [True], ["mainA"], ["mainA"]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep,
                                              collision=collision, main=main))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)      # collision on mainA
    coord.cycle("claude")                           # deferred
    assert record_of(coord, ident).attempts == 0

    main[0] = "mainB"                               # main moved — the conflict may be gone now
    collision[0] = None                             # a fresh session gets a clean rebase
    coord.cycle("claude")                           # prepare passes → the one warranted retry launches
    rec = record_of(coord, ident)
    assert rec.state == "running" and rec.attempts == 1


def test_a_second_collision_after_a_retry_hands_off_visibly(make_coord):
    fake = FakeSession()
    handoffs = []
    pr, prep, collision, main = [False], [True], ["mainA"], ["mainA"]
    adapter = _adapter(fake, pr=pr, prep=prep, collision=collision, main=main,
                       handoff=lambda record: handoffs.append(record.identity) or "issue-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)      # first collision on mainA → deferred
    coord.cycle("claude")

    main[0] = "mainB"                               # main moves; the granted retry launches
    collision[0] = "mainB"                          # and collides again on the new main
    coord.cycle("claude")
    assert record_of(coord, ident).state == "running"
    fake.end(ident, cause=ProviderCause.NONE)      # second collision
    settled = coord.cycle("claude")

    assert [o.status for o in settled] == ["held"]
    assert settled[0].handoff == "issue:needs-grilling"
    rec = record_of(coord, ident)
    assert rec.attempts == 1                         # only the real retry was ever charged
    assert rec.claim is False                        # the building claim is released on handoff
    assert rec.handoffs == 1 and rec.notifications == 1
    assert rec.source == "/wt/issue-7"               # worktree retained for a human to resume
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]                       # a restart never repeats the external handoff


def test_budget_exhaustion_with_a_collision_freshest_hands_off(make_coord):
    # The other handoff trigger: the budget runs out with a collision as the freshest outcome.
    fake = FakeSession()
    handoffs = []
    pr, prep, collision, main = [False], [True], [None], ["mainA"]
    adapter = _adapter(fake, pr=pr, prep=prep, collision=collision, main=main,
                       handoff=lambda record: handoffs.append(record.identity) or "issue-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)   # attempt 1 fails, no collision
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)   # attempt 2 fails, no collision
    coord.cycle("claude")
    collision[0] = "mainA"                           # the last attempt reports a collision
    fake.end(ident, cause=ProviderCause.NONE)
    settled = coord.cycle("claude")

    assert [o.status for o in settled] == ["held"]
    rec = record_of(coord, ident)
    assert rec.attempts == 3 and rec.hold_reason == "integration collision"
    assert handoffs == [ident]


def test_non_collision_exhaustion_is_unchanged(make_coord):
    # A build that exhausts its budget with no collision keeps today's exhaustion handoff.
    fake = FakeSession()
    pr, prep, collision, main = [False], [True], [None], ["mainA"]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep,
                                              collision=collision, main=main,
                                              handoff=lambda record: "issue-proof"))
    ident = coord.submit_stage(_build())
    outcome = None
    for _ in range(6):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held"
    rec = record_of(coord, ident)
    assert rec.attempts == 3 and rec.hold_reason == "continuation budget exhausted"
    assert rec.collision_main_sha is None


# --- maintainer resume of an exhausted held build (#245) ---------------------------------

def test_a_naive_resubmission_of_a_held_build_never_relaunches(make_coord):
    # A Build held after exhausting its budget keeps its stable identity live but terminal. An
    # ordinary resubmission of that same identity (transfer_from=None, supersede=False, resume=0)
    # reuses the held record and launches nothing — the #245 collision the resume path must fix.
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True], handoff=lambda record: "issue-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_build())
    starts_until_held(coord, fake, ident, "claude")
    held = record_of(coord, ident)
    assert held.state == "held" and not held.retired and held.attempts == 3

    resubmitted = coord.submit_stage(_build())
    assert resubmitted == ident                    # same stable identity — no successor record
    assert len(_records(coord)) == 1
    assert coord.cycle("claude") == []             # nothing admitted — still terminal
    after = record_of(coord, ident)
    assert after.state == "held" and after.attempts == 3   # no fresh attempt was charged


def test_a_maintainer_resume_opens_a_fresh_bounded_build_on_the_retained_worktree(make_coord):
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True], handoff=lambda record: "issue-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_build())
    starts_until_held(coord, fake, ident, "claude")
    assert record_of(coord, ident).state == "held"

    # The deliberate maintainer resume opens the next resume dimension — a genuinely new identity.
    resume_ident = coord.submit_stage(replace(_build(), resume=1))
    assert resume_ident != ident and resume_ident.endswith("|s1")
    resumed = record_of(coord, resume_ident)
    assert resumed.state == "waiting"              # eligible to run, unlike the held record
    assert resumed.attempts == 0                   # a fresh bounded attempt budget
    assert resumed.source == "/wt/issue-7"         # same retained worktree recovered, not re-created
    assert resumed.lineage == "claude"             # builder lineage preserved

    coord.cycle("claude")                          # admits and launches a provider session
    started = record_of(coord, resume_ident)
    assert started.state == "running" and started.attempts == 1
    assert record_of(coord, ident).state == "held"  # the held predecessor is left untouched


def test_a_repeated_resume_never_opens_a_second_concurrent_build(make_coord):
    # Idempotency: once a resume is live, re-issuing the same resume returns it unchanged rather
    # than spawning a second concurrent Build at the same resume identity.
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True], handoff=lambda record: "issue-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_build())
    starts_until_held(coord, fake, ident, "claude")

    first = coord.submit_stage(replace(_build(), resume=1))
    coord.cycle("claude")
    again = coord.submit_stage(replace(_build(), resume=1))
    assert first == again
    builds = [r for r in _records(coord) if r.stage == "build" and r.resume == 1]
    assert len(builds) == 1                        # one resume record, not two


# --- idempotent submission ---------------------------------------------------------------

def test_repeated_submission_and_restart_make_one_record(make_coord):
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter)
    first = coord.submit_stage(_build())
    again = coord.submit_stage(_build())
    restarted = make_coord(fake, adapter=adapter).submit_stage(_build())
    assert first == again == restarted
    assert len(_records(coord)) == 1


# --- build is the only enabled stage -----------------------------------------------------

def test_pr_bound_review_admits_and_the_build_stays_waiting(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_review_revise_gate)
    build = coord.submit_stage(_build())
    review = coord.submit_stage(Submission(repo="o/r", subject="7", stage="review",
                                           pool="claude"))
    coord.cycle("claude")
    # The PR-bound review drains first (ADR 0039); the 5-permit build cannot fit beside it.
    assert record_of(coord, review).state == "running"
    build_rec = record_of(coord, build)
    assert build_rec.state == "waiting"            # visibly queued
    assert build_rec.attempts == 0                 # consumed no attempt
    assert permits(coord, "claude") == 1           # only the review's demand is reserved


# --- a never-started build migrates off a throttled pool (#273) --------------------------

def _gate_blocking(*pools):
    """An admission gate that refuses launches on the named pools (e.g. one whose codex weekly
    budget is spent) while its permit ledger is untouched — the launch-gate block that froze the
    multiple enrolled-project builds at zero attempts."""
    blocked = set(pools)
    return lambda record: record.pool not in blocked


# A worktree source in the pool-keyed layout the real builder bakes (loop._builder_worktree),
# so the migration's source rewrite is checked against the actual _source_facts parser.
_SRC = "/work/o-r/.agentflow/worktrees/codex/issue-7-fix"


def test_a_never_started_build_migrates_off_a_throttled_pool_instead_of_deadlocking(make_coord):
    """A waiting, zero-attempt build pinned to codex whose codex launch gate is blocked (weekly
    pacing) while claude has headroom migrates to claude within a single cycle and starts, rather
    than freezing forever behind its own live claim. Reproduces a guarded-project
    incident: before
    the fix the build could neither launch on codex nor fall back to claude, and its live claim
    shielded the building label from reclaim."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_gate_blocking("codex"),
                       adapter=_adapter(fake, pr=[False], prep=[True]))
    ident = coord.submit_stage(_build("7", pool="codex", source=_SRC))
    coord.cycle("codex", now=0)                        # codex cannot launch it; ledger untouched
    frozen = record_of(coord, ident)
    assert frozen.state == "waiting" and frozen.attempts == 0
    assert frozen.claim is True                        # the live claim that shields the label
    assert permits(coord, "codex") == 0

    coord.cycle("claude", now=0)                       # re-placed onto claude, which can launch it
    moved = record_of(coord, ident)
    assert moved.pool == "claude" and moved.state == "running"
    assert moved.lineage == "claude"                   # lineage re-pinned to the destination
    assert moved.source == "/work/o-r/.agentflow/worktrees/claude/issue-7-fix"
    assert coordinated_build._source_facts(moved) is not None  # the real parser accepts the move
    assert permits(coord, "claude") == moved.demand


def test_a_started_build_never_migrates_off_its_pinned_pool(make_coord):
    """A build that already made a real attempt (branch/worktree/PR may exist) stays pinned to its
    builder lineage even when that pool is later throttled — only a genuinely never-started build
    is safe to move (#273)."""
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]))
    ident = coord.submit_stage(_build("7", pool="codex", source=_SRC))
    coord.cycle("codex", now=0)                        # first attempt starts
    fake.end(ident, cause=ProviderCause.PROCESS)       # interrupted → continuation on codex
    coord._gate = _gate_blocking("codex")              # codex now throttled before it can relaunch
    coord.cycle("codex", now=0)                        # reconciles to a waiting continuation
    assert record_of(coord, ident).attempts == 1
    coord.cycle("claude", now=0)                       # claude cycle must not adopt it
    stayed = record_of(coord, ident)
    assert stayed.pool == "codex" and stayed.lineage == "codex"
    assert permits(coord, "claude") == 0


def test_a_never_started_build_stays_home_when_both_pools_are_throttled(make_coord):
    """With both pools launch-blocked the build reverts every moved field cleanly to its home pool
    each cycle (never a half-move or cross-pool leak), and once codex regains budget it launches at
    home from the un-rewritten source — proving the revert restored it intact (#273)."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_gate_blocking("codex", "claude"),
                       adapter=_adapter(fake, pr=[False], prep=[True]))
    ident = coord.submit_stage(_build("7", pool="codex", source=_SRC))
    coord.cycle("codex", now=0)
    home_demand = record_of(coord, ident).demand
    for _ in range(3):                                 # several cycles, both pools blocked
        coord.cycle("codex", now=0)
        coord.cycle("claude", now=0)
    parked = record_of(coord, ident)
    assert parked.pool == "codex" and parked.state == "waiting" and parked.attempts == 0
    assert parked.lineage == "codex" and parked.source == _SRC   # migration fields reverted
    assert parked.demand == home_demand                          # no half-move
    assert permits(coord, "claude") == 0

    coord._gate = _gate_blocking("claude")             # codex regains its weekly budget
    coord.cycle("codex", now=0)                        # launches at home from the intact source
    launched = record_of(coord, ident)
    assert launched.pool == "codex" and launched.state == "running"
    assert coordinated_build._source_facts(launched) is not None


# --- live projection & claim ownership ---------------------------------------------------

def test_running_build_projects_to_live_board_waiting_does_not(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_review_revise_gate)
    coord.submit_stage(_build("7"))
    coord.submit_stage(_build("8", source="/wt/issue-8"))   # a second build cannot fit; stays waiting
    coord.cycle("claude", now=123)
    projection = tracer.live_projection(_records(coord))
    assert [e["number"] for e in projection] == [7]     # only the running build
    assert projection[0]["stage"] == "building" and projection[0]["tool"] == "claude"
    assert set(projection[0]) == {"repo", "number", "title", "stage", "tool", "model",
                                  "branch", "worktree", "pid", "started_at"}
    assert projection[0]["started_at"].startswith("1970-01-01T00:02:03")


def test_live_projection_derives_branch_from_worktree_path(make_coord):
    # live_projection must derive the branch name from the WorktreeRef layout, not by hand-rolling
    # the path split. Fails against the hand-rolled form if the WorktreeRef round-trip disagrees.
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_review_revise_gate)
    coord.submit_stage(_build("7", source="/repo/.agentflow/worktrees/claude/issue-7-owned"))
    coord.cycle("claude")
    projection = tracer.live_projection(_records(coord))
    assert projection[0]["branch"] == "agentflow/claude/issue-7-owned"
    assert projection[0]["worktree"] == "/repo/.agentflow/worktrees/claude/issue-7-owned"


def test_live_projection_branch_is_none_for_non_worktree_source(make_coord):
    # A source path that does not follow the worktree layout yields branch=None.
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_review_revise_gate)
    coord.submit_stage(_build("7", source="/wt/issue-7"))
    coord.cycle("claude")
    projection = tracer.live_projection(_records(coord))
    assert projection[0]["branch"] is None


def test_owned_issues_track_coordinator_ownership(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build("7"))
    coord.cycle("claude")
    # A running build owns its claim.
    assert tracer.owned_issues(_records(coord), "o/r") == {7}
    assert tracer.owned_issues(_records(coord), "other/repo") == set()
    # A completed Build still owns its claim until Review atomically assumes it.
    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


def _owner(stage, subject):
    return Record(identity=f"{stage}-{subject}", stage=stage, pool="claude", demand=1,
                  repo="o/r", subject=str(subject), claim=True)


def test_owned_issues_is_claim_type_aware():
    # Only Intake owns a triaging claim; only Build/Review/Revise own a building claim.
    records = [_owner("build", 7), _owner("review", 9), _owner("revise", 11),
               _owner("intake", 8)]
    assert tracer.owned_issues(records, "o/r", lane="building") == {7, 9, 11}
    assert tracer.owned_issues(records, "o/r", lane="triaging") == {8}
    assert tracer.owned_issues(records, "o/r") == {7, 8, 9, 11}  # lane=None: every owner


def test_one_claim_type_never_hides_the_others_stale_claim():
    # An Intake record owns issue 7's *triaging* claim, not a *building* one — so a stale
    # building claim on issue 7 is NOT shielded from reclamation by the intake record.
    intake = _owner("intake", 7)
    assert 7 not in tracer.owned_issues([intake], "o/r", lane="building")
    assert tracer.owned_issues([intake], "o/r", lane="triaging") == {7}
    # Symmetric: a Build record owns issue 7's building claim, never a triaging one.
    build = _owner("build", 7)
    assert 7 not in tracer.owned_issues([build], "o/r", lane="triaging")
    assert tracer.owned_issues([build], "o/r", lane="building") == {7}


# --- ADR 0028 log shapes -----------------------------------------------------------------

def test_attempt_interrupt_continuation_and_completion_log_shapes(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep), log=lines.append)
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    assert "o/r: 7: build: attempt 1/3 → claude" in lines
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert ("o/r: 7: build: attempt 1/3 interrupted (process) — continuation 1/2 eligible "
            "next cycle; claim retained") in lines
    assert "o/r: 7: build: continuation 1/2 (attempt 2/3) → claude" in lines
    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert "o/r: 7: build: attempt 2/3 completed — pr opened; claim retained" in lines


def test_exhaustion_log_shape(make_coord):
    fake = FakeSession()
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]), log=lines.append)
    ident = coord.submit_stage(_build())
    for _ in range(6):
        if coord.cycle("claude"):
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert ("o/r: 7: build: attempt 3/3 held for human — durable handoff proved; "
            "claim released") in lines


def test_recovered_running_log_shape_after_restart(make_coord):
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter)
    coord.submit_stage(_build())
    coord.cycle("claude")                          # attempt running, family still alive
    # A fresh coordinator over the same store is the restart; the family is still alive.
    lines: list[str] = []
    restarted = make_coord(fake, adapter=adapter, log=lines.append)
    restarted.cycle("claude")
    assert any(line.startswith("o/r: 7: build: recovered running attempt 1/3 pid ")
               and "observing until" in line and "claim retained" in line for line in lines)


def test_claim_transfer_log_shape(make_coord):
    fake = FakeSession()
    pr, prep = [True], [True]
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep), log=lines.append)
    build = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")                          # build completes
    # The next stage assumes the claim — the transfer line names the completed stage and target.
    coord.submit_stage(Submission(repo="o/r", subject="7", stage="review", pool="claude",
                                  transfer_from=build))
    assert "o/r: 7: build: attempt 1/3 completed — pr opened; claim transferred to review" in lines


def test_live_build_preparation_verifies_branch_and_provisions_before_admission(
        tmp_path, monkeypatch):
    from agentflow import loop, runner

    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / "issue-7-owned"
    wt.mkdir(parents=True)
    record = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                    repo="o/r", subject="7", source=str(wt), claim=True, lineage="claude")
    monkeypatch.setattr(runner, "_worktree_is_registered", lambda workdir, path: True)
    provisioned = []
    monkeypatch.setattr(runner.ClaudeRunner, "provision",
                        lambda self, path: provisioned.append(path))
    expected = "agentflow/claude/issue-7-owned"
    monkeypatch.setattr(
        coordinated_build, "_run",
        lambda cmd, cwd=None, timeout=None: subprocess.CompletedProcess(cmd, 0, expected, ""),
    )

    assert coordinated_build._worktree_ready(record) is True
    assert provisioned == [wt]

    monkeypatch.setattr(
        coordinated_build, "_run",
        lambda cmd, cwd=None, timeout=None: subprocess.CompletedProcess(cmd, 0, "wrong", ""),
    )
    assert coordinated_build._worktree_ready(record) is False


def test_pr_outcome_read_failure_does_not_look_like_an_absent_pr(tmp_path, monkeypatch):
    from agentflow import github

    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / "issue-7-owned"
    record = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                    repo="o/r", subject="7", source=str(wt), claim=True, lineage="claude")
    # An unreadable PR listing comes back as None from the module — the Build outcome stays
    # unknown, so it must raise, never be mistaken for an absent PR.
    monkeypatch.setattr(github, "api", lambda *a, **k: None)

    with pytest.raises(RuntimeError, match="cannot verify Build PR outcome"):
        coordinated_build._pr_exists(record)


def _build_hold_seams(monkeypatch, state, *, notifications, labels_readable=None,
                      label_failures=None):
    """Wire the build hold's shared-envelope seams (ADR 0042): the durable comment thread it reads
    and proves the handoff through, the projection that posts the held-route comment and state
    label, the release of the visible building claim, and the operator ping. Everything is stated
    as a fact about the issue, never as a ``gh`` argument vector. ``labels_readable`` is a
    one-element list a test flips to stand a label read that couldn't reach GitHub, and
    ``label_failures`` a countdown of projections whose comment lands but whose label write is
    interrupted.
    """
    from agentflow import github, intake, notify as notify_module

    def drop_building(repo, number, label):
        state["labels"] = [entry for entry in state["labels"] if entry["name"] != label]
        return True

    def fake_apply(repo, number, title, labels, result):
        if not any(comment["body"] == result.body for comment in state["comments"]):
            state["comments"].append({"body": result.body})
        if label_failures and label_failures[0]:
            label_failures[0] -= 1
        else:
            state["labels"] = [{"name": "agentflow:needs-grilling"},
                               {"name": "agentflow:building"}]
        return "applied"

    monkeypatch.setattr(github, "api", lambda *a, **k: state)
    monkeypatch.setattr(github, "issue_comments",
                        lambda repo, number: [github.Comment(body=entry["body"], created_at="")
                                              for entry in state["comments"]])
    monkeypatch.setattr(github, "issue_labels",
                        lambda repo, number: None if labels_readable == [False]
                        else frozenset(entry["name"] for entry in state["labels"]))
    monkeypatch.setattr(github, "remove_label", drop_building)
    monkeypatch.setattr(intake, "apply_intake", fake_apply)
    monkeypatch.setattr(notify_module, "notify", lambda *args: notifications.append(args))


def _held_build_issue():
    return {"title": "Build it", "url": "https://github.com/o/r/issues/7",
            "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:building"}],
            "comments": []}


def _held_build_record(**extra):
    return Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                  repo="o/r", subject="7", source="/retained/wt", claim=True, **extra)


def test_live_exhaustion_handoff_is_idempotent_and_releases_the_visible_claim(monkeypatch):
    state = _held_build_issue()
    notifications = []
    _build_hold_seams(monkeypatch, state, notifications=notifications)
    record = _held_build_record()

    assert coordinated_build._hold_build(record) == state["url"]
    assert coordinated_build._hold_build(record) == state["url"]
    assert {label["name"] for label in state["labels"]} == {"agentflow:needs-grilling"}
    assert len(state["comments"]) == 1
    assert len(notifications) == 1


def test_build_hold_interrupted_after_its_comment_does_not_ping_the_operator_twice(monkeypatch):
    # The crash window the shared envelope exists to close: the hold comment lands and the
    # operator is pinged, then the daemon dies before the held state is proven (stood here as a
    # label read that couldn't reach GitHub). This hold used to ping with no delivery key at all,
    # so the restart's ping arrived as a second notification; now the comment gates it.
    state = _held_build_issue()
    notifications, readable = [], [False]
    _build_hold_seams(monkeypatch, state, notifications=notifications, labels_readable=readable)
    record = _held_build_record()

    assert coordinated_build._hold_build(record) is None
    assert len(state["comments"]) == 1 and len(notifications) == 1

    readable[0] = True
    assert coordinated_build._hold_build(record) == state["url"]
    assert len(state["comments"]) == 1 and len(notifications) == 1
    assert {label["name"] for label in state["labels"]} == {"agentflow:needs-grilling"}
    assert notifications[0][3]   # the ping carries a delivery key it previously had none of


def test_a_hold_projection_interrupted_before_its_state_label_is_still_finished(monkeypatch):
    # Projecting the hold is idempotent across partial writes, so the envelope's post-once gate
    # must not strand one: a hold whose comment landed but whose state label did not still
    # reaches the held state, on a later pass, without a second comment or a second ping.
    state = _held_build_issue()
    notifications = []
    _build_hold_seams(monkeypatch, state, notifications=notifications, label_failures=[1])
    record = _held_build_record()

    assert coordinated_build._hold_build(record) is None   # the label write was interrupted
    assert coordinated_build._hold_build(record) == state["url"]
    assert {label["name"] for label in state["labels"]} == {"agentflow:needs-grilling"}
    assert len(state["comments"]) == 1 and len(notifications) == 1


def test_live_collision_handoff_names_the_collision_and_leaves_a_resumable_issue(monkeypatch):
    # A collision handoff routes through the same visible stuck-build handoff, but its comment and
    # notification name the collision — and it leaves the issue in the resume-ready state ADR 0019
    # intake picks up on a maintainer reply (needs-grilling, no building/ready-for-agent claim).
    state = _held_build_issue()
    notifications = []
    _build_hold_seams(monkeypatch, state, notifications=notifications)
    record = _held_build_record(hold_reason="integration collision")

    assert coordinated_build._hold_build(record) == state["url"]
    # Resume-ready for a maintainer reply: needs-grilling only, no build claim, no ready-for-agent.
    assert {label["name"] for label in state["labels"]} == {"agentflow:needs-grilling"}
    body = state["comments"][0]["body"]
    assert "collision" in body and "/retained/wt" in body   # names the collision + retained worktree
    assert len(notifications) == 1
    assert "integration collision" in notifications[0][1].lower()
