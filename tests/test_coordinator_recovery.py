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

import pytest

from conftest import FakeSession, permits, record_of, starts_until_held

from agentflow.coordinator import Submission
from agentflow.coordinator import providers
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.recovery import (NO_NEW_STATE, REPAIR, Recovery,
                                            durable_progress, targeted_repair)
from agentflow.pool_control import POOLS, pool_paused
from agentflow.routing import routing
from agentflow.runner import codex_spent_at_render


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


class CapturingSession(ProgressingSession):
    """Records the prompt at the launcher boundary while retaining the public fake world."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []

    def start(self, record, store):
        self.prompts.append(providers._durable_prompt(record))
        return super().start(record, store)


def _review(subject: str = "7", pool: str = "claude", **kw) -> Submission:
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool, **kw)


def _build(subject: str = "7", pool: str = "claude", **kw) -> Submission:
    return Submission(repo="o/r", subject=subject, stage="build", pool=pool,
                      builder_lineage=pool, **kw)


_TASK_PREFIX = "Implement issue 529 exactly; preserve its recovery facts.\n"


def _stale_native_helper_contract(*, codex_spent: bool = False) -> str:
    return routing.session_lead_instructions(
        "build", None, parent_provider="codex", codex_spent=codex_spent).replace(
            "Codex workers use the bounded AgentFlow command.",
            "Codex workers use native Codex sub-agents.")


def _observed_529_brief(prefix: str = _TASK_PREFIX) -> str:
    return prefix + _stale_native_helper_contract()


def _submit_codex_session_lead(coord, stale: str, native_helpers_marker: str | None) -> str:
    identity = coord.submit_stage(_build(
        pool="codex", input_ptr=stale, source="/wt/issue-529",
        session_lead=native_helpers_marker is None))
    if native_helpers_marker is not None:
        record = record_of(coord, identity)
        record.native_helpers_marker = native_helpers_marker
        coord._store.upsert(record)
    return identity


@pytest.mark.parametrize("native_helpers_marker", [None, "codex-cli 0.144.0\n"],
                         ids=["current", "pre-555-native-helper"])
def test_codex_continuation_refreshes_a_session_lead_contract_at_launch(
        make_coord, native_helpers_marker):
    """Current and #529-shaped Codex records keep task/recovery bytes, never stale policy."""
    fake = CapturingSession()
    coord = make_coord(fake)
    stale = _observed_529_brief()
    identity = _submit_codex_session_lead(coord, stale, native_helpers_marker)
    coord.cycle("codex")
    fake.end(identity, cause=ProviderCause.PROCESS)

    assert coord.cycle("codex") == []
    record = record_of(coord, identity)
    assert record.input_ptr == stale
    assert record.recovery_envelope is not None
    assert len(fake.prompts) == 2
    refreshed = providers._durable_prompt(record).removesuffix(
        f"\n\n{record.recovery_envelope}")
    expected_contract = routing.session_lead_instructions(
        record.stage, record.effort, parent_provider=record.pool,
        codex_spent=codex_spent_at_render(),
        unavailable_providers=frozenset(pool for pool in POOLS if pool_paused(pool)))
    assert refreshed == _TASK_PREFIX + expected_contract
    assert fake.prompts == [refreshed, f"{refreshed}\n\n{record.recovery_envelope}"]
    assert "Codex workers use the bounded AgentFlow command." in refreshed
    assert "agentflow-codex-worker --worker <routed-name>" in refreshed
    assert "native Codex sub-agents" not in refreshed
    assert "spawn_agent agent_type roles" not in refreshed


def test_restart_re_admission_refreshes_a_legacy_session_lead_contract_at_launch(make_coord):
    """A restart-resumed historical record receives the current worker contract too."""
    fake = CapturingSession()
    stale = _observed_529_brief()
    started = make_coord(fake, daemon_generation="before-restart")
    identity = _submit_codex_session_lead(started, stale, "codex-cli 0.144.0\n")
    started.cycle("codex")
    fake.kill(identity)

    resumed = make_coord(fake, daemon_generation="after-restart")
    assert resumed.cycle("codex") == []
    assert len(fake.prompts) == 2
    assert all("Codex workers use the bounded AgentFlow command." in prompt
               for prompt in fake.prompts)
    assert all("native Codex sub-agents" not in prompt for prompt in fake.prompts)


def test_task_marker_before_generated_session_lead_contract_preserves_task_bytes(make_coord):
    fake = CapturingSession()
    coord = make_coord(fake)
    task_text = ("Implement the task's own heading exactly.\n"
                 "## Session lead — benchmarked capability routing\n"
                 "This heading belongs to the task, not AgentFlow policy.\n")
    identity = _submit_codex_session_lead(coord, _observed_529_brief(task_text), None)

    assert coord.cycle("codex") == []
    record = record_of(coord, identity)
    expected_contract = routing.session_lead_instructions(
        record.stage, record.effort, parent_provider=record.pool,
        codex_spent=codex_spent_at_render(),
        unavailable_providers=frozenset(pool for pool in POOLS if pool_paused(pool)))
    assert fake.prompts == [task_text + expected_contract]
    assert record.input_ptr == _observed_529_brief(task_text)


def test_generated_session_lead_preamble_refreshes_at_launch(make_coord):
    fake = CapturingSession()
    coord = make_coord(fake)
    stale = _TASK_PREFIX + _stale_native_helper_contract(codex_spent=True)
    identity = _submit_codex_session_lead(coord, stale, None)

    assert coord.cycle("codex") == []
    assert fake.prompts[0].startswith(_TASK_PREFIX)
    assert "native Codex sub-agents" not in fake.prompts[0]


@pytest.mark.parametrize(("task_text", "session_lead"), [
    ("Task-owned section\n## Session lead — benchmarked capability routing\nkeep this text",
     False),
    ("Task-owned section\n## Session lead — benchmarked capability routing\nkeep this text",
     True),
    (_observed_529_brief()[:-20], True),
    (_observed_529_brief() + _stale_native_helper_contract(), True),
], ids=["marker-only-no-provenance", "marker-only-provenance", "truncated-proven-contract",
        "duplicate-proven-contract"])
def test_unproven_or_incomplete_session_lead_input_refuses_before_provider_start(
        make_coord, task_text, session_lead):
    fake = CapturingSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(_build(
        pool="codex", input_ptr=task_text, source="/wt/issue-529", session_lead=session_lead))

    assert coord.cycle("codex") == []
    record = record_of(coord, identity)
    assert fake.prompts == []
    assert identity not in fake.family_of
    assert fake.alive == set()
    assert record.attempts == 0
    assert record.refusal.startswith("session-lead-input-unreadable:")
    assert record.input_ptr == task_text


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

def test_a_capacity_interruption_never_holds_and_never_spends_the_budget(make_coord):
    """A provider-declared five-hour capacity interruption is an automatic reset wait, not a spent
    attempt (AC5, #305): it refunds the attempt and requeues eligible at the reset, so it never
    consumes the bounded continuation budget and never hardens into a durable hold — even after far
    more interruptions than the attempt budget, and even when the stage reports no new state."""
    fake = ClassifyingSession(Recovery(NO_NEW_STATE))
    coord = make_coord(fake)
    identity = coord.submit_stage(_review())
    for i in range(6):                                   # twice the attempt budget
        base = i * 1000                                  # monotonic clock across iterations
        coord.cycle("claude", now=base)                  # the reset wait re-admits a fresh attempt
        assert permits(coord, "claude") == 1
        fake.end(identity, cause=ProviderCause.CAPACITY, reset_at=base + 100)
        assert coord.cycle("claude", now=base + 50) == []  # free reset wait — never held, not eligible yet
        rec = record_of(coord, identity)
        assert rec.state == "waiting" and not rec.hold_pending
        assert rec.attempts == 0                         # the reset wait refunded the attempt
        assert rec.eligible_at == base + 100 and rec.repairs == 0


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
    revision = "9" * 40
    identity = coord.submit_stage(_review(pool="codex", subject_revision=revision))
    coord.cycle("codex")
    assert permits(coord, "codex") == 2                 # a codex review is running
    before = record_of(coord, identity)
    route = (before.route_id, before.route_cell_digest, before.launch_config_digest)
    assert all(route)

    fake.kill(identity)                                 # a restart kills the family — no end fact
    restarted = make_coord(fake, daemon_generation="gen-2")
    restarted.cycle("codex")

    rec = record_of(restarted, identity)
    assert rec.restart_resumes == 1 and rec.repairs == 0
    assert rec.subject_revision == revision
    assert (rec.route_id, rec.route_cell_digest, rec.launch_config_digest) == route


# --- a stage with no classifier keeps the historical behavior ----------------------------

def test_a_stage_with_no_recovery_classifier_keeps_the_full_budget(make_coord):
    """A bare adapter with no ``recover`` hook is unchanged: every non-permanent ending continues
    within the budget, and no recovery envelope is ever stamped."""
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(_review())
    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 3
    assert record_of(coord, identity).recovery_envelope is None


# --- a typed verify miss is named everywhere a human or fresh session looks ---------------

class MissingProofSession(FakeSession):
    """A worktree stage whose verifier is typed: every unverified ending names the conjunct."""

    def verify(self, record, obs):
        from agentflow.coordinator.verification import VERIFIED, unverified
        ending = self._script.get(record.identity)
        if ending and ending.success:
            return VERIFIED
        return unverified("targeted-reply", "no marked agentflow reply names target 'IC_1'")

    def recover(self, record, obs) -> Recovery:
        return durable_progress(record, "a posted reply to the answered comment")


def test_a_typed_verify_miss_is_named_in_envelope_hold_reason_and_telemetry(make_coord):
    """The diagnosability contract end to end: an unverified ending records its first failed
    conjunct on the record, hands it to the fresh session's envelope, stamps it into every
    attempt's telemetry, and the exhaustion hold reason carries it — no more silent False."""
    from agentflow.coordinator.telemetry import read_attempts
    fake = MissingProofSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="respond", pool="claude", target="IC_1",
        source="/wt/pr-7", input_ptr="answer the maintainer comment"))
    coord.cycle("claude")                              # attempt 1 running
    fake.end(identity, cause=ProviderCause.NONE)       # clean exit, reply not recognized
    assert coord.cycle("claude") == []                 # continuation granted, not parked

    rec = record_of(coord, identity)
    assert rec.verify_miss.startswith("targeted-reply: ")
    assert "targeted-reply" in (rec.recovery_envelope or "")

    starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE)
    rec = record_of(coord, identity)
    assert rec.state == "held" and "targeted-reply" in (rec.hold_reason or "")
    entries = read_attempts(coord._store.path)
    assert entries and all(e.verify_miss.startswith("targeted-reply") for e in entries)


def test_the_recovery_envelope_names_the_exact_failed_check():
    from agentflow.coordinator.record import Record
    record = Record(identity="o/r|7|respond|IC_1", stage="respond", pool="claude", demand=3,
                    repo="o/r", subject="7", attempts=1, source="/wt/pr-7",
                    verify_miss="pushed-head: the reply marks change 'abc' but the live PR "
                                "head is 'def'")
    env = durable_progress(record, "a posted reply to the answered comment").envelope
    assert "pushed-head" in env and "must not be redone" in env
