"""The coordinator's public seam (ADR 0030): idempotent logical-stage submission and pool
cycling, with the admission matrix, continuation priority, atomic permit reservation, and
provider observations kept private. Everything here is driven through ``submit_stage`` and
``cycle`` — the only two calls stage orchestration makes. Also asserts the dormant guarantee:
nothing here is wired into the daemon yet.
"""

from __future__ import annotations

import json
import sqlite3
import threading

from conftest import FakeSession, NeverStartsLauncher, permits, record_of

from agentflow.coordinator import Coordinator, StageOutcome, Submission
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.store import ReservationLimits


def test_submit_stage_is_idempotent_on_the_logical_stage_identity(make_coord):
    coord = make_coord(FakeSession())
    first = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    again = coord.submit_stage(Submission(repo="o/r", subject="5", stage="review"))
    assert first == again


def test_legacy_lane_alias_never_turns_revise_into_build(make_coord):
    fake = FakeSession()
    coord = make_coord(fake)
    # A revise reported on the ambiguous `building` lane must charge revise, not build.
    build = coord.submit_stage(Submission(repo="o/r", subject="9", stage="building",
                                          pool="claude", complexity="deep"))
    revise = coord.submit_stage(Submission(repo="o/r", subject="9", stage="revise",
                                           pool="claude", complexity="deep"))
    # Build (deep, no effort) reserves the exclusive five; revise reserves three. If the alias
    # had collapsed revise into build the pool could not have fit both — it fits exactly.
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 5  # build 5 admitted; revise deferred, pool full
    fake.end(build, success=True)
    assert [o.stage for o in coord.cycle("claude")] == ["build"]
    assert permits(coord, "claude") == 3  # now revise (3) admitted — proving it stayed revise


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
    """Two coordinator instances over one store draw from the same durable permit ledger, so
    a second instance sees the first's reservations and cannot push a pool past its budget —
    two demand-2 reviews fit, a third does not (ADR 0029/0030)."""
    fake = FakeSession()
    a = make_coord(fake)
    b = make_coord(fake)
    a.submit_stage(Submission(repo="o/r", subject="a1", stage="review", pool="codex"))
    a.submit_stage(Submission(repo="o/r", subject="a2", stage="review", pool="codex"))
    b.submit_stage(Submission(repo="o/r", subject="b1", stage="review", pool="codex"))

    a.cycle("codex")                     # a reserves two (four permits)
    assert permits(a, "codex") == 4
    b.cycle("codex")                     # b sees the shared ledger is full and reserves none
    assert permits(b, "codex") == 4


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
    fake.end(identity, cause=ProviderCause.CAPACITY, reset_at=100)
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


def test_only_build_is_wired_behind_the_coordinator():
    """Guardrail for issue #103: Build — and only Build — has moved behind the coordinator.
    Dispatch routes it through the rollout; the legacy provider surface (`runner`) and the other
    six logical stages' orchestration (`loop`) still never import the coordinator, so nothing
    else submits work there."""
    import agentflow.dispatch
    import agentflow.loop
    import agentflow.runner
    dispatch_source = agentflow.dispatch.__loader__.get_source("agentflow.dispatch") or ""
    assert "coordinated_build" in dispatch_source  # Build is wired
    for module in (agentflow.loop, agentflow.runner):
        source = module.__loader__.get_source(module.__name__) or ""
        assert "agentflow.coordinator" not in source


def test_stage_outcome_is_the_only_terminal_fact_that_crosses_the_seam():
    """cycle returns typed terminal outcomes, not the mutable record and not started ids."""
    assert StageOutcome("id", "review", "completed").status == "completed"
    assert not hasattr(Coordinator, "reconcile")  # reconciliation is private to cycle


def test_public_surface_keeps_completed_boundary_settlement_private(make_coord):
    """Completed boundary settlement stays behind cycle, preserving ADR 0030's deep seam."""
    coord = make_coord(FakeSession())
    public = {name for name in dir(coord)
              if not name.startswith("_") and callable(getattr(coord, name))}
    assert public == {"submit_stage", "cycle", "park_completed"}
    assert not hasattr(coord, "permits")      # permit accounting is an internal invariant
    assert not hasattr(coord, "records")      # the working set is private (_records)
