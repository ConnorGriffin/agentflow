"""Per-stage production health (#430), exercised through its one public read.

Every fixture writes real per-attempt records with the coordinator's own writer, so these
run over the same durable store the daemon reads in production — not a stand-in shape.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from agentflow.attack import AttackResult
from agentflow.coordinator.attack_stage import encode_result
from agentflow.coordinator.telemetry import AttemptTelemetry, record_attempt
from agentflow.stage_health import DROUGHT_WINDOW, stage_health


@pytest.fixture
def store(tmp_path):
    return tmp_path / "records.db"


def session(store, *, stage, identity, attempt=1, verified=True, outcome="done",
            finalized_at=1_000):
    """One ended session on record, exactly as the coordinator stamps it."""
    record_attempt(store, AttemptTelemetry(
        token=uuid4().hex, identity=identity, repo="o/r", subject="1",
        stage=stage, pool="claude", model="opus", complexity="deep", effort=None,
        reasoning_effort=None, attempt=attempt, continuation=attempt > 1, restart_resumes=0,
        round=0, conflict_round=0, verified=verified, outcome=outcome if verified else "",
        cause="none", classification="complete", started_at=finalized_at - 60,
        finalized_at=finalized_at))


def attacked(store, identity, *, objections="1. it breaks", remedied=False, at=1_000):
    """One finished attack round that captured a readable answer — the engine's own idea of a
    clean finish — carrying whatever the attacker actually said."""
    session(store, stage="attack", identity=identity, verified=True,
            outcome=encode_result(AttackResult(objections, True, "", remedied, "")),
            finalized_at=at)


def only(rows, stage):
    return next(row for row in rows if row["stage"] == stage)


def test_a_stage_that_finished_ten_attempts_and_published_nothing_reads_as_producing_nothing(
        store):
    """The regression this exists to prevent. Every one of these rounds finished cleanly by the
    attack stage's own reckoning — it captured a readable answer — and not one of them cleared
    a draft for publication, which is the whole of what the stage is for (#418)."""
    for i in range(DROUGHT_WINDOW):
        attacked(store, f"o/r|{i}|attack|", at=1_000 + i)

    row = only(stage_health(store), "pre-publish attack")
    assert (row["attempts"], row["produced"]) == (DROUGHT_WINDOW, 0)
    assert row["produces"] == "published a hardened brief"
    assert row["check"] == "held drafts and ready-for-agent holdbacks"
    assert row["window"] == DROUGHT_WINDOW


def test_a_round_that_cleared_its_draft_counts_as_production(store):
    """Both ways a brief actually publishes: nothing survived the attack, or everything that
    survived carries its own fix and none of it is a fork."""
    for i in range(DROUGHT_WINDOW - 2):
        attacked(store, f"o/r|{i}|attack|", at=1_000 + i)
    attacked(store, "o/r|survived|attack|", objections="", at=2_000)
    attacked(store, "o/r|remedied|attack|", remedied=True, at=2_001)

    row = only(stage_health(store), "pre-publish attack")
    assert (row["attempts"], row["produced"]) == (DROUGHT_WINDOW, 2)


def test_a_stage_that_verifies_its_own_artifact_counts_that_verification(store):
    for i in range(DROUGHT_WINDOW):
        session(store, stage="review", identity=f"o/r|{i}|review|", verified=i < 3,
                attempt=3, finalized_at=1_000 + i)

    row = only(stage_health(store), "review")
    assert (row["attempts"], row["produced"]) == (DROUGHT_WINDOW, 3)
    assert row["produces"] == "recorded a review verdict"
    assert row["check"] == "open PRs waiting on a verdict"


def test_one_attempt_that_burned_several_sessions_is_one_attempt_that_cost_them(store):
    for attempt in (1, 2, 3):
        session(store, stage="review", identity="o/r|1|review|", attempt=attempt,
                verified=False, finalized_at=1_000 + attempt)

    row = only(stage_health(store), "review")
    assert (row["attempts"], row["produced"], row["sessions_spent"]) == (1, 0, 3)


def test_an_attempt_that_may_still_get_another_session_is_not_counted_at_all(store):
    """A stage caught mid-run must never read as a stage producing nothing — the signal is
    absence, so absence has to be provable."""
    session(store, stage="review", identity="o/r|1|review|", attempt=1, verified=False)

    assert stage_health(store) == []


def test_only_the_last_window_of_finished_attempts_is_judged(store):
    """An old drought a stage has since recovered from never keeps the row alive."""
    for i in range(DROUGHT_WINDOW):
        session(store, stage="build", identity=f"o/r|old{i}|build|", verified=False,
                attempt=3, finalized_at=1_000 + i)
    for i in range(DROUGHT_WINDOW):
        session(store, stage="build", identity=f"o/r|new{i}|build|", finalized_at=2_000 + i)

    row = only(stage_health(store), "build")
    assert (row["attempts"], row["produced"]) == (DROUGHT_WINDOW, DROUGHT_WINDOW)


def test_a_stage_with_fewer_finished_attempts_than_the_window_reports_what_it_has(store):
    for i in range(4):
        attacked(store, f"o/r|{i}|attack|", at=1_000 + i)

    row = only(stage_health(store), "pre-publish attack")
    assert (row["attempts"], row["produced"], row["window"]) == (4, 0, DROUGHT_WINDOW)


def test_every_stage_reports_separately_in_a_stable_order(store):
    attacked(store, "o/r|1|attack|")
    session(store, stage="review", identity="o/r|2|review|")
    session(store, stage="build", identity="o/r|3|build|")

    assert [row["stage"] for row in stage_health(store)] == [
        "build", "pre-publish attack", "review"]


def test_nothing_on_record_reports_nothing(store):
    assert stage_health(store) == []


def test_no_two_stages_describe_themselves_the_same_way(store):
    """Two broken stages that read the same tell the operator nothing about either — the name,
    the artifact, and where to look are all the stage's own."""
    for stage in ("intake", "attack", "build", "review", "revise", "mockup", "respond",
                  "converse", "research"):
        session(store, stage=stage, identity=f"o/r|1|{stage}|")

    rows = stage_health(store)
    assert len(rows) == 9
    for field in ("stage", "produces", "check"):
        assert len({row[field] for row in rows}) == 9
