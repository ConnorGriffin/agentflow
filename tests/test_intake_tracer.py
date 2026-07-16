"""Durable Intake behavior through the session coordinator's public interface."""

from __future__ import annotations

from conftest import FakeSession, record_of, starts_until_held

from agentflow import coordinated_intake, loop
from agentflow.coordinator import IntakeStageAdapter, Submission
from agentflow.coordinator.providers import ProviderCause, ProviderObservation


class IntakeSession(FakeSession):
    def __init__(self) -> None:
        super().__init__()
        self.message = ""

    def observe(self, record) -> ProviderObservation:
        ending = self._script.get(record.identity)
        cause = ending.obs.cause if ending else ProviderCause.UNKNOWN
        return ProviderObservation(cause=cause, final_message=self.message)


def _submission(target=None):
    return Submission(repo="o/r", subject="7", stage="intake", target=target,
                      pool="claude", source="/read-only/issue-7", input_ptr="durable issue")


def test_parsed_route_is_durable_before_projection_even_after_bad_exit(make_coord):
    fake = IntakeSession()
    applied = []
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: applied.append(result) or "issue-proof")
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())
    coord.cycle("claude")
    fake.message = ('{"route":"ready","title":"Scoped","body":"brief",'
                    '"complexity":"deep","effort":"medium"}')
    fake.end(identity, cause=ProviderCause.PROCESS)

    outcomes = coord.cycle("claude")

    assert [outcome.status for outcome in outcomes] == ["completed"]
    assert record_of(coord, identity).outcome is not None
    assert applied == []  # projection is a later, restart-safe reconciliation step

    restarted = make_coord(fake, adapter=adapter)
    assert restarted.settle_completed(identity) is True
    assert restarted.settle_completed(identity) is True
    assert len(applied) == 1


def test_unparsed_success_uses_three_started_attempts_then_one_hold(make_coord):
    fake = IntakeSession()
    holds = []
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: "issue-proof",
        handoff=lambda record: holds.append(record.identity) or "hold-proof")
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    starts = starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE)

    assert starts == 3
    assert holds == [identity]


def test_human_reply_targets_a_fresh_intake_stage(make_coord):
    coord = make_coord(IntakeSession())
    initial = coord.submit_stage(_submission())
    replied = coord.submit_stage(_submission(target="comment-99"))
    assert initial != replied


def test_production_projection_applies_once_then_releases_claim(make_coord, monkeypatch):
    fake = IntakeSession()
    applied, released, notified = [], [], []
    adapter = IntakeStageAdapter(worktree_reset=lambda record: True, observer=fake,
                                 apply_route=coordinated_intake.apply_route)
    coord = make_coord(fake, adapter=adapter)
    submission = _submission()
    submission = Submission(**{**submission.__dict__, "input_ptr":
                            '{"snapshot":{"title":"old"},"prompt":"p"}'})
    identity = coord.submit_stage(submission)
    coord.cycle("claude")
    fake.message = '{"route":"grill","body":"question"}'
    fake.end(identity, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    monkeypatch.setattr(loop, "_run", lambda cmd: type("R", (), {
        "returncode": 0, "stdout": '{"title":"old","labels":[]}'})())
    monkeypatch.setattr(coordinated_intake, "apply_intake",
                        lambda *args: applied.append(args))
    durable = iter((False, True))
    monkeypatch.setattr(coordinated_intake, "intake_result_is_durable",
                        lambda *args: next(durable))
    monkeypatch.setattr(loop, "_release_triage",
                        lambda repo, number: released.append(number))
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args))

    assert coord.settle_completed(identity) is True
    assert coord.settle_completed(identity) is True
    assert len(applied) == 1 and released == [7] and len(notified) == 1


def test_production_preparation_recreates_read_only_worktree(make_coord, monkeypatch, tmp_path):
    fake = IntakeSession()
    prepared = []
    source = tmp_path / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    source.mkdir(parents=True)
    monkeypatch.setattr(loop, "_run", lambda cmd: type("R", (), {"returncode": 0})())
    monkeypatch.setattr("agentflow.runner.ClaudeRunner.prepare_worktree_detached",
                        lambda self, workdir, ref, wt: prepared.append((ref, wt)))
    monkeypatch.setattr("agentflow.runner.ClaudeRunner.provision", lambda self, wt: None)
    adapter = IntakeStageAdapter(worktree_reset=coordinated_intake.reset_worktree,
                                 observer=fake, apply_route=lambda *args: "proof")
    coord = make_coord(fake, adapter=adapter)
    sub = Submission(repo="o/r", subject="7", stage="intake", pool="claude",
                     source=str(source), input_ptr='{"snapshot":{},"prompt":"p"}')
    coord.submit_stage(sub)

    coord.cycle("claude")

    assert prepared == [("origin/main", source)]


def test_production_exhaustion_notifies_once(make_coord, monkeypatch):
    fake = IntakeSession()
    applied, released, notified = [], [], []
    monkeypatch.setattr(loop, "_run", lambda cmd: type("R", (), {
        "returncode": 0, "stdout": '{"title":"old","labels":[],"comments":[]}'})())
    monkeypatch.setattr(coordinated_intake, "apply_intake",
                        lambda *args: applied.append(args))
    monkeypatch.setattr(coordinated_intake, "intake_result_is_durable", lambda *args: True)
    monkeypatch.setattr(loop, "_release_triage",
                        lambda repo, number: released.append(number))
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args))
    adapter = IntakeStageAdapter(worktree_reset=lambda record: True, observer=fake,
                                 apply_route=lambda *args: "proof",
                                 handoff=coordinated_intake.hold_intake)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 3
    coord.cycle("claude")

    assert len(applied) == 1 and released == [7] and len(notified) == 1
