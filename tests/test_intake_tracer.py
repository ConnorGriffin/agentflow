"""Durable Intake behavior through the session coordinator's public interface."""

from __future__ import annotations

import json
from types import SimpleNamespace

from conftest import FakeSession, record_of, starts_until_held

from agentflow import coordinated_intake, intake as intake_mod, loop
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


def test_submission_pins_the_source_commit(monkeypatch):
    monkeypatch.setattr(loop, "_run", lambda cmd: SimpleNamespace(
        returncode=0, stdout="abc123\n"))
    cfg = SimpleNamespace(repo="o/r", workdir="/repo")

    submission = coordinated_intake.intake_submission(
        cfg, {"number": 7, "title": "t", "body": "b", "labels": []}, "", "claude")

    assert submission is not None
    assert json.loads(submission.input_ptr)["source_ref"] == "abc123"


def test_preparation_proves_the_triaging_claim_before_admission(make_coord):
    fake = IntakeSession()
    claims = []
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True,
        claim_ready=lambda record: claims.append(record.identity) or False,
        observer=fake, apply_route=lambda *args: "proof")
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    assert coord.cycle("claude") == []
    assert record_of(coord, identity).attempts == 0
    assert identity not in fake.family_of
    assert claims == [identity]


def test_production_claim_proof_fails_closed_on_missing_label(monkeypatch):
    monkeypatch.setattr(loop, "_run", lambda cmd: SimpleNamespace(
        returncode=0, stdout='{"labels":[{"name":"ready-for-agent"}]}'))
    record = SimpleNamespace(repo="o/r", subject="7")

    assert coordinated_intake.intake_claim_ready(record) is False


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
    assert restarted.cycle("claude") == []
    assert restarted.cycle("claude") == []
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


def test_permanent_hold_preserves_its_reason_for_the_stage_handoff(make_coord):
    fake = IntakeSession()
    reasons = []
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: "issue-proof",
        handoff=lambda record: reasons.append(record.hold_reason) or "hold-proof")
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())
    coord.cycle("claude")
    fake.end(identity, cause=ProviderCause.PERMANENT)

    assert [outcome.status for outcome in coord.cycle("claude")] == ["held"]
    assert reasons == ["permanent provider condition (permanent)"]


def test_human_reply_targets_a_fresh_intake_stage(make_coord):
    coord = make_coord(IntakeSession())
    initial = coord.submit_stage(_submission())
    replied = coord.submit_stage(_submission(target="comment-99"))
    assert initial != replied


def test_production_projection_applies_once_then_releases_claim(make_coord, monkeypatch):
    fake = IntakeSession()
    released, notified = [], []
    issue = {"title": "old", "body": "original", "labels": ["agentflow:triaging"],
             "comments": []}
    label_failures = [1]

    def gh(cmd, cwd=None, timeout=None):
        if cmd[:3] == ["gh", "issue", "view"]:
            payload = {
                "title": issue["title"], "body": issue["body"],
                "labels": [{"name": name} for name in issue["labels"]],
                "comments": [{"body": body} for body in issue["comments"]],
            }
            return SimpleNamespace(returncode=0, stdout=json.dumps(payload))
        if cmd[:3] == ["gh", "issue", "comment"]:
            issue["comments"].append(cmd[cmd.index("--body") + 1])
            return SimpleNamespace(returncode=0, stdout="")
        if cmd[:3] == ["gh", "label", "create"]:
            return SimpleNamespace(returncode=0, stdout="")
        if cmd[:3] == ["gh", "issue", "edit"]:
            if "--title" in cmd:
                issue["title"] = cmd[cmd.index("--title") + 1]
            if "--body" in cmd:
                issue["body"] = cmd[cmd.index("--body") + 1]
            if "--add-label" in cmd:
                if label_failures[0]:
                    label_failures[0] -= 1
                else:
                    for index, part in enumerate(cmd):
                        if part == "--add-label" and cmd[index + 1] not in issue["labels"]:
                            issue["labels"].append(cmd[index + 1])
                        if part == "--remove-label" and cmd[index + 1] in issue["labels"]:
                            issue["labels"].remove(cmd[index + 1])
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(cmd)

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
    monkeypatch.setattr(loop, "_run", gh)
    monkeypatch.setattr(intake_mod, "_run", gh)
    monkeypatch.setattr(loop, "_release_triage",
                        lambda repo, number: (released.append(number),
                                              issue["labels"].remove("agentflow:triaging")))
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args) or True)

    assert coord.cycle("claude") == []  # comment lands, label write is interrupted
    assert coord.cycle("claude") == []  # retry finishes labels without reposting
    assert len(issue["comments"]) == 1
    assert released == [7] and len(notified) == 1


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
                     source=str(source),
                     input_ptr='{"snapshot":{},"source_ref":"abc123","prompt":"p"}')
    coord.submit_stage(sub)

    coord.cycle("claude")

    assert prepared == [("abc123", source)]


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
                        lambda *args: notified.append(args) or True)
    adapter = IntakeStageAdapter(worktree_reset=lambda record: True, observer=fake,
                                 apply_route=lambda *args: "proof",
                                 handoff=coordinated_intake.hold_intake)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 3
    coord.cycle("claude")

    assert len(applied) == 1 and released == [7] and len(notified) == 1


def test_exhaustion_retries_a_failed_notification_before_holding(make_coord, monkeypatch):
    fake = IntakeSession()
    deliveries = iter((False, True))
    notified = []
    monkeypatch.setattr(loop, "_run", lambda cmd: SimpleNamespace(
        returncode=0, stdout='{"title":"old","labels":[],"comments":[]}'))
    monkeypatch.setattr(coordinated_intake, "apply_intake", lambda *args: None)
    monkeypatch.setattr(coordinated_intake, "intake_result_is_durable", lambda *args: True)
    monkeypatch.setattr(loop, "_release_triage", lambda *args: None)
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args) or next(deliveries))
    adapter = IntakeStageAdapter(worktree_reset=lambda record: True, observer=fake,
                                 apply_route=lambda *args: "proof",
                                 handoff=coordinated_intake.hold_intake)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 3
    assert len(notified) == 2
    assert record_of(coord, identity).notifications == 1
