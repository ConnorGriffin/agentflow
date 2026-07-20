"""Respond as the fifth coordinated stage (issue #107), driven through the public
``submit_stage`` / ``cycle`` seam.

One unanswered maintainer comment on an existing agentflow PR maps to one stable Respond identity;
a later comment is a new target with a fresh budget. Respond adopts the change's original tool
lineage and the retained PR branch/worktree, stays pinned to that lineage (a closed home pool makes
it wait, never switch tools), keeps its local work across an interrupted continuation, completes
only on the marked reply plus any verified pushed change (independent of provider exit), releases
its change claim on completion (it has no successor — the answered PR returns to the normal merge
pipeline), and parks the PR once on exhaustion without discarding that work.

The coordinator crash boundaries are exercised through the public seam behind the same stage router
that runs the live stages; the pure submission mapping and the live reply/claim-release reads are
exercised directly with only their external GitHub/worktree reads faked (ADR 0020).
"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_build, github
from agentflow.coordinator import RespondStageAdapter, StageRouter, Submission, tracer
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import Record

RESPOND_WT = "/w/.agentflow/worktrees/claude/issue-7-x"


def _respond_sub(subject="7", *, pool="claude", target="cid-1", source=None):
    return Submission(repo="o/r", subject=subject, stage="respond", pool=pool, complexity="deep",
                      target=target, builder_lineage=pool,
                      source=source or f"/w/.agentflow/worktrees/{pool}/issue-{subject}-x",
                      input_ptr="<!-- agentflow-respond-baseline:base-head -->")


def _respond_adapter(fake, *, reply, prep=None, handoff=None, settle=None):
    """A Respond adapter wired to test flags: ``reply``/``prep`` are single-element lists so a test
    flips posted-reply durability and retained-worktree readiness mid-flight; the fake plays observer."""
    prep = prep or [True]
    return RespondStageAdapter(reply_ready=lambda r, o: reply[0],
                               worktree_ready=lambda r: prep[0], observer=fake,
                               handoff=handoff, settle=settle)


def _ident(subject, target="cid-1"):
    return f"o/r|{subject}|respond|{target}"


# --- identity: one comment is one Respond, a later comment is a new one --------------------

def test_one_comment_is_one_identity_and_a_later_comment_is_a_new_stage(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_respond_adapter(fake, reply=[False]))
    first = coord.submit_stage(_respond_sub(target="cid-1"))
    again = coord.submit_stage(_respond_sub(target="cid-1"))   # duplicate discovery of the same comment
    assert first == again == _ident("7", "cid-1")              # one stable identity, no second record
    later = coord.submit_stage(_respond_sub(target="cid-2"))   # a later maintainer comment
    assert later != first                                      # genuinely new Respond target
    assert record_of(coord, later).attempts == 0              # with its own fresh budget


# --- preparation and continuation both retain the branch, lineage, and local work ----------

def test_worktree_miss_consumes_no_permit_or_attempt_and_keeps_local_work(make_coord):
    fake = FakeSession()
    reply, prep = [False], [False]                       # the retained PR-branch worktree is not ready
    coord = make_coord(fake, adapter=_respond_adapter(fake, reply=reply, prep=prep))
    ident = coord.submit_stage(_respond_sub(source=RESPOND_WT))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 0                 # nothing reserved
    rec = record_of(coord, ident)
    assert rec.attempts == 0 and rec.state == "waiting"
    assert rec.source == RESPOND_WT and rec.claim is True  # branch, claim, and local work untouched

    prep[0] = True
    coord.cycle("claude")
    assert permits(coord, "claude") == 3                 # respond (claude, deep) reserves three
    assert record_of(coord, ident).attempts == 1


def test_public_respond_prepare_recreates_only_from_the_owned_remote_pr_branch(
        make_coord, monkeypatch, tmp_path):
    fake = FakeSession()
    source = tmp_path / ".agentflow/worktrees/claude/issue-7-x"
    branch = "agentflow/claude/issue-7-x"
    calls = []

    def git(argv):
        calls.append(argv)
        if "show-ref" in argv and f"refs/heads/{branch}" in argv:
            return SimpleNamespace(returncode=1, stdout="")
        if "show-ref" in argv and f"refs/remotes/origin/{branch}" in argv:
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agentflow.loop._run", git)
    monkeypatch.setattr("agentflow.runner.ClaudeRunner.provision", lambda self, wt: None)
    adapter = RespondStageAdapter(
        reply_ready=lambda record, obs: False,
        worktree_ready=coordinated_build._worktree_ready,
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub(source=str(source)))
    coord.cycle("claude")

    added = next(call for call in calls if "worktree" in call and "add" in call)
    assert f"origin/{branch}" in added
    assert "origin/main" not in added
    assert record_of(coord, ident).attempts == 1


def test_public_respond_prepare_waits_when_the_remote_pr_branch_is_missing(
        make_coord, monkeypatch, tmp_path):
    fake = FakeSession()
    source = tmp_path / ".agentflow/worktrees/claude/issue-7-x"
    calls = []

    def git(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=1 if "show-ref" in argv else 0, stdout="")

    monkeypatch.setattr("agentflow.loop._run", git)
    adapter = RespondStageAdapter(
        reply_ready=lambda record, obs: False,
        worktree_ready=coordinated_build._worktree_ready,
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub(source=str(source)))
    coord.cycle("claude")

    assert not any("origin/main" in call for call in calls)
    rec = record_of(coord, ident)
    assert rec.state == "waiting" and rec.attempts == 0


def test_interrupted_respond_continues_on_the_same_retained_worktree(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_respond_adapter(fake, reply=[False], prep=[True]))
    ident = coord.submit_stage(_respond_sub(source=RESPOND_WT))
    coord.cycle("claude")
    fake.end(ident, cause=ProviderCause.PROCESS)         # interrupted with only local changes
    coord.cycle("claude")                                # continues on the same worktree
    rec = record_of(coord, ident)
    assert rec.continuation is True and rec.attempts == 2 and rec.claim is True
    assert rec.source == RESPOND_WT and rec.pool == "claude" and rec.lineage == "claude"


def test_respond_never_migrates_to_the_other_pool(make_coord):
    """A closed pool makes Respond wait, never switch tools — it is code-writing, pinned to the
    change's original lineage (ADR 0028). Capacity on the other pool cannot adopt it."""
    fake = FakeSession()
    coord = make_coord(fake, adapter=_respond_adapter(fake, reply=[False]))
    ident = coord.submit_stage(_respond_sub("9", pool="codex"))
    coord.cycle("codex")
    fake.end(ident, cause=ProviderCause.CAPACITY, reset_at=0)  # paused → continuation on codex
    coord.cycle("claude", now=0)                               # the claude cycle must not adopt it
    assert record_of(coord, ident).pool == "codex"
    assert permits(coord, "claude") == 0


# --- completion: the marked reply plus any verified push, independent of provider exit ------

def test_clean_exit_without_a_reply_stays_incomplete_and_continues(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, adapter=_respond_adapter(fake, reply=[False]))
    ident = coord.submit_stage(_respond_sub(source=RESPOND_WT))
    coord.cycle("claude")
    fake.end(ident, success=True, cause=ProviderCause.PROCESS)  # a clean exit, but no reply posted
    coord.cycle("claude")
    rec = record_of(coord, ident)
    assert rec.continuation is True and rec.attempts == 2       # not completed — it continues
    assert not rec.retired and rec.state != "held"
    assert rec.claim is True and rec.source == RESPOND_WT       # claim and local work retained


def test_respond_completes_on_a_posted_reply_even_after_a_bad_exit_then_releases_the_claim(make_coord):
    fake = FakeSession()
    settled = []
    adapter = _respond_adapter(fake, reply=[True],
                               settle=lambda r: settled.append(r.identity) or "pr-url")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub())
    coord.cycle("claude")                                       # admit
    fake.end(ident, cause=ProviderCause.PROCESS)               # the provider exited badly...
    out = coord.cycle("claude")                                # ...but the reply is posted → completed
    assert [o.status for o in out] == ["completed"]
    assert record_of(coord, ident).claim is True              # claim retained at the completion boundary

    coord.cycle("claude")                                      # terminal settle: release claim, retire
    rec = record_of(coord, ident)
    assert rec.retired is True and rec.claim is False          # no successor — the claim is released
    assert settled == [ident]
    # Idempotent across a restart: the retired record is re-observed, never re-released.
    make_coord(fake, adapter=adapter).cycle("claude")
    assert settled == [ident]


def test_public_respond_seam_requires_targeted_reply_and_clean_pushed_worktree(
        make_coord, monkeypatch, tmp_path):
    """Drive the production reply verifier through Coordinator + RespondStageAdapter."""
    from agentflow.gate import respond_change_marker, respond_reply_disclaimer
    from agentflow.loop import _run as real_run

    fake = FakeSession()
    wt = tmp_path / ".agentflow/worktrees/claude/issue-7-x"
    wt.mkdir(parents=True)
    dirty = [" M requested-change.py\n"]

    def git(argv):
        if argv[:3] == ["git", "-C", str(wt)]:
            if "rev-list" in argv:
                return SimpleNamespace(returncode=0, stdout="0\n")
            if "status" in argv:
                return SimpleNamespace(returncode=0, stdout=dirty[0])
            return SimpleNamespace(returncode=0, stdout="")
        return real_run(argv)

    monkeypatch.setattr("agentflow.loop._run", git)
    monkeypatch.setattr("agentflow.github.list_open_prs",
                        lambda repo, head=None: [github.PrRow(42, head or "", "remote-head")])
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: [
        {"body": "please make a change", "id": "cid-1"},
        {"body": (respond_reply_disclaimer("cid-1") + "\n" +
                  respond_change_marker("none") + "\n\nDone.")},
    ])
    adapter = RespondStageAdapter(
        reply_ready=coordinated_build._reply_ready,
        worktree_ready=lambda record: True,
        observer=fake,
        settle=lambda record: "pr-url",
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub(target="cid-1", source=str(wt)))
    coord.cycle("claude")
    fake.end(ident, success=True, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert record_of(coord, ident).state == "running"  # dirty local work cannot complete

    dirty[0] = ""
    fake.end(ident, success=False, cause=ProviderCause.PROCESS)
    assert [out.status for out in coord.cycle("claude")] == ["completed"]


def test_public_respond_seam_fails_closed_when_owned_worktree_is_missing(
        make_coord, monkeypatch, tmp_path):
    from agentflow.gate import respond_change_marker, respond_reply_disclaimer

    fake = FakeSession()
    missing = tmp_path / ".agentflow/worktrees/claude/issue-7-missing"
    monkeypatch.setattr("agentflow.github.list_open_prs",
                        lambda repo, head=None: [github.PrRow(42, head or "", "remote-head")])
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: [
        {"body": "please make a change", "id": "cid-1"},
        {"body": (respond_reply_disclaimer("cid-1") + "\n" +
                  respond_change_marker("none") + "\n\nDone.")},
    ])
    adapter = RespondStageAdapter(
        reply_ready=coordinated_build._reply_ready,
        worktree_ready=lambda record: True,
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub(target="cid-1", source=str(missing)))
    coord.cycle("claude")
    fake.end(ident, success=True, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert record_of(coord, ident).state == "running"


def test_public_respond_seam_rejects_a_reply_for_a_different_comment_target(
        make_coord, monkeypatch, tmp_path):
    from agentflow.gate import respond_change_marker, respond_reply_disclaimer

    fake = FakeSession()
    wt = tmp_path / ".agentflow/worktrees/claude/issue-7-x"
    wt.mkdir(parents=True)

    def git(argv):
        if "rev-list" in argv:
            return SimpleNamespace(returncode=0, stdout="0\n")
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agentflow.loop._run", git)
    monkeypatch.setattr("agentflow.github.list_open_prs",
                        lambda repo, head=None: [github.PrRow(42, head or "", "remote-head")])
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: [
        {"body": "first question", "id": "cid-1"},
        {"body": (respond_reply_disclaimer("cid-2") + "\n" +
                  respond_change_marker("none") + "\n\nAnswered another question.")},
    ])
    adapter = RespondStageAdapter(
        reply_ready=coordinated_build._reply_ready,
        worktree_ready=lambda record: True,
        observer=fake,
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub(target="cid-1", source=str(wt)))
    coord.cycle("claude")
    fake.end(ident, success=True, cause=ProviderCause.PROCESS)
    coord.cycle("claude")
    assert record_of(coord, ident).state == "running"


# --- exhaustion parks the PR once, keeping the local work ----------------------------------

def test_exhaustion_parks_the_pr_once_and_does_not_discard_local_work(make_coord):
    fake = FakeSession()
    handoffs = []
    adapter = _respond_adapter(fake, reply=[False], prep=[True],
                               handoff=lambda r: handoffs.append(r.identity) or "pr-proof")
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub(source=RESPOND_WT))
    outcome = None
    for _ in range(8):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        assert record_of(coord, ident).claim is True          # keeps its claim while budget remains
        fake.end(ident, cause=ProviderCause.PROCESS)
    assert outcome is not None and outcome.status == "held" and outcome.handoff == "pr:parked"
    rec = record_of(coord, ident)
    assert rec.attempts == 3 and rec.handoffs == 1 and rec.notifications == 1
    assert rec.claim is False                                  # claim released only at the park boundary
    assert rec.source == RESPOND_WT                            # local work is neither discarded nor forced
    assert handoffs == [ident]
    assert make_coord(fake, adapter=adapter).cycle("claude") == []
    assert handoffs == [ident]                                 # a restart never repeats the external park


def test_public_respond_exhaustion_has_its_own_durable_park_and_notification(
        make_coord, monkeypatch):
    fake = FakeSession()
    comments = [{"body": "> *agentflow: parked for human review.*\n\nOld review park."}]
    notifications = []

    def pr_comment(repo, pr, body):
        comments.append({"body": body})
        return True

    monkeypatch.setattr(coordinated_build, "_park_pr_number", lambda record: 42)
    monkeypatch.setattr("agentflow.github.pr_comment", pr_comment)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: list(comments))
    monkeypatch.setattr("agentflow.notify.notify",
                        lambda *args, **kwargs: notifications.append((args, kwargs)) or True)
    adapter = RespondStageAdapter(
        reply_ready=lambda record, obs: False,
        worktree_ready=lambda record: True,
        observer=fake,
        handoff=coordinated_build._park_respond,
    )
    coord = make_coord(fake, adapter=adapter)
    ident = coord.submit_stage(_respond_sub(target="cid-1", source=RESPOND_WT))
    for _ in range(3):
        coord.cycle("claude")
        fake.end(ident, cause=ProviderCause.PROCESS)
        outcomes = coord.cycle("claude")

    assert [out.status for out in outcomes] == ["held"]
    respond_parks = [comment["body"] for comment in comments
                     if "agentflow-respond-park-target:" in comment["body"]]
    assert len(respond_parks) == 1
    assert "cid-1" in respond_parks[0] and "review budget" not in respond_parks[0]
    assert len(notifications) == 1
    assert notifications[0][1]["sequence_id"]

    make_coord(fake, adapter=adapter).cycle("claude")
    assert len(respond_parks) == 1 and len(notifications) == 1


# --- admission: Respond is enabled; unknown future stages stay queued ----------------------

def test_gate_admits_respond_and_keeps_unknown_stage_waiting(make_coord):
    fake = FakeSession()
    router = StageRouter({"respond": _respond_adapter(fake, reply=[False])})
    coord = make_coord(fake, adapter=router, gate=tracer.build_review_revise_gate)
    respond = coord.submit_stage(_respond_sub())
    future = coord.submit_stage(Submission(repo="o/r", subject="11", stage="future",
                                           pool="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, respond).state == "running"       # Respond now admits
    assert permits(coord, "claude") == 3                       # against its reviewed three-permit demand
    pending = record_of(coord, future)
    assert pending.state == "waiting" and pending.attempts == 0


# --- pure mapping -------------------------------------------------------------------------

def test_respond_submission_adopts_the_branch_lineage_and_holds_the_claim():
    from agentflow.gate import respond_reply_disclaimer
    cfg = SimpleNamespace(repo="o/r", workdir="/home/w")
    sub = coordinated_build.respond_submission(
        cfg, 42, "agentflow/claude/issue-7-fix-thing", "please tweak the copy", "cid-9",
        "base-sha")
    assert sub is not None
    assert sub.stage == "respond" and sub.subject == "7" and sub.target == "cid-9"
    assert sub.pool == "claude" and sub.builder_lineage == "claude"   # the change's original lineage
    assert sub.complexity == "deep" and sub.claim is True
    assert sub.source == "/home/w/.agentflow/worktrees/claude/issue-7-fix-thing"  # retained PR-branch wt
    assert "please tweak the copy" in sub.input_ptr and "#42" in sub.input_ptr
    assert respond_reply_disclaimer("cid-9") in sub.input_ptr
    assert "agentflow-respond-baseline:base-sha" in sub.input_ptr
    prompt = " ".join(sub.input_ptr.lower().split())
    assert "do not post the reply again" in prompt
    assert "edit that existing reply in place" in prompt
    assert "do not make or push it again" in prompt
    # A non-agentflow branch or a missing comment target yields no submission.
    assert coordinated_build.respond_submission(
        cfg, 42, "feature/x", "c", "cid-9", "base-sha") is None
    assert coordinated_build.respond_submission(
        cfg, 42, "agentflow/claude/issue-7-fix-thing", "c", "", "base-sha") is None
    assert coordinated_build.respond_submission(
        cfg, 42, "agentflow/claude/issue-7-fix-thing", "c", "cid-9", "") is None


# --- live reads: the marked reply and the claim release (faked GitHub/worktree, ADR 0020) --

def _respond_record():
    return Record(identity="o/r|7|respond|cid", stage="respond", pool="claude", demand=3,
                  repo="o/r", subject="7", target="cid", lineage="claude",
                  source="/w/.agentflow/worktrees/claude/issue-7-fix")


def test_reply_ready_rejects_a_generic_reply_that_is_not_bound_to_its_target(
        monkeypatch, tmp_path):
    from agentflow.gate import PR_MARK
    rec = _reply_read(monkeypatch, tmp_path)
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: [
        {"body": "please tweak the copy", "id": "cid"},
        {"body": f"{PR_MARK} reply from the build agent: done"},
    ])
    assert coordinated_build._reply_ready(rec, None) is False


def _reply_read(monkeypatch, tmp_path, *, ahead="0", status="", status_rc=0,
                change="none", head="h", baseline="h"):
    """Wire ``_reply_ready`` against a real retained worktree that exists on disk (so the pushed-
    change reads run) with our marked reply already the last word. ``ahead``/``status`` fake the
    ``git rev-list`` and ``git status --porcelain`` reads, and the record is returned so the caller
    just asserts the outcome."""
    from agentflow.gate import respond_change_marker, respond_reply_disclaimer
    wt = tmp_path / ".agentflow/worktrees/claude/issue-7-fix"
    wt.mkdir(parents=True)
    rec = Record(identity="o/r|7|respond|cid", stage="respond", pool="claude", demand=3,
                 repo="o/r", subject="7", target="cid", lineage="claude", source=str(wt),
                 input_ptr=f"<!-- agentflow-respond-baseline:{baseline} -->")

    head_sha = head

    def _run(cmd, *a, **k):
        if "rev-list" in cmd:
            return SimpleNamespace(returncode=0, stdout=ahead)
        if "status" in cmd:
            return SimpleNamespace(returncode=status_rc, stdout=status)
        if "merge-base" in cmd:
            # The rebased head does not descend from the baseline: a strict baseline-ancestry check
            # would reject it. The current verifier must not consult this at all.
            return SimpleNamespace(returncode=1, stdout="")
        return SimpleNamespace(returncode=0, stdout="")               # fetch and anything else

    def list_open_prs(repo, *, head=None, limit=100):
        return [github.PrRow(42, head or "", head_sha)]

    monkeypatch.setattr("agentflow.loop._run", _run)
    monkeypatch.setattr("agentflow.github.list_open_prs", list_open_prs)
    monkeypatch.setattr("agentflow.loop._pr_comments",
                        lambda repo, pr: [
                            {"body": "please tweak the copy", "id": "cid"},
                            {"body": (respond_reply_disclaimer("cid") + "\n" +
                                      respond_change_marker(change) + "\n\nDone.")},
                        ])
    return rec


def test_reply_ready_rejects_an_uncommitted_tracked_edit(monkeypatch, tmp_path):
    # The reply is posted and no commit is ahead of the remote head, but the requested edit is left
    # modified-but-uncommitted in the worktree: that change never became a pushed commit, so the
    # stage must stay incomplete rather than retire over unpushed work.
    rec = _reply_read(monkeypatch, tmp_path, ahead="0", status=" M agentflow/copy.py\n")
    assert coordinated_build._reply_ready(rec, None) is False


def test_reply_ready_rejects_an_untracked_new_file(monkeypatch, tmp_path):
    # The reply is posted and nothing is ahead of the remote head, but the responder left an
    # untracked new file — an uncommitted, unpushed change all the same — so the stage stays open.
    rec = _reply_read(monkeypatch, tmp_path, ahead="0", status="?? agentflow/new_helper.py\n")
    assert coordinated_build._reply_ready(rec, None) is False


def test_reply_ready_treats_an_unreadable_worktree_as_incomplete(monkeypatch, tmp_path):
    # A failed status read cannot prove the worktree is clean, so completion is withheld.
    rec = _reply_read(monkeypatch, tmp_path, ahead="0", status="", status_rc=1)
    assert coordinated_build._reply_ready(rec, None) is False


def test_reply_ready_completes_on_a_posted_reply_with_a_clean_pushed_worktree(monkeypatch, tmp_path):
    # Reply posted, nothing ahead of the remote head, and a clean worktree: the change is genuinely
    # pushed, so the stage completes — and completion is a stable read (restart idempotence: a
    # repeat over the same durable state re-proves the same result without posting a second reply).
    rec = _reply_read(monkeypatch, tmp_path, ahead="0", status="")
    assert coordinated_build._reply_ready(rec, None) is True
    assert coordinated_build._reply_ready(rec, None) is True


def test_reply_ready_verifies_the_pushed_head_named_by_the_reply(monkeypatch, tmp_path):
    rec = _reply_read(monkeypatch, tmp_path, change="new-head", head="new-head",
                      baseline="old-head")
    assert coordinated_build._reply_ready(rec, None) is True


def test_reply_ready_rejects_a_pushed_head_proof_that_did_not_advance_or_match(
        monkeypatch, tmp_path):
    unchanged = _reply_read(monkeypatch, tmp_path / "unchanged", change="old-head",
                            head="old-head", baseline="old-head")
    assert coordinated_build._reply_ready(unchanged, None) is False

    mismatch = _reply_read(monkeypatch, tmp_path / "mismatch", change="claimed-head",
                           head="different-head", baseline="old-head")
    assert coordinated_build._reply_ready(mismatch, None) is False


def test_reply_ready_completes_after_a_rebase_that_rewrote_history(monkeypatch, tmp_path):
    # Reproduces PR #194: the maintainer asked for a conflict fix, the responder rebased onto main
    # (so the baseline is no longer an ancestor of the pushed head), resolved, pushed, and marked the
    # reply with the exact pushed head. The clean owned worktree at that head proves the work, so the
    # rewritten history must NOT block completion.
    rec = _reply_read(monkeypatch, tmp_path, change="rebased-head", head="rebased-head",
                      baseline="pre-rebase-base")
    assert coordinated_build._reply_ready(rec, None) is True


def test_reply_ready_rejects_multiple_ambiguous_outcome_markers(monkeypatch, tmp_path):
    from agentflow.gate import respond_change_marker, respond_reply_disclaimer

    rec = _reply_read(monkeypatch, tmp_path, change="none", head="new-head",
                      baseline="old-head")
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: [
        {"body": "please change it", "id": "cid"},
        {"body": (respond_reply_disclaimer("cid") + "\n" +
                  respond_change_marker("none") + "\n" +
                  respond_change_marker("new-head"))},
    ])
    assert coordinated_build._reply_ready(rec, None) is False


def test_settle_respond_releases_the_building_claim_and_proves_it(monkeypatch):
    rec = _respond_record()
    labels = ["agentflow:building"]
    removed = []

    def remove_label(repo, number, label):
        removed.append(label)
        labels.clear()
        return True

    def api(args, *, parse_json=False):
        return {"labels": [{"name": n} for n in labels],
                "url": "https://github.com/o/r/issues/7"}

    monkeypatch.setattr("agentflow.github.remove_label", remove_label)
    monkeypatch.setattr("agentflow.github.api", api)
    monkeypatch.setattr(coordinated_build, "_park_pr_number", lambda record: 42)
    assert coordinated_build._settle_respond(rec) == "https://github.com/o/r/pull/42"
    assert removed == ["agentflow:building"]                       # the change claim is dropped
    # Idempotent: a repeat with the label already gone re-proves the same release.
    assert coordinated_build._settle_respond(rec) == "https://github.com/o/r/pull/42"
