"""Preparation refuses by name, and the engine keeps and publishes that name (#405).

Everything a stage checks *before* a provider runs used to answer with a bare bool. A review
checkout that refused every cycle for half an hour over one untracked scratch file left a daemon
log saying only that admission was stuck (#397/#399), and an operator could not tell a paced
fleet from a stalled one (#365). These tests drive the real preparation collaborators and the
public ``submit_stage`` / ``cycle`` seam, and assert three things:

- every refusing check names itself and quotes the live values behind it;
- the record holds exactly the refusal of the *latest* cycle — cleared the moment nothing refuses,
  replaced when the capacity gate refuses instead, and written only when it changes;
- the refusal is published on its own, never folded into the running board that pool counts
  derive from.

The admission decisions themselves are untouched: every collaborator here refuses exactly the
cases it refused before, and every truthy answer stays truthy.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import NeverStartsLauncher, record_of

from agentflow import (coordinated_attack, coordinated_converse, coordinated_intake,
                       coordinated_mockup, coordinated_research, coordinated_review, github,
                       live, stage_worktree)
from agentflow.coordinator import BuildStageAdapter, IntakeStageAdapter, StageRouter, Submission
from agentflow.coordinator import tracer
from agentflow.coordinator.record import Record
from agentflow.coordinator.verification import Verification, payload_preview, unprepared
from agentflow.worktree_ref import WorktreeRef


def _git(cwd, *args) -> str:
    out = subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)
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


def _check(answer) -> str:
    """The stable id a refusal named. Fails loudly on a bare bool, which is the regression."""
    assert isinstance(answer, Verification), f"untyped refusal: {answer!r}"
    assert not answer, "expected a refusal"
    return answer.check


def _raises(*_a, **_k):
    raise subprocess.CalledProcessError(128, ["git", "worktree", "add"])


# --- the named checks, one collaborator at a time ---------------------------------------


def test_the_owned_worktree_preparation_names_each_of_its_nine_refusals(tmp_path, monkeypatch):
    """Build/Revise/Respond/Mockup share one checkout preparation with nine ways to say no, and
    every one of them is a different thing for an operator to do. The combined branch check is
    two of them: git failing to read the branch and git reporting the wrong branch are not the
    same problem, and lumping them together is what makes a stuck stage undiagnosable."""
    from agentflow import runner

    repo = _repo(tmp_path)
    branch = "agentflow/claude/issue-7-x"
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-7-x"

    def record(*, stage="build", source=str(wt)):
        return Record(identity="o/r|7|build|-", stage=stage, pool="claude", demand=5,
                      repo="o/r", subject="7", source=source, lineage="claude")

    seen = {}

    # 1. a pointer that is not this record's own checkout at all
    seen["source-unreadable"] = _check(stage_worktree.worktree_ready(record(source="/nope")))

    # 2. the directory survives but git has forgotten it (a daemon killed mid-prepare)
    wt.mkdir(parents=True)
    seen["worktree-unregistered"] = _check(stage_worktree.worktree_ready(record()))
    wt.rmdir()

    # 3./4. a registered checkout whose branch cannot be read, and one on the wrong branch
    _git(repo, "worktree", "add", "-b", branch, str(wt), "main")
    monkeypatch.setattr(runner.ClaudeRunner, "provision", lambda self, path: None)
    real_run = stage_worktree._run
    monkeypatch.setattr(stage_worktree, "_run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 129, "", ""))
    seen["branch-read-failed"] = _check(stage_worktree.worktree_ready(record()))
    monkeypatch.setattr(stage_worktree, "_run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "elsewhere\n", ""))
    seen["branch-mismatch"] = _check(stage_worktree.worktree_ready(record()))
    monkeypatch.setattr(stage_worktree, "_run", real_run)

    # 5. the retained checkout is fine but its toolchain will not come up
    assert stage_worktree.worktree_ready(record())          # the same checkout still prepares
    monkeypatch.setattr(runner.ClaudeRunner, "provision", _raises)
    seen["retained-provision-failed"] = _check(stage_worktree.worktree_ready(record()))
    monkeypatch.setattr(runner.ClaudeRunner, "provision", lambda self, path: None)
    _git(repo, "worktree", "remove", "--force", str(wt))

    # 6. no checkout, and the repository cannot reach its origin
    _git(repo, "remote", "remove", "origin")
    seen["fetch-failed"] = _check(stage_worktree.worktree_ready(record()))
    _git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))

    # 7. no checkout and no branch anywhere — a continuation stage may not invent one
    _git(repo, "branch", "-D", branch)
    seen["branch-absent"] = _check(stage_worktree.worktree_ready(record(stage="revise")))

    # 8. the branch exists but another checkout already holds it
    other = repo / ".agentflow" / "worktrees" / "claude" / "issue-7-other"
    _git(repo, "worktree", "add", "-b", branch, str(other), "main")
    seen["worktree-add-failed"] = _check(stage_worktree.worktree_ready(record()))
    _git(repo, "worktree", "remove", "--force", str(other))

    # 9. the fresh checkout is created but will not provision
    monkeypatch.setattr(runner.ClaudeRunner, "provision", _raises)
    seen["provision-failed"] = _check(stage_worktree.worktree_ready(record()))

    assert set(seen) == set(seen.values()) and len(seen) == 9


def test_the_branch_checks_quote_the_git_step_and_the_two_branches(tmp_path, monkeypatch):
    """The two branch refusals carry what an operator needs to act: the git step and its exit
    status when the read failed, the expected and observed branches when it disagreed."""
    from agentflow import runner

    repo = _repo(tmp_path)
    branch = "agentflow/claude/issue-7-x"
    wt = repo / ".agentflow" / "worktrees" / "claude" / "issue-7-x"
    _git(repo, "worktree", "add", "-b", branch, str(wt), "main")
    monkeypatch.setattr(runner.ClaudeRunner, "provision", lambda self, path: None)
    record = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                    repo="o/r", subject="7", source=str(wt), lineage="claude")

    monkeypatch.setattr(stage_worktree, "_run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 129, "", ""))
    failed = stage_worktree.worktree_ready(record)
    assert "branch --show-current" in failed.detail and "129" in failed.detail

    monkeypatch.setattr(stage_worktree, "_run",
                        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "elsewhere\n", ""))
    mismatch = stage_worktree.worktree_ready(record)
    assert "elsewhere" in mismatch.detail and branch in mismatch.detail


def test_the_review_checkout_preparation_names_each_of_its_five_refusals(tmp_path, monkeypatch):
    """Review's detached exact-head checkout has five ways to say no, and only one of them —
    a live sibling still holding the checkout — is the fleet working as intended."""
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    wt = repo / ".agentflow" / "worktrees" / "claude-review" / "pr-42-x"

    def record(*, source=str(wt), target=head):
        return SimpleNamespace(repo="o/r", source=source, target=target, pool="claude")

    seen = {_check(coordinated_review._review_worktree_reset(record(source="/nope"))),
            _check(coordinated_review._review_worktree_reset(record(target="")))}

    _git(repo, "worktree", "add", "--detach", str(wt), head)
    marker = Path(_git(wt, "rev-parse", "--git-path", "agentflow-active"))
    marker = marker if marker.is_absolute() else wt / marker
    marker.write_text(str(os.getpid()))
    contention = coordinated_review._review_worktree_reset(record())
    assert contention.expected is True          # ordinary overlap, not a checkout to chase
    seen.add(_check(contention))
    marker.unlink()
    _git(repo, "worktree", "remove", "--force", str(wt))

    monkeypatch.setattr("agentflow.runner.ClaudeRunner.prepare_worktree_detached", _raises)
    seen.add(_check(coordinated_review._review_worktree_reset(record())))
    seen.add(_check(coordinated_review._review_worktree_reset(record(target="0" * 40))))

    assert seen == {"source-unreadable", "target-empty", "sibling-active",
                    "checkout-failed", "reviewed-head-gone"}


@pytest.mark.parametrize("module, kind", [(coordinated_intake, "intake"),
                                          (coordinated_attack, "attack")])
def test_the_read_only_checkout_rebuilds_name_each_of_their_five_refusals(
        tmp_path, monkeypatch, module, kind):
    """Intake and the attack round each rebuild a read-only checkout from durable payload text
    before admission. Both have five ways to refuse, including corrupt durable payload — the
    refusal that used to be a bare False with the malformed bytes nowhere to be seen."""
    repo = _repo(tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    wt = repo / ".agentflow" / "worktrees" / f"claude-{kind}" / "issue-7"
    good = json.dumps({"snapshot": {"body": ""}, "source_ref": head})

    def record(*, source=str(wt), input_ptr=good):
        return SimpleNamespace(repo="o/r", subject="7", pool="claude", stage=kind,
                               source=source, input_ptr=input_ptr)

    seen = {
        _check(module.reset_worktree(record(input_ptr=None))),
        _check(module.reset_worktree(record(input_ptr="{not json"))),
        _check(module.reset_worktree(
            record(input_ptr=json.dumps({"snapshot": {}, "source_ref": ""})))),
        _check(module.reset_worktree(record(source=str(repo / "elsewhere")))),
    }
    monkeypatch.setattr("agentflow.runner.ClaudeRunner.prepare_worktree_detached", _raises)
    seen.add(_check(module.reset_worktree(record())))

    assert seen == {"source-missing", "input-unreadable", "source-ref-invalid",
                    "worktree-ref-unreadable", "checkout-failed"}


@pytest.mark.parametrize("prove", [coordinated_intake.intake_claim_ready,
                                   coordinated_attack.attack_claim_ready])
def test_the_triage_claim_proofs_name_each_of_their_three_refusals(monkeypatch, prove):
    """The triaging claim is proved immediately before admission. Unreachable GitHub, a claim
    somebody else took, and an issue closed under the record are three different dispositions."""
    record = SimpleNamespace(repo="o/r", subject="7", pool="claude")
    standings = {}
    monkeypatch.setattr(github, "issue_standing", lambda repo, number: standings.get("now"))

    seen = {_check(prove(record))}                                 # unreadable: standings["now"] is None
    standings["now"] = SimpleNamespace(labels=[], state="OPEN")
    seen.add(_check(prove(record)))
    standings["now"] = SimpleNamespace(labels=["agentflow:triaging"], state="CLOSED")
    seen.add(_check(prove(record)))
    standings["now"] = SimpleNamespace(labels=["agentflow:triaging"], state="OPEN")
    assert prove(record)                                           # the decision itself is unchanged

    assert seen == {"claim-unreadable", "claim-released", "subject-closed"}


def test_the_mockup_claim_proof_names_each_of_its_four_refusals(monkeypatch):
    """Mockup proves both halves of its visible claim — that it still holds the drawing lane, and
    that a mockup is still wanted — so an operator clearing either label reads differently."""
    labels = {}
    monkeypatch.setattr(github, "issue_labels", lambda repo, number: labels.get("now"))

    def record(subject="7"):
        return SimpleNamespace(repo="o/r", subject=subject, pool="claude")

    seen = {_check(coordinated_mockup._mockup_claim_ready(record(subject="not-a-number"))),
            _check(coordinated_mockup._mockup_claim_ready(record()))}
    labels["now"] = ["agentflow:needs-mockup"]
    seen.add(_check(coordinated_mockup._mockup_claim_ready(record())))
    labels["now"] = ["agentflow:drawing-mockup"]
    seen.add(_check(coordinated_mockup._mockup_claim_ready(record())))
    labels["now"] = ["agentflow:drawing-mockup", "agentflow:needs-mockup"]
    assert coordinated_mockup._mockup_claim_ready(record())

    assert seen == {"subject-unreadable", "labels-unreadable", "claim-released",
                    "mockup-not-wanted"}


@pytest.mark.parametrize("ready, lane", [
    (coordinated_converse._ask_worktree_ready, "converse"),
    (coordinated_research._research_worktree_ready, "research"),
])
def test_the_detached_read_worktrees_name_each_of_their_four_refusals(tmp_path, ready, lane):
    """An Ask turn and a research run each provision a detached exact-revision checkout and
    reuse it exactly as it is on resume. Four ways to refuse, each a different git step."""
    repo = _repo(tmp_path)
    revision = _git(repo, "rev-parse", "HEAD")
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "--bare", str(empty)], check=True, capture_output=True)
    ref = (WorktreeRef.for_converse(str(repo), "claude", "abc") if lane == "converse"
           else WorktreeRef.for_research(str(repo), "claude", 5))
    wt = Path(ref.path)

    def record(*, source=ref.path, subject_revision=revision):
        return SimpleNamespace(repo="o/r", subject="5", pool="claude", stage=lane,
                               source=source, subject_revision=subject_revision)

    seen = {_check(ready(record(source="/nope")))}
    wt.mkdir(parents=True)
    seen.add(_check(ready(record())))                     # on disk, but git has forgotten it
    wt.rmdir()

    _git(repo, "remote", "remove", "origin")
    seen.add(_check(ready(record())))
    _git(repo, "remote", "add", "origin", str(tmp_path / "origin.git"))

    assert ready(record())                                # the ordinary path still prepares
    _git(repo, "worktree", "remove", "--force", str(wt))

    # A reachable origin with nothing to check out: the fetch succeeds and the add cannot.
    _git(repo, "remote", "set-url", "origin", str(empty))
    _git(repo, "update-ref", "-d", "refs/remotes/origin/main")
    seen.add(_check(ready(record(subject_revision="f" * 40))))

    assert seen == {"source-unreadable", "worktree-unregistered", "fetch-failed",
                    "worktree-add-failed"}


def test_the_build_collision_guard_names_the_sha_and_what_has_to_move():
    """A builder that reported an integration collision is deferred while ``origin/main`` still
    equals the head it collided on — a provably identical retry (#209). Saying so by name is
    what separates "waiting on the world" from "stuck"."""
    record = Record(identity="o/r|7|build|-", stage="build", pool="claude", demand=5,
                    repo="o/r", subject="7", source="/wt", collision_main_sha="c" * 40)
    adapter = BuildStageAdapter(pr_exists=lambda r: False, worktree_ready=lambda r: True,
                                main_head=lambda r: "c" * 40)

    refusal = adapter.prepare(record)
    assert _check(refusal) == "collision-unmoved"
    assert ("c" * 12) in refusal.detail and "origin/main" in refusal.detail

    adapter = BuildStageAdapter(pr_exists=lambda r: False, worktree_ready=lambda r: True,
                                main_head=lambda r: "d" * 40)
    assert adapter.prepare(record)                        # main moved — the decision is unchanged


def test_a_composed_preparation_surfaces_whichever_half_refused():
    """Intake and the attack round rebuild a checkout *and* prove a claim. Python's ``and``
    yields the first falsy operand, so both answers have to stay typed — a ``bool()`` anywhere in
    that composition erases whichever half refused back into a silent False."""
    record = Record(identity="o/r|7|intake|-", stage="intake", pool="claude", demand=1,
                    repo="o/r", subject="7", source="/wt")

    checkout_refused = IntakeStageAdapter(
        worktree_reset=lambda r: unprepared("checkout-failed", "git said no"),
        apply_route=lambda r, result: None,
        claim_ready=lambda r: unprepared("claim-released", "never reached"))
    assert _check(checkout_refused.prepare(record)) == "checkout-failed"

    claim_refused = IntakeStageAdapter(
        worktree_reset=lambda r: True,
        apply_route=lambda r, result: None,
        claim_ready=lambda r: unprepared("claim-released", "somebody took the issue"))
    assert _check(claim_refused.prepare(record)) == "claim-released"


# --- the bounded preview of corrupt durable payload --------------------------------------


def test_a_corrupt_payload_is_quoted_bounded_and_on_one_line_everywhere_it_travels(
        tmp_path, make_coord):
    """``input_ptr`` is durable external text: a crash or a hand edit can leave kilobytes of
    multi-line junk in it. The refusal it produces lands in the record, the daemon log, and the
    published projection, so the payload is named and previewed, never copied."""
    corrupt = "\n".join(f"line {n} of a payload nobody can parse" * 4 for n in range(60))
    assert len(corrupt) > 1024 and "\n" in corrupt

    adapter = IntakeStageAdapter(worktree_reset=coordinated_intake.reset_worktree,
                                 apply_route=lambda r, result: None,
                                 claim_ready=lambda r: True)
    lines: list[str] = []
    coord = make_coord(adapter=StageRouter({"intake": adapter}), gate=lambda r: True,
                       launcher=NeverStartsLauncher(), log=lines.append)
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="intake", pool="claude", complexity="deep",
        source=str(tmp_path / ".agentflow" / "worktrees" / "claude-intake" / "issue-7"),
        input_ptr=corrupt))

    coord.cycle("claude")
    coord.cycle("claude")                                   # the second miss prints the breadcrumb

    refusal = record_of(coord, ident).refusal
    assert refusal.startswith("input-unreadable: input_ptr is not a readable payload:")
    assert "\n" not in refusal and "(truncated)" in refusal
    assert len(refusal) < 300 and "line 3 of a payload" not in refusal
    breadcrumb = [line for line in lines if "unprepared for" in line]
    assert len(breadcrumb) == 1 and refusal in breadcrumb[0] and "\n" not in breadcrumb[0]
    published = tracer.refusal_projection(coord._store.load().values())
    assert [row["refusal"] for row in published] == [refusal]


def test_a_short_payload_is_quoted_whole_and_still_escaped():
    """Under the cap there is no truncation marker, and the quote is still a single escaped line —
    a two-line payload must not become a two-line log entry."""
    preview = payload_preview("input_ptr", "one\ntwo")
    assert preview == "input_ptr is not a readable payload: 'one\\ntwo'"
    assert "(truncated)" not in preview


# --- what the record keeps, and when it is written ---------------------------------------


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


def test_every_observed_refusal_is_recorded_once_counting_how_long_it_has_gone_on(make_coord):
    """A stage that refuses the same way every cycle is the common case — a checkout that will
    not come up until somebody clears it. #405 wrote that reason once and then went quiet, which
    is why nothing could say how long it had been going on across a restart. Each cycle now
    records the observation itself, exactly once: same reason, one more consecutive refusal."""
    answer = [unprepared("checkout-failed", "git worktree add exited 128")]
    coord = _refusing_coord(make_coord, answer)
    ident = coord.submit_stage(_cold_build())
    submitted = record_of(coord, ident).revision

    coord.cycle("claude")
    after_first = record_of(coord, ident)
    assert after_first.refusal == "checkout-failed: git worktree add exited 128"
    assert after_first.refusals == 1
    assert after_first.revision == submitted + 1

    coord.cycle("claude")
    after_second = record_of(coord, ident)
    assert after_second.refusals == 2                       # one more observation...
    assert after_second.revision == submitted + 2           # ...and exactly one more write


def test_a_refusal_that_stops_refusing_clears_from_the_record_and_the_board(make_coord):
    """The record answers "why is this waiting *now*". The moment preparation succeeds the old
    reason is gone — even when a later admission check, which names nothing, blocks the record
    anyway. Leaving it behind would publish a checkout problem for a record held by capacity."""
    answer = [unprepared("checkout-failed", "git worktree add exited 128")]
    gate = [True]
    coord = _refusing_coord(make_coord, answer, gate=lambda record: gate[0])
    ident = coord.submit_stage(_cold_build())

    coord.cycle("claude")
    assert record_of(coord, ident).refusal
    assert len(tracer.refusal_projection(coord._store.load().values())) == 1

    answer[0], gate[0] = True, False        # prepares now; an untyped admission check blocks it
    coord.cycle("claude")
    assert record_of(coord, ident).refusal == ""
    assert record_of(coord, ident).state == "waiting"
    assert tracer.refusal_projection(coord._store.load().values()) == []


def test_a_refusal_clears_when_the_stage_finally_starts(make_coord):
    """The other half of the same rule: a record that goes on to start must not carry the
    checkout problem it had last cycle into its running row."""
    from conftest import FakeSession

    fake = FakeSession()
    answer = [unprepared("checkout-failed", "git worktree add exited 128")]
    coord = make_coord(fake, adapter=StageRouter({"build": _PreparesOnCue(answer)}))
    ident = coord.submit_stage(_cold_build())

    coord.cycle("claude")
    assert record_of(coord, ident).refusal

    answer[0] = True
    coord.cycle("claude")
    assert record_of(coord, ident).state == "running"
    assert record_of(coord, ident).refusal == ""
    assert tracer.refusal_projection(coord._store.load().values()) == []


class _NamedGate:
    """A gate that refuses everything and names the reason, like the production capacity gate."""

    def __init__(self, reason) -> None:
        self.reason = reason
        self.lookups = 0

    def __call__(self, record) -> bool:
        return False

    def deferral_reason(self, record):
        self.lookups += 1
        return self.reason[0]


def test_a_capacity_refusal_replaces_the_cleared_preparation_reason_and_writes_once(make_coord):
    """When preparation succeeds and the pool refuses instead, the record carries the pool's
    reason — read exactly once per cycle, so the durable record and the daemon line can never
    disagree — and an unchanged capacity reason costs no further write."""
    gate = _NamedGate(["five-hour utilization at 96% (ceiling 90%)"])
    coord = _refusing_coord(make_coord, [True], gate=gate)
    ident = coord.submit_stage(_cold_build())
    submitted = record_of(coord, ident).revision

    coord.cycle("claude")
    assert record_of(coord, ident).refusal == "five-hour utilization at 96% (ceiling 90%)"
    assert record_of(coord, ident).revision == submitted + 1
    assert gate.lookups == 1

    coord.cycle("claude")
    assert record_of(coord, ident).revision == submitted + 1     # same reason, no second write
    assert gate.lookups == 2                                     # still evaluated every cycle


def test_a_reverted_pool_move_never_leaves_the_destinations_reason_on_the_home_record(make_coord):
    """A never-started Build may be probed against the other pool when its own cannot launch it.
    A probe that does not start reverts every moved field — the refusal included, or the record
    would sit on its home pool publishing a checkout problem from a pool it never moved to."""
    class _CodexIsFull:
        def __call__(self, record) -> bool:
            return record.pool == "claude"

        def deferral_reason(self, record):
            return "codex weekly allowance spent" if record.pool == "codex" else None

    class _OnlyCodexHasACheckout:
        """Prepares on the record's home pool and refuses on the destination, so the probe's own
        refusal is distinguishable from the one the home pool recorded."""

        def prepare(self, record):
            if record.pool == "codex":
                return True
            return unprepared("checkout-failed", "no destination checkout")

        def verify(self, record, obs) -> bool:
            return False

    coord = make_coord(adapter=StageRouter({"build": _OnlyCodexHasACheckout()}),
                       gate=_CodexIsFull(), launcher=NeverStartsLauncher())
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="build", pool="codex", complexity="deep",
        source="/work/.agentflow/worktrees/codex/issue-7-x"))

    coord.cycle("codex")
    assert record_of(coord, ident).refusal == "codex weekly allowance spent"

    coord.cycle("claude")                       # the destination probe refuses and reverts
    home = record_of(coord, ident)
    assert home.pool == "codex"
    assert home.refusal == "codex weekly allowance spent"


def test_optional_provider_capability_probe_never_holds_the_healthy_home_stage(make_coord):
    """A speculative destination check is non-finalizing even when capability is unavailable."""
    from agentflow.capability_contracts import CapabilityPreflightResult

    class _CodexIsFull:
        def __call__(self, record) -> bool:
            return record.pool == "claude"

        def deferral_reason(self, record):
            return "codex weekly allowance spent" if record.pool == "codex" else None

    checks = []

    def capability(record, materialize):
        checks.append((record.pool, materialize))
        if record.pool == "claude":
            return CapabilityPreflightResult(
                record.stage, record.pool, (), "missing", ("native receipt missing",), "repair")
        return None

    class _Ready:
        def prepare(self, _record):
            return True

        def verify(self, _record, _obs):
            return False

    coord = make_coord(
        adapter=StageRouter({"build": _Ready()}), gate=_CodexIsFull(),
        launcher=NeverStartsLauncher(), capability_preflight=capability)
    ident = coord.submit_stage(Submission(
        repo="o/r", subject="7", stage="build", pool="codex", complexity="deep",
        source="/work/.agentflow/worktrees/codex/issue-7-x"))

    coord.cycle("codex")
    coord.cycle("claude")

    home = record_of(coord, ident)
    assert checks == [("codex", False), ("codex", True), ("claude", False)]
    assert home.pool == "codex" and home.state == "waiting"
    assert not home.hold_pending and home.hold_reason is None
    assert home.claim and home.attempts == 0


def test_the_breadcrumb_cadence_is_quiet_once_then_periodic(make_coord):
    """A refusal that clears next cycle should page nobody; one that never clears should not
    print a line a tick either. Quiet on the first miss, one line on the second, then every
    tenth — Review's cadence, now every stage's."""
    answer = [unprepared("checkout-failed", "git worktree add exited 128")]
    lines: list[str] = []
    coord = _refusing_coord(make_coord, answer, log=lines.append)
    coord.submit_stage(_cold_build())

    printed = []
    for cycle in range(1, 13):
        coord.cycle("claude")
        printed.append(len([line for line in lines if "unprepared for" in line]))
    assert printed == [0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2]


def test_an_expected_refusal_publishes_without_ever_counting_or_printing(make_coord):
    """Which refusals are benign is the collaborator's call — it marks them, and the coordinator
    honors the mark without keeping a list of blessed check ids. An expected refusal stays on the
    board so the fleet is legible, and never touches the repeat counter."""
    answer = [unprepared("sibling-active", "a live sibling holds /wt", expected=True)]
    lines: list[str] = []
    coord = _refusing_coord(make_coord, answer, log=lines.append)
    ident = coord.submit_stage(_cold_build())

    for _ in range(12):
        coord.cycle("claude")
    assert [line for line in lines if "unprepared for" in line] == []
    assert record_of(coord, ident).refusal_expected is True
    published = tracer.refusal_projection(coord._store.load().values())
    assert len(published) == 1 and published[0]["expected"] is True


def test_a_legacy_bare_bool_preparation_stays_valid_and_carries_no_reason(make_coord):
    """Not every collaborator has to be converted at once. A plain False refuses exactly as it did
    — the record simply waits and retries with nothing to publish, and nothing to print either,
    since a breadcrumb with no check and no values would be pure noise."""
    lines: list[str] = []
    coord = _refusing_coord(make_coord, [False], log=lines.append)
    ident = coord.submit_stage(_cold_build())

    for _ in range(12):
        coord.cycle("claude")
    assert record_of(coord, ident).state == "waiting"
    assert record_of(coord, ident).refusal == ""
    assert tracer.refusal_projection(coord._store.load().values()) == []
    assert [line for line in lines if "unprepared for" in line] == []


# --- the published surfaces ---------------------------------------------------------------


def test_the_refusal_fields_default_so_records_written_before_this_change_still_load(coord_state):
    """The refusal fields remain defaulted across the safety-table schema migration."""
    from agentflow.coordinator.store import SCHEMA_VERSION, Store, default_store_path

    store = Store(default_store_path())
    try:
        legacy = json.loads(store._encode(Record(
            identity="o/r|7|build|-", stage="build", pool="claude", demand=5)))
        legacy.pop("refusal")
        legacy.pop("refusal_expected")
        restored = store._decode(json.dumps(legacy))
    finally:
        store.close()

    assert SCHEMA_VERSION == 5
    assert restored.refusal == "" and restored.refusal_expected is False


def test_verification_carries_only_the_two_facts_preparation_added():
    """One result type for both sides of the provider (ADR 0052). Preparation needed two facts
    verification did not have, both about a refusal's disposition — whether it is ordinary
    contention, and whether it is one only a human can clear — and nothing else. Neither is set
    unless a check says so, so an unclassified refusal escalates to nobody."""
    assert [f.name for f in fields(Verification)] == ["ok", "check", "detail", "expected",
                                                     "stall"]
    assert Verification(False, "x", "y").expected is False
    assert Verification(False, "x", "y").stall is False


def test_publishing_refusals_leaves_the_running_board_and_pool_counts_untouched(
        coord_state, monkeypatch):
    """Refusals ride in their own key. Folding them into the running rows would inflate every
    pool's running count with work that has not started and reserves nothing."""
    from agentflow import dashboard_data

    monkeypatch.setattr(dashboard_data, "pools",
                        lambda: [{"tool": "claude", "clear": True, "spent_pct": 4.0},
                                 {"tool": "codex", "clear": True, "spent_pct": 9.0}])
    live.replace_projection([{"repo": "o/r", "number": 7, "tool": "claude", "stage": "building"}])
    before = dashboard_data.snapshot([], dispatch_enabled=True)

    live.replace_refusals([
        {"repo": "o/r", "subject": "8", "stage": "review", "pool": "claude",
         "refusal": "checkout-failed: git worktree add exited 128", "expected": False}])
    after = dashboard_data.snapshot([], dispatch_enabled=True)

    assert after["running"] == before["running"]
    assert after["pools"] == before["pools"]
    assert before["refusals"] == []
    assert after["refusals"][0]["stage"] == "review"
    assert after["refusals"][0]["refusal"].startswith("checkout-failed: ")


def test_a_missing_or_corrupt_refusal_file_reads_as_nothing_refused(coord_state):
    """Derived state the console only displays: a half-written or absent file renders an empty
    board, never an error."""
    assert live.refusals() == []
    live.REFUSALS_FILE.write_text("{ not json")
    assert live.refusals() == []
