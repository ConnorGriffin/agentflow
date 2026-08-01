"""Coordinator-only dispatch: submission, pause/drain, and deletion guards (issue #109)."""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeSession, record_of

from agentflow import (coordinated_build, coordinated_mockup, coordinated_respond,
                       coordinated_review, dispatch, github, live, loop, pipeline)
from agentflow.coordinator import MockupStageAdapter
from agentflow.coordinator import admission
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import WAITING, Record
from agentflow.intake import INTAKE_MARK
from agentflow.loop import RepoConfig


def test_stage_caps_remain_named_inputs_to_the_coordinator_gate():
    assert dict(admission.STAGE_CAPS) == {"triage": 3, "build": 2, "mockup": 1, "respond": 1,
                                          "research": 1}
    assert admission.MACHINE_CEILING > 0


def test_paused_cycle_submits_nothing_but_still_reconciles(monkeypatch):
    monkeypatch.setattr(dispatch, "_submit_repo", lambda *a: pytest.fail(
        "pause may not submit cold work"))
    reconciled = []
    monkeypatch.setattr(dispatch.pipeline, "reconcile_and_project",
                        lambda coord, _log=None: reconciled.append(coord))
    claims = []
    monkeypatch.setattr(dispatch.pipeline, "reconcile_orphaned_claims",
                        lambda cfg, _log=None: claims.append(cfg.repo))
    coord = object()

    dispatch.run_cycle([RepoConfig("o/r", "/tmp")], submit_new=False,
                       coordinator=coord, _log=lambda _line: None)

    assert reconciled == [coord]
    assert claims == ["o/r"]


def test_active_cycle_submits_each_repo_then_reconciles_once(monkeypatch):
    submitted = []
    monkeypatch.setattr(dispatch, "_submit_repo",
                        lambda cfg, coord, log: submitted.append((cfg.repo, coord)))
    reconciled = []
    monkeypatch.setattr(dispatch.pipeline, "reconcile_and_project",
                        lambda coord, _log=None: reconciled.append(coord))
    claims = []
    monkeypatch.setattr(dispatch.pipeline, "reconcile_orphaned_claims",
                        lambda cfg, _log=None: claims.append(cfg.repo))
    coord = object()

    dispatch.run_cycle([RepoConfig("o/a", "/a"), RepoConfig("o/b", "/b")],
                       coordinator=coord, _log=lambda _line: None)

    assert sorted(submitted) == [("o/a", coord), ("o/b", coord)]
    assert reconciled == [coord]
    assert sorted(claims) == ["o/a", "o/b"]


def test_cycle_withdraws_cold_mockup_but_recovers_started_continuation(
        make_coord, monkeypatch):
    fake = FakeSession()
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        observer=fake,
    )
    old = make_coord(fake, adapter=adapter)
    running = old.submit_stage(coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 11, "title": "Started", "body": "Draw it"}, "claude"))
    old.cycle("claude")
    fake.end(running, cause=ProviderCause.PROCESS)
    cold = old.submit_stage(coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 12, "title": "Still held", "body": "Draw it"}, "claude"))
    coord = make_coord(fake, adapter=adapter,
                       disabled_cold_stages=frozenset({"mockup"}))
    monkeypatch.setattr(live, "replace_projection", lambda records: None)
    monkeypatch.setattr(pipeline, "reconcile_orphaned_claims", lambda *a, **k: 0)

    dispatch.run_cycle([RepoConfig("o/r", "/w")], submit_new=False,
                       coordinator=coord, _log=lambda _line: None)

    assert coord.stage_record(cold) is None
    resumed = record_of(coord, running)
    assert resumed.state == "running" and resumed.continuation and resumed.attempts == 2


def test_cycle_withdraws_a_mockup_reservation_that_never_started(
        make_coord, monkeypatch):
    fake = FakeSession()
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        observer=fake,
    )
    old = make_coord(fake, adapter=adapter)
    identity = old.submit_stage(coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 13, "title": "Reserved", "body": "Draw it"}, "claude"))
    fake.crash_start = True
    with pytest.raises(RuntimeError):
        old.cycle("claude")
    fake.crash_start = False
    coord = make_coord(fake, adapter=adapter,
                       disabled_cold_stages=frozenset({"mockup"}))
    monkeypatch.setattr(live, "replace_projection", lambda records: None)
    monkeypatch.setattr(pipeline, "reconcile_orphaned_claims", lambda *a, **k: 0)

    dispatch.run_cycle([RepoConfig("o/r", "/w")], submit_new=False,
                       coordinator=coord, _log=lambda _line: None)

    assert coord.stage_record(identity) is None


def test_cycle_keeps_a_capacity_blocked_mockup_restart_resume(
        make_coord, monkeypatch):
    fake = FakeSession()
    adapter = MockupStageAdapter(
        outcome_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        observer=fake,
    )
    old = make_coord(fake, adapter=adapter, daemon_generation="old")
    identity = old.submit_stage(coordinated_mockup.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 14, "title": "Restart", "body": "Draw it"}, "claude"))
    old.cycle("claude")
    fake.kill(identity)
    fake.gate_open = False
    restarted = make_coord(fake, adapter=adapter, daemon_generation="new",
                           disabled_cold_stages=frozenset({"mockup"}))
    monkeypatch.setattr(live, "replace_projection", lambda records: None)
    monkeypatch.setattr(pipeline, "reconcile_orphaned_claims", lambda *a, **k: 0)

    dispatch.run_cycle([RepoConfig("o/r", "/w")], submit_new=False,
                       coordinator=restarted, _log=lambda _line: None)
    dispatch.run_cycle([RepoConfig("o/r", "/w")], submit_new=False,
                       coordinator=restarted, _log=lambda _line: None)

    waiting = record_of(restarted, identity)
    assert waiting.state == "waiting" and waiting.restart_resumes == 1
    fake.gate_open = True
    dispatch.run_cycle([RepoConfig("o/r", "/w")], submit_new=False,
                       coordinator=restarted, _log=lambda _line: None)
    resumed = record_of(restarted, identity)
    assert resumed.state == "running" and resumed.restart_resumes == 1


def test_orphaned_claim_is_cleared_only_after_durable_reconciliation(monkeypatch):
    from agentflow import coordinated_build, github

    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    # The four claim lanes are listed in order (building, triaging, drawing, resolving); only the
    # building lane holds a stale-claimed issue. The proof read back shows the label gone.
    listings = iter([[github.ClaimedIssue(7, "2020-01-01T00:00:00Z")], [], [], []])
    monkeypatch.setattr(github, "claimed_issues", lambda repo, label: next(listings))
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append((issue, label)) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert pipeline.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert removed == [(7, "agentflow:building")]


def test_claim_reconciliation_reads_labels_off_the_hourly_budget_not_search(monkeypatch):
    """Reconciliation runs four lanes per repo every cycle. Asking GitHub's search for each one
    exceeds its ~30/minute ceiling across a fleet and starves the lane permanently, so the listing
    must be the module's off-search claim read — whose own contract (`github.claimed_issues`)
    keeps it on REST and drops the pull requests that share the issue number sequence."""
    from agentflow import coordinated_build, github

    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [])
    asked = []

    def listing(repo, label):
        asked.append((repo, label))
        if label != "agentflow:building":
            return []
        return [github.ClaimedIssue(7, "2020-01-01T00:00:00Z")]

    monkeypatch.setattr(github, "claimed_issues", listing)
    monkeypatch.setattr(github, "api",
                        lambda *a, **k: pytest.fail("no lane may reach for the escape hatch"))
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append(issue) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert pipeline.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert removed == [7]
    assert asked == [("o/r", "agentflow:building"), ("o/r", "agentflow:triaging"),
                     ("o/r", "agentflow:drawing-mockup"), ("o/r", "wayfinder:resolving")]


def test_unreadable_coordinator_state_clears_no_claim(monkeypatch):
    from agentflow import coordinated_build, github
    from agentflow.coordinator.store import StoreUnavailable

    monkeypatch.setattr(pipeline.tracer, "load_records",
                        lambda: (_ for _ in ()).throw(StoreUnavailable("locked")))
    monkeypatch.setattr(github, "claimed_issues",
                        lambda *a, **k: pytest.fail("must not inspect or clear claims"))
    monkeypatch.setattr(github, "remove_label",
                        lambda *a, **k: pytest.fail("must not clear claims"))

    assert pipeline.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 0


def test_waiting_owner_retains_claim_but_settled_hold_does_not(monkeypatch):
    from agentflow import coordinated_build, github
    from agentflow.coordinator.record import HELD, WAITING, Record

    waiting = Record(identity="wait", stage="build", pool="claude", demand=5,
                     repo="o/r", subject="7", state=WAITING, claim=True)
    held = Record(identity="held", stage="review", pool="codex", demand=2,
                  repo="o/r", subject="8", state=HELD, claim=False)
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [waiting, held])
    # The building lane lists both issues; #7 is shielded by the live waiting build, #8 is not.
    listings = iter([[github.ClaimedIssue(7, "2020-01-01T00:00:00Z"),
                      github.ClaimedIssue(8, "2020-01-01T00:00:00Z")], [], [], []])
    monkeypatch.setattr(github, "claimed_issues", lambda repo, label: next(listings))
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append(issue) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert pipeline.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert removed == [8]


def test_build_submission_enters_the_coordinator_then_claims_runnable_work(monkeypatch):
    from agentflow.coordinator.record import Record, WAITING

    issue = {"number": 7, "title": "Do it", "body": "brief",
             "labels": [{"name": "ready-for-agent"},
                        {"name": "agentflow:complexity:deep"},
                        {"name": "agentflow:effort:high"}]}
    monkeypatch.setattr(loop, "_next_ready_issue",
                        lambda cfg, reserved=frozenset(), _log=None: issue)
    builder = SimpleNamespace(tool="claude")
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (builder, None, ""))
    events = []
    monkeypatch.setattr(dispatch, "claim", lambda repo, number, _label: events.append("claim") or True)
    waiting = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                     state=WAITING)
    coord = SimpleNamespace(
        submit_stage=lambda submission: events.append(submission.stage) or "o/r|7|build|-",
        stage_record=lambda identity: waiting)

    assert "submitted" in dispatch._submit_coordinated_build(
        RepoConfig("o/r", "/tmp"), coord, None)
    # The submission enters the coordinator first; the issue is claimed only once admission has a
    # runnable record — never before, so a held no-op never stamps a false building claim (#245).
    assert events == ["build", "claim"]


def test_daemon_does_not_claim_or_launch_when_the_build_stays_held(monkeypatch):
    # After a maintainer `pickup` relabels an exhausted issue back to `ready-for-agent`, the daemon
    # can pick it — but it must not auto-resume the terminal held Build. An ordinary resubmission
    # reuses the held record, so the daemon claims nothing and reports the held state (#245).
    from agentflow.coordinator.record import Record, HELD

    issue = {"number": 7, "title": "Do it", "body": "brief",
             "labels": [{"name": "ready-for-agent"},
                        {"name": "agentflow:complexity:deep"},
                        {"name": "agentflow:effort:high"}]}
    monkeypatch.setattr(loop, "_next_ready_issue",
                        lambda cfg, reserved=frozenset(), _log=None:
                        None if 7 in reserved else issue)
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(dispatch, "claim", lambda *a: pytest.fail("must not claim a held no-op"))
    held = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                  state=HELD, claim=False)
    coord = SimpleNamespace(
        submit_stage=lambda submission: "o/r|7|build|-",
        stage_record=lambda identity: held)

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", "/tmp"), coord, None)
    assert "held" in result and "submitted" not in result


def _ready_queue(monkeypatch, rows, in_flight=frozenset()):
    """Install a real ready-for-agent queue behind `_next_ready_issue`: the labelled listing, no
    blockers, and a known in-flight set. `rows` is a list of (number, labels)."""
    listing = [loop.github.IssueRow(number=n, title=f"issue {n}", body="brief",
                                    labels=frozenset(labels)) for n, labels in rows]
    monkeypatch.setattr(loop.github, "list_issues", lambda repo, **k: list(listing))
    monkeypatch.setattr(loop, "_native_blockers", lambda cfg, n: set())
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: set(in_flight))


_DIALS = ["ready-for-agent", "agentflow:complexity:deep", "agentflow:effort:high"]


def test_build_pass_skips_a_mislabelled_queue_head_and_submits_the_next_issue(
        monkeypatch, tmp_path):
    # A ready issue with no complexity dial can never run, so it must not starve the valid work
    # queued behind it — the same pass reports it and submits the next candidate (#327). The skip
    # also self-heals: the labels came off the ready listing itself, so the missing dial is a
    # definite fact, and stripping `ready-for-agent` sends the issue back through intake to earn
    # its dials instead of logging the same skip forever (#433).
    from agentflow.coordinator.record import Record, WAITING

    _ready_queue(monkeypatch, [(462, ["ready-for-agent"]), (468, _DIALS)])
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    stripped = []
    monkeypatch.setattr(dispatch.github, "remove_label",
                        lambda repo, number, label: stripped.append((repo, number, label)) or True)
    claimed = []
    monkeypatch.setattr(dispatch, "claim", lambda repo, number, _label: claimed.append(number) or True)
    submitted = []
    coord = SimpleNamespace(
        submit_stage=lambda s: submitted.append(int(s.subject)) or f"o/r|{s.subject}|build|-",
        stage_record=lambda identity: Record(identity=identity, stage="build", pool="claude",
                                             demand=5, state=WAITING))

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", str(tmp_path)), coord, None)

    assert claimed == [468] and submitted == [468]
    assert stripped == [("o/r", 462, "ready-for-agent")]
    assert "#462" in result and "complexity" in result and "re-triages" in result
    assert "#468: submitted" in result


def test_a_failed_ready_label_strip_still_reaches_the_work_behind_it(monkeypatch, tmp_path):
    # The heal is best-effort: a strip the tracker refuses changes nothing — the skip is
    # reported, the pass still submits the valid candidate behind it, and the untouched label
    # simply retries next cycle.
    from agentflow.coordinator.record import Record, WAITING

    _ready_queue(monkeypatch, [(462, ["ready-for-agent"]), (468, _DIALS)])
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(dispatch.github, "remove_label", lambda repo, number, label: False)
    claimed = []
    monkeypatch.setattr(dispatch, "claim", lambda repo, number, _label: claimed.append(number) or True)
    coord = SimpleNamespace(
        submit_stage=lambda s: f"o/r|{s.subject}|build|-",
        stage_record=lambda identity: Record(identity=identity, stage="build", pool="claude",
                                             demand=5, state=WAITING))

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", str(tmp_path)), coord, None)

    assert claimed == [468]
    assert "#462" in result and "will retry next cycle" in result
    assert "#468: submitted" in result


def test_build_pass_passes_over_an_exhausted_held_head_without_resuming_it(monkeypatch, tmp_path):
    # An exhausted held Build is terminal until a maintainer resumes it by hand — the pass leaves
    # it untouched, reports it, and still reaches the valid issue behind it (#327/#245).
    from agentflow.coordinator.record import HELD, Record, WAITING

    _ready_queue(monkeypatch, [(59, _DIALS), (64, _DIALS)])
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(dispatch.coordinated_build, "resume_if_held",
                        lambda *a: pytest.fail("automatic dispatch must never auto-resume"))
    claimed = []
    monkeypatch.setattr(dispatch, "claim", lambda repo, number, _label: claimed.append(number) or True)
    records = {
        "o/r|59|build|-": Record(identity="o/r|59|build|-", stage="build", pool="claude",
                                 demand=5, state=HELD, claim=False),
        "o/r|64|build|-": Record(identity="o/r|64|build|-", stage="build", pool="claude",
                                 demand=5, state=WAITING),
    }
    coord = SimpleNamespace(submit_stage=lambda s: f"o/r|{s.subject}|build|-",
                            stage_record=records.get)

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", str(tmp_path)), coord, None)

    assert claimed == [64]
    assert records["o/r|59|build|-"].state == HELD
    assert "#59: Build held" in result and "#64: submitted" in result


def test_build_pass_stops_when_the_ready_queue_cannot_be_read(monkeypatch, tmp_path):
    # Unknown shared state is not a per-candidate skip: with the in-flight set unreadable the pass
    # dispatches nothing rather than scanning on with incomplete duplicate-work protection.
    _ready_queue(monkeypatch, [(1, _DIALS)])
    monkeypatch.setattr(loop, "_issues_in_flight", lambda cfg: None)
    monkeypatch.setattr(dispatch, "claim", lambda *a: pytest.fail("must not claim while blind"))
    # The self-heal strips a label only on a definite "ready with no dial" answer — an unreadable
    # queue is not one, so no label may move while blind.
    monkeypatch.setattr(dispatch.github, "remove_label",
                        lambda *a: pytest.fail("must not strip a label while blind"))
    coord = SimpleNamespace(
        submit_stage=lambda s: pytest.fail("must not submit while blind"),
        stage_record=lambda identity: None)

    assert dispatch._submit_coordinated_build(
        RepoConfig("o/r", str(tmp_path)), coord, None) == "no ready-for-agent issues"


def test_build_pass_reports_when_every_ready_candidate_is_undispatchable(monkeypatch, tmp_path):
    _ready_queue(monkeypatch, [(1, ["ready-for-agent"]), (2, ["ready-for-agent"])])
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(dispatch.github, "remove_label", lambda repo, number, label: True)
    monkeypatch.setattr(dispatch, "claim", lambda *a: pytest.fail("nothing runnable to claim"))
    coord = SimpleNamespace(submit_stage=lambda s: "id", stage_record=lambda identity: None)

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", str(tmp_path)), coord, None)

    assert "#1" in result and "#2" in result and "no further runnable" in result


# --- a reply to a parked review's decision belongs to that review (#344) ------------------

_DECISION = {"options": ["Keep the conservative behavior.", "Prompt every user on first run."],
             "missing_guidance": "what a first-time user should see",
             "recommendation": "keep the conservative behavior"}


def _parked_decision_review(**overrides):
    """A review parked on a recorded product decision: held, unretired, and claimless."""
    import json
    from agentflow.coordinator.record import Record

    fields = dict(
        identity="o/r|7|review|sha-a", stage="review", pool="codex", demand=2, repo="o/r",
        subject="7", target="sha-a", state="held", retired=False, claim=False,
        builder_lineage="claude", builder_complexity="deep", change_author_tool="claude",
        review_sequence=3, created_at=100, review_uncertainty=json.dumps(_DECISION),
        source="/work/.agentflow/worktrees/codex-review/pr-42-fix")
    fields.update(overrides)
    return Record(**fields)


def _answered_park_thread():
    """The park handoff, then the maintainer's answer to it — agentflow spoke last before them."""
    return [{"id": "IC_0", "body": "> *agentflow: parked for human review.*\n\nDecide, please."},
            {"id": "IC_1", "body": "keep the conservative behavior"}]


def _stub_answered_park(monkeypatch, records):
    """Wire one PR whose oldest unanswered comment answers its parked review."""
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "keep the conservative behavior", "IC_1", "sha-a"))
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: records)
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: _answered_park_thread())
    monkeypatch.setattr(coordinated_review, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(coordinated_respond, "respond_submission",
                        lambda *a, **k: pytest.fail("a decision answer is never a generic Respond"))
    posted = []
    monkeypatch.setattr(pipeline.github, "pr_comment",
                        lambda repo, pr, body: posted.append(body) or True)
    claimed = []
    stamp = lambda repo, number, _label: claimed.append(number) or True   # noqa: E731
    monkeypatch.setattr(dispatch, "claim", stamp)
    monkeypatch.setattr(coordinated_review, "claim", stamp)
    submitted = []
    return posted, claimed, submitted


def test_a_maintainer_answer_resumes_the_parked_review_instead_of_claiming_a_respond(monkeypatch):
    """The production sequel: the maintainer chose one of the recorded options on the PR. That
    answer opens exactly one resumed review at the same exact head — never a second, generic reply
    stage holding a competing claim on the same issue."""
    parked = _parked_decision_review()
    posted, claimed, submitted = _stub_answered_park(monkeypatch, [parked])
    coord = SimpleNamespace(submit_stage=submitted.append)

    result = dispatch._submit_coordinated_respond(RepoConfig("o/r", "/work"), coord, None)

    assert "resumed the parked review" in result
    assert claimed == [7]
    assert len(submitted) == 1
    resumed = submitted[0]
    assert resumed.stage == "review" and resumed.target == "sha-a"
    assert resumed.pool == "codex" and resumed.builder_lineage == "claude"
    assert resumed.review.sequence == 4 and resumed.review.uncertainty is None
    from agentflow.review_policy import decision_answer_target
    assert decision_answer_target(resumed.review.handoff) == "IC_1"
    # One public marker answers that exact comment, so the reply queue and the merge gate both
    # see the question closed without a second comment protocol.
    assert len(posted) == 1 and "agentflow-respond-target:IC_1" in posted[0]


def test_a_replay_after_the_resumed_review_was_recorded_opens_no_second_lifecycle(monkeypatch):
    """Crash boundary: the resumed review is durable but its answered-marker comment never landed.
    The replay completes only the marker — no second review, claim, or notification."""
    from agentflow.review_policy import decision_answer_handoff

    parked = _parked_decision_review()
    already = _parked_decision_review(
        identity="o/r|7|review|sha-a|s4", state="waiting", claim=True, review_sequence=4,
        created_at=200, review_uncertainty=None,
        review_handoff=decision_answer_handoff("IC_1", "keep the conservative behavior"))
    posted, claimed, submitted = _stub_answered_park(monkeypatch, [parked, already])
    coord = SimpleNamespace(
        submit_stage=lambda _s: pytest.fail("the resumed review is already durable"))

    result = dispatch._submit_coordinated_respond(RepoConfig("o/r", "/work"), coord, None)

    assert "resumed the parked review" in result
    assert claimed == [] and submitted == []
    assert len(posted) == 1 and "agentflow-respond-target:IC_1" in posted[0]


def test_an_answer_waits_while_a_hand_started_review_already_owns_the_issue(monkeypatch):
    """The maintainer ran the recovery command *and* answered on the PR. The running review owns the
    issue, so the answer waits for it rather than opening a competing second review — and no
    Respond claims the comment in the meantime."""
    parked = _parked_decision_review()
    by_hand = _parked_decision_review(
        identity="o/r|7|review|sha-a|s4", state="waiting", claim=True, review_sequence=4,
        created_at=200)
    posted, claimed, submitted = _stub_answered_park(monkeypatch, [parked, by_hand])
    coord = SimpleNamespace(submit_stage=lambda _s: pytest.fail("no competing second review"))

    result = dispatch._submit_coordinated_respond(RepoConfig("o/r", "/work"), coord, None)

    assert "already owns this issue" in result
    assert claimed == [] and posted == []


def test_ordinary_pr_discussion_after_a_parked_review_still_enters_respond(monkeypatch):
    """Out of scope by design: once the recorded decision has been answered, a further comment is
    discussion, not approval to resume. It keeps the ordinary reply path, claim and all."""
    from agentflow.review_policy import decision_answer_handoff

    parked = _parked_decision_review()
    resumed = _parked_decision_review(
        identity="o/r|7|review|sha-a|s4", state="waiting", claim=True, review_sequence=4,
        created_at=200, review_uncertainty=None,
        review_handoff=decision_answer_handoff("IC_1", "keep the conservative behavior"))
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "unrelated question", "IC_2", "sha-a"))
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [parked, resumed])
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: [
        *_answered_park_thread(),
        {"id": "IC_x", "body": "> *agentflow: your decision resumed the parked review.*\n"
                               "<!-- agentflow-respond-target:IC_1 -->"},
        {"id": "IC_2", "body": "unrelated question"}])
    monkeypatch.setattr(pipeline.github, "pr_comment",
                        lambda *a, **k: pytest.fail("discussion never resumes a parked review"))
    monkeypatch.setattr(coordinated_respond, "respond_submission",
                        lambda *a, **k: SimpleNamespace(subject="7", pool="claude"))
    monkeypatch.setattr(pipeline, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "claim", lambda repo, number, _label: True)
    submitted = []

    result = dispatch._submit_coordinated_respond(
        RepoConfig("o/r", "/work"), SimpleNamespace(submit_stage=submitted.append), None)

    assert "(respond)" in result and len(submitted) == 1


def test_a_second_decision_round_is_still_answerable_after_agentflow_replied(monkeypatch):
    """The resumed review hit a second question and parked again. agentflow has spoken since the
    first park — the resume marker sits below it and the repeat park updates the original comment
    in place — so 'is the park our newest comment?' would strand the maintainer's second answer in
    the generic reply path, claiming the issue for a product decision all over again."""
    from agentflow.review_policy import decision_answer_handoff

    first = _parked_decision_review()
    round_two = _parked_decision_review(
        identity="o/r|7|review|sha-a|s4", review_sequence=4, created_at=200,
        review_handoff=decision_answer_handoff("IC_1", "keep the conservative behavior"))
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "prompt every user", "IC_2", "sha-a"))
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [first, round_two])
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: [
        {"id": "IC_0", "body": "> *agentflow: parked for human review.*\n\nDecide again, please."},
        {"id": "IC_1", "body": "keep the conservative behavior"},
        {"id": "IC_x", "body": "> *agentflow: your decision resumed the parked review.*\n"
                               "<!-- agentflow-respond-target:IC_1 -->"},
        {"id": "IC_2", "body": "prompt every user"}])
    monkeypatch.setattr(coordinated_review, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(coordinated_respond, "respond_submission",
                        lambda *a, **k: pytest.fail("a decision answer is never a generic Respond"))
    posted = []
    monkeypatch.setattr(pipeline.github, "pr_comment",
                        lambda repo, pr, body: posted.append(body) or True)
    claimed = []
    stamp = lambda repo, number, _label: claimed.append(number) or True   # noqa: E731
    monkeypatch.setattr(dispatch, "claim", stamp)
    monkeypatch.setattr(coordinated_review, "claim", stamp)
    submitted = []

    result = dispatch._submit_coordinated_respond(
        RepoConfig("o/r", "/work"), SimpleNamespace(submit_stage=submitted.append), None)

    assert "resumed the parked review" in result
    assert claimed == [7] and len(submitted) == 1
    from agentflow.review_policy import decision_answer_target
    assert decision_answer_target(submitted[0].review.handoff) == "IC_2"
    assert submitted[0].review.sequence == 5        # monotone in the same-head chain
    assert len(posted) == 1 and "agentflow-respond-target:IC_2" in posted[0]


def test_an_older_unanswered_comment_before_the_park_remains_ordinary_discussion(monkeypatch):
    """Reply discovery serves the oldest unanswered comment first. A discussion comment that
    predates the park cannot answer the later decision merely because that decision is still
    outstanding; it stays on the ordinary Respond path."""
    parked = _parked_decision_review()
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "earlier discussion", "IC_old", "sha-a"))
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [parked])
    monkeypatch.setattr(github, "pr_comment_rows", lambda repo, pr: [
        {"id": "IC_old", "body": "earlier discussion"},
        {"id": "IC_park", "body": "> *agentflow: parked for human review.*\n\nDecide."},
        {"id": "IC_answer", "body": "keep the conservative behavior"},
    ])
    monkeypatch.setattr(pipeline.github, "pr_comment",
                        lambda *a, **k: pytest.fail("older discussion never resumes review"))
    monkeypatch.setattr(coordinated_respond, "respond_submission",
                        lambda *a, **k: SimpleNamespace(subject="7", pool="claude"))
    monkeypatch.setattr(pipeline, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "claim", lambda repo, number, _label: True)
    submitted = []

    result = dispatch._submit_coordinated_respond(
        RepoConfig("o/r", "/work"), SimpleNamespace(submit_stage=submitted.append), None)

    assert "(respond)" in result and len(submitted) == 1


def test_respond_waits_while_a_prior_change_record_owns_the_claim(monkeypatch):
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "please adjust", "cid-1", "base"))
    monkeypatch.setattr(dispatch.pipeline, "owned_issues",
                        lambda cfg, lane=None: {7})
    monkeypatch.setattr(dispatch, "claim", lambda *a: pytest.fail("must not double-claim"))

    result = dispatch._submit_coordinated_respond(
        RepoConfig("o/r", "/tmp"), SimpleNamespace(), None)
    assert "prior change stage" in result


def test_intake_skips_an_issue_a_live_pipeline_stage_already_owns(monkeypatch):
    # A mid-pipeline issue whose triaging label was stripped by the reconciler but whose
    # downstream record still owns it must not be re-claimed by intake — the ownership guard
    # catches the label-already-stripped window (#201).
    from agentflow import coordinated_intake

    def candidate(cfg, reserved=frozenset()):
        return None if 42 in reserved else ({"number": 42, "labels": []}, "")

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(github, "issue_comment_rows", lambda repo, issue: [])
    monkeypatch.setattr(dispatch.pipeline, "owned_issues",
                        lambda cfg, lane=None: {42})
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda: pytest.fail("must not pick a pool for an owned issue"))
    monkeypatch.setattr(dispatch, "claim", lambda *a: pytest.fail("must not re-claim"))
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda *a, **k: pytest.fail("must not submit an owned issue"))

    result = dispatch._submit_coordinated_intake(RepoConfig("o/r", "/tmp"), SimpleNamespace(), None)
    assert result == "no un-triaged issues"


def test_intake_still_claims_a_genuinely_new_issue(monkeypatch):
    from agentflow import coordinated_intake
    from agentflow.coordinator.record import Record, WAITING

    def candidate(cfg, reserved=frozenset()):
        return None if 42 in reserved else ({"number": 42, "labels": []}, "")

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(github, "issue_comment_rows", lambda repo, issue: [])
    monkeypatch.setattr(dispatch.pipeline, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda *a, **k: SimpleNamespace(pool="claude"))
    claimed = []
    monkeypatch.setattr(dispatch, "claim", lambda repo, n, _label: claimed.append(n) or True)
    waiting = Record(identity="o/r|42|intake|-", stage="triage", pool="claude", demand=5,
                     state=WAITING)
    coord = SimpleNamespace(
        submit_stage=lambda submission: "o/r|42|intake|-",
        stage_record=lambda identity: waiting)

    result = dispatch._submit_coordinated_intake(RepoConfig("o/r", "/tmp"), coord, None)
    assert claimed == [42]
    assert "#42 → claude" in result


def test_intake_does_not_claim_a_dedup_hit_on_a_completed_record(monkeypatch):
    # An orphan-reclaim pass strips the triaging label of an already-completed Intake identity;
    # the next cycle reselects the issue and resubmits the same stable identity. submit_stage is
    # idempotent, so it reuses the terminal completed record and creates nothing to run. Dispatch
    # must not re-stamp agentflow:triaging or report a false launch, or the label recreates
    # forever (#308). Covers both a fresh-issue identity (target=None) and a reply-targeted one.
    from agentflow import coordinated_intake
    from agentflow.coordinator.record import Record, COMPLETED

    for target, extra in (("-", ""), ("IC_kwreply", {"_intake_target": "IC_kwreply"})):
        def candidate(cfg, reserved=frozenset(), extra=extra):
            return None if 393 in reserved else ({"number": 393, "labels": []}, extra)

        monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
        monkeypatch.setattr(github, "issue_comment_rows", lambda repo, issue: [])
        monkeypatch.setattr(dispatch.pipeline, "owned_issues",
                            lambda cfg, lane=None: set())
        monkeypatch.setattr(dispatch, "pick_pair",
                            lambda: (SimpleNamespace(tool="claude"), None, ""))
        monkeypatch.setattr(coordinated_intake, "intake_submission",
                            lambda *a, **k: SimpleNamespace(pool="claude"))
        monkeypatch.setattr(dispatch, "claim",
                            lambda *a: pytest.fail("must not claim a terminal dedup no-op"))
        completed = Record(identity=f"o/r|393|intake|{target}", stage="triage", pool="claude",
                           demand=5, state=COMPLETED)
        coord = SimpleNamespace(
            submit_stage=lambda submission, target=target: f"o/r|393|intake|{target}",
            stage_record=lambda identity: completed)

        result = dispatch._submit_coordinated_intake(RepoConfig("o/r", "/tmp"), coord, None)
        assert "already triaged" in result


def test_intake_advances_past_an_already_triaged_held_reply(monkeypatch):
    """The daemon dispatch boundary must not let one consumed reply hide the next one (#449)."""
    from agentflow import coordinated_intake
    from agentflow.coordinator.record import COMPLETED

    cfg = RepoConfig("o/r", "/tmp")
    first = github.IssueRow(396, "first", "b", frozenset({"agentflow:needs-grilling"}))
    second = github.IssueRow(425, "second", "b", frozenset({"agentflow:needs-grilling"}))
    monkeypatch.setattr(github, "list_issues",
                        lambda repo, *, label=None, limit=100: [first, second])
    monkeypatch.setattr(github, "issue_comment_rows", lambda repo, number: [
        {"id": "intake", "body": f"{INTAKE_MARK} — generated by AI.",
         "author": {"login": "agentflow"}},
        {"id": f"reply-{number}", "body": "Please re-triage this.",
         "author": {"login": "o"}},
    ])
    monkeypatch.setattr(loop, "intake_allowlist", lambda repo, workdir: {"o"})
    monkeypatch.setattr(dispatch, "intake_allowlist", lambda repo, workdir: {"o"})
    monkeypatch.setattr(dispatch.pipeline, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="codex"), None, ""))
    monkeypatch.setattr(dispatch, "claim", lambda *args: True)
    submitted = []
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda cfg, issue, extra, comments, tool:
                        submitted.append(issue["number"]) or SimpleNamespace(pool=tool))
    waiting = Record(identity="o/r|425|intake|reply-425", stage="triage", pool="codex",
                     demand=1, state=WAITING)
    completed = Record(identity="o/r|396|intake|reply-396", stage="triage", pool="codex",
                       demand=1, state=COMPLETED)
    coord = SimpleNamespace(
        submit_stage=lambda sub: f"o/r|{submitted[-1]}|intake|reply-{submitted[-1]}",
        stage_record=lambda identity: completed if "|396|" in identity else waiting)

    result = dispatch._submit_coordinated_intake(cfg, coord, None)

    assert submitted == [396, 425]
    assert "#396: maintainer reply already triaged" in result
    assert "#425 → codex" in result


def test_intake_withdraws_the_submission_when_the_claim_fails(monkeypatch):
    # A runnable submission whose GitHub claim mutation fails must withdraw the never-started
    # WAITING record, so no unowned Intake work survives — mirrors build_issue's fail-closed
    # rollback (#308).
    from agentflow import coordinated_intake
    from agentflow.coordinator.record import Record, WAITING

    def candidate(cfg, reserved=frozenset()):
        return None if 42 in reserved else ({"number": 42, "labels": []}, "")

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(github, "issue_comment_rows", lambda repo, issue: [])
    monkeypatch.setattr(dispatch.pipeline, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda *a, **k: SimpleNamespace(pool="claude"))
    monkeypatch.setattr(dispatch, "claim", lambda *a: False)
    waiting = Record(identity="o/r|42|intake|-", stage="triage", pool="claude", demand=5,
                     state=WAITING)
    withdrawn = []
    coord = SimpleNamespace(
        submit_stage=lambda submission: "o/r|42|intake|-",
        stage_record=lambda identity: waiting,
        withdraw_stage=lambda identity: withdrawn.append(identity))

    result = dispatch._submit_coordinated_intake(RepoConfig("o/r", "/tmp"), coord, None)
    assert withdrawn == ["o/r|42|intake|-"]
    assert "claim pending" in result


def test_live_board_is_overwritten_from_the_durable_projection(tmp_path, monkeypatch):
    from agentflow import live

    monkeypatch.setattr(live, "LIVE_FILE", tmp_path / "live.json")
    live.replace_projection([{"number": 9, "stage": "building"}])
    live.replace_projection([{"number": 10, "stage": "reviewing"}])
    assert live.running() == [{"number": 10, "stage": "reviewing"}]


def test_production_dispatch_has_no_legacy_bypass_or_second_counter():
    source = inspect.getsource(dispatch)
    assert "class Governor" not in source
    assert "launch_legacy" not in source
    assert "produce_once" not in source
    assert "respond_once" not in source
    assert "run_once" not in source
    assert "_live =" not in source and "_per_stage" not in source


def test_no_rollout_switch_or_direct_provider_call_survives_in_production_orchestration():
    root = Path(__file__).parents[1] / "agentflow"
    assert not (root / "coordinator" / "rollout.py").exists()
    production = "\n".join(path.read_text() for path in root.rglob("*.py"))
    assert ".launch(" not in production
    assert ".build(" not in production
    assert "MODE_LEGACY" not in production
    assert "class Governor" not in production
    assert "running_strict" not in production

    allowed_spawners = {root / "coordinator" / "launcher.py",
                        root / "coordinator" / "_launch_child.py"}
    allowed_subprocess_run = {
        root / "balancer.py",
        root / "macos_service.py",
        root / "notify.py",
        root / "runner.py",
        root / "coordinator" / "quota_poll.py",
    }
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr == "Popen"):
                    assert path in allowed_spawners, f"provider-capable spawn outside launcher: {path}"
                if (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr == "run"):
                    assert path in allowed_subprocess_run, f"subprocess.run outside adapters: {path}"
                if (isinstance(node.func.value, ast.Name) and node.func.value.id == "os"
                        and (node.func.attr.startswith("exec") or node.func.attr.startswith("spawn")
                             or node.func.attr == "fork")):
                    assert path in allowed_spawners, f"process start outside launcher: {path}"
            if isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.List):
                first = node.args[0].elts[0] if node.args[0].elts else None
                if isinstance(first, ast.Constant) and first.value in {"claude", "codex"}:
                    assert path == root / "runner.py", f"direct provider command execution: {path}"
            if isinstance(node, ast.Call):
                counter_name = None
                if isinstance(node.func, ast.Name):
                    counter_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    counter_name = node.func.attr
                if counter_name in {"Semaphore", "BoundedSemaphore"}:
                    raise AssertionError(f"second capacity ledger primitive: {path}:{node.lineno}")
                if counter_name == "Counter":
                    assert path in {root / "pipeline.py", root / "dashboard_data.py"}, (
                        f"counter outside pacing/projection owners: {path}:{node.lineno}")
            if "coordinator" not in path.parts and isinstance(
                    node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [item.id for target in targets for item in ast.walk(target)
                         if isinstance(item, ast.Name)]
                assert not any("permit" in name.lower() for name in names), (
                    f"second permit ledger outside coordinator: {path}:{node.lineno}")


def _described_argv(tree: ast.AST) -> set[int]:
    """Argv lists that a raised error only *names* — `raise CalledProcessError(1, [...])`. The
    list has to be a direct argument of the exception being raised: once it sits inside a further
    call, that call runs before the exception is ever built, so the argv is executed after all."""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        for arg in [*node.exc.args, *(kw.value for kw in node.exc.keywords)]:
            if isinstance(arg, ast.List):
                ids.add(id(arg))
    return ids


def test_no_module_outside_the_github_module_shells_out_to_gh():
    """ADR 0040: all GitHub access flows through one typed, fail-closed module. Any `gh` argument
    vector built anywhere else is a bypass of the seam — however it is later run.

    It catches an argv written as a literal list, in any of the shapes real code uses. It does not
    chase an argv deliberately disguised — `["g" + "h", ...]`, a `"gh"` hidden behind a module
    constant, a tuple instead of a list. Those take intent to write, and a rule that hunts them
    starts firing on innocent code."""
    root = Path(__file__).parents[1] / "agentflow"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        described = _described_argv(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.List) or id(node) in described:
                continue
            first = node.elts[0] if node.elts else None
            if isinstance(first, ast.Constant) and first.value == "gh":
                assert path == root / "github.py", (
                    f"GitHub access outside the github module: {path}:{node.lineno}")


# GitHub's own wire field names. Each is now read exclusively inside `github.py` and handed
# back as a typed row field, so a reappearance anywhere else is a stage re-learning GitHub's
# schema.
_GITHUB_WIRE_FIELDS = {
    "headRefOid", "headRefName", "baseRefName", "isDraft", "reviewDecision",
    "mergeStateStatus", "statusCheckRollup", "closingIssuesReferences", "mergedAt",
    "mergeCommit", "updatedAt", "createdAt",
}

# `author` and `login` are wire fields too, but unlike the camel-cased names above they are also
# ordinary English words — matched anywhere inside a string they fire on prose like "the author
# supplied no review-depth proposal". Code reads them as dict keys and nothing else, so they are
# matched only when the whole string *is* the key.
_GITHUB_WIRE_KEYS = {"author", "login"}

# The raw comment rows still read outside `github.py`, named site by site rather than dropped
# from the rule above. Six reads, two reasons:
#
#   author / login — the typed comment carries no author at all, so the three predicates that
#     filter comments down to the maintainer's own have nothing else to read:
#     `intake.awaiting_recheck`, `intake.replies_since_intake`, and the qualifying-comment scan
#     in `loop._next_recheck_candidate`.
#   createdAt — the typed comment *does* carry this (as `created_at`), but these three are handed
#     the raw rows by their callers: `dashboard_data._park_since`, `coordinated_revise.
#     _round_evidence`, and the intake-target stamp in `loop._next_recheck_candidate`.
#
# Both are real gaps in the seam, not exceptions to it. Granularity is per file and field, not
# per line — line numbers rot on the next edit — so a *second* `createdAt` read in one of these
# files goes unseen; a new one in any other file does not.
_RAW_COMMENT_ROW_READS = {
    ("intake.py", "author"), ("intake.py", "login"),
    ("loop.py", "author"), ("loop.py", "login"), ("loop.py", "createdAt"),
    ("dashboard_data.py", "createdAt"), ("coordinated_revise.py", "createdAt"),
}

# Instruction text telling a review agent which `gh` command to run is not GitHub access — it is
# prose handed to a session, not a caller reading a PR. Exempt the prompt constant, never the
# module: `reviewer.py` is 400 lines of verdict parsing around this one string, and executable
# code there re-derives GitHub's schema exactly like anywhere else.
_PROMPT_TEXT = {("reviewer.py", "REVIEW_PROMPT")}


def _docstrings(tree: ast.AST) -> set[int]:
    """Every module/class/function docstring in ``tree``. Prose that names a field is
    describing the seam, not crossing it."""
    ids = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        first = node.body[0] if node.body else None
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            ids.add(id(first.value))
    return ids


def _prompt_text(path: Path, tree: ast.AST) -> set[int]:
    """Every string inside this module's declared prompt constants — and only those. A prompt is
    a module-level constant of instruction prose; anything in a function body is code."""
    ids = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {item.id for target in targets for item in ast.walk(target)
                 if isinstance(item, ast.Name)}
        if any((path.name, name) in _PROMPT_TEXT for name in names):
            ids |= {id(item) for item in ast.walk(node) if isinstance(item, ast.Constant)}
    return ids


def test_github_wire_field_names_never_leave_the_github_module():
    """ADR 0040's other half — the *schema* seam. `gh` argument vectors already cannot be built
    outside `github.py`; neither may GitHub's own field names be spoken outside it.

    A stage that writes `headRefOid` is re-deriving GitHub's schema at a site with no business
    owning it — and, worse, re-deriving with it the fail-closed rule for a read that failed.
    This repo decides merges: that rule gets one owner, or it gets silently wrong somewhere.

    Two things are exempt and both are named above, not waved through by file: the prompt prose
    that tells a review agent which `gh` command to run, and the six raw-comment-row reads the
    typed comment cannot yet serve. Prose describing the seam — docstrings — is not crossing it."""
    root = Path(__file__).parents[1] / "agentflow"
    for path in root.rglob("*.py"):
        if path == root / "github.py":
            continue
        tree = ast.parse(path.read_text())
        exempt = _docstrings(tree) | _prompt_text(path, tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if id(node) in exempt:
                continue
            spoken = _GITHUB_WIRE_FIELDS.intersection(re.split(r"[^A-Za-z]+", node.value))
            spoken |= _GITHUB_WIRE_KEYS.intersection({node.value})
            leaked = {field for field in spoken
                      if (path.name, field) not in _RAW_COMMENT_ROW_READS}
            assert not leaked, (
                f"GitHub wire field {sorted(leaked)} outside the github module: "
                f"{path}:{node.lineno}")


# The stage-policy layer — what a stage decides, and the pacing that admits one. Not
# `coordinated_build.py` alone: that file is a 200-line husk now, and the two thousand lines of
# policy that used to live beside it sit in its siblings and in `pipeline`.
_STAGE_POLICY_GLOBS = ("coordinated_*.py", "pipeline.py")

# The one import ring this repo tolerates. `github.py` reaches the `gh` binary through
# `runner._run` at module level, and `runner` gets away with calling back into `github` by
# deferring those imports into function bodies. Named here rather than quietly skipped:
# untangling it means moving process execution out of `runner`, which is its own change. Every
# other ring the graph grows is the defect this test exists to catch.
_TOLERATED_IMPORT_CYCLE = frozenset({"agentflow.github", "agentflow.runner"})


def _module_name(root: Path, path: Path) -> str:
    """The dotted name ``path`` is imported by."""
    parts = list(path.relative_to(root).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(["agentflow", *parts])


def _imported_modules(root: Path, path: Path, tree: ast.AST):
    """Every ``agentflow`` module ``path`` reaches for, in whichever way it spells the import:
    absolute or relative, whole-module or name-from-module, at module level or deferred inside a
    function body, or named as a string handed to ``importlib``. Yields ``(module, node)``."""
    package = _module_name(root, path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "agentflow":
                    yield alias.name, node
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = ".".join(base[:len(base) - node.level + 1])
            elif node.module and node.module.split(".")[0] == "agentflow":
                base = None
            else:
                continue
            module = node.module if base is None else (
                f"{base}.{node.module}" if node.module else base)
            yield module, node
            # `from agentflow import loop` names a module in the alias, not in `module`.
            for alias in node.names:
                yield f"{module}.{alias.name}", node
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == "import_module" and node.args):
            named = node.args[0]
            if isinstance(named, ast.Constant) and isinstance(named.value, str):
                yield named.value, node


def _import_cycles(edges: dict[str, set[str]]) -> list[tuple[str, ...]]:
    """Every ring in the import graph, each as the modules that close it."""
    rings: list[tuple[str, ...]] = []
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(module: str) -> None:
        state[module] = 1
        stack.append(module)
        for other in sorted(edges.get(module, ())):
            if state.get(other) == 1:
                rings.append(tuple(stack[stack.index(other):]))
            elif not state.get(other):
                visit(other)
        stack.pop()
        state[module] = 2

    for module in sorted(edges):
        if not state.get(module):
            visit(module)
    return rings


def test_dispatch_policy_never_imports_the_dispatch_loop():
    """Stage policy stays readable on its own: the coordinated stages own what a stage decides,
    the loop owns which one runs next, and only that second direction may know the first.

    The two used to import each other, and the cycle stayed invisible because every one of those
    imports sat inside a function body — deferred until call time, so Python never complained.
    So this checks the invariant twice over. Stage policy may not name the loop at all, however
    the import is spelled. And no module in the package may take part in an import ring, because
    a function-local import is precisely how the *next* cycle would hide, under some other pair
    of names — the only way to see one coming is to look at the whole graph. Anything genuinely
    shared belongs in a module both sides can import outright.
    """
    root = Path(__file__).parents[1] / "agentflow"
    modules = {_module_name(root, path): path for path in root.rglob("*.py")}

    stage_policy = sorted({path for glob in _STAGE_POLICY_GLOBS for path in root.glob(glob)})
    assert {root / "coordinated_review.py", root / "pipeline.py"} <= set(stage_policy), (
        "the stage-policy globs no longer match the real surface")
    for path in stage_policy:
        for module, node in _imported_modules(root, path, ast.parse(path.read_text())):
            assert module != "agentflow.loop", (
                f"{path.name} imports agentflow.loop at line {node.lineno} — move the shared "
                "vocabulary into a module both sides can import instead of reaching back into "
                "dispatch")

    edges: dict[str, set[str]] = {}
    for name, path in modules.items():
        for module, _ in _imported_modules(root, path, ast.parse(path.read_text())):
            if module in modules and module != name:
                edges.setdefault(name, set()).add(module)

    for ring in _import_cycles(edges):
        assert frozenset(ring) == _TOLERATED_IMPORT_CYCLE, (
            "import cycle — a deferred import is standing in for a module that should not be "
            f"reached at all: {' -> '.join([*ring, ring[0]])}")


def test_only_live_records_protect_a_worktree_from_reclamation(tmp_path, monkeypatch):
    """A hold is terminal and is never retired, so held sources would otherwise be protected for
    the life of the store — they were the majority of everything 'owned' when a repository grew
    past what its sessions could survive (ADR 0050)."""
    from agentflow.coordinator.record import COMPLETED, HELD, RUNNING, WAITING, Record

    store = tmp_path / "records.db"
    store.write_text("")

    def record(state, source, *, stage="build", repo="o/r", retired=False):
        return Record(identity=f"{repo}|1|{stage}|{source}", stage=stage, pool="claude",
                      demand=1, repo=repo, source=source, state=state, retired=retired)

    monkeypatch.setattr(pipeline.tracer, "load_records", lambda path=None: [
        record(WAITING, "/queued"), record(RUNNING, "/running"), record(COMPLETED, "/finishing"),
        record(HELD, "/held-build"), record(HELD, "/held-review", stage="review"),
        record(COMPLETED, "/retired", retired=True),
        record(RUNNING, "/other-repo", repo="o/elsewhere"),
    ])

    assert pipeline.owned_worktrees(RepoConfig("o/r", "/tmp"), store_path=store) == {
        "/queued", "/running", "/finishing"}


def test_a_repository_that_cannot_carry_a_session_receives_no_cold_work(monkeypatch):
    submitted = []
    monkeypatch.setattr(dispatch.pipeline, "owned_worktrees", lambda cfg: {"/live"})
    monkeypatch.setattr(dispatch, "dispatch_preflight",
                        lambda repo, workdir, protected, _log=None: repo != "o/poisoned")
    for name in ("_submit_coordinated_intake", "_submit_coordinated_build",
                 "_submit_coordinated_respond", "_submit_coordinated_research"):
        monkeypatch.setattr(dispatch, name,
                            lambda *a, _stage=name: submitted.append(_stage) or "done")

    dispatch._submit_repo(RepoConfig("o/poisoned", "/p"), SimpleNamespace(), lambda _line: None)
    assert submitted == []

    dispatch._submit_repo(RepoConfig("o/healthy", "/h"), SimpleNamespace(), lambda _line: None)
    assert submitted == ["_submit_coordinated_intake", "_submit_coordinated_build",
                         "_submit_coordinated_respond", "_submit_coordinated_research"]


def _thread(*rows) -> list[dict]:
    """GitHub's own comment rows, shaped as the intake predicates read them."""
    return [{"author": {"login": login}, "body": body, "id": f"IC_{i}"}
            for i, (login, body) in enumerate(rows)]


def _intake_prompt_for(monkeypatch, cfg, issue, thread, *, extra="") -> str:
    """Run one issue through cold intake submission and return the prompt it durably recorded."""
    from agentflow import coordinated_intake

    def candidate(cfg, reserved=frozenset()):
        return None if issue["number"] in reserved else (issue, extra)

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(github, "issue_comment_rows", lambda repo, number: thread)
    monkeypatch.setattr(dispatch.pipeline, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(dispatch, "claim", lambda *a: True)
    monkeypatch.setattr(coordinated_intake, "_run",
                        lambda cmd: SimpleNamespace(returncode=0, stdout="abc123\n"))
    recorded = []
    coord = SimpleNamespace(
        submit_stage=lambda submission: recorded.append(submission) or "id",
        stage_record=lambda identity: Record(identity="id", stage="triage", pool="claude",
                                             demand=5, state=WAITING))

    dispatch._submit_coordinated_intake(cfg, coord, None)
    assert recorded, "the issue was never submitted"
    return json.loads(recorded[0].input_ptr)["prompt"]


def test_a_fresh_intake_reads_the_owners_later_comments_as_authoritative(monkeypatch):
    """An issue is routinely narrowed in its own thread minutes after it is filed. Triaging from
    the body alone produced a confident brief for the half its owner had just deferred."""
    cfg = RepoConfig("connor/repo", "/tmp")
    monkeypatch.setattr(dispatch, "intake_allowlist", lambda repo, workdir: {"connor"})
    issue = {"number": 493, "title": "expose per-night provenance", "body": "as filed",
             "labels": []}

    prompt = _intake_prompt_for(monkeypatch, cfg, issue, _thread(
        ("connor", "the provenance half is deferred — the live work is the conditioning gate"),
        ("driveby", "+1, and please also rewrite the parser"),
    ))

    assert "the live work is the conditioning gate" in prompt
    assert "the comment wins" in prompt         # stated as authority, not background
    assert "rewrite the parser" not in prompt   # not an allowlisted author


def test_a_re_intake_ignores_comments_already_settled_into_the_brief(monkeypatch):
    """A settled route rewrites the issue body, so replaying the whole thread would let a comment
    already folded into the brief re-open the question it closed."""
    cfg = RepoConfig("connor/repo", "/tmp")
    monkeypatch.setattr(dispatch, "intake_allowlist", lambda repo, workdir: {"connor"})
    issue = {"number": 12, "title": "t", "body": "b", "labels": []}

    prompt = _intake_prompt_for(monkeypatch, cfg, issue, _thread(
        ("connor", "an old aside from before we triaged it"),
        ("connor", f"> *{INTAKE_MARK} disclaimer.* routed ready"),
        ("connor", "but drop the migration for now"),
    ))

    assert "drop the migration" in prompt
    assert "an old aside" not in prompt
    assert "routed ready" not in prompt  # nor our own intake comment itself


def test_an_issue_answering_an_earlier_hold_keeps_one_comment_channel(monkeypatch):
    """The resume path already delivers these comments as the maintainer's answer, under framing
    that says so. A second copy under a second framing would put two contradictory instructions
    for the same words in one prompt — and would pay to read the thread twice."""
    cfg = RepoConfig("connor/repo", "/tmp")
    monkeypatch.setattr(dispatch, "intake_allowlist", lambda repo, workdir: {"connor"})
    monkeypatch.setattr(github, "issue_comment_rows",
                        lambda repo, number: pytest.fail("the resume path already read the thread"))
    issue = {"number": 12, "title": "t", "body": "b", "labels": [],
             "_intake_target": "IC_answer"}

    prompt = _intake_prompt_for(monkeypatch, cfg, issue, None,
                                extra="yes, waive the visual spec")

    assert "THE MAINTAINER HAS REPLIED" in prompt
    assert "yes, waive the visual spec" in prompt
    assert "the comment wins" not in prompt


def test_an_empty_thread_triages_exactly_as_before(monkeypatch):
    cfg = RepoConfig("connor/repo", "/tmp")
    monkeypatch.setattr(dispatch, "intake_allowlist", lambda repo, workdir: {"connor"})

    prompt = _intake_prompt_for(
        monkeypatch, cfg, {"number": 5, "title": "t", "body": "b", "labels": []}, [])

    assert "MAINTAINER COMMENTS" not in prompt


def test_an_unreadable_thread_defers_only_that_issue(monkeypatch):
    """Triaging on a thread we could not read is exactly the failure this closes, so it fails
    closed — but one unreadable issue must not sink the other work in the pass."""
    from agentflow import coordinated_intake

    cfg = RepoConfig("connor/repo", "/tmp")
    monkeypatch.setattr(dispatch, "intake_allowlist", lambda repo, workdir: {"connor"})
    queue = [{"number": 8, "title": "unreadable", "body": "b", "labels": []},
             {"number": 9, "title": "fine", "body": "b", "labels": []}]

    def candidate(cfg, reserved=frozenset()):
        return next(((issue, "") for issue in queue if issue["number"] not in reserved), None)

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(github, "issue_comment_rows",
                        lambda repo, number: None if number == 8 else [])
    monkeypatch.setattr(dispatch.pipeline, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(dispatch, "claim", lambda *a: True)
    submitted = []
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda cfg, issue, extra, comments, tool:
                        submitted.append(issue["number"]) or SimpleNamespace(pool="claude"))
    coord = SimpleNamespace(
        submit_stage=lambda submission: "id",
        stage_record=lambda identity: Record(identity="id", stage="triage", pool="claude",
                                             demand=5, state=WAITING))

    result = dispatch._submit_coordinated_intake(cfg, coord, None)

    assert submitted == [9]
    assert "#9" in result
