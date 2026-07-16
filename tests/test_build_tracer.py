"""Build behind the session coordinator (issue #103), driven through the public
``submit_stage`` / ``cycle`` seam. The Build stage adapter's outcome-first PR verification,
worktree/lineage reuse on continuation, the build-only admission gate, the live-session
projection, the coordinator-owned claim guard, and the ADR 0028 log shapes are all asserted at
the coordinator interface — never by poking private transitions.
"""

from __future__ import annotations

import json
import subprocess

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_build
from agentflow.coordinator import BuildStageAdapter, Submission
from agentflow.coordinator import tracer
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import Record
from agentflow.loop import RepoConfig


def _build(subject="7", *, pool="claude", source="/wt/issue-7", effort="high"):
    return Submission(repo="o/r", subject=subject, stage="build", pool=pool,
                      complexity="deep", effort=effort, source=source)


def _adapter(fake, *, pr, prep, handoff=None):
    """A Build adapter wired to test flags: ``pr``/``prep`` are single-element lists so a test
    flips PR existence and worktree readiness mid-flight; the fake plays observer + launcher."""
    return BuildStageAdapter(pr_exists=lambda r: pr[0],
                             worktree_ready=lambda r: prep[0], observer=fake,
                             handoff=handoff)


def _records(coord):
    return list(coord._store.load().values())


# --- outcome-first PR verification -------------------------------------------------------

def test_build_completes_when_pr_exists_even_after_a_bad_exit(make_coord):
    fake = FakeSession()
    pr, prep = [True], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    assert coord.cycle("claude") == []            # attempt running
    fake.end(ident, cause=ProviderCause.PROCESS)  # provider exited badly (non-zero)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]  # PR present → done


def test_clean_exit_without_the_pr_stays_incomplete(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.NONE)     # clean exit, but no PR
    assert coord.cycle("claude") == []            # not completed — bounded continuation instead
    rec = record_of(coord, ident)
    assert rec.state != "completed" and rec.continuation and rec.attempts == 2


# --- interruption keeps the worktree, lineage, branch, and claim -------------------------

def test_interrupted_build_continues_in_a_fresh_session_keeping_ownership(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)  # interrupted after local changes

    coord.cycle("claude")  # reconciles to waiting, then continues in a fresh session same cycle
    rec = record_of(coord, ident)
    assert rec.attempts == 2                       # a second attempt was consumed
    assert rec.source == "/wt/issue-7"             # same retained worktree
    assert rec.lineage == "claude"                 # pinned tool lineage held
    assert rec.claim is True                        # claim retained across the interruption
    assert rec.state == "running"

    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]


# --- preparation happens before admission ------------------------------------------------

def test_preparation_failure_consumes_no_permit_or_attempt(make_coord):
    fake = FakeSession()
    pr, prep = [False], [False]                    # worktree not ready yet
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build())
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0           # nothing reserved
    assert record_of(coord, ident).attempts == 0   # no attempt consumed
    assert record_of(coord, ident).state == "waiting"

    prep[0] = True                                  # worktree recovered → admits normally
    coord.cycle("claude")
    assert permits(coord, "claude") == 5
    assert record_of(coord, ident).attempts == 1


# --- exhaustion ---------------------------------------------------------------------------

def test_exhaustion_holds_once_with_one_handoff_and_notification(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    handoffs = []
    adapter = _adapter(
        fake, pr=pr, prep=prep,
        handoff=lambda record: handoffs.append(record.identity) or "issue-proof",
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_build())
    outcome = None
    for _ in range(6):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held"
    assert outcome.handoff == "issue:needs-grilling"
    rec = record_of(coord, ident)
    assert rec.attempts == 3                        # initial + two continuations, no more
    assert rec.handoffs == 1 and rec.notifications == 1
    assert rec.source == "/wt/issue-7"              # worktree left untouched for human re-entry
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]                       # restart cannot repeat the external handoff


def test_failed_exhaustion_handoff_retries_on_a_later_cycle(make_coord):
    fake = FakeSession()
    proofs = iter((None, "issue-proof"))
    calls = []

    def handoff(record):
        calls.append(record.identity)
        return next(proofs)

    coord = make_coord(
        fake,
        adapter=_adapter(fake, pr=[False], prep=[True], handoff=handoff),
    )
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    for _ in range(2):
        fake.end(ident, cause=ProviderCause.PROCESS)
        assert coord.cycle("claude") == []           # reconcile, then start continuation
    fake.end(ident, cause=ProviderCause.PROCESS)

    assert coord.cycle("claude") == []               # GitHub handoff failed; keep pending
    assert record_of(coord, ident).hold_pending is True
    settled = coord.cycle("claude")                   # next daemon cycle retries finalization

    assert [outcome.status for outcome in settled] == ["held"]
    assert calls == [ident, ident]


# --- idempotent submission ---------------------------------------------------------------

def test_repeated_submission_and_restart_make_one_record(make_coord):
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter)
    first = coord.submit_stage(_build())
    again = coord.submit_stage(_build())
    restarted = make_coord(fake, adapter=adapter).submit_stage(_build())
    assert first == again == restarted
    assert len(_records(coord)) == 1


# --- build is the only enabled stage -----------------------------------------------------

def test_only_build_admits_other_stages_stay_waiting(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_only_gate)
    build = coord.submit_stage(_build())
    review = coord.submit_stage(Submission(repo="o/r", subject="7", stage="review",
                                           pool="claude"))
    coord.cycle("claude")
    assert record_of(coord, build).state == "running"
    review_rec = record_of(coord, review)
    assert review_rec.state == "waiting"           # visibly queued
    assert review_rec.attempts == 0                # consumed no attempt
    assert permits(coord, "claude") == 5           # only the build's demand is reserved


# --- live projection & claim ownership ---------------------------------------------------

def test_running_build_projects_to_live_board_waiting_does_not(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]),
                       gate=tracer.build_only_gate)
    coord.submit_stage(_build("7"))
    coord.submit_stage(Submission(repo="o/r", subject="8", stage="review", pool="claude"))
    coord.cycle("claude", now=123)
    projection = tracer.live_projection(_records(coord))
    assert [e["number"] for e in projection] == [7]     # only the running build
    assert projection[0]["stage"] == "building" and projection[0]["tool"] == "claude"
    assert set(projection[0]) == {"repo", "number", "title", "stage", "tool", "model",
                                  "branch", "worktree", "pid", "started_at"}
    assert projection[0]["started_at"].startswith("1970-01-01T00:02:03")


def test_owned_issues_and_active_track_coordinator_ownership(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep))
    ident = coord.submit_stage(_build("7"))
    coord.cycle("claude")
    # A running build owns its claim and is in flight.
    assert tracer.owned_issues(_records(coord), "o/r") == {7}
    assert tracer.coordinator_active(_records(coord)) is True
    assert tracer.owned_issues(_records(coord), "other/repo") == set()
    # Complete it: the PR is a durable boundary, so it no longer holds a rollback drain open,
    # but it still owns its claim until a next stage transfers it.
    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert tracer.coordinator_active(_records(coord)) is False
    assert tracer.owned_issues(_records(coord), "o/r") == {7}


# --- ADR 0028 log shapes -----------------------------------------------------------------

def test_attempt_interrupt_continuation_and_completion_log_shapes(make_coord):
    fake = FakeSession()
    pr, prep = [False], [True]
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep), log=lines.append)
    ident = coord.submit_stage(_build())
    coord.cycle("claude")
    assert "o/r: 7: build: attempt 1/3 → claude" in lines
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert ("o/r: 7: build: attempt 1/3 interrupted (process) — continuation 1/2 eligible "
            "next cycle; claim retained") in lines
    assert "o/r: 7: build: continuation 1/2 (attempt 2/3) → claude" in lines
    pr[0] = True
    fake.end(ident, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert "o/r: 7: build: attempt 2/3 completed — pr opened; claim retained" in lines


def test_exhaustion_log_shape(make_coord):
    fake = FakeSession()
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=[False], prep=[True]), log=lines.append)
    ident = coord.submit_stage(_build())
    for _ in range(6):
        if coord.cycle("claude"):
            break
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert ("o/r: 7: build: attempt 3/3 interrupted (process) — continuation budget "
            "exhausted; held for human; claim released") in lines


def test_recovered_running_log_shape_after_restart(make_coord):
    fake = FakeSession()
    adapter = _adapter(fake, pr=[False], prep=[True])
    coord = make_coord(fake, adapter=adapter)
    coord.submit_stage(_build())
    coord.cycle("claude")                          # attempt running, family still alive
    # A fresh coordinator over the same store is the restart; the family is still alive.
    lines: list[str] = []
    restarted = make_coord(fake, adapter=adapter, log=lines.append)
    restarted.cycle("claude")
    assert any(line.startswith("o/r: 7: build: recovered running attempt 1/3 pid ")
               and "observing until" in line and "claim retained" in line for line in lines)


def test_claim_transfer_log_shape(make_coord):
    fake = FakeSession()
    pr, prep = [True], [True]
    lines: list[str] = []
    coord = make_coord(fake, adapter=_adapter(fake, pr=pr, prep=prep), log=lines.append)
    build = coord.submit_stage(_build())
    coord.cycle("claude")
    fake.end(build, cause=ProviderCause.PROCESS)
    coord.cycle("claude")                          # build completes
    # The next stage assumes the claim — the transfer line names the completed stage and target.
    coord.submit_stage(Submission(repo="o/r", subject="7", stage="review", pool="claude",
                                  transfer_from=build))
    assert "o/r: 7: build: attempt 1/3 completed — pr opened; claim transferred to review" in lines


def test_forward_activation_names_live_claim_pid_and_dirty_worktree(tmp_path, monkeypatch):
    from agentflow import loop, runner

    cfg = RepoConfig("o/r", str(tmp_path))
    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / "issue-7-legacy"
    wt.mkdir(parents=True)
    marker = tmp_path / "active-pid"
    marker.write_text("123")
    monkeypatch.setattr(runner, "_registered_worktrees",
                        lambda workdir: [(str(wt), "agentflow/claude/issue-7-legacy")])
    monkeypatch.setattr(runner, "_active_marker", lambda path: marker)

    def fake_run(cmd, cwd=None, timeout=None):
        if cmd[:3] == ["gh", "api", "--paginate"]:
            return subprocess.CompletedProcess(cmd, 0, '[[], [{"number": 7}]]', "")
        if cmd[:3] == ["git", "-C", str(wt)]:
            return subprocess.CompletedProcess(cmd, 0, " M progress.py\n", "")
        raise AssertionError(cmd)

    monkeypatch.setattr(loop, "_run", fake_run)
    evidence = coordinated_build.activation_evidence(
        [cfg], [{"repo": "o/r", "number": 7, "stage": "building",
                 "worktree": str(wt)}], [])

    assert any("legacy session live" in item for item in evidence)
    assert any("legacy building claim" in item for item in evidence)
    assert any("PID marker" in item for item in evidence)
    assert any("is dirty" in item for item in evidence)


def test_forward_activation_excludes_coordinator_owned_claim_and_worktree(tmp_path, monkeypatch):
    from agentflow import loop, runner

    cfg = RepoConfig("o/r", str(tmp_path))
    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / "issue-7-owned"
    wt.mkdir(parents=True)
    record = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                    repo="o/r", subject="7", source=str(wt), claim=True)
    monkeypatch.setattr(runner, "_registered_worktrees",
                        lambda workdir: [(str(wt), "agentflow/claude/issue-7-owned")])
    monkeypatch.setattr(runner, "_active_marker", lambda path: None)

    def fake_run(cmd, cwd=None, timeout=None):
        if cmd[:3] == ["gh", "api", "--paginate"]:
            return subprocess.CompletedProcess(cmd, 0, '[[{"number": 7}]]', "")
        raise AssertionError(cmd)

    monkeypatch.setattr(loop, "_run", fake_run)
    evidence = coordinated_build.activation_evidence(
        [cfg], [{"repo": "o/r", "number": 7, "stage": "building",
                 "worktree": str(wt)}], [record])

    assert evidence == ()


def test_live_build_preparation_verifies_branch_and_provisions_before_admission(
        tmp_path, monkeypatch):
    from agentflow import loop, runner

    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / "issue-7-owned"
    wt.mkdir(parents=True)
    record = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                    repo="o/r", subject="7", source=str(wt), claim=True, lineage="claude")
    monkeypatch.setattr(runner, "_worktree_is_registered", lambda workdir, path: True)
    provisioned = []
    monkeypatch.setattr(runner.ClaudeRunner, "provision",
                        lambda self, path: provisioned.append(path))
    expected = "agentflow/claude/issue-7-owned"
    monkeypatch.setattr(
        loop, "_run",
        lambda cmd, cwd=None, timeout=None: subprocess.CompletedProcess(cmd, 0, expected, ""),
    )

    assert coordinated_build._worktree_ready(record) is True
    assert provisioned == [wt]

    monkeypatch.setattr(
        loop, "_run",
        lambda cmd, cwd=None, timeout=None: subprocess.CompletedProcess(cmd, 0, "wrong", ""),
    )
    assert coordinated_build._worktree_ready(record) is False


def test_live_exhaustion_handoff_is_idempotent_and_releases_the_visible_claim(monkeypatch):
    from agentflow import intake, loop, notify as notify_module

    state = {
        "title": "Build it",
        "url": "https://github.com/o/r/issues/7",
        "labels": [{"name": "ready-for-agent"}, {"name": "agentflow:building"}],
        "comments": [],
    }

    def fake_run(cmd, cwd=None, timeout=None):
        if cmd[:3] == ["gh", "issue", "view"]:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(state), "")
        if cmd[:3] == ["gh", "issue", "edit"] and "--remove-label" in cmd:
            state["labels"] = [label for label in state["labels"]
                               if label["name"] != "agentflow:building"]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise AssertionError(cmd)

    def fake_apply(repo, number, title, labels, result):
        state["labels"] = [{"name": "agentflow:needs-grilling"},
                           {"name": "agentflow:building"}]
        if not any(comment["body"] == result.body for comment in state["comments"]):
            state["comments"].append({"body": result.body})
        return "applied"

    notifications = []
    monkeypatch.setattr(loop, "_run", fake_run)
    monkeypatch.setattr(intake, "apply_intake", fake_apply)
    monkeypatch.setattr(notify_module, "notify", lambda *args: notifications.append(args))
    record = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                    repo="o/r", subject="7", source="/retained/wt", claim=True)

    assert coordinated_build._hold_build(record) == state["url"]
    assert coordinated_build._hold_build(record) == state["url"]
    assert {label["name"] for label in state["labels"]} == {"agentflow:needs-grilling"}
    assert len(state["comments"]) == 1
    assert len(notifications) == 1
