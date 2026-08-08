"""The stage-adapter skeleton and the one set of defaults behind it.

Every logical stage extends the same base (``StageAdapter``), and every optional hook has exactly
one default (``StageCalls``, which the coordinator and the router both call through). These tests
pin the two properties that made the old duplication a defect: a stage's hold names the same
handoff as ADR 0028's exhaustion table, and a record whose stage has no adapter registered behaves
like a bare coordinator instead of a differently-conservative second layer.
"""

from __future__ import annotations

import pytest

from agentflow.coordinator import (BuildStageAdapter, ConverseStageAdapter, IntakeStageAdapter,
                                   MockupStageAdapter, ResearchStageAdapter, RespondStageAdapter,
                                   ReviewStageAdapter, ReviseStageAdapter, StageRouter)
from agentflow.coordinator.admission import STAGE_NATIVE_HANDOFF
from agentflow.coordinator.providers import ProviderObservation
from agentflow.coordinator.record import Record
from agentflow.coordinator.recovery import PROGRESS

# One bare adapter per logical stage: only the collaborator each stage cannot do without, so the
# shared skeleton's own defaults are what these tests observe.
BARE_ADAPTERS = {
    "intake": lambda: IntakeStageAdapter(worktree_reset=lambda record: True,
                                         apply_route=lambda record, result: "url"),
    "build": lambda: BuildStageAdapter(pr_exists=lambda record: False),
    "review": lambda: ReviewStageAdapter(verdict_ready=lambda record, obs: False),
    "revise": lambda: ReviseStageAdapter(revision_ready=lambda record, obs: False),
    "respond": lambda: RespondStageAdapter(reply_ready=lambda record, obs: False),
    "mockup": lambda: MockupStageAdapter(outcome_ready=lambda record, obs: False),
    "converse": lambda: ConverseStageAdapter(reply_ready=lambda record, obs: False),
    "research": lambda: ResearchStageAdapter(findings_ready=lambda record, obs: False),
}


def _record(stage: str, **fields) -> Record:
    return Record(identity=f"o/r#1:{stage}", stage=stage, pool="claude", demand=1, **fields)


@pytest.mark.parametrize("stage", sorted(BARE_ADAPTERS))
def test_a_stage_holds_with_the_handoff_its_exhaustion_row_names(stage):
    """A stage that wires no park/hold collaborator still proves the *same* handoff the coordinator
    stamps on the record — the kind lives in one table, not in a literal per adapter."""
    record = _record(stage)
    proof = BARE_ADAPTERS[stage]().finalize_hold(record)
    assert proof == f"proof:{record.identity}:{STAGE_NATIVE_HANDOFF[stage]}"


# Intake rebuilds its read-only checkout and Review resets one at an exact head SHA, so both
# supply their own readiness rather than the shared "the record already owns a source" default.
@pytest.mark.parametrize("stage", sorted(set(BARE_ADAPTERS) - {"intake", "review"}))
def test_a_stage_waits_for_the_worktree_it_owns_before_admission(stage):
    """The shared preparation: no durable source means nothing to resume, so the stage is not
    ready and the coordinator spends neither a permit nor an attempt."""
    adapter = BARE_ADAPTERS[stage]()
    assert not adapter.prepare(_record(stage, source=None))
    assert adapter.prepare(_record(stage, source="/tmp/wt"))


@pytest.mark.parametrize("stage", sorted(BARE_ADAPTERS))
def test_a_continuation_is_told_which_outcome_is_still_missing(stage):
    """The shared recovery envelope names the stage's own required outcome, so a fresh session
    resumes toward it rather than replaying the original prompt."""
    adapter = BARE_ADAPTERS[stage]()
    recovery = adapter.recover(_record(stage, source="/tmp/wt", attempts=1),
                               ProviderObservation())
    assert recovery.envelope
    if adapter.required_outcome:
        assert adapter.required_outcome in recovery.envelope


def test_a_stage_with_no_adapter_registered_falls_back_like_a_bare_coordinator():
    """The router's defaults are the coordinator's defaults — one layer, not two. A record whose
    stage nobody registered prepares trivially, proves no outcome, projects nothing, and still
    holds against its own exhaustion-table handoff."""
    router = StageRouter({"build": BARE_ADAPTERS["build"]()})
    record = _record("review")
    obs = ProviderObservation()
    assert router.prepare(record)
    assert router.observe(record) == ProviderObservation()
    assert router.verify(record, obs) is False
    assert router.capture(record, obs) is None
    assert router.prepare_completed(record) is True
    assert router.finalize_completed(record) is None
    assert router.integration_collision(record) is None
    assert router.recover(record, obs).kind == PROGRESS
    assert router.finalize_hold(record) == f"proof:{record.identity}:pr:parked"


def test_review_rejects_a_verdict_after_a_captured_follow_up_issue_create():
    """Review cannot settle after its session used the retired tracker-write action."""
    adapter = ReviewStageAdapter(verdict_ready=lambda record, obs: True)
    obs = ProviderObservation(events=(
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "gh issue create --title follow-up"}},
    ))

    assert adapter.verify(_record("review"), obs) is False


def test_review_keeps_tracker_reads_available_without_the_retired_write_action():
    adapter = ReviewStageAdapter(verdict_ready=lambda record, obs: True)
    obs = ProviderObservation(events=(
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "gh issue list --limit 10"}},
    ))

    assert adapter.verify(_record("review"), obs) is True


def test_a_stage_that_classifies_no_recovery_keeps_continuing_within_budget():
    """A partial adapter — one that implements neither ``recover`` nor ``integration_collision`` —
    keeps the historical behavior instead of tripping over the missing hook."""

    class BareStage:
        def verify(self, record, obs):
            return False

    router = StageRouter({"build": BareStage()})
    record = _record("build")
    assert router.recover(record, ProviderObservation()).kind == PROGRESS
    assert router.integration_collision(record) is None
    assert router.finalize_hold(record) == f"proof:{record.identity}:issue:needs-grilling"
