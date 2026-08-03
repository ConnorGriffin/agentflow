"""A stage nobody can un-stick eventually calls a human (#406).

A stage refused *before* its session starts reserves no permit and spends no attempt, so the
exhaustion machinery that parks everything else never fires: the record sits at 0 of 3 attempts
forever, and the only trace is a log line. That is how one checkout stalled a review for half an
hour with the operator none the wiser (#399), and #405 — which named what refused — made it
legible without making it *end*.

So refusals are now clocked, but only the ones that have proved a human must act. The proof is
deliberately narrow: a registered checkout a human pinned with ``git worktree lock``, holding
work that therefore cannot be archived out of the way. Everything else a stage can trip over —
an unreachable remote, a dependency sync that fails, a sibling session still holding the
checkout, a payload nobody can parse — keeps its breadcrumbs and escalates to nobody, however
long it lasts, because retrying really is the right answer for all of them.

These tests drive the real checkout predicates and the public ``submit_stage`` / ``cycle`` seam
against real git repositories, with time supplied by the caller so an hour costs no wall clock.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import NeverStartsLauncher, record_of

from agentflow import (coordinated_attack, coordinated_intake, coordinated_review, github, live,
                       pr_park)
from agentflow.coordinator import AttackStageAdapter, IntakeStageAdapter, StageRouter, Submission
from agentflow.coordinator import tracer
from agentflow.coordinator.record import (STALL_OBSERVATION_MAX_GAP, STALL_PARK_AFTER,
                                          STALL_STALLED_AFTER, Record)
from agentflow.coordinator.verification import unprepared

MINUTE = 60


# --- repositories and checkouts, in the states that do and do not prove a stall ------------


def _git(cwd, *args) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                         text=True)
    return out.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "agentflow@example.com")
    _git(repo, "config", "user.name", "agentflow test")
    (repo / "README.md").write_text("start\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "start")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    return repo


def _pinned_checkout(repo: Path, wt: Path) -> None:
    """The one state that proves a human has to act: a registered checkout holding work of its
    own, pinned open so that work cannot be archived out of the way."""
    _git(repo, "worktree", "add", "--detach", str(wt), "origin/main")
    (wt / "half-finished.txt").write_text("what the operator pinned this to keep")
    _git(repo, "worktree", "lock", str(wt))


def _unpin(repo: Path, wt: Path) -> None:
    _git(repo, "worktree", "unlock", str(wt))


# --- the engine seam: a cold intake driven over a real checkout ----------------------------


def _intake_coord(make_coord, repo: Path, wt: Path, *, log=None, handoff=None):
    adapter = IntakeStageAdapter(worktree_reset=coordinated_intake.reset_worktree,
                                 apply_route=lambda record, result: None,
                                 claim_ready=lambda record: True,
                                 handoff=handoff)
    return make_coord(adapter=StageRouter({"intake": adapter}), gate=lambda record: True,
                      launcher=NeverStartsLauncher(), log=log or (lambda line: None))


def _intake_submission(repo: Path, wt: Path) -> Submission:
    head = _git(repo, "rev-parse", "HEAD")
    return Submission(
        repo="o/r", subject="7", stage="intake", pool="claude", complexity="deep",
        source=str(wt),
        input_ptr=json.dumps({"snapshot": {"body": ""}, "source_ref": head}))


def _stalled_rows(coord, now: int):
    return tracer.stalled_projection(coord._store.load().values(), now=now)


# --- what the clock does over time --------------------------------------------------------


def test_a_pinned_checkout_is_called_stalled_at_ten_minutes_and_parked_at_an_hour(
        tmp_path, make_coord, monkeypatch):
    """The regression, end to end. On today's code this refusal repeats forever at 0 of 3
    attempts with nothing published and nobody told; here it becomes visible at ten minutes and
    somebody's problem at an hour — while the pinned checkout is never touched."""
    repo = _repo(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    _pinned_checkout(repo, wt)
    comments: list[str] = []
    _hold_seams(monkeypatch, comments)
    lines: list[str] = []
    coord = _intake_coord(make_coord, repo, wt, log=lines.append,
                          handoff=coordinated_intake.hold_intake)
    ident = coord.submit_stage(_intake_submission(repo, wt))

    # Minute 0 through 30: refused every cycle, named, and — from minute 10 — called stalled.
    for minute in range(0, 31, 5):
        coord.cycle("claude", now=minute * MINUTE)
    stalled = record_of(coord, ident)
    assert stalled.refusals == 7
    assert stalled.state == "waiting" and stalled.attempts == 0
    assert stalled.refusal.startswith("checkout-locked: ")
    assert stalled.stall_refusal_id == "checkout-locked"
    assert [row["subject"] for row in _stalled_rows(coord, 30 * MINUTE)] == ["7"]
    assert any("stalled for" in line and "checkout-locked" in line for line in lines)
    assert comments == []                       # visible, but nobody has been called yet

    # Minute 60 crosses the park bound; the next cycle proves the durable handoff.
    for minute in range(35, 66, 5):
        coord.cycle("claude", now=minute * MINUTE)
    parked = record_of(coord, ident)
    assert parked.state == "held" and parked.attempts == 0
    assert len(comments) == 1

    # And the thing everyone was waiting on is exactly as the operator left it.
    assert (wt / "half-finished.txt").read_text() == "what the operator pinned this to keep"
    assert _git(repo, "worktree", "list", "--porcelain").count("locked") == 1


def test_clearing_the_pin_before_the_hour_leaves_no_park_and_no_ping(
        tmp_path, make_coord, monkeypatch):
    """The whole point of a clock is that stopping it is free. A checkout released at minute 30
    resets the record completely — nothing published, nothing posted, nobody woken."""
    repo = _repo(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    _pinned_checkout(repo, wt)
    comments: list[str] = []
    _hold_seams(monkeypatch, comments)
    coord = _intake_coord(make_coord, repo, wt, handoff=coordinated_intake.hold_intake)
    ident = coord.submit_stage(_intake_submission(repo, wt))

    for minute in (0, 5, 10, 20, 30):
        coord.cycle("claude", now=minute * MINUTE)
    assert record_of(coord, ident).stall_refusal_id == "checkout-locked"
    assert _stalled_rows(coord, 30 * MINUTE)

    _unpin(repo, wt)
    coord.cycle("claude", now=35 * MINUTE)

    cleared = record_of(coord, ident)
    assert cleared.stall_refusal_id == "" and cleared.stall_started_at == 0
    assert cleared.stall_last_observed_at == 0 and cleared.refusals == 0
    assert cleared.refusal == "" and cleared.state == "waiting"
    assert _stalled_rows(coord, 35 * MINUTE) == []

    for minute in (40, 60, 70, 90, 120):        # and an hour of the clear world parks nothing
        coord.cycle("claude", now=minute * MINUTE)
    assert comments == []
    assert record_of(coord, ident).state != "held"


def test_a_daemon_restart_mid_clock_keeps_the_elapsed_time_and_parks_exactly_once(
        tmp_path, make_coord, monkeypatch):
    """Fault injection. The clock is durable, so a daemon that dies at minute 40 comes back
    knowing this refusal is already 40 minutes old — it neither restarts the hour nor, once it
    parks, posts a second comment when it is replayed again."""
    repo = _repo(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    _pinned_checkout(repo, wt)
    comments: list[str] = []
    notified = _hold_seams(monkeypatch, comments)
    coord = _intake_coord(make_coord, repo, wt, handoff=coordinated_intake.hold_intake)
    ident = coord.submit_stage(_intake_submission(repo, wt))

    for minute in range(0, 41, 5):
        coord.cycle("claude", now=minute * MINUTE)
    before = record_of(coord, ident)
    assert before.stall_started_at == 0 and before.stall_last_observed_at == 40 * MINUTE

    restarted = _intake_coord(make_coord, repo, wt, handoff=coordinated_intake.hold_intake)
    restarted.cycle("claude", now=45 * MINUTE)
    resumed = record_of(restarted, ident)
    assert resumed.stall_refusal_id == "checkout-locked"
    assert resumed.stall_started_at == before.stall_started_at   # the hour did not restart
    assert resumed.refusals == before.refusals + 1               # nor did the count

    for minute in range(50, 66, 5):
        restarted.cycle("claude", now=minute * MINUTE)
    assert record_of(restarted, ident).state == "held"
    assert len(comments) == 1

    # Replayed once more over the durable comment: it proves the same handoff, never a second.
    _intake_coord(make_coord, repo, wt,
                  handoff=coordinated_intake.hold_intake).cycle("claude", now=70 * MINUTE)
    assert len(comments) == 1
    assert len(notified) >= 1                   # pinging again is the deliberate ADR 0042 trade


def test_a_replacement_refusal_gets_a_fresh_clock_even_across_a_restart(make_coord):
    """One refusal's age must never escalate a different one. The clock is keyed on the typed
    check, so swapping the cause resets it — including when the swap straddles a restart, which
    is exactly the case a pair of bare timestamps would get wrong."""
    answer = [unprepared("checkout-locked", "pinned open", stall=True)]
    coord = _refusing_coord(make_coord, answer)
    ident = coord.submit_stage(_cold_build())

    for minute in (0, 5, 10, 20, 30, 40, 50):
        coord.cycle("claude", now=minute * MINUTE)
    assert record_of(coord, ident).stall_started_at == 0

    answer[0] = unprepared("dependencies-pinned", "a different pin entirely", stall=True)
    restarted = _refusing_coord(make_coord, answer)
    restarted.cycle("claude", now=55 * MINUTE)

    swapped = record_of(restarted, ident)
    assert swapped.stall_refusal_id == "dependencies-pinned"
    assert swapped.stall_started_at == 55 * MINUTE           # refusal A's 50 minutes are gone

    restarted.cycle("claude", now=60 * MINUTE)               # A's clock would have parked here
    assert record_of(restarted, ident).state == "waiting"


def test_an_observation_gap_restarts_the_clock_before_anything_is_evaluated(make_coord):
    """Head-of-line ordering can leave a record unoffered for an hour and a half. That is time
    nobody was watching, not time something was stuck, so the next observation starts over: it
    does not report stalled and it certainly does not park."""
    answer = [unprepared("checkout-locked", "pinned open", stall=True)]
    coord = _refusing_coord(make_coord, answer)
    ident = coord.submit_stage(_cold_build())

    coord.cycle("claude", now=0)
    assert record_of(coord, ident).stall_started_at == 0

    coord.cycle("claude", now=90 * MINUTE)                   # nothing observed for 90 minutes
    resumed = record_of(coord, ident)
    assert resumed.stall_started_at == 90 * MINUTE and resumed.state == "waiting"
    assert _stalled_rows(coord, 90 * MINUTE) == []

    coord.cycle("claude", now=99 * MINUTE)                   # nine minutes in: still not stalled
    assert _stalled_rows(coord, 99 * MINUTE) == []
    coord.cycle("claude", now=101 * MINUTE)                  # eleven: now it is
    assert len(_stalled_rows(coord, 101 * MINUTE)) == 1
    assert record_of(coord, ident).state == "waiting"

    for minute in range(106, 152, 5):                        # an hour from the *restart*, not
        coord.cycle("claude", now=minute * MINUTE)           # from the observation 90 before it
    assert record_of(coord, ident).hold_pending


@pytest.mark.parametrize("gap, restarts", [(STALL_OBSERVATION_MAX_GAP, False),
                                           (STALL_OBSERVATION_MAX_GAP + 1, True)])
def test_the_observation_gap_bound_is_the_documented_one(make_coord, gap, restarts):
    """Exactly at the bound is still one continuous refusal; a second past it is not."""
    answer = [unprepared("checkout-locked", "pinned open", stall=True)]
    coord = _refusing_coord(make_coord, answer)
    ident = coord.submit_stage(_cold_build())

    coord.cycle("claude", now=1000)
    coord.cycle("claude", now=1000 + gap)
    assert (record_of(coord, ident).stall_started_at == 1000 + gap) is restarts


# --- what must never start a clock --------------------------------------------------------


def test_a_sibling_session_holding_a_review_checkout_never_escalates(tmp_path, make_coord):
    """Contention is the fleet working as intended: a superseded session finishing while its
    successor waits its turn. Two hours of it must read exactly as it does today — published so
    the fleet stays legible, counted toward nothing, and escalated to no one."""
    from agentflow.coordinator import ReviewStageAdapter
    from agentflow.runner import worktree_session

    repo = _repo(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-9-x"
    _git(repo, "worktree", "add", "--detach", str(wt), "origin/main")
    head = _git(repo, "rev-parse", "HEAD")
    lines: list[str] = []
    adapter = ReviewStageAdapter(verdict_ready=lambda record, obs: False,
                                 worktree_reset=coordinated_review._review_worktree_reset)
    coord = make_coord(adapter=StageRouter({"review": adapter}), gate=lambda record: True,
                       launcher=NeverStartsLauncher(), log=lines.append)
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="9", stage="review", pool="claude", complexity="deep",
        source=str(wt), target=head))

    with worktree_session(wt):
        for minute in range(0, 121, 10):
            coord.cycle("claude", now=minute * MINUTE)

    busy = record_of(coord, ident)
    assert busy.refusal.startswith("sibling-active: ") and busy.refusal_expected
    assert busy.stall_refusal_id == "" and busy.refusals == 0
    assert busy.state == "waiting" and busy.claim and busy.attempts == 0
    assert _stalled_rows(coord, 120 * MINUTE) == []
    assert [line for line in lines if "stalled for" in line] == []


def test_an_unreachable_remote_keeps_its_breadcrumbs_for_two_hours_and_escalates_to_nobody(
        tmp_path, make_coord, monkeypatch):
    """A fetch that keeps failing is the machine's problem to keep retrying, not a human's to
    clear. It counts, it prints periodically, and it never becomes stalled or parked."""
    repo = _repo(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    submission = _intake_submission(repo, wt)
    subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", str(tmp_path / "gone")],
                   check=True, capture_output=True)
    lines: list[str] = []
    coord = _intake_coord(make_coord, repo, wt, log=lines.append)
    ident = coord.submit_stage(submission)

    for minute in range(0, 121, 10):
        coord.cycle("claude", now=minute * MINUTE)

    offline = record_of(coord, ident)
    assert offline.refusal.startswith("checkout-failed: ")
    assert offline.refusals == 13 and offline.stall_refusal_id == ""
    assert offline.state == "waiting" and offline.attempts == 0
    assert _stalled_rows(coord, 120 * MINUTE) == []
    assert [line for line in lines if "stalled for" in line] == []
    assert [line for line in lines if "unprepared for" in line]


def test_an_undisposed_refusal_never_escalates_however_old(make_coord):
    """A check that stated no disposition made no claim about who can clear it. Silence is not
    permission to page somebody — it keeps the breadcrumbs and nothing else."""
    coord = _refusing_coord(make_coord, [unprepared("mystery", "something said no")])
    ident = coord.submit_stage(_cold_build())

    for minute in range(0, 241, 10):
        coord.cycle("claude", now=minute * MINUTE)

    aged = record_of(coord, ident)
    assert aged.refusals == 25 and aged.stall_refusal_id == ""
    assert aged.state == "waiting" and aged.attempts == 0


def test_an_admission_gate_wait_starts_no_clock_and_parks_nothing(make_coord):
    """Weekly pacing, five-hour headroom, and permits are waits with a reset time, not refusals
    a human can clear. Two hours of one leaves the record exactly where it started."""
    class _PacedOut:
        def __call__(self, record) -> bool:
            return False

        def deferral_reason(self, record):
            return "codex weekly allowance spent (resets Monday)"

    coord = _refusing_coord(make_coord, [True], gate=_PacedOut())
    ident = coord.submit_stage(_cold_build())

    for minute in range(0, 121, 10):
        coord.cycle("claude", now=minute * MINUTE)

    paced = record_of(coord, ident)
    assert paced.refusal == "codex weekly allowance spent (resets Monday)"
    assert paced.stall_refusal_id == "" and paced.refusals == 0
    assert paced.state == "waiting" and not paced.hold_pending
    assert _stalled_rows(coord, 120 * MINUTE) == []


def test_a_refusal_whose_subject_cannot_be_resolved_waits_forever_rather_than_parking(
        make_coord):
    """The trap: a park with no issue or PR to post on proves nothing, so the record would sit
    at ``hold_pending`` with no comment and no ping — invisible in a worse way than before. Such
    a record is never clocked at all; it keeps its claim and keeps retrying."""
    answer = [unprepared("checkout-locked", "pinned open", stall=True)]
    coord = _refusing_coord(make_coord, answer)
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="not-a-number", stage="build", pool="claude", complexity="deep",
        source="/work/.agentflow/worktrees/claude/issue-x"))

    for minute in range(0, 121, 10):
        coord.cycle("claude", now=minute * MINUTE)

    unresolvable = record_of(coord, ident)
    assert unresolvable.stall_refusal_id == "" and unresolvable.stall_started_at == 0
    assert unresolvable.state == "waiting" and unresolvable.claim
    assert not unresolvable.hold_pending and unresolvable.attempts == 0
    assert unresolvable.refusals == 13          # still counted, still breadcrumbed


def test_a_pool_move_probe_neither_counts_nor_clocks(make_coord):
    """A never-started Build may be speculatively tried against the other pool when its own
    cannot launch it. That trial is not the record's turn at anything, so its refusal is nobody's
    evidence that the record is stuck."""
    class _ClaudeIsFull:
        def __call__(self, record) -> bool:
            return record.pool == "codex"

        def deferral_reason(self, record):
            return "five-hour utilization at 99%" if record.pool == "claude" else None

    class _OnlyTheDestinationIsPinned:
        def prepare(self, record):
            if record.pool == "codex":
                return unprepared("checkout-locked", "the codex checkout is pinned", stall=True)
            return True

        def verify(self, record, obs) -> bool:
            return False

    coord = make_coord(adapter=StageRouter({"build": _OnlyTheDestinationIsPinned()}),
                       gate=_ClaudeIsFull(), launcher=NeverStartsLauncher())
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="build", pool="claude", complexity="deep",
        source="/work/.agentflow/worktrees/claude/issue-7-x"))

    for minute in range(0, 121, 10):
        coord.cycle("codex", now=minute * MINUTE)           # each cycle probes the move

    probed = record_of(coord, ident)
    assert probed.pool == "claude"                          # the move never took
    assert probed.stall_refusal_id == "" and probed.refusals == 0
    assert not probed.hold_pending


@pytest.mark.parametrize("state", ["dirty", "unregistered", "moved-off-the-remote"])
def test_the_checkouts_preparation_already_fixes_itself_never_start_a_clock(
        tmp_path, make_coord, state):
    """The three states preparation recovers on its own. None of them is anybody's problem, so
    none may start a clock — and the untracked scratch file in the first one is the very thing
    that stalled a review for half an hour before any of this existed (#399)."""
    repo = _repo(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    if state == "unregistered":
        wt.mkdir(parents=True)
        (wt / "orphan.txt").write_text("git has forgotten this directory")
    else:
        _git(repo, "worktree", "add", "--detach", str(wt), "origin/main")
        (wt / "scratch.txt").write_text("leftover")
        if state == "moved-off-the-remote":
            _git(wt, "add", "scratch.txt")
            _git(wt, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-m", "off-remote")

    coord = _intake_coord(make_coord, repo, wt)
    ident = coord.submit_stage(_intake_submission(repo, wt))
    coord.cycle("claude", now=MINUTE)

    recovered = record_of(coord, ident)
    assert recovered.refusal == "", f"{state} refused: {recovered.refusal}"
    assert recovered.stall_refusal_id == "" and recovered.refusals == 0


# --- the durable count behind the breadcrumb ----------------------------------------------


def test_the_consecutive_count_survives_a_restart_and_resets_on_success(make_coord):
    """Review's process-local counter is gone: the count is the record's, so a daemon restart
    picks the cadence up where it left off instead of re-announcing a refusal that is hours old
    as if it were the second one."""
    answer = [unprepared("checkout-failed", "git worktree add exited 128")]
    lines: list[str] = []
    coord = _refusing_coord(make_coord, answer, log=lines.append)
    ident = coord.submit_stage(_cold_build())

    for cycle in range(6):
        coord.cycle("claude", now=cycle * MINUTE)
    assert record_of(coord, ident).refusals == 6
    assert len([line for line in lines if "unprepared for" in line]) == 1

    restarted = _refusing_coord(make_coord, answer, log=lines.append)
    for cycle in range(6, 12):
        restarted.cycle("claude", now=cycle * MINUTE)
    assert record_of(restarted, ident).refusals == 12
    # Twelfth consecutive refusal, so exactly one more line — a re-armed counter would have
    # printed its own "second" one instead.
    assert len([line for line in lines if "unprepared for" in line]) == 2
    assert "unprepared for 12 consecutive cycles" in lines[-1]

    answer[0] = True
    restarted.cycle("claude", now=12 * MINUTE)
    assert record_of(restarted, ident).refusals == 0


def test_an_offline_review_that_declares_no_disposition_still_breadcrumbs(make_coord):
    """The count applies to every preparation refusal, not only the ones worth escalating."""
    lines: list[str] = []
    coord = _refusing_coord(make_coord, [unprepared("checkout-failed", "origin unreachable")],
                            log=lines.append)
    ident = coord.submit_stage(_cold_build())

    for cycle in range(12):
        coord.cycle("claude", now=cycle * MINUTE)

    assert record_of(coord, ident).stall_refusal_id == ""
    assert len([line for line in lines if "unprepared for" in line]) == 2


# --- the published surface ----------------------------------------------------------------


def test_a_stalled_record_publishes_in_its_own_key_and_never_as_a_running_session(
        tmp_path, make_coord, monkeypatch):
    """A stalled record has started nothing and reserves nothing. It rides in its own key,
    survives the publish/read round trip the console reads through, and touches neither the
    running rows nor the pool counts derived from them."""
    from agentflow import dashboard_data

    monkeypatch.setattr(dashboard_data, "pools",
                        lambda: [{"tool": "claude", "clear": True, "spent_pct": 4.0}])
    live.replace_projection([{"repo": "o/r", "number": 9, "tool": "claude",
                              "stage": "building"}])
    before = dashboard_data.snapshot([], dispatch_enabled=True)
    assert before["stalled"] == []

    repo = _repo(tmp_path)
    wt = repo / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"
    _pinned_checkout(repo, wt)
    coord = _intake_coord(make_coord, repo, wt)
    coord.submit_stage(_intake_submission(repo, wt))
    for minute in (0, 5, 10, 15):
        coord.cycle("claude", now=minute * MINUTE)

    rows = _stalled_rows(coord, 15 * MINUTE)
    live.replace_stalled(rows)
    after = dashboard_data.snapshot([], dispatch_enabled=True)

    assert after["running"] == before["running"]
    assert after["pools"] == before["pools"]
    assert len(after["stalled"]) == 1
    published = after["stalled"][0]
    assert published["repo"] == "o/r" and published["subject"] == "7"
    assert published["stage"] == "intake" and published["pool"] == "claude"
    assert published["refusal_id"] == "checkout-locked"
    assert published["refusal"].startswith("checkout-locked: ")
    assert published["stall_started_at"] == 0
    assert all(row["number"] != 7 for row in after["running"])

    _unpin(repo, wt)
    coord.cycle("claude", now=20 * MINUTE)
    live.replace_stalled(_stalled_rows(coord, 20 * MINUTE))
    assert dashboard_data.snapshot([], dispatch_enabled=True)["stalled"] == []


def test_a_missing_or_corrupt_stalled_file_reads_as_nothing_stalled(coord_state):
    """Derived state the console only displays: a half-written or absent file renders empty."""
    assert live.stalled() == []
    live.STALLED_FILE.write_text("{ not json")
    assert live.stalled() == []


def test_the_new_record_fields_default_so_older_stores_still_open(coord_state):
    """Continuation records are JSON blobs, so the clock needs no schema migration — but a store
    written before it existed must still load, or a fleet would have to stop to upgrade."""
    from agentflow.coordinator.store import SCHEMA_VERSION, Store, default_store_path

    store = Store(default_store_path())
    try:
        legacy = json.loads(store._encode(Record(
            identity="o/r|7|build|-", stage="build", pool="claude", demand=5)))
        for field in ("refusals", "stall_refusal_id", "stall_started_at",
                      "stall_last_observed_at"):
            legacy.pop(field)
        restored = store._decode(json.dumps(legacy))
    finally:
        store.close()

    assert SCHEMA_VERSION == 1
    assert restored.refusals == 0 and restored.stall_refusal_id == ""
    assert restored.stall_started_at == 0 and restored.stall_last_observed_at == 0


# --- what the park actually says ----------------------------------------------------------

#: Claims a park for a stage that never started must never make. Every one of them is the exact
#: wording of some *other* park in this codebase, which is why borrowing that copy would be a lie
#: here — and why matching on bare words like "budget" would not do: the new copy has to be free
#: to say a budget was *not* spent.
_NEVER_CLAIMED = ("ran out of room", "exhaust", "couldn't ground", "i drafted a plan",
                  "review executions failed", "cut off at its", "did not reach")

#: And what it must state instead, in so many words.
_ALWAYS_DENIED = ("no session ran", "no attempt was used", "no budget was drawn down")


def _forbidden(body: str) -> list[str]:
    lowered = body.lower()
    return [claim for claim in _NEVER_CLAIMED if claim in lowered]


def _missing_denials(body: str) -> list[str]:
    lowered = body.lower()
    return [denial for denial in _ALWAYS_DENIED if denial not in lowered]


def test_the_intake_park_says_triage_never_started_and_claims_nothing_it_did_not_do(
        monkeypatch):
    """Intake's ordinary hold asks the maintainer to settle a scope question. Nobody asked one
    here — no session ran — so the comment says that, names what is pinned, and asks for the one
    thing that would unblock it."""
    from agentflow.coordinator.coordinator import refused_before_start_hold_reason

    comments: list[str] = []
    _hold_seams(monkeypatch, comments)
    blocker = ("/work/.agentflow/worktrees/claude-intake/issue-7 is pinned open by "
               "`git worktree lock` and holds uncommitted work")
    record = Record(identity="o/r|7|intake|-", stage="intake", pool="claude", demand=5,
                    repo="o/r", subject="7",
                    hold_reason=refused_before_start_hold_reason(f"checkout-locked: {blocker}"))

    assert coordinated_intake.hold_intake(record)

    body = comments[0]
    assert _forbidden(body) == [] and _missing_denials(body) == []
    assert blocker in body                          # names the pin, and where it is
    assert "haven't started triaging" in body
    assert "checkout-locked" not in body            # the check id is for the log, not the human
    assert "`/agentflow pickup`" in body            # and the resume command comes after the fix


def test_the_attack_park_shows_the_draft_without_claiming_anyone_argued_with_it(monkeypatch):
    """The round never ran, so the draft below it is untested. The comment may still show the
    draft — it is worth something unhardened — but it must not suggest a round was spent."""
    from agentflow.coordinator.coordinator import refused_before_start_hold_reason
    from agentflow.coordinator.intake_stage import encode_result
    from agentflow.intake import IntakeResult, IntakeRoute

    comments: list[str] = []
    _hold_seams(monkeypatch, comments, module=coordinated_attack)
    draft = encode_result(IntakeResult(IntakeRoute.READY, "Rewrite the widget pipeline.", "t"))
    payload = json.dumps({"draft": draft, "source_ref": "abc123", "snapshot": {"body": ""}})
    record = Record(identity="o/r|7|attack|-", stage="attack", pool="claude", demand=5,
                    repo="o/r", subject="7", input_ptr=payload,
                    hold_reason=refused_before_start_hold_reason(
                        "checkout-locked: the attack checkout is pinned open"))

    assert coordinated_attack.hold_attack(record)

    body = comments[0]
    assert _forbidden(body) == [] and _missing_denials(body) == []
    assert "the attack checkout is pinned open" in body
    assert "never started" in body
    assert "Rewrite the widget pipeline." in body   # the draft still travels, marked untested
    assert "untested" in body


def test_the_review_park_says_nothing_looked_at_the_change_at_all(monkeypatch):
    """A parked review normally reports an execution failure or a product decision. Neither
    happened: no review session was ever started, and the comment has to say so plainly or a
    maintainer reads a spent budget where there is none."""
    from agentflow.coordinator.coordinator import refused_before_start_hold_reason

    posted: list[dict] = []
    monkeypatch.setattr(pr_park, "park_pr_number", lambda record: 42)
    monkeypatch.setattr(pr_park, "chain_uncertainty", lambda record: None)
    monkeypatch.setattr(github, "pr_comments",
                        lambda repo, number: [github.Comment(body=p["marker"], created_at="")
                                              for p in posted])
    monkeypatch.setattr("agentflow.notify.notify", lambda *args: True)

    def fake_park(repo, pr, _verdict, *, reason, missing_outcome, context, proof_marker):
        posted.append({"reason": reason, "missing": missing_outcome, "context": context,
                       "marker": proof_marker})

    monkeypatch.setattr("agentflow.gate.park", fake_park)
    record = Record(identity="o/r|42|review|abc", stage="review", pool="claude", demand=5,
                    repo="o/r", subject="42", target="abc123def456",
                    source="/work/.agentflow/worktrees/claude-review/pr-42-x",
                    hold_reason=refused_before_start_hold_reason(
                        "checkout-locked: the review checkout is pinned open"))

    assert pr_park.park_pr(record)

    park = posted[0]
    text = " ".join([park["reason"], park["missing"], *park["context"].options,
                     park["context"].consequences, park["context"].recommendation,
                     park["context"].next_action, *park["context"].checks])
    assert _forbidden(text) == [] and _missing_denials(text) == []
    assert "no session ran at all" in park["missing"]
    assert "the review checkout is pinned open" in " ".join(park["context"].checks)
    assert not park["context"].decision_needed


@pytest.mark.parametrize("stage", ["build", "mockup", "revise", "respond", "converse",
                                   "research"])
def test_no_other_stage_grew_a_refused_before_start_outcome(stage):
    """Only Intake, the attack round, and a fresh Review can produce the refusal that proves a
    human must act, so only those three earned new copy. Every other stage's handoff must be
    reachable by exactly the reasons it had before."""
    from agentflow.coordinator.coordinator import refused_before_start

    assert not refused_before_start(None)
    assert not refused_before_start("continuation budget exhausted")
    assert not refused_before_start(f"{stage} exhausted its attempts")


# --- shared fixtures for the engine-level cases -------------------------------------------


class _PreparesOnCue:
    """A one-stage adapter whose preparation answer the test flips between cycles."""

    def __init__(self, answer) -> None:
        self.answer = answer

    def prepare(self, record):
        return self.answer[0]

    def verify(self, record, obs) -> bool:
        return False


def _cold_build(**kwargs) -> Submission:
    return Submission(repo="o/r", subject="7", stage="build", pool="claude", complexity="deep",
                      source="/work/.agentflow/worktrees/claude/issue-7-x", **kwargs)


def _refusing_coord(make_coord, answer, *, gate=None, log=None):
    return make_coord(adapter=StageRouter({"build": _PreparesOnCue(answer)}),
                      gate=gate or (lambda record: True),
                      launcher=NeverStartsLauncher(), log=log or (lambda line: None))


def _hold_seams(monkeypatch, comments, *, module=coordinated_intake):
    """Wire the shared handoff envelope's seams (ADR 0042) — the comment thread it proves
    through, the projection that posts, the claim release, and the ping — stated as facts."""
    notified: list[tuple] = []
    monkeypatch.setattr(github, "issue_headline",
                        lambda repo, number: github.IssueHeadline("old", frozenset()))
    monkeypatch.setattr(github, "issue_comments",
                        lambda repo, number: [github.Comment(body=body, created_at="")
                                              for body in comments])
    monkeypatch.setattr(module, "apply_intake",
                        lambda repo, number, title, labels, result: comments.append(result.body))
    monkeypatch.setattr(module, "release", lambda repo, number, label: True)
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args: notified.append(args) or True)
    return notified
