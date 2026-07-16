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

import json
from types import SimpleNamespace

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_build
from agentflow.coordinator import RespondStageAdapter, StageRouter, Submission, tracer
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import Record

RESPOND_WT = "/w/.agentflow/worktrees/claude/issue-7-x"


def _respond_sub(subject="7", *, pool="claude", target="cid-1", source=None):
    return Submission(repo="o/r", subject=subject, stage="respond", pool=pool, complexity="deep",
                      target=target, builder_lineage=pool,
                      source=source or f"/w/.agentflow/worktrees/{pool}/issue-{subject}-x")


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


# --- admission: Respond is enabled, Mockup stays queued ------------------------------------

def test_gate_admits_respond_and_keeps_mockup_waiting(make_coord):
    fake = FakeSession()
    router = StageRouter({"respond": _respond_adapter(fake, reply=[False])})
    coord = make_coord(fake, adapter=router, gate=tracer.build_review_revise_gate)
    respond = coord.submit_stage(_respond_sub())
    mockup = coord.submit_stage(Submission(repo="o/r", subject="11", stage="mockup",
                                           pool="claude", complexity="deep"))
    coord.cycle("claude")
    assert record_of(coord, respond).state == "running"       # Respond now admits
    assert permits(coord, "claude") == 3                       # against its reviewed three-permit demand
    m = record_of(coord, mockup)
    assert m.state == "waiting" and m.attempts == 0           # Mockup stays visibly queued, dormant


# --- pure mapping -------------------------------------------------------------------------

def test_respond_submission_adopts_the_branch_lineage_and_holds_the_claim():
    cfg = SimpleNamespace(repo="o/r", workdir="/home/w")
    sub = coordinated_build.respond_submission(
        cfg, 42, "agentflow/claude/issue-7-fix-thing", "please tweak the copy", "cid-9")
    assert sub is not None
    assert sub.stage == "respond" and sub.subject == "7" and sub.target == "cid-9"
    assert sub.pool == "claude" and sub.builder_lineage == "claude"   # the change's original lineage
    assert sub.complexity == "deep" and sub.claim is True
    assert sub.source == "/home/w/.agentflow/worktrees/claude/issue-7-fix-thing"  # retained PR-branch wt
    assert "please tweak the copy" in sub.input_ptr and "#42" in sub.input_ptr
    # A non-agentflow branch or a missing comment target yields no submission.
    assert coordinated_build.respond_submission(cfg, 42, "feature/x", "c", "cid-9") is None
    assert coordinated_build.respond_submission(
        cfg, 42, "agentflow/claude/issue-7-fix-thing", "c", "") is None


# --- live reads: the marked reply and the claim release (faked GitHub/worktree, ADR 0020) --

def _respond_record():
    return Record(identity="o/r|7|respond|cid", stage="respond", pool="claude", demand=3,
                  repo="o/r", subject="7", lineage="claude",
                  source="/w/.agentflow/worktrees/claude/issue-7-fix")


def test_reply_ready_completes_only_once_our_marked_reply_has_the_last_word(monkeypatch):
    from agentflow.gate import PR_MARK
    rec = _respond_record()
    monkeypatch.setattr("agentflow.loop._run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps([{"number": 42, "headRefOid": "h"}])))
    comments = [{"body": "please tweak the copy"}]                 # the maintainer still has the last word
    monkeypatch.setattr("agentflow.loop._pr_comments", lambda repo, pr: list(comments))
    assert coordinated_build._reply_ready(rec, None) is False
    comments.append({"body": f"{PR_MARK} reply from the build agent: done"})  # our marker replies
    assert coordinated_build._reply_ready(rec, None) is True


def test_settle_respond_releases_the_building_claim_and_proves_it(monkeypatch):
    rec = _respond_record()
    labels = ["agentflow:building"]
    removed = []

    def _run(cmd, *a, **k):
        if "edit" in cmd and "--remove-label" in cmd:
            removed.append(cmd[-1])
            labels.clear()
            return SimpleNamespace(returncode=0, stdout="")
        if "issue" in cmd and "view" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps(
                {"labels": [{"name": n} for n in labels], "url": "https://github.com/o/r/issues/7"}))
        if "pr" in cmd and "list" in cmd:
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"number": 42}]))
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr("agentflow.loop._run", _run)
    assert coordinated_build._settle_respond(rec) == "https://github.com/o/r/pull/42"
    assert removed == ["agentflow:building"]                       # the change claim is dropped
    # Idempotent: a repeat with the label already gone re-proves the same release.
    assert coordinated_build._settle_respond(rec) == "https://github.com/o/r/pull/42"
