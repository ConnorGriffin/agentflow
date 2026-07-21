"""Durable Intake behavior through the session coordinator's public interface."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

from conftest import FakeSession, permits, record_of, starts_until_held

from agentflow import coordinated_intake, github, intake as intake_mod, loop
from agentflow.coordinator import IntakeStageAdapter, Submission
from agentflow.coordinator import tracer
from agentflow.coordinator.providers import ProviderCause, ProviderObservation

_READY_MESSAGE = ('{"route":"ready","title":"Scoped","body":"brief",'
                  '"complexity":"deep","effort":"medium"}')


def _capture(final_message: str):
    """Run one raw provider message through Intake's public stage capture."""
    obs = ProviderObservation(cause=ProviderCause.NONE, final_message=final_message)
    return IntakeStageAdapter(worktree_reset=lambda r: True,
                              apply_route=lambda *a: "proof").capture(None, obs)


def test_valid_structured_routes_capture_through_the_stage_interface():
    from agentflow.coordinator.intake_stage import decode_result
    from agentflow.intake import IntakeRoute

    ready = decode_result(_capture(_READY_MESSAGE))
    assert ready.route is IntakeRoute.READY and ready.complexity.value == "deep"
    grill = decode_result(_capture('{"route":"grill","body":"which did you mean?"}'))
    assert grill.route is IntakeRoute.GRILL
    mockup = decode_result(_capture('{"route":"mockup","body":"kickoff"}'))
    assert mockup.route is IntakeRoute.MOCKUP


def test_invalid_structured_output_captures_no_outcome():
    # A ready with no complexity, and a non-object payload, both yield no captured outcome —
    # the stage stays incomplete and retries rather than projecting partial content.
    assert _capture('{"route":"ready","title":"t","body":"brief"}') is None
    assert _capture("not structured output at all") is None


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
    monkeypatch.setattr(github, "issue_labels",
                        lambda repo, issue: frozenset({"ready-for-agent"}))
    record = SimpleNamespace(repo="o/r", subject="7")

    assert coordinated_intake.intake_claim_ready(record) is False


def test_production_claim_proof_fails_closed_when_labels_unreadable(monkeypatch):
    # A read that couldn't reach GitHub comes back as None (unknown) — the claim proof
    # refuses to admit rather than treating unknown as "claim absent".
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: None)
    record = SimpleNamespace(repo="o/r", subject="7")

    assert coordinated_intake.intake_claim_ready(record) is False


def test_production_claim_proof_admits_on_present_triaging_label(monkeypatch):
    from agentflow.loop import TRIAGING
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset({TRIAGING}))
    record = SimpleNamespace(repo="o/r", subject="7")

    assert coordinated_intake.intake_claim_ready(record) is True


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


def test_completed_projection_logs_durable_claim_release(make_coord):
    fake = IntakeSession()
    lines = []
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: "issue-proof")
    coord = make_coord(fake, adapter=adapter, log=lines.append)
    identity = coord.submit_stage(_submission())
    coord.cycle("claude")
    fake.message = ('{"route":"ready","title":"Scoped","body":"brief",'
                    '"complexity":"deep","effort":"medium"}')
    fake.end(identity, cause=ProviderCause.PROCESS)

    coord.cycle("claude")
    coord.cycle("claude")

    assert "o/r: 7: intake: attempt 1/3 settled — route parsed; claim released" in lines


def test_unprojected_completed_intake_keeps_rollback_draining(make_coord):
    fake = IntakeSession()
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: None)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())
    coord.cycle("claude")
    fake.message = ('{"route":"ready","title":"Scoped","body":"brief",'
                    '"complexity":"deep","effort":"medium"}')
    fake.end(identity, cause=ProviderCause.PROCESS)

    coord.cycle("claude")

    assert record_of(coord, identity).state == "completed"
    assert tracer.owned_issues(coord._store.load().values(), "o/r", lane="triaging") == {7}


def test_unparsed_clean_exit_gets_one_targeted_repair_then_holds(make_coord):
    # A clean Intake exit that parsed no route owns no durable partial work, so a second full
    # replay would be identical. The stage gets its initial attempt plus exactly one targeted
    # repair (naming the missing route), then parks rather than burning a third session (#225).
    fake = IntakeSession()
    holds = []
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: "issue-proof",
        handoff=lambda record: holds.append(record.identity) or "hold-proof")
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    starts = starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE)

    assert starts == 2
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


def test_completed_intake_disposes_its_worktree_before_retiring(make_coord, tmp_path):
    """A completed Intake settlement removes its read-only checkout *before* the record retires,
    so a leftover worktree can never later read as ambiguous legacy activation evidence
    (issue #106). Exercised end to end through ``Coordinator.cycle``."""
    fake = IntakeSession()
    wt = tmp_path / "wd" / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    wt.mkdir(parents=True)
    retired_at_dispose = []

    def dispose(record):
        retired_at_dispose.append(record.retired)  # must still be owned when we dispose
        shutil.rmtree(record.source, ignore_errors=True)
        return not Path(record.source).exists()

    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: "issue-proof", worktree_dispose=dispose)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="intake", pool="claude",
        source=str(wt), input_ptr="durable issue"))
    coord.cycle("claude")
    fake.message = _READY_MESSAGE
    fake.end(identity, cause=ProviderCause.PROCESS)

    coord.cycle("claude")   # classifies the ended attempt -> completed
    coord.cycle("claude")   # settles the completed record -> disposes then retires

    assert not wt.exists()                             # the read-only checkout is gone
    assert record_of(coord, identity).retired is True  # the record settled
    assert retired_at_dispose == [False]               # disposed BEFORE it retired


def test_undisposable_intake_worktree_defers_retirement(make_coord):
    """If the checkout cannot be disposed, settlement withholds the completion proof and the
    record stays completed-and-owned so a later cycle retries, never retiring over evidence."""
    fake = IntakeSession()
    adapter = IntakeStageAdapter(
        worktree_reset=lambda record: True, observer=fake,
        apply_route=lambda record, result: "issue-proof",
        worktree_dispose=lambda record: False)  # never manages to remove it
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())
    coord.cycle("claude")
    fake.message = _READY_MESSAGE
    fake.end(identity, cause=ProviderCause.PROCESS)

    coord.cycle("claude")
    coord.cycle("claude")

    settled = record_of(coord, identity)
    assert settled.state == "completed" and settled.retired is False


def test_dispose_worktree_is_idempotent_and_marker_guarded(monkeypatch, tmp_path):
    """The disposer removes only a real ``<pool>-intake/issue-<n>`` checkout, is a no-op success
    on an already-absent one, and refuses a source outside the intake marker."""
    calls = []
    monkeypatch.setattr(loop, "_run",
                        lambda cmd: calls.append(cmd) or SimpleNamespace(returncode=0, stdout=""))
    wt = tmp_path / "wd" / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    absent = SimpleNamespace(source=str(wt), pool="claude", subject="7")
    assert coordinated_intake.dispose_worktree(absent) is True   # already gone — no git call
    assert calls == []

    outside = SimpleNamespace(source="/tmp/not-an-intake-worktree", pool="claude", subject="7")
    assert coordinated_intake.dispose_worktree(outside) is False

    wt.mkdir(parents=True)
    monkeypatch.setattr(loop, "_run", lambda cmd: (shutil.rmtree(wt, ignore_errors=True)
                                                   or SimpleNamespace(returncode=0, stdout="")))
    present = SimpleNamespace(source=str(wt), pool="claude", subject="7")
    assert coordinated_intake.dispose_worktree(present) is True
    assert not wt.exists()


def test_production_projection_applies_once_then_releases_claim(make_coord, monkeypatch):
    fake = IntakeSession()
    released, notified = [], []
    issue = {"title": "old", "body": "original", "labels": ["agentflow:triaging"],
             "comments": []}
    label_failures = [1]

    def gh(cmd, cwd=None, timeout=None):
        # Both intake and coordinated_intake now reach GitHub through the shared module
        # (ADR 0040), so this fake serves every read and write via github's one `_run`.
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
    # Intake and coordinated_intake both shell out through the shared github module's one
    # `_run` (ADR 0040); patching it here lets this fake serve every read and write.
    monkeypatch.setattr(github, "_run", gh)
    def release(repo, number):
        released.append(number)
        issue["labels"].remove("agentflow:triaging")
        return True

    monkeypatch.setattr(loop, "_release_triage", release)
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args) or True)

    assert coord.cycle("claude") == []  # comment lands, label write is interrupted
    assert coord.cycle("claude") == []  # retry finishes labels without reposting
    assert len(issue["comments"]) == 1
    assert released == [7] and len(notified) == 1


def test_route_notification_retries_with_one_stable_delivery_identity(make_coord, monkeypatch):
    fake = IntakeSession()
    deliveries = iter((False, True))
    notified = []
    monkeypatch.setattr(github, "api",
                        lambda *a, **k: {"title": "old", "labels": [], "comments": []})
    monkeypatch.setattr(coordinated_intake, "apply_intake", lambda *args: None)
    monkeypatch.setattr(coordinated_intake, "intake_result_is_durable", lambda *args: True)
    monkeypatch.setattr(loop, "_release_triage", lambda *args: True)
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args) or next(deliveries))
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

    coord.cycle("claude")  # captures the route; projection starts next cycle
    coord.cycle("claude")  # first ntfy delivery reports failure
    assert record_of(coord, identity).retired is False
    coord.cycle("claude")  # stable-sequence retry succeeds

    assert notified[0][3] == notified[1][3]
    assert record_of(coord, identity).retired is True


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


def _hold_seams(monkeypatch, comments, *, applied, released, notified,
                deliver=lambda: True):
    """Wire the intake hold's shared-envelope seams: the durable comment thread it reads and
    proves through (ADR 0042), the projection that posts the held-route comment, the claim
    release, and the operator ping — all stated as facts, none as ``gh`` argument vectors."""
    monkeypatch.setattr(github, "api", lambda *a, **k: {"title": "old", "labels": []})
    monkeypatch.setattr(github, "issue_comments",
                        lambda repo, number: None if comments is None
                        else [github.Comment(body=body, created_at="") for body in comments])

    def apply(repo, number, title, labels, result):
        applied.append(number)
        comments.append(result.body)  # the held-route comment is the durable handoff marker

    monkeypatch.setattr(coordinated_intake, "apply_intake", apply)
    monkeypatch.setattr(loop, "_release_triage",
                        lambda repo, number: released.append(number) or True)
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args) or deliver())


def test_production_exhaustion_holds_and_notifies_once(make_coord, monkeypatch):
    fake = IntakeSession()
    applied, released, notified, comments = [], [], [], []
    _hold_seams(monkeypatch, comments, applied=applied, released=released, notified=notified)
    adapter = IntakeStageAdapter(worktree_reset=lambda record: True, observer=fake,
                                 apply_route=lambda *args: "proof",
                                 handoff=coordinated_intake.hold_intake)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 2
    coord.cycle("claude")

    # The hold is posted once, the claim released, and the operator pinged exactly once.
    assert applied == [7] and released == [7] and len(notified) == 1


def test_exhaustion_hold_is_idempotent_across_a_restart(make_coord, monkeypatch):
    fake = IntakeSession()
    applied, released, notified, comments = [], [], [], []
    _hold_seams(monkeypatch, comments, applied=applied, released=released, notified=notified)
    adapter = IntakeStageAdapter(worktree_reset=lambda record: True, observer=fake,
                                 apply_route=lambda *args: "proof",
                                 handoff=coordinated_intake.hold_intake)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    assert starts_until_held(coord, fake, identity, "claude", ProviderCause.NONE) == 2
    assert applied == [7] and len(notified) == 1

    # A daemon restart replays the handoff over the durable comment marker: it re-proves the
    # same hold and neither posts a second time nor pings again.
    make_coord(fake, adapter=adapter).cycle("claude")
    assert applied == [7] and len(notified) == 1


def test_exhaustion_holds_nothing_when_the_thread_is_unreadable(make_coord, monkeypatch):
    """Fail closed: a comment read that could not reach GitHub stays unknown, so the hold is
    never proven, no ping is sent, and the triaging claim is retained for a retry — an
    unreadable thread is never silently treated as an empty one that completes the hold."""
    fake = IntakeSession()
    applied, released, notified = [], [], []
    _hold_seams(monkeypatch, None, applied=applied, released=released, notified=notified)
    adapter = IntakeStageAdapter(worktree_reset=lambda record: True, observer=fake,
                                 apply_route=lambda *args: "proof",
                                 handoff=coordinated_intake.hold_intake)
    coord = make_coord(fake, adapter=adapter)
    identity = coord.submit_stage(_submission())

    held = False
    for _ in range(12):
        outcomes = coord.cycle("claude")
        if any(o.identity == identity and o.status == "held" for o in outcomes):
            held = True
            break
        if permits(coord, "claude") > 0:
            fake.end(identity, cause=ProviderCause.NONE)
    assert not held
    assert applied == [] and released == [] and notified == []
