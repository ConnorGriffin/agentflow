"""The coordinator's public seam (ADR 0030): idempotent logical-stage submission and pool
cycling, with the admission matrix, continuation priority, atomic permit reservation, and
provider observations kept private. Everything here is driven through ``submit_stage`` and
``cycle`` — the only two calls stage orchestration makes.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import pytest

from conftest import FakeSession, NeverStartsLauncher, permits, record_of

from agentflow.coordinator import Coordinator, StageOutcome, Submission
from agentflow.coordinator.admission import ADMISSION_MATRIX, PERMIT_BUDGET, admission_demand
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.store import ReservationLimits, Store
from agentflow.capability_contracts import CapabilityPreflightResult, ContractRequirement
from agentflow.coordinator.record import Record


def test_submission_binds_one_subject_revision_and_route_through_store_seam_before_persistence(
        make_coord, monkeypatch):
    calls = []
    original = Store.route_selection_identity

    def public_identity(store, selection):
        calls.append(selection)
        return original(store, selection)

    monkeypatch.setattr(Store, "route_selection_identity", public_identity)
    coord = make_coord(FakeSession())
    revision = "a" * 40

    identity = coord.submit_stage(Submission(
        repo="o/r", subject="627", stage="build", pool="claude", effort="low",
        subject_revision=revision))

    record = coord.stage_record(identity)
    assert record is not None
    assert record.subject_revision == revision
    assert record.route_id == "production/build/deep/low"
    assert len(record.route_cell_digest) == 64
    assert len(record.launch_config_digest) == 64
    assert len(calls) == 1


def test_scoped_coordinator_rejects_foreign_submission_without_persistence(make_coord):
    route_selections = []
    coord = make_coord(
        FakeSession(), managed_repositories=frozenset({"owner/b"}),
        route_selector=lambda *args, **kwargs: route_selections.append((args, kwargs)))

    with pytest.raises(ValueError, match="outside this coordinator's configured repositories"):
        coord.submit_stage(Submission(
            repo="owner/a", subject="680", stage="build", pool="claude", effort="low",
            subject_revision="a" * 40))

    assert coord._store.load() == {}
    assert route_selections == []


def test_scoped_coordinator_cannot_withdraw_foreign_identity(make_coord, monkeypatch):
    owner = make_coord(FakeSession())
    identity = owner.submit_stage(Submission(
        repo="owner/a", subject="680", stage="build", pool="claude", effort="low"))
    before = owner.stage_record(identity)
    assert before is not None
    discard_calls = []
    original_discard = Store.discard
    monkeypatch.setattr(
        Store, "discard", lambda store, record: discard_calls.append(record) or
        original_discard(store, record))
    scoped = make_coord(FakeSession(), managed_repositories=frozenset({"owner/b"}))

    assert scoped.withdraw_stage(identity) is False

    assert scoped.stage_record(identity) == before
    assert discard_calls == []


@pytest.mark.parametrize("provider", ("claude", "codex"))
def test_nonready_capability_preflight_holds_before_admission_and_survives_restart(
        make_coord, provider):
    session = FakeSession()
    events = []
    preparation = []

    class _MustNotPrepare:
        def prepare(self, _record):
            preparation.append(True)
            return True

        def observe(self, record):
            return session.observe(record)

        def verify(self, record, observation):
            return session.verify(record, observation)
    result = CapabilityPreflightResult(
        stage="build", provider=provider,
        contracts=(ContractRequirement("tdd", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"),),
        state="missing", evidence=("tdd project-local destination missing",),
        repair_command="agentflow enroll /repo --apply")
    coord = make_coord(
        session, adapter=_MustNotPrepare(),
        capability_preflight=lambda _record, _materialize: result, log=events.append
    )
    ident = coord.submit_stage(Submission(repo="o/r", subject="5", stage="build", pool=provider,
                                          effort="low"))

    coord.cycle(provider)
    held = record_of(coord, ident)
    assert held.state == "held" and held.claim and held.attempts == 0
    assert held.capability_preflight and "environment_failure" in held.hold_reason
    assert permits(coord, provider) == 0 and not session.family_of
    assert preparation == []
    from agentflow.coordinator.store import default_store_path
    from agentflow.coordinator.telemetry import read_attempts
    assert read_attempts(default_store_path()) == []
    assert any("environment_failure missing; claim retained" in event for event in events)
    assert not any("claim released" in event for event in events)

    restarted = make_coord(session, capability_preflight=lambda _record, _materialize: result)
    assert record_of(restarted, ident).capability_preflight == held.capability_preflight


@pytest.mark.parametrize("durable", ("{", "[]", "null", '"ui"'))
def test_malformed_historical_capability_context_becomes_named_environment_failure(
        tmp_path, durable):
    from agentflow.pipeline import _capability_preflight

    record = Record(
        identity="o/r|5|build|-", stage="build", pool="claude", demand=1,
        source=str(tmp_path), capability_context=durable)
    result = _capability_preflight(record, False)
    assert result is not None and result.state == "incompatible"
    assert result.ready_fact is None
    assert result.evidence[0].startswith("capability-context-invalid:")


def test_historical_record_preflights_retained_source_not_unrelated_capability_root(
        tmp_path, monkeypatch):
    from agentflow import pipeline

    main = tmp_path / "repo"
    retained = main / ".agentflow" / "worktrees" / "claude" / "issue-5-fix"
    unrelated = tmp_path / "unrelated-main"
    retained.mkdir(parents=True)
    unrelated.mkdir()
    (retained / "AGENTS.md").write_text("profile: reviewed\nui-surfaces: frontend/\n")
    calls = []
    monkeypatch.setattr(
        "agentflow.provider_skills.materialize_launch_capabilities",
        lambda source, destination, provider, *, materialize_runtime: calls.append(
            (source, destination, provider, materialize_runtime)) or (True, "ok"),
    )
    monkeypatch.setattr(
        "agentflow.capability_contracts.preflight",
        lambda root, stage, provider, contracts: calls.append(
            (root, stage, provider, tuple(item.id for item in contracts))) or
            CapabilityPreflightResult(stage, provider, contracts, "ready", ("ok",), "repair"),
    )
    record = Record(
        identity="o/r|5|review|sha", stage="review", pool="claude", demand=1,
        source=str(retained), capability_root=str(unrelated), capability_context="{}")

    result = pipeline._capability_preflight(record, True)

    assert result is not None and result.ready
    assert calls[0] == (unrelated, retained, "claude", True)
    assert calls[1][0] == retained
    assert calls[1][3] == ("ui-craft", "drive-local-webapp", "playwright")


def test_pre_582_record_without_capability_facts_derives_real_worktree_root(
        tmp_path, monkeypatch):
    from agentflow import pipeline

    main = tmp_path / "repo"
    retained = main / ".agentflow" / "worktrees" / "claude" / "issue-5-fix"
    retained.mkdir(parents=True)
    (main / "AGENTS.md").write_text("profile: reviewed\nui-surfaces: none\n")
    calls = []
    monkeypatch.setattr(
        "agentflow.provider_skills.materialize_launch_capabilities",
        lambda source, destination, provider, *, materialize_runtime: calls.append(
            (source, destination, materialize_runtime)) or (True, "ok"),
    )
    monkeypatch.setattr(
        "agentflow.capability_contracts.preflight",
        lambda root, stage, provider, contracts:
            CapabilityPreflightResult(stage, provider, contracts, "ready", (str(root),), "repair"),
    )
    record = Record(
        identity="o/r|5|build|-", stage="build", pool="claude", demand=1,
        source=str(retained), capability_root=None, capability_context="{}")

    result = pipeline._capability_preflight(record, True)

    assert result is not None and result.ready
    assert calls == [(main, retained, False)]


def test_pre_582_record_without_any_safe_surface_fact_fails_closed(tmp_path):
    from agentflow.pipeline import _capability_preflight

    retained = tmp_path / "repo" / ".agentflow" / "worktrees" / "claude" / "issue-5-fix"
    retained.mkdir(parents=True)
    record = Record(
        identity="o/r|5|build|-", stage="build", pool="claude", demand=1,
        source=str(retained), capability_root=None, capability_context="{}")

    result = _capability_preflight(record, False)

    assert result is not None and result.state == "incompatible"
    assert result.ready_fact is None
    assert result.evidence[0].startswith("capability-context-unavailable:")


def test_missing_launch_root_never_carries_a_ready_fact(tmp_path):
    from agentflow.pipeline import _capability_preflight

    record = Record(
        identity="o/r|5|build|-", stage="build", pool="claude", demand=1,
        source=str(tmp_path / "missing"), capability_context="{}")

    result = _capability_preflight(record, False)

    assert result is not None and result.state == "missing"
    assert result.ready_fact is None
    assert result.evidence[0].startswith("launch-root-missing:")


def test_failed_launch_materialization_never_carries_a_ready_fact(tmp_path, monkeypatch):
    from agentflow.pipeline import _capability_preflight

    source = tmp_path / "repo"
    launch = source / ".agentflow" / "worktrees" / "claude" / "issue-5-fix"
    launch.mkdir(parents=True)
    (launch / "AGENTS.md").write_text("profile: reviewed\nui-surfaces: none\n")
    monkeypatch.setattr(
        "agentflow.provider_skills.materialize_launch_capabilities",
        lambda *_args, **_kwargs: (False, "copy failed"),
    )
    record = Record(
        identity="o/r|5|build|-", stage="build", pool="claude", demand=1,
        source=str(launch), capability_root=str(source), capability_context="{}")

    result = _capability_preflight(record, True)

    assert result is not None and result.state == "incompatible"
    assert result.ready_fact is None
    assert result.evidence == ("launch-root-materialization-failed: copy failed",)


def test_production_admission_budget_reads_its_startup_environment(monkeypatch):
    import importlib
    import agentflow.coordinator.admission as admission

    monkeypatch.setenv("AGENTFLOW_PERMIT_BUDGET", "99")
    assert importlib.reload(admission).PERMIT_BUDGET == 99
    monkeypatch.delenv("AGENTFLOW_PERMIT_BUDGET")
    importlib.reload(admission)


def test_production_admission_matrix_is_immutable(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_REVIEW_DEMAND", "0")

    assert PERMIT_BUDGET == 25
    assert admission_demand("review", "claude", "opus", "deep") == 1
    assert admission_demand("future", "claude", "opus", "deep") == PERMIT_BUDGET
    assert admission_demand("review", "future", "opus", "deep") is None
    with pytest.raises(TypeError):
        ADMISSION_MATRIX[("review", "claude", "opus", "deep", None)] = 0


@pytest.mark.parametrize("stage,pool,model,complexity,demand", (
    ("intake", "claude", "opus", "deep", 1),
    ("intake", "codex", "sol", "deep", 1),
    ("attack", "claude", "opus", "deep", 1),
    ("attack", "claude", "sonnet", "standard", 1),
    ("attack", "codex", "sol", "deep", 1),
    ("attack", "codex", "terra", "standard", 1),
    ("review", "claude", "opus", "deep", 1),
    ("review", "codex", "sol", "deep", 2),
    ("revise", "claude", "sonnet", "standard", 3),
    ("revise", "claude", "opus", "deep", 3),
    ("revise", "codex", "terra", "standard", 4),
    ("revise", "codex", "sol", "deep", PERMIT_BUDGET),
    ("respond", "claude", "opus", "deep", 3),
    ("respond", "codex", "sol", "deep", 5),
    ("mockup", "claude", "opus", "deep", 5),
    ("mockup", "codex", "sol", "deep", 5),
    ("converse", "claude", "opus", "deep", 2),
    ("converse", "codex", "sol", "deep", 2),
    ("research", "claude", "opus", "deep", 2),
    ("research", "codex", "sol", "deep", 2),
))
def test_effort_blind_admission_rows_discard_every_supplied_effort(
        stage, pool, model, complexity, demand):
    for effort in ("low", "medium", "high", "extra"):
        assert admission_demand(stage, pool, model, complexity, effort) == demand


@pytest.mark.parametrize("pool,model,complexity,demands", (
    ("claude", "sonnet", "standard", (3, 4, 5, 5)),
    ("claude", "opus", "deep", (4, 4, 5, 5)),
    ("codex", "terra", "standard", (4, 5, 5, 5)),
    ("codex", "sol", "deep", (PERMIT_BUDGET,) * 4),
))
def test_build_admission_keeps_its_effort_rows_and_missing_effort_fallback(
        pool, model, complexity, demands):
    for effort, demand in zip(("low", "medium", "high", "extra"), demands, strict=True):
        assert admission_demand("build", pool, model, complexity, effort) == demand
        assert admission_demand("building", pool, model, complexity, effort) == demand
    assert admission_demand("build", pool, model, complexity) == PERMIT_BUDGET


def test_submit_stage_is_idempotent_on_the_logical_stage_identity(make_coord):
    coord = make_coord(FakeSession())
    first = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    again = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    assert first == again


def test_withdraw_stage_frees_the_slot_so_no_cycle_launches_it(make_coord):
    # The by-hand build path submits the coordinator record before it can guard the issue on GitHub;
    # a lost claim race must be able to withdraw the un-started record so a later cycle never launches
    # it without the ownership claim (#245).
    coord = make_coord(FakeSession())
    identity = coord.submit_stage(Submission(repo="o/r", subject="5", stage="build", pool="claude",
                                             complexity="deep"))
    assert record_of(coord, identity).state == "waiting"

    assert coord.withdraw_stage(identity) is True
    assert coord._store.record_of(identity) is None        # the slot is freed, not tombstoned
    assert coord.cycle("claude") == []                     # nothing to admit
    assert permits(coord, "claude") == 0
    assert coord.withdraw_stage(identity) is False          # idempotent: a repeat is a no-op


def test_a_withdrawn_never_run_build_reopens_on_the_next_cold_submission(make_coord):
    # A build withdrawn before it ever ran must not permanently strand the issue (#251): a subsequent
    # cold submission at the same identity must produce a runnable (waiting) build, exactly as if the
    # failed attempt never happened — not reuse a terminal tombstone the daemon can never launch.
    # Fails on the pre-#251 code, where withdrawal left a retired/completed record the resubmission
    # returned unchanged.
    coord = make_coord(FakeSession())
    sub = Submission(repo="o/r", subject="5", stage="build", pool="claude", complexity="deep")
    identity = coord.submit_stage(sub)
    assert coord.withdraw_stage(identity) is True

    reopened = coord.submit_stage(sub)                      # a fresh cold build for the same issue
    assert reopened == identity
    record = coord.stage_record(identity)
    assert record is not None and record.state == "waiting" and not record.retired
    assert coord.cycle("claude") == []                     # admitted this cycle — a real launch
    assert record_of(coord, identity).state == "running"


def test_withdraw_stage_refuses_a_started_build(make_coord):
    # Withdrawal only rolls back a never-started submission; a Build that already consumed an attempt
    # is untouched, so a genuine in-flight build is never silently retired (#245).
    coord = make_coord(FakeSession())
    identity = coord.submit_stage(Submission(repo="o/r", subject="6", stage="build", pool="claude",
                                             complexity="deep"))
    assert coord.cycle("claude") == []                     # the build starts
    assert record_of(coord, identity).state == "running"
    assert coord.withdraw_stage(identity) is False
    assert record_of(coord, identity).state == "running"   # left in flight


def test_withdraw_stage_accepts_a_launch_proven_not_started(make_coord):
    coord = make_coord(FakeSession(), launcher=NeverStartsLauncher())
    identity = coord.submit_stage(Submission(repo="o/r", subject="7", stage="mockup",
                                             pool="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, identity).start_fact == "not_started"

    assert coord.withdraw_stage(identity) is True
    assert coord.stage_record(identity) is None


def test_legacy_lane_alias_never_turns_revise_into_build(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    # A revise reported on the ambiguous `building` lane must charge revise, not build.
    build = coord.submit_stage(Submission(repo="o/r", subject="9", stage="building",
                                          pool="claude", complexity="deep"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise",
                                           pool="claude", complexity="deep"))
    # Revise is PR-bound, so it drains first (ADR 0039) and reserves three — not the exclusive
    # five a build would. That three-permit reservation is the proof the alias kept it a revise:
    # a build (deep, no effort) would have taken all five, leaving no room to distinguish.
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 3  # revise (3) admitted; build (5) deferred, pool full
    fake.end(revise, success=True)
    assert [o.stage for o in coord.cycle("claude")] == ["revise"]
    assert permits(coord, "claude") == 5  # now the deferred build (5) admitted


def test_conflict_revise_is_admitted_ahead_of_a_cold_build(make_coord):
    # ADR 0038 (AC5): a conflict Revise is a continuation of nearly-merged work, so it is admitted
    # ahead of a cold build submission competing for the same pool. The pool fits only one here —
    # the continuation takes it. Without the continuation priority the build (sorted first by
    # identity) would seize the pool and the conflict Revise would wait.
    fake = FakeSession()
    coord = make_coord(fake)
    build = coord.submit_stage(Submission(repo="o/r", subject="1", stage="build", pool="claude",
                                          complexity="deep"))                       # cold, 5 permits
    conflict = coord.submit_stage(Submission(repo="o/r", subject="2", stage="revise", pool="claude",
                                             complexity="deep", target="sha-conf", conflict_round=1,
                                             builder_lineage="claude", continuation=True))  # 3 permits
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 3                       # the conflict Revise went first
    assert record_of(coord, conflict).state == "running"
    assert record_of(coord, build).state == "waiting"          # the cold build was deferred


def test_cycle_admits_intake_and_charges_one_permit(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="1", stage="intake",
                                             pool="claude"))
    assert coord.cycle("claude") == []       # admitted; its outcome is not terminal this cycle
    assert permits(coord, "claude") == 1
    assert coord.cycle("codex") == []        # the other pool has no work
    fake.end(identity, success=True)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    assert permits(coord, "claude") == 0


def test_unknown_pool_submission_is_inadmissible(make_coord):
    coord = make_coord(FakeSession())
    coord.submit_stage(Submission(repo="o/r", subject="2", stage="review", pool="gemini"))
    # No ledger to charge an unknown pool, so it never starts and never yields an outcome.
    assert coord.cycle("gemini") == []
    assert permits(coord, "gemini") == 0


def test_capacity_reset_defers_a_continuation_until_it_is_eligible(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="3", stage="review",
                                             pool="claude"))
    assert coord.cycle("claude", now=0) == []
    assert permits(coord, "claude") == 1

    # A capacity interruption with a future reset returns the stage to waiting and defers it.
    fake.end(identity, cause=ProviderCause.CAPACITY, reset_at=50)
    assert coord.cycle("claude", now=49) == []          # reconciled to waiting, permits freed
    assert permits(coord, "claude") == 0
    assert coord.cycle("claude", now=49) == []          # still not eligible, not restarted
    assert permits(coord, "claude") == 0
    assert coord.cycle("claude", now=50) == []          # reset reached, restarted
    assert permits(coord, "claude") == 1                 # a second attempt is now running


def test_never_started_launch_consumes_no_permit(make_coord):
    coord = make_coord(FakeSession(), launcher=NeverStartsLauncher())
    coord.submit_stage(Submission(repo="o/r", subject="4", stage="review", pool="claude"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0  # a launch that never started reserves nothing


def test_permit_ledger_is_shared_across_coordinator_instances(make_coord):
    """Coordinator instances share one durable permit ledger and cannot exceed its budget."""
    fake = FakeSession()
    a = make_coord(fake)
    b = make_coord(fake)
    for number in range(PERMIT_BUDGET // 2):
        a.submit_stage(Submission(repo="o/r", subject=f"a{number}", stage="review", pool="codex"))
    b.submit_stage(Submission(repo="o/r", subject="b1", stage="review", pool="codex"))

    a.cycle("codex")                     # a reserves as many demand-2 reviews as fit
    assert permits(a, "codex") == (PERMIT_BUDGET // 2) * 2
    b.cycle("codex")                     # b sees the shared ledger is full and reserves none
    assert permits(b, "codex") == (PERMIT_BUDGET // 2) * 2


def test_global_stage_limit_is_enforced_through_the_coordinator_seam(make_coord):
    """A Build already running on one pool keeps a Review waiting on the other because both
    consume the shared Build lane and its limit is reserved in the durable ledger."""
    fake = FakeSession()

    class OneBuildLane:
        def __call__(self, record):
            return True

        def reservation_limits(self, record):
            return ReservationLimits(
                machine_ceiling=4, stage_cap=1, stage_lane="build",
                lane_by_stage={"build": "build", "review": "build", "revise": "build"},
            )

    gate = OneBuildLane()
    first = make_coord(fake, gate=gate)
    build = first.submit_stage(Submission(
        repo="o/r", subject="1", stage="build", pool="claude", effort="low"))
    first.cycle("claude")

    second = make_coord(fake, gate=gate)
    review = second.submit_stage(Submission(
        repo="o/r", subject="2", stage="review", pool="codex"))
    second.cycle("codex")

    assert record_of(second, build).state == "running"
    assert record_of(second, review).state == "waiting"


def test_stale_waiting_generation_cannot_reset_a_newer_attempt(make_coord):
    """Two public ``cycle`` calls overlap one record. The first pauses after loading the old
    waiting generation; the second starts and durably returns attempt 1 to waiting. Resuming the
    stale cycle must lose the token CAS and preserve the newer attempt budget/state."""
    fake = FakeSession()
    seed = make_coord(fake)
    identity = seed.submit_stage(Submission(
        repo="o/r", subject="cas", stage="review", pool="claude"))
    entered = threading.Event()
    resume = threading.Event()

    class BlockingAdapter:
        def prepare(self, record):
            entered.set()
            assert resume.wait(timeout=5)
            return True

        def observe(self, record):
            return fake.observe(record)

        def verify(self, record, obs):
            return fake.verify(record, obs)

    stale = make_coord(fake, adapter=BlockingAdapter())
    current = make_coord(fake)
    errors = []

    def run_stale_cycle():
        try:
            stale.cycle("claude", now=0)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_stale_cycle)
    thread.start()
    assert entered.wait(timeout=5)

    current.cycle("claude", now=0)
    winning_token = record_of(current, identity).launch_token
    # A recoverable (non-capacity) interruption exercises the stale-generation guard while still
    # consuming the attempt — a provider-declared capacity reset now refunds it (#305), which would
    # muddy this test's subject.
    fake.end(identity, cause=ProviderCause.SERVER, reset_at=100)
    current.cycle("claude", now=0)
    advanced = record_of(current, identity)
    assert advanced.state == "waiting" and advanced.attempts == 1

    resume.set()
    thread.join(timeout=5)
    assert not thread.is_alive() and errors == []
    durable = record_of(current, identity)
    assert durable.state == "waiting" and durable.attempts == 1
    assert durable.launch_token == winning_token and durable.eligible_at == 100
    assert permits(current, "claude") == 0


def test_completed_settlement_has_one_cross_coordinator_owner(make_coord):
    """The external completion projection runs once even when two public cycles race it."""
    fake = FakeSession()
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Adapter:
        def observe(self, record):
            return fake.observe(record)

        def verify(self, record, obs):
            return fake.verify(record, obs)

        def finalize_completed(self, record):
            calls.append(record.identity)
            entered.set()
            assert release.wait(timeout=5)
            return "durable-proof"

    adapter = Adapter()
    seed = make_coord(fake, adapter=adapter)
    identity = seed.submit_stage(Submission(
        repo="o/r", subject="settle-race", stage="review", pool="claude"))
    seed.cycle("claude")
    fake.end(identity, success=True)
    assert [o.status for o in seed.cycle("claude")] == ["completed"]

    first = make_coord(fake, adapter=adapter)
    second = make_coord(fake, adapter=adapter)
    errors = []

    def cycle(coord):
        try:
            coord.cycle("claude")
        except BaseException as error:
            errors.append(error)

    one = threading.Thread(target=cycle, args=(first,))
    two = threading.Thread(target=cycle, args=(second,))
    one.start()
    assert entered.wait(timeout=5)
    two.start()
    release.set()
    one.join(timeout=5)
    two.join(timeout=5)

    assert not one.is_alive() and not two.is_alive() and errors == []
    assert calls == [identity]
    durable = record_of(first, identity)
    assert durable.state == "completed" and durable.retired is True
    assert durable.claim is False and durable.handoff_proof == "durable-proof"


def test_hold_finalization_has_one_cross_coordinator_owner(make_coord):
    """A pending hold's external handoff is serialized across two public cycles."""
    fake = FakeSession()
    enabled = [False]
    entered = threading.Event()
    release = threading.Event()
    calls = []

    class Adapter:
        def observe(self, record):
            return fake.observe(record)

        def verify(self, record, obs):
            return False

        def finalize_hold(self, record):
            if not enabled[0]:
                return None
            calls.append(record.identity)
            entered.set()
            assert release.wait(timeout=5)
            return "hold-proof"

    adapter = Adapter()
    seed = make_coord(fake, adapter=adapter)
    identity = seed.submit_stage(Submission(
        repo="o/r", subject="hold-race", stage="review", pool="claude"))
    seed.cycle("claude")
    fake.end(identity, cause=ProviderCause.PERMANENT)
    assert seed.cycle("claude") == []
    assert record_of(seed, identity).hold_pending is True
    enabled[0] = True

    first = make_coord(fake, adapter=adapter)
    second = make_coord(fake, adapter=adapter)
    errors = []

    def cycle(coord):
        try:
            coord.cycle("claude")
        except BaseException as error:
            errors.append(error)

    one = threading.Thread(target=cycle, args=(first,))
    two = threading.Thread(target=cycle, args=(second,))
    one.start()
    assert entered.wait(timeout=5)
    two.start()
    release.set()
    one.join(timeout=5)
    two.join(timeout=5)

    assert not one.is_alive() and not two.is_alive() and errors == []
    assert calls == [identity]
    durable = record_of(first, identity)
    assert durable.state == "held" and durable.hold_pending is False
    assert durable.claim is False and durable.handoff_proof == "hold-proof"


def test_delayed_launcher_result_cannot_disown_a_newer_attempt(make_coord):
    """T1's launcher disowns its token, but before it returns, another public ``cycle`` recovers
    that no-start and admits T2. T1's delayed result must not release the newer live attempt."""
    fake = FakeSession()
    newer = []

    class FirstLauncher:
        def start(self, record, store):
            token = record.launch_token
            from agentflow.coordinator.launcher import NOT_STARTED, StartResult
            assert store.disown_launch(record.identity, token) == (NOT_STARTED, None)
            concurrent = make_coord(fake)
            concurrent.cycle("claude")
            newer.append(record_of(concurrent, record.identity))
            return StartResult(NOT_STARTED)

        def is_alive(self, family):
            return False

    first = make_coord(fake, launcher=FirstLauncher())
    identity = first.submit_stage(Submission(
        repo="o/r", subject="launch-generation", stage="review", pool="claude"))
    first.cycle("claude")
    before = newer.pop()
    after = record_of(first, identity)
    assert before.state == after.state == "running"
    assert after.launch_token == before.launch_token
    assert after.family == before.family and fake.is_alive(after.family)
    assert permits(first, "claude") == before.demand


def test_same_cycle_launch_observation_cannot_reconcile_a_newer_attempt(make_coord):
    """A's post-launch sweep owns v1, never B's successor v2 for the same stage identity."""
    fake = FakeSession()
    successor = []

    def gate(_record):
        return True

    def advance_to_successor(record):
        fake.kill(record.identity)
        second = make_coord(fake)  # a distinct Coordinator and Store over the same durable ledger
        second.cycle("claude")
        durable = record_of(second, record.identity)
        successor.append((durable.launch_token, durable.family))
        fake.kill(record.identity)  # v2 is gone before A reaches its final launch observation

    gate.started = advance_to_successor
    first = make_coord(fake, gate=gate)
    identity = first.submit_stage(Submission(
        repo="o/r", subject="launch-token-race", stage="review", pool="claude"))

    assert first.cycle("claude") == []
    token, family = successor.pop()
    durable = record_of(first, identity)
    assert durable.launch_token == token and durable.family == family
    assert durable.state == "running" and durable.attempts == 2
    assert durable.process_alive and durable.claim and not fake.is_alive(family)


def test_concurrent_descendant_submissions_are_atomically_registered(make_coord):
    """Two public submissions racing on one root both survive its revision changes, and the
    root's completion retires both children that share its reservation."""
    fake = FakeSession()
    seed = make_coord(fake)
    root = seed.submit_stage(Submission(
        repo="o/r", subject="root", stage="review", pool="claude"))
    seed.cycle("claude")
    assert record_of(seed, root).state == "running"
    barrier = threading.Barrier(2)
    children = []
    errors = []
    lock = threading.Lock()

    def submit(subject):
        try:
            coord = make_coord(fake)
            barrier.wait()
            child = coord.submit_stage(Submission(
                repo="o/r", subject=subject, stage="respond", pool="claude",
                descendant_of=root))
            with lock:
                children.append(child)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=submit, args=(subject,))
               for subject in ("child-a", "child-b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == [] and len(children) == 2
    assert record_of(seed, root).descendants == set(children)
    fake.end(root, success=True)
    assert [outcome.identity for outcome in seed.cycle("claude")] == [root]
    assert all(record_of(seed, child).retired for child in children)


def test_descendant_racing_root_completion_never_survives_orphaned(make_coord):
    """Atomic root-state validation means the child either registers before completion and is
    retired with the root, or its submission rolls back after completion. No active orphan can
    survive either transaction order."""
    from agentflow.coordinator.store import StoreUnavailable

    fake = FakeSession()
    seed = make_coord(fake)
    root = seed.submit_stage(Submission(
        repo="o/r", subject="terminal-root", stage="review", pool="claude"))
    seed.cycle("claude")
    fake.end(root, success=True)
    barrier = threading.Barrier(2)
    submitted = []
    rejected = []
    errors = []

    def complete():
        try:
            coord = make_coord(fake)
            barrier.wait()
            coord.cycle("claude")
        except BaseException as error:
            errors.append(error)

    def submit_child():
        try:
            coord = make_coord(fake)
            barrier.wait()
            submitted.append(coord.submit_stage(Submission(
                repo="o/r", subject="late-child", stage="respond", pool="claude",
                descendant_of=root)))
        except StoreUnavailable:
            rejected.append(True)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=complete), threading.Thread(target=submit_child)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == [] and len(submitted) + len(rejected) == 1
    # If the child transaction won first, the completion cycle intentionally lost its root
    # revision CAS. The next normal pass reloads that registration and retires both together.
    make_coord(fake).cycle("claude")
    durable = seed._store.load()
    children = [record for record in durable.values() if record.root == root]
    assert all(record.retired for record in children)


def test_crash_after_root_completion_commit_cannot_orphan_descendant(make_coord):
    """Root completion and descendant retirement commit together. A daemon crash in the first
    instruction after that transaction leaves both terminal for a fresh coordinator."""
    fake = FakeSession()
    armed = [False]

    def crash_after_commit(line):
        if armed[0] and " completed — " in line:
            raise RuntimeError("simulated daemon crash after terminal commit")

    coord = make_coord(fake, log=crash_after_commit)
    root = coord.submit_stage(Submission(
        repo="o/r", subject="crash-root", stage="review", pool="claude"))
    coord.cycle("claude")
    child = coord.submit_stage(Submission(
        repo="o/r", subject="crash-child", stage="respond", pool="claude",
        descendant_of=root))
    fake.end(root, success=True)
    armed[0] = True

    try:
        coord.cycle("claude")
    except RuntimeError as error:
        assert str(error) == "simulated daemon crash after terminal commit"
    else:
        raise AssertionError("simulated crash did not fire")

    restarted = make_coord(fake)
    durable_root = record_of(restarted, root)
    durable_child = record_of(restarted, child)
    assert durable_root.state == "completed"
    assert durable_child.state == "completed" and durable_child.retired is True
    assert durable_child.claim is False


def test_existing_descendant_submission_is_idempotent_after_root_completion(make_coord):
    """A lost submit response may be retried after the root becomes terminal. The exact existing
    child is returned without reopening it; only a genuinely new late child is rejected."""
    fake = FakeSession()
    coord = make_coord(fake)
    root = coord.submit_stage(Submission(
        repo="o/r", subject="idempotent-root", stage="review", pool="claude"))
    coord.cycle("claude")
    submission = Submission(
        repo="o/r", subject="idempotent-child", stage="respond", pool="claude",
        descendant_of=root)
    child = coord.submit_stage(submission)
    fake.end(root, success=True)
    coord.cycle("claude")
    assert record_of(coord, child).retired is True

    restarted = make_coord(fake)
    assert restarted.submit_stage(submission) == child
    durable = record_of(restarted, child)
    assert durable.retired is True and durable.claim is False


def test_schema_v1_record_without_revision_advances_through_public_cycle(make_coord):
    """A pre-revision schema-v1 payload decodes at generation zero and can be admitted by the
    public coordinator seam after an upgrade."""
    fake = FakeSession()
    seed = make_coord(fake)
    identity = seed.submit_stage(Submission(
        repo="o/r", subject="legacy-v1", stage="review", pool="claude"))
    path = seed._store.path
    seed._store.close()

    conn = sqlite3.connect(path)
    payload = json.loads(conn.execute(
        "SELECT data FROM records WHERE identity = ?", (identity,)).fetchone()[0])
    payload.pop("revision")
    conn.execute("UPDATE records SET data = ? WHERE identity = ?",
                 (json.dumps(payload), identity))
    conn.commit()
    conn.close()

    upgraded = make_coord(fake)
    upgraded.cycle("claude")
    durable = record_of(upgraded, identity)
    assert durable.state == "running" and durable.revision > 0
    assert durable.attempts == 1 and fake.is_alive(durable.family)


def test_all_production_dispatch_is_wired_behind_the_coordinator():
    """The daemon and by-hand Build route durable work through the coordinator."""
    import agentflow.dispatch
    import agentflow.loop
    dispatch_source = agentflow.dispatch.__loader__.get_source("agentflow.dispatch") or ""
    loop_source = agentflow.loop.__loader__.get_source("agentflow.loop") or ""
    assert "coordinated_build" in dispatch_source
    assert "coordinator.submit_stage" in loop_source


def test_stage_outcome_is_the_only_terminal_fact_that_crosses_the_seam():
    """cycle returns typed terminal outcomes, not the mutable record and not started ids."""
    assert StageOutcome("id", "review", "completed").status == "completed"
    assert not hasattr(Coordinator, "reconcile")  # reconciliation is private to cycle


def test_public_surface_keeps_completed_boundary_settlement_private(make_coord):
    """Completed boundary settlement stays behind cycle, preserving ADR 0030's deep seam."""
    coord = make_coord(FakeSession())
    public = {name for name in dir(coord)
              if not name.startswith("_") and callable(getattr(coord, name))}
    # The deliberate public operations beside submit_stage/cycle: the completed-stage park handoff,
    # the two Review-native disposals for a PR head that moved off the immutable target (#208), the
    # Revise-native retire for a PR closed or merged out from under the revise (#432), and the
    # triage-side retire for an issue closed out from under an intake or attack round (#438).
    # ``stage_record`` is a read-only observation, not a transition — the dispatch layer checks
    # whether a submission produced runnable work before claiming the issue (#245); ``withdraw_stage``
    # rolls back a never-started submission whose GitHub claim was lost to a race (#245).
    assert public == {"submit_stage", "cycle", "park_completed", "retire_stale_review",
                      "retire_stale_revise", "retire_stale_intake", "retire_stale_hold", "park_stale_review",
                      "stage_record", "withdraw_stage"}
    assert not hasattr(coord, "permits")      # permit accounting is an internal invariant
    assert not hasattr(coord, "records")      # the working set is private (_records)


# --- ADR 0039: PR-bound stages drain ahead of issue-bound stages at admission ------------

class _SharedLaneCap:
    """A gate that admits every stage but caps a shared lane at one running record, so which of
    two competitors starts is decided purely by admission ordering, not by permit arithmetic."""

    def __init__(self, *stages):
        self._lane = {stage: "shared" for stage in stages}

    def __call__(self, record):
        return True

    def reservation_limits(self, record):
        return ReservationLimits(machine_ceiling=99, stage_cap=1, stage_lane="shared",
                                 lane_by_stage=self._lane)


class _RefuseReviewGate:
    """Admits every stage but review — used to freeze a PR-bound head and prove head-of-line
    blocking (ADR 0029) still holds: a stalled review must not let a build behind it start."""

    def __call__(self, record):
        return record.stage != "review"


def test_cold_pr_bound_review_drains_before_issue_bound_build(make_coord):
    """A waiting cold review and a waiting cold build on one pool, the build lexically first: the
    review (an open PR one verdict from merging) admits ahead of the brand-new build, which then
    cannot fit the pool (ADR 0039). Fails before the fix — identity order started the build,
    which reserved the whole pool and left no room for the review."""
    fake = FakeSession()
    coord = make_coord(fake)
    build = coord.submit_stage(Submission(repo="o/r", subject="1", stage="build",
                                          pool="claude", complexity="deep"))
    review = coord.submit_stage(Submission(repo="o/r", subject="2", stage="review",
                                           pool="claude"))
    coord.cycle("claude")
    assert record_of(coord, review).state == "running"     # PR-bound drains first
    assert record_of(coord, build).state == "waiting"      # issue-bound waits behind it
    assert permits(coord, "claude") == 1                    # only the review's single permit reserved


def test_cold_pr_bound_revise_and_respond_drain_before_build(make_coord):
    """Fresh revise and respond records are themselves cold, but as PR-bound stages they drain
    ahead of a brand-new build even when the build sorts first by identity; within their own tier
    they keep identity order, so revise (not respond) takes the head (ADR 0039)."""
    fake = FakeSession()
    coord = make_coord(fake)
    build = coord.submit_stage(Submission(repo="o/r", subject="1", stage="build",
                                          pool="claude", complexity="deep"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="2", stage="revise",
                                           pool="claude", complexity="deep"))
    respond = coord.submit_stage(Submission(repo="o/r", subject="3", stage="respond",
                                            pool="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, revise).state == "running"     # PR-bound, lexically first in its tier
    assert record_of(coord, respond).state == "waiting"    # PR-bound but the pool is now full
    assert record_of(coord, build).state == "waiting"      # issue-bound drains last
    assert permits(coord, "claude") == 3


def test_pr_bound_continuation_drains_before_earlier_eligible_issue_bound(make_coord):
    """In the continuation queue a PR-bound stage still drains first, even against an issue-bound
    continuation that became eligible earlier — and head-of-line then keeps the build behind it
    (ADR 0039). Fails before the fix: eligible-at order ran the build and filled the pool."""
    fake = FakeSession()
    coord = make_coord(fake)
    build = coord.submit_stage(Submission(repo="o/r", subject="1", stage="build",
                                          pool="claude", complexity="deep"))
    coord.cycle("claude", now=0)                            # build runs, reserves the pool
    fake.end(build, cause=ProviderCause.CAPACITY, reset_at=1)
    coord.cycle("claude", now=0)                            # build → waiting continuation (eligible 1)

    review = coord.submit_stage(Submission(repo="o/r", subject="2", stage="review",
                                           pool="claude"))
    coord.cycle("claude", now=0)                            # build not yet eligible; review runs
    fake.end(review, cause=ProviderCause.CAPACITY, reset_at=2)
    coord.cycle("claude", now=0)                            # review → waiting continuation (eligible 2)

    coord.cycle("claude", now=5)                            # both eligible now
    assert record_of(coord, review).state == "running"     # PR-bound drains first
    assert record_of(coord, build).state == "waiting"      # earlier-eligible build held behind it
    assert permits(coord, "claude") == 1


def test_interactive_turn_outranks_pr_bound_review_in_cold_queue(make_coord):
    """The operator's interactive turn still sorts ahead of a PR-bound review — the PR tier sits
    below the interactive tier, not above it (ADR 0039 keeps ADR 0034 on top). A shared one-slot
    lane forces the choice; the interactive turn takes it."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_SharedLaneCap("converse", "review"))
    review = coord.submit_stage(Submission(repo="o/r", subject="1", stage="review",
                                           pool="claude"))
    ask = coord.submit_stage(Submission(repo="o/r", subject="2", stage="converse",
                                        pool="claude", interactive=True))
    coord.cycle("claude")
    assert record_of(coord, ask).state == "running"        # interactive heads the queue
    assert record_of(coord, review).state == "waiting"     # PR-bound sits below it


def test_interactive_continuation_outranks_pr_bound_continuation(make_coord):
    """The same tier order holds in the continuation queue: an interactive turn heads it, and
    head-of-line then holds a PR-bound continuation behind it (ADR 0039 / ADR 0034)."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_SharedLaneCap("converse", "review"))
    ask = coord.submit_stage(Submission(repo="o/r", subject="1", stage="converse",
                                        pool="claude", interactive=True))
    coord.cycle("claude", now=0)
    fake.end(ask, cause=ProviderCause.CAPACITY, reset_at=1)
    coord.cycle("claude", now=0)                            # ask → waiting continuation

    review = coord.submit_stage(Submission(repo="o/r", subject="2", stage="review",
                                           pool="claude"))
    coord.cycle("claude", now=0)                            # lane free — review runs
    fake.end(review, cause=ProviderCause.CAPACITY, reset_at=1)
    coord.cycle("claude", now=0)                            # review → waiting continuation

    coord.cycle("claude", now=5)
    assert record_of(coord, ask).state == "running"        # interactive heads the queue
    assert record_of(coord, review).state == "waiting"     # PR-bound held behind it


def test_within_tier_two_cold_builds_keep_identity_order(make_coord):
    """Within a tier the existing tie-breakers are unchanged: two cold builds competing for a pool
    that fits only one admit in identity order (ADR 0039 reorders across tiers, never within one)."""
    fake = FakeSession()
    coord = make_coord(fake)
    first = coord.submit_stage(Submission(repo="o/r", subject="1", stage="build",
                                          pool="claude", complexity="deep"))
    second = coord.submit_stage(Submission(repo="o/r", subject="2", stage="build",
                                           pool="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, first).state == "running"      # lexically-smaller identity admits
    assert record_of(coord, second).state == "waiting"
    assert permits(coord, "claude") == 5


def test_within_tier_two_pr_bound_continuations_keep_eligible_order(make_coord):
    """Within the PR tier the continuation tie-break still orders by eligibility, not identity: the
    earlier-eligible review admits first even though its identity sorts later, and head-of-line
    then holds the other behind it."""
    fake = FakeSession()
    coord = make_coord(fake, gate=_SharedLaneCap("review"))
    # The earlier-eligible review is given the lexically-larger subject, so only eligible-at
    # order — not identity — can explain which one admits.
    early = coord.submit_stage(Submission(repo="o/r", subject="z", stage="review", pool="claude"))
    coord.cycle("claude", now=0)
    fake.end(early, cause=ProviderCause.CAPACITY, reset_at=1)
    coord.cycle("claude", now=0)

    late = coord.submit_stage(Submission(repo="o/r", subject="a", stage="review", pool="claude"))
    coord.cycle("claude", now=0)
    fake.end(late, cause=ProviderCause.CAPACITY, reset_at=2)
    coord.cycle("claude", now=0)

    coord.cycle("claude", now=5)
    assert record_of(coord, early).state == "running"      # eligible_at=1 wins over identity
    assert record_of(coord, late).state == "waiting"


def test_blocked_pr_bound_head_does_not_let_issue_bound_leapfrog(make_coord):
    """Head-of-line semantics are unchanged (ADR 0029): a PR-bound continuation the gate refuses
    blocks the pool, so a ready issue-bound continuation behind it does not leapfrog — even though
    the pool has room. Fails before the fix: eligible-at order put the build ahead of the review,
    so it started regardless of the stalled head."""
    fake = FakeSession()
    seed = make_coord(fake)
    build = seed.submit_stage(Submission(repo="o/r", subject="1", stage="build",
                                         pool="claude", complexity="deep"))
    seed.cycle("claude", now=0)
    fake.end(build, cause=ProviderCause.CAPACITY, reset_at=1)
    seed.cycle("claude", now=0)                             # build → continuation (eligible 1)

    review = seed.submit_stage(Submission(repo="o/r", subject="2", stage="review", pool="claude"))
    seed.cycle("claude", now=0)                             # review runs (build not yet eligible)
    fake.end(review, cause=ProviderCause.CAPACITY, reset_at=2)
    seed.cycle("claude", now=0)                             # review → continuation (eligible 2)

    # A fresh coordinator whose gate now refuses reviews cycles the pool: the review heads the
    # queue (PR tier) but the gate blocks it, so head-of-line keeps the build waiting behind it.
    blocked = make_coord(fake, gate=_RefuseReviewGate())
    blocked.cycle("claude", now=5)
    assert record_of(blocked, review).state == "waiting"
    assert record_of(blocked, build).state == "waiting"    # did not leapfrog the blocked head
    assert permits(blocked, "claude") == 0                  # nothing started this cycle


# --- #293: issue-bound new work defers behind a waiting PR-bound stage at the reservation gate ---

def test_a_waiting_review_defers_an_issue_bound_build_across_repos(make_coord):
    """#293: a high-effort build continuation is admitted ahead of cold work (continuations run
    first) and, at demand five, would reserve the whole pool — starving a review in *another* repo
    that shares the pool. The reservation gate now holds any issue-bound stage back while a PR-bound
    stage is waiting to start on the same pool, and the ledger is shared per pool, so the deferral
    holds across repos: the review starts and the high-effort build waits. Fails before the fix —
    the build continuation ran and left no permit for the review."""
    fake = FakeSession()
    seed = make_coord(fake)
    build = seed.submit_stage(Submission(repo="o/b", subject="1", stage="build",
                                         pool="claude", complexity="deep", effort="high"))
    seed.cycle("claude", now=0)                             # build runs, reserves all five
    assert permits(seed, "claude") == 5
    fake.end(build, cause=ProviderCause.CAPACITY, reset_at=1)
    seed.cycle("claude", now=0)                             # build → waiting continuation (eligible 1)

    coord = make_coord(fake)
    review = coord.submit_stage(Submission(repo="o/a", subject="9", stage="review", pool="claude"))
    coord.cycle("claude", now=5)                            # both eligible; the review must win the pool
    assert record_of(coord, review).state == "running"     # PR-bound admits, even from another repo
    assert record_of(coord, build).state == "waiting"      # the high-effort build defers (#293)
    assert permits(coord, "claude") == 1                    # only the review's single permit reserved


def test_high_effort_build_still_takes_the_whole_pool_when_no_review_waits(make_coord):
    """The #293 guard is scoped to a *waiting* PR-bound stage: with the review queue empty a
    high-effort build (demand five) still reserves the entire pool, so builds are unaffected when
    nothing is waiting to finish."""
    fake = FakeSession()
    coord = make_coord(fake)
    build = coord.submit_stage(Submission(repo="o/r", subject="1", stage="build",
                                          pool="claude", complexity="deep", effort="high"))
    coord.cycle("claude")
    assert record_of(coord, build).state == "running"      # no PR-bound stage waiting to defer it
    assert permits(coord, "claude") == 5                    # full pool reserved


def test_unprepared_review_does_not_freeze_issue_work_behind_it(make_coord):
    """A review whose checkout cannot be prepared reserves no capacity, so it must not keep
    otherwise-runnable issue work idle while it waits for its next preparation attempt."""
    fake = FakeSession()
    fake.prepare = lambda record: record.stage != "review"
    coord = make_coord(fake)
    review = coord.submit_stage(Submission(repo="o/r", subject="1", stage="review",
                                           pool="claude"))
    build = coord.submit_stage(Submission(repo="o/r", subject="2", stage="build",
                                          pool="claude", complexity="deep"))

    coord.cycle("claude", now=10)

    assert record_of(coord, review).state == "waiting"
    assert record_of(coord, build).state == "running"
    assert permits(coord, "claude") == 5
