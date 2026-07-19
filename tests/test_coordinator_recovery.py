"""Identical fresh-session replays are stopped when no new recovery state exists (issue #225).

Every case is driven through the coordinator's public ``submit_stage`` / ``cycle`` seam with a
:class:`FakeSession` that also classifies recovery, exactly as a live stage adapter does. The
coordinator turns that classification into a bounded continuation, a single targeted repair, or a
park — never a blind replay of the identical durable prompt when a fresh session would have
nothing new to act on.

The four recovery buckets from the issue are covered here: a clean exit with a missing required
outcome (one targeted repair, then park), retained partial work (continue within the budget behind
a bounded envelope), a genuine capacity interruption (always continues automatically), and a
daemon restart (resumes uncharged, never mistaken for a repair). The restart cap itself lives in
tests/test_coordinator_launcher.py.
"""

from __future__ import annotations

from conftest import FakeSession, permits, record_of, starts_until_held

from agentflow.coordinator import Submission
from agentflow.coordinator import providers
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.recovery import (NO_NEW_STATE, REPAIR, Recovery,
                                            durable_progress, targeted_repair)


class ClassifyingSession(FakeSession):
    """A FakeSession whose stage adapter also returns a fixed recovery classification, so the
    coordinator's identical-replay policy is exercised through the public seam."""

    def __init__(self, recovery: Recovery) -> None:
        super().__init__()
        self.recovery = recovery

    def recover(self, record, obs) -> Recovery:
        return self.recovery


class RepairingSession(FakeSession):
    """A read-only stage: a clean exit with no outcome earns one targeted repair naming the proof."""

    def recover(self, record, obs) -> Recovery:
        return targeted_repair(record, "a recorded review verdict for the reviewed head SHA")


class ProgressingSession(FakeSession):
    """A worktree-owning stage: a continuation carries retained partial work forward."""

    def recover(self, record, obs) -> Recovery:
        return durable_progress(record, "an opened pull request on the owned branch")


def _review(subject: str = "7", pool: str = "claude", **kw) -> Submission:
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool, **kw)


def _build(subject: str = "7", pool: str = "claude", **kw) -> Submission:
    return Submission(repo="o/r", subject=subject, stage="build", pool=pool,
                      builder_lineage=pool, **kw)


# --- clean exit, missing outcome: one targeted repair, then park -------------------------

def test_a_clean_exit_missing_its_outcome_gets_one_repair_then_parks(make_coord):
    """A clean provider exit with no verified outcome and no durable partial work: the initial
    attempt plus exactly one targeted repair, then a park — never a second identical full retry."""
    fake = ClassifyingSession(Recovery(REPAIR, "name the missing verdict"))
    coord = make_coord(fake)
    identity = coord.submit_stage(_review())

    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 2
    rec = record_of(coord, identity)
    assert rec.state == "held" and rec.repairs == 1
    assert rec.hold_reason == "no new recovery state to act on"


def test_the_targeted_repair_names_the_missing_outcome_and_keeps_the_base_task(make_coord):
    """The one repair is not an identical replay: its prompt carries a bounded recovery envelope
    that names the exact missing proof and preserves the base task, without a transcript."""
    fake = RepairingSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(_review(input_ptr="review the PR at sha-a"))
    coord.cycle("claude")                               # attempt 1 running
    fake.end(identity, cause=ProviderCause.NONE)        # clean exit, no verdict

    assert coord.cycle("claude") == []                  # one repair granted — not yet a park
    rec = record_of(coord, identity)
    assert rec.recovery_envelope is not None
    prompt = providers._durable_prompt(rec)
    assert "review the PR at sha-a" in prompt                       # the base task is preserved
    assert "required outcome" in prompt and "verdict" in prompt     # the missing proof is named
    assert rec.recovery_envelope.count("\n") < 8                    # bounded facts, not a transcript


# --- retained partial work: continue within the budget behind an envelope ----------------

def test_retained_partial_work_continues_and_points_the_session_at_its_worktree(make_coord):
    """A worktree-owning stage's continuation is genuinely new state, so it keeps continuing — and
    the fresh session is handed the retained worktree so it resumes instead of restarting."""
    fake = ProgressingSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(
        _build(source="/wt/issue-7", input_ptr="build issue 7"))
    coord.cycle("claude")                               # attempt 1 running
    fake.end(identity, cause=ProviderCause.NONE)        # clean exit, no PR yet

    assert coord.cycle("claude") == []                  # continues (partial work), not parked
    rec = record_of(coord, identity)
    assert rec.continuation and rec.repairs == 0        # a progress continuation is not a repair
    prompt = providers._durable_prompt(rec)
    assert "/wt/issue-7" in prompt and "build issue 7" in prompt


def test_a_worktree_stage_keeps_its_full_continuation_budget(make_coord):
    """Continuing on retained work is not the waste the issue targets, so a worktree-owning stage
    still runs its full budget before holding — the replay-stop never shortens a legitimate build."""
    fake = ProgressingSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(_build(source="/wt/issue-7", input_ptr="build issue 7"))
    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 3


# --- genuine capacity interruption: always continues -------------------------------------

def test_a_capacity_interruption_always_continues_even_with_no_new_state(make_coord):
    """A rate/quota interruption is an external event, not an identical replay: it continues
    automatically within the budget even when the stage reports no new recovery state (AC5)."""
    fake = ClassifyingSession(Recovery(NO_NEW_STATE))
    coord = make_coord(fake)
    identity = coord.submit_stage(_review())
    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.CAPACITY) == 3
    assert record_of(coord, identity).repairs == 0


# --- no new state at all: park at once, no replay ----------------------------------------

def test_no_new_state_parks_at_once_without_a_single_replay(make_coord):
    """The strongest waste case: a clean exit that left nothing new to act on parks immediately —
    not one identical fresh session is spent."""
    fake = ClassifyingSession(Recovery(NO_NEW_STATE))
    coord = make_coord(fake)
    identity = coord.submit_stage(_review())
    coord.cycle("claude")                               # attempt 1 running
    fake.end(identity, cause=ProviderCause.NONE)        # clean exit, nothing new

    assert [o.status for o in coord.cycle("claude")] == ["held"]
    rec = record_of(coord, identity)
    assert rec.attempts == 1 and rec.repairs == 0
    assert rec.hold_reason == "no new recovery state to act on"


# --- daemon restart: resumes uncharged, never counted as a repair ------------------------

def test_a_daemon_restart_resume_is_not_counted_as_a_repair(make_coord):
    """A family a daemon restart kills leaves no provider end fact and resumes in place uncharged.
    It must never be mistaken for a clean-exit repair, or a restart storm would burn the repair
    budget and park work a live provider never actually failed."""
    fake = RepairingSession()
    coord = make_coord(fake, daemon_generation="gen-1")
    identity = coord.submit_stage(_review(pool="codex"))
    coord.cycle("codex")
    assert permits(coord, "codex") == 2                 # a codex review is running

    fake.kill(identity)                                 # a restart kills the family — no end fact
    restarted = make_coord(fake, daemon_generation="gen-2")
    restarted.cycle("codex")

    rec = record_of(restarted, identity)
    assert rec.restart_resumes == 1 and rec.repairs == 0


# --- a stage with no classifier keeps the historical behavior ----------------------------

def test_a_stage_with_no_recovery_classifier_keeps_the_full_budget(make_coord):
    """A bare adapter with no ``recover`` hook is unchanged: every non-permanent ending continues
    within the budget, and no recovery envelope is ever stamped."""
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(_review())
    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 3
    assert record_of(coord, identity).recovery_envelope is None
