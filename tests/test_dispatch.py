"""Coordinator-only dispatch: submission, pause/drain, and deletion guards (issue #109)."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeSession, record_of

from agentflow import coordinated_build, dispatch, live, loop
from agentflow.coordinator import MockupStageAdapter
from agentflow.coordinator.providers import ProviderCause
from agentflow.loop import RepoConfig


def test_stage_caps_remain_named_inputs_to_the_coordinator_gate():
    assert dispatch.STAGE_CAPS == {"triage": 3, "build": 2, "mockup": 1, "respond": 1,
                                   "research": 1}
    assert dispatch.MACHINE_CEILING > 0


def test_paused_cycle_submits_nothing_but_still_reconciles(monkeypatch):
    monkeypatch.setattr(dispatch, "_submit_repo", lambda *a: pytest.fail(
        "pause may not submit cold work"))
    reconciled = []
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project",
                        lambda coord, _log=None: reconciled.append(coord))
    claims = []
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_orphaned_claims",
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
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_and_project",
                        lambda coord, _log=None: reconciled.append(coord))
    claims = []
    monkeypatch.setattr(dispatch.coordinated_build, "reconcile_orphaned_claims",
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
    running = old.submit_stage(coordinated_build.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 11, "title": "Started", "body": "Draw it"}, "claude"))
    old.cycle("claude")
    fake.end(running, cause=ProviderCause.PROCESS)
    cold = old.submit_stage(coordinated_build.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 12, "title": "Still held", "body": "Draw it"}, "claude"))
    coord = make_coord(fake, adapter=adapter,
                       disabled_cold_stages=frozenset({"mockup"}))
    monkeypatch.setattr(live, "replace_projection", lambda records: None)
    monkeypatch.setattr(coordinated_build, "reconcile_orphaned_claims", lambda *a, **k: 0)

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
    identity = old.submit_stage(coordinated_build.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 13, "title": "Reserved", "body": "Draw it"}, "claude"))
    fake.crash_start = True
    with pytest.raises(RuntimeError):
        old.cycle("claude")
    fake.crash_start = False
    coord = make_coord(fake, adapter=adapter,
                       disabled_cold_stages=frozenset({"mockup"}))
    monkeypatch.setattr(live, "replace_projection", lambda records: None)
    monkeypatch.setattr(coordinated_build, "reconcile_orphaned_claims", lambda *a, **k: 0)

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
    identity = old.submit_stage(coordinated_build.mockup_submission(
        SimpleNamespace(repo="o/r", workdir="/w"),
        {"number": 14, "title": "Restart", "body": "Draw it"}, "claude"))
    old.cycle("claude")
    fake.kill(identity)
    fake.gate_open = False
    restarted = make_coord(fake, adapter=adapter, daemon_generation="new",
                           disabled_cold_stages=frozenset({"mockup"}))
    monkeypatch.setattr(live, "replace_projection", lambda records: None)
    monkeypatch.setattr(coordinated_build, "reconcile_orphaned_claims", lambda *a, **k: 0)

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

    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    # The four claim lanes are listed in order (building, triaging, drawing, resolving); only the
    # building lane holds a stale-claimed issue. The proof read back shows the label gone.
    listings = iter([[{"number": 7, "updated_at": "2020-01-01T00:00:00Z"}], [], [], []])
    monkeypatch.setattr(github, "api", lambda args, *, parse_json=False: next(listings))
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append((issue, label)) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert removed == [(7, "agentflow:building")]


def test_claim_reconciliation_reads_labels_off_the_hourly_budget_not_search(monkeypatch):
    """Reconciliation runs four lanes per repo every cycle. Asking GitHub's search for each one
    exceeds its ~30/minute ceiling across a fleet and starves the lane permanently, so the listing
    must be an ordinary REST read. That endpoint also returns pull requests, which share the issue
    number sequence — one must never be mistaken for a claimed issue."""
    from agentflow import coordinated_build, github

    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [])
    asked = []

    def listing(args, *, parse_json=False):
        asked.append(args)
        if "building" not in args[-1]:
            return []
        return [
            {"number": 7, "updated_at": "2020-01-01T00:00:00Z"},
            {"number": 9, "updated_at": "2020-01-01T00:00:00Z",
             "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/9"}},
        ]

    monkeypatch.setattr(github, "api", listing)
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append(issue) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
    assert removed == [7], "the pull request must not be read as a claimed issue"
    assert all(call[0] == "api" and call[1].startswith("repos/o/r/issues?") for call in asked)
    assert not any("issue" == call[0] and "list" == call[1] for call in asked)


def test_unreadable_coordinator_state_clears_no_claim(monkeypatch):
    from agentflow import coordinated_build, github
    from agentflow.coordinator.store import StoreUnavailable

    monkeypatch.setattr(coordinated_build.tracer, "load_records",
                        lambda: (_ for _ in ()).throw(StoreUnavailable("locked")))
    monkeypatch.setattr(github, "api",
                        lambda *a, **k: pytest.fail("must not inspect or clear claims"))
    monkeypatch.setattr(github, "remove_label",
                        lambda *a, **k: pytest.fail("must not clear claims"))

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 0


def test_waiting_owner_retains_claim_but_settled_hold_does_not(monkeypatch):
    from agentflow import coordinated_build, github
    from agentflow.coordinator.record import HELD, WAITING, Record

    waiting = Record(identity="wait", stage="build", pool="claude", demand=5,
                     repo="o/r", subject="7", state=WAITING, claim=True)
    held = Record(identity="held", stage="review", pool="codex", demand=2,
                  repo="o/r", subject="8", state=HELD, claim=False)
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [waiting, held])
    # The building lane lists both issues; #7 is shielded by the live waiting build, #8 is not.
    listings = iter([[{"number": 7, "updated_at": "2020-01-01T00:00:00Z"},
                      {"number": 8, "updated_at": "2020-01-01T00:00:00Z"}], [], [], []])
    monkeypatch.setattr(github, "api", lambda args, *, parse_json=False: next(listings))
    removed = []
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: removed.append(issue) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp")) == 1
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
    monkeypatch.setattr(loop, "_claim", lambda repo, number: events.append("claim") or True)
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
    monkeypatch.setattr(loop, "_claim", lambda *a: pytest.fail("must not claim a held no-op"))
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
    # queued behind it — the same pass reports it and submits the next candidate (#327).
    from agentflow.coordinator.record import Record, WAITING

    _ready_queue(monkeypatch, [(462, ["ready-for-agent"]), (468, _DIALS)])
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    claimed = []
    monkeypatch.setattr(loop, "_claim", lambda repo, number: claimed.append(number) or True)
    submitted = []
    coord = SimpleNamespace(
        submit_stage=lambda s: submitted.append(int(s.subject)) or f"o/r|{s.subject}|build|-",
        stage_record=lambda identity: Record(identity=identity, stage="build", pool="claude",
                                             demand=5, state=WAITING))

    result = dispatch._submit_coordinated_build(RepoConfig("o/r", str(tmp_path)), coord, None)

    assert claimed == [468] and submitted == [468]
    assert "#462" in result and "complexity" in result
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
    monkeypatch.setattr(loop, "_claim", lambda repo, number: claimed.append(number) or True)
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
    monkeypatch.setattr(loop, "_claim", lambda *a: pytest.fail("must not claim while blind"))
    coord = SimpleNamespace(
        submit_stage=lambda s: pytest.fail("must not submit while blind"),
        stage_record=lambda identity: None)

    assert dispatch._submit_coordinated_build(
        RepoConfig("o/r", str(tmp_path)), coord, None) == "no ready-for-agent issues"


def test_build_pass_reports_when_every_ready_candidate_is_undispatchable(monkeypatch, tmp_path):
    _ready_queue(monkeypatch, [(1, ["ready-for-agent"]), (2, ["ready-for-agent"])])
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(loop, "_claim", lambda *a: pytest.fail("nothing runnable to claim"))
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
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: records)
    monkeypatch.setattr(loop, "_pr_comments", lambda repo, pr: _answered_park_thread())
    monkeypatch.setattr(loop, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(coordinated_build, "respond_submission",
                        lambda *a, **k: pytest.fail("a decision answer is never a generic Respond"))
    posted = []
    monkeypatch.setattr(coordinated_build.github, "pr_comment",
                        lambda repo, pr, body: posted.append(body) or True)
    claimed = []
    monkeypatch.setattr(loop, "_claim", lambda repo, number: claimed.append(number) or True)
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
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [parked, resumed])
    monkeypatch.setattr(loop, "_pr_comments", lambda repo, pr: [
        *_answered_park_thread(),
        {"id": "IC_x", "body": "> *agentflow: your decision resumed the parked review.*\n"
                               "<!-- agentflow-respond-target:IC_1 -->"},
        {"id": "IC_2", "body": "unrelated question"}])
    monkeypatch.setattr(coordinated_build.github, "pr_comment",
                        lambda *a, **k: pytest.fail("discussion never resumes a parked review"))
    monkeypatch.setattr(coordinated_build, "respond_submission",
                        lambda *a, **k: SimpleNamespace(subject="7", pool="claude"))
    monkeypatch.setattr(coordinated_build, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(loop, "_claim", lambda repo, number: True)
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
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [first, round_two])
    monkeypatch.setattr(loop, "_pr_comments", lambda repo, pr: [
        {"id": "IC_0", "body": "> *agentflow: parked for human review.*\n\nDecide again, please."},
        {"id": "IC_1", "body": "keep the conservative behavior"},
        {"id": "IC_x", "body": "> *agentflow: your decision resumed the parked review.*\n"
                               "<!-- agentflow-respond-target:IC_1 -->"},
        {"id": "IC_2", "body": "prompt every user"}])
    monkeypatch.setattr(loop, "repo_profile", lambda workdir: "autonomous")
    monkeypatch.setattr(coordinated_build, "respond_submission",
                        lambda *a, **k: pytest.fail("a decision answer is never a generic Respond"))
    posted = []
    monkeypatch.setattr(coordinated_build.github, "pr_comment",
                        lambda repo, pr, body: posted.append(body) or True)
    claimed = []
    monkeypatch.setattr(loop, "_claim", lambda repo, number: claimed.append(number) or True)
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
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [parked])
    monkeypatch.setattr(loop, "_pr_comments", lambda repo, pr: [
        {"id": "IC_old", "body": "earlier discussion"},
        {"id": "IC_park", "body": "> *agentflow: parked for human review.*\n\nDecide."},
        {"id": "IC_answer", "body": "keep the conservative behavior"},
    ])
    monkeypatch.setattr(coordinated_build.github, "pr_comment",
                        lambda *a, **k: pytest.fail("older discussion never resumes review"))
    monkeypatch.setattr(coordinated_build, "respond_submission",
                        lambda *a, **k: SimpleNamespace(subject="7", pool="claude"))
    monkeypatch.setattr(coordinated_build, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(loop, "_claim", lambda repo, number: True)
    submitted = []

    result = dispatch._submit_coordinated_respond(
        RepoConfig("o/r", "/work"), SimpleNamespace(submit_stage=submitted.append), None)

    assert "(respond)" in result and len(submitted) == 1


def test_respond_waits_while_a_prior_change_record_owns_the_claim(monkeypatch):
    monkeypatch.setattr(loop, "_next_pr_awaiting_reply", lambda cfg: (
        42, "agentflow/claude/issue-7-fix", "please adjust", "cid-1", "base"))
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues",
                        lambda cfg, lane=None: {7})
    monkeypatch.setattr(loop, "_claim", lambda *a: pytest.fail("must not double-claim"))

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
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues",
                        lambda cfg, lane=None: {42})
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda: pytest.fail("must not pick a pool for an owned issue"))
    monkeypatch.setattr(loop, "_claim_triage", lambda *a: pytest.fail("must not re-claim"))
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
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda *a, **k: SimpleNamespace(pool="claude"))
    claimed = []
    monkeypatch.setattr(loop, "_claim_triage", lambda repo, n: claimed.append(n) or True)
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
        monkeypatch.setattr(dispatch.coordinated_build, "owned_issues",
                            lambda cfg, lane=None: set())
        monkeypatch.setattr(dispatch, "pick_pair",
                            lambda: (SimpleNamespace(tool="claude"), None, ""))
        monkeypatch.setattr(coordinated_intake, "intake_submission",
                            lambda *a, **k: SimpleNamespace(pool="claude"))
        monkeypatch.setattr(loop, "_claim_triage",
                            lambda *a: pytest.fail("must not claim a terminal dedup no-op"))
        completed = Record(identity=f"o/r|393|intake|{target}", stage="triage", pool="claude",
                           demand=5, state=COMPLETED)
        coord = SimpleNamespace(
            submit_stage=lambda submission, target=target: f"o/r|393|intake|{target}",
            stage_record=lambda identity: completed)

        result = dispatch._submit_coordinated_intake(RepoConfig("o/r", "/tmp"), coord, None)
        assert result == "no un-triaged issues"


def test_intake_withdraws_the_submission_when_the_claim_fails(monkeypatch):
    # A runnable submission whose GitHub claim mutation fails must withdraw the never-started
    # WAITING record, so no unowned Intake work survives — mirrors build_issue's fail-closed
    # rollback (#308).
    from agentflow import coordinated_intake
    from agentflow.coordinator.record import Record, WAITING

    def candidate(cfg, reserved=frozenset()):
        return None if 42 in reserved else ({"number": 42, "labels": []}, "")

    monkeypatch.setattr(loop, "_next_intake_candidate", candidate)
    monkeypatch.setattr(dispatch.coordinated_build, "owned_issues", lambda cfg, lane=None: set())
    monkeypatch.setattr(dispatch, "pick_pair", lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr(coordinated_intake, "intake_submission",
                        lambda *a, **k: SimpleNamespace(pool="claude"))
    monkeypatch.setattr(loop, "_claim_triage", lambda *a: False)
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
                    assert path in {root / "coordinated_build.py", root / "dashboard_data.py"}, (
                        f"counter outside pacing/projection owners: {path}:{node.lineno}")
            if "coordinator" not in path.parts and isinstance(
                    node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = [item.id for target in targets for item in ast.walk(target)
                         if isinstance(item, ast.Name)]
                assert not any("permit" in name.lower() for name in names), (
                    f"second permit ledger outside coordinator: {path}:{node.lineno}")


def test_no_module_outside_the_github_module_shells_out_to_gh():
    """ADR 0040: all GitHub access flows through one typed, fail-closed module. Any `gh` argument
    vector built anywhere else is a bypass of the seam — however it is later run."""
    root = Path(__file__).parents[1] / "agentflow"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text())
        # An argv that is only *described* in a raised error is never executed, so it isn't
        # GitHub access. Nothing else is exempt.
        described = {id(item) for node in ast.walk(tree) if isinstance(node, ast.Raise)
                     for item in ast.walk(node) if isinstance(item, ast.List)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.List) or id(node) in described:
                continue
            first = node.elts[0] if node.elts else None
            if isinstance(first, ast.Constant) and first.value == "gh":
                assert path == root / "github.py", (
                    f"GitHub access outside the github module: {path}:{node.lineno}")
