"""Unattended research as the coordinated ``research`` stage (ADR 0037), driven through the public
``submit_stage`` / ``cycle`` seam.

The daemon dispatches an unattended session to answer an open, unblocked, unclaimed
``wayfinder:research`` planning ticket. The proofs that matter: selection admits only AFK-able
research tickets (never another ``wayfinder:*`` type, a claimed ticket, or a blocked one) while the
build-intake wall stays up; the run's required outcome is a durable findings artifact; the
daemon-side finalizer is the *only* writer that resolves the ticket — findings comment, close, one
map breadcrumb, claim release — and does so idempotently across a replay; research reserves its own
stage lane/cap and shows up in the live board; and a dead run's shared claim is reclaimed.

The finalizer's GitHub reads/writes are faked (ADR 0020); the coordinator crash boundaries run
through the same public seam and real stage router that runs the live stages.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_research, dispatch, loop
from agentflow.coordinator import ResearchStageAdapter, StageRouter, tracer
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import HELD, RUNNING, Record
from agentflow.loop import RepoConfig

REPO = "o/r"


def _R(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


class FakeGitHub:
    """A stateful stand-in for the ticket, its parent Decision Map, and the shared claim. The
    finalizer's GitHub reads/writes are stated through the GitHub module's helpers (ADR 0040) —
    the typed ``comment``/``close``/``edit_body`` writes and the ``api`` escape hatch — never by
    matching a ``gh`` argument vector. ``run`` stands in for ``coordinated_research._run`` for the git worktree
    cleanup that remains loop-owned."""

    def __init__(self, *, state="OPEN", title="Audit the widget path",
                 labels=("wayfinder:research", "wayfinder:resolving"),
                 map_number=4, map_body="# Map\n\n## Decisions so far\n\n- earlier (#3).\n"):
        self.state = state
        self.title = title
        self.labels = list(labels)
        self.map_number = map_number
        self.map_body = map_body
        self.comments: list[dict] = []

    # --- GitHub module seam (ADR 0040) ------------------------------------------------
    def api(self, args, *, parse_json=False):
        # The two escape-hatch reads the finalizer still reaches through — routed by their leading
        # verb (a GraphQL parent-map lookup vs. an issue snapshot), never by matching a field vector.
        if args[0] == "api":                       # GraphQL parent-map read
            return {"data": {"repository": {"issue": {"parent": {
                "number": self.map_number, "body": self.map_body,
                "labels": {"nodes": [{"name": "wayfinder:map"}]}}}}}}
        return {"state": self.state, "title": self.title, "comments": list(self.comments),
                "url": f"https://github.com/{REPO}/issues/{args[2]}"}

    def comment(self, repo, number, body):
        self.comments.append({"body": body})
        return True

    def close(self, repo, number):
        self.state = "CLOSED"
        return True

    def edit_body(self, repo, number, body):       # the parent map's breadcrumb edit
        self.map_body = body
        return True

    def release(self, repo, number, _label):       # stands in for coordinated_research.release_claim
        if "wayfinder:resolving" in self.labels:
            self.labels.remove("wayfinder:resolving")
        return True

    def run(self, argv):                           # coordinated_research._run: only the git worktree cleanup remains
        assert argv and argv[0] == "git", f"unexpected non-git coordinated_research._run call: {argv}"
        return _R(0)

    def install(self, monkeypatch):
        from agentflow import github
        monkeypatch.setattr(github, "api", self.api)
        monkeypatch.setattr(github, "comment", self.comment)
        monkeypatch.setattr(github, "close", self.close)
        monkeypatch.setattr(github, "edit_body", self.edit_body)
        monkeypatch.setattr(coordinated_research, "release_claim", self.release)
        monkeypatch.setattr(coordinated_research, "_run", self.run)


def _adapter(fake):
    return ResearchStageAdapter(
        findings_ready=coordinated_research._findings_ready,
        resolve=coordinated_research.resolve,
        release=coordinated_research.release,
        observer=fake)


def _coord(make_coord, fake):
    router = StageRouter({"research": _adapter(fake)})
    return make_coord(fake, adapter=router, gate=tracer.build_review_revise_gate)


def _ticket(n=5, title="Audit the widget path"):
    return {"number": n, "title": title, "body": "why is X wired this way?",
            "labels": [{"name": "wayfinder:research"}, {"name": "wayfinder:resolving"}]}


def _write_findings(record, text):
    path = Path(coordinated_research.findings_path(record))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


# --- selection: only AFK-able research tickets, wall stays up ---------------------------

def test_research_eligible_refuses_every_non_research_type_and_a_claimed_ticket():
    """The regression that must fail if the AFK boundary ever leaks: only `wayfinder:research`
    is dispatchable, never grilling/prototype/task (even mixed onto a research label), and never a
    ticket already carrying the shared `wayfinder:resolving` claim (ADR 0037)."""
    assert loop._research_eligible({"labels": [{"name": "wayfinder:research"}]})
    for other in ("wayfinder:task", "wayfinder:grilling", "wayfinder:prototype"):
        assert not loop._research_eligible({"labels": [{"name": other}]})
        assert not loop._research_eligible(
            {"labels": [{"name": "wayfinder:research"}, {"name": other}]})
    assert not loop._research_eligible(
        {"labels": [{"name": "wayfinder:research"}, {"name": "wayfinder:resolving"}]})


def test_the_intake_wall_still_excludes_every_wayfinder_ticket():
    """ADR 0037 keeps ADR 0027's half: no `wayfinder:*` ticket is ever routed through build intake,
    so the research stage can never hand one an Agent Brief."""
    assert loop._untriaged({"number": 4, "labels": [{"name": "wayfinder:map"}]}) is False
    assert loop._untriaged({"number": 5, "labels": [{"name": "wayfinder:research"}]}) is False
    assert loop._untriaged({"number": 6, "labels": [{"name": "wayfinder:task"}]}) is False


def test_next_research_ticket_picks_the_oldest_eligible_unblocked_ticket(monkeypatch):
    listed = [
        _ticket(6, "already claimed"),                                   # carries resolving → skip
        {"number": 5, "title": "open", "body": "",
         "labels": [{"name": "wayfinder:research"}]},                     # the eligible one
    ]

    def run(argv):
        if argv[1:3] == ["issue", "list"]:
            assert argv[argv.index("--label") + 1] == "wayfinder:research"
            return _R(0, json.dumps(listed))
        if argv[1] == "api":                                             # no native blockers
            return _R(0, "[]")
        raise AssertionError(argv)

    monkeypatch.setattr(coordinated_research, "_run", run)
    monkeypatch.setattr("agentflow.github._run", run)
    picked = loop._next_research_ticket(RepoConfig(REPO, "/tmp"))
    assert picked["number"] == 5


def test_next_research_ticket_skips_a_ticket_with_an_open_native_blocker(monkeypatch):
    def run(argv):
        if argv[1:3] == ["issue", "list"]:
            return _R(0, json.dumps([{"number": 5, "title": "r", "body": "",
                                      "labels": [{"name": "wayfinder:research"}]}]))
        if argv[1] == "api":                                            # one blocked_by edge, same repo
            return _R(0, json.dumps([{"number": 3, "repository": {"full_name": REPO}}]))
        if argv[1:3] == ["issue", "view"]:                              # blocker #3 is still open
            return _R(0, '{"state":"OPEN"}')
        raise AssertionError(argv)

    monkeypatch.setattr(coordinated_research, "_run", run)
    monkeypatch.setattr("agentflow.github._run", run)
    assert loop._next_research_ticket(RepoConfig(REPO, "/tmp")) is None


def test_next_research_ticket_fails_closed_on_an_unreadable_blocker_graph(monkeypatch):
    def run(argv):
        if argv[1:3] == ["issue", "list"]:
            return _R(0, json.dumps([{"number": 5, "title": "r", "body": "",
                                      "labels": [{"name": "wayfinder:research"}]}]))
        if argv[1] == "api":                                            # blocked_by edges unreadable
            return _R(1, "")
        raise AssertionError(argv)

    monkeypatch.setattr(coordinated_research, "_run", run)
    monkeypatch.setattr("agentflow.github._run", run)
    assert loop._next_research_ticket(RepoConfig(REPO, "/tmp")) is None  # unreadable ≠ unblocked


# --- resolution: the finalizer is the single writer, and it is idempotent ---------------

def test_a_dispatched_ticket_ends_closed_with_findings_and_one_map_line(make_coord, coord_state,
                                                                        tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.install(monkeypatch)
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    cfg = RepoConfig(REPO, str(tmp_path / "wd"))
    ident = coord.submit_stage(coordinated_research.research_submission(cfg, _ticket(), "claude"))
    coord.cycle("claude")                                               # admit the run
    record = record_of(coord, ident)
    assert permits(coord, "claude") == 2                               # research (deep) reserves two

    _write_findings(record, "Investigated the widget path; decision: fold it into the router.")
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    coord.cycle("claude")                                              # settle → finalize resolves
    assert record_of(coord, ident).retired is True

    assert gh.state == "CLOSED"
    findings = [c for c in gh.comments if "agentflow-research-findings" in c["body"]]
    assert len(findings) == 1
    assert "fold it into the router" in findings[0]["body"]
    assert "wayfinder:resolving" not in gh.labels                      # shared claim released
    assert coordinated_research.decision_present(gh.map_body, 5)       # one titled breadcrumb
    assert gh.map_body.count("(#5)") == 1

    # A restart re-observes the retired record and never resolves a second time.
    _coord(make_coord, fake).cycle("claude")
    assert len([c for c in gh.comments if "research-findings" in c["body"]]) == 1
    assert gh.map_body.count("(#5)") == 1


def test_resolution_replays_without_a_duplicate_comment_or_map_line(tmp_path, monkeypatch):
    """A crash between the comment and the map edit (or after the ticket is closed) must not
    double-write: replaying the finalizer over the already-closed ticket is a no-op."""
    gh = FakeGitHub()
    gh.install(monkeypatch)
    record = SimpleNamespace(repo=REPO, subject="5", source=str(tmp_path / "wt"))
    _write_findings(record, "the finding and its decision")

    assert coordinated_research.resolve(record) is not None
    assert coordinated_research.resolve(record) is not None            # replay
    assert coordinated_research.resolve(record) is not None            # and again

    assert gh.state == "CLOSED"
    assert len([c for c in gh.comments if "research-findings" in c["body"]]) == 1
    assert gh.map_body.count("(#5)") == 1
    assert "wayfinder:resolving" not in gh.labels


def test_exhaustion_releases_the_claim_so_the_ticket_is_eligible_again(make_coord, coord_state,
                                                                       tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.install(monkeypatch)
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    cfg = RepoConfig(REPO, str(tmp_path / "wd"))
    ident = coord.submit_stage(coordinated_research.research_submission(cfg, _ticket(), "claude"))

    outcome = None
    for _ in range(8):
        settled = coord.cycle("claude")
        if settled:
            outcome = settled[0]
            break
        fake.end(ident, cause=ProviderCause.PROCESS)                   # never records findings
    assert outcome is not None and outcome.status == "held"
    assert outcome.handoff == "ticket:claim-released"
    assert gh.state == "OPEN"                                          # not resolved — just released
    assert "wayfinder:resolving" not in gh.labels                     # eligible again next cycle


# --- capacity: research reserves its own lane/cap and shows in the live board -----------

def test_research_reserves_its_own_stage_lane_and_cap():
    from agentflow.coordinated_build import _ProductionGate
    limits = _ProductionGate.reservation_limits(
        Record(identity="i", stage="research", pool="claude", demand=2, repo=REPO, subject="5"))
    assert limits.stage_lane == "research"                             # distinct lane, not build/triage
    assert limits.stage_cap == 1
    assert limits.lane_by_stage["research"] == "research"


def test_a_running_research_record_appears_in_the_live_board():
    running = Record(identity="i", stage="research", pool="claude", demand=2, repo=REPO,
                     subject="5", state=RUNNING, started_at=100, family="900001",
                     source="/w/.agentflow/worktrees/claude/research-5")
    entries = tracer.live_projection([running])
    assert len(entries) == 1
    assert entries[0]["number"] == 5 and entries[0]["stage"] == "resolving"


# --- crash recovery: a dead run's shared claim is reclaimed -----------------------------

def test_a_dead_research_run_releases_the_resolving_claim_a_live_one_retains_it(monkeypatch):
    from agentflow import coordinated_build

    dead = Record(identity="dead", stage="research", pool="claude", demand=2, repo=REPO,
                  subject="5", state=HELD, claim=False)
    live = Record(identity="live", stage="research", pool="claude", demand=2, repo=REPO,
                  subject="6", state=RUNNING, claim=True)
    monkeypatch.setattr(coordinated_build.tracer, "load_records", lambda: [dead, live])
    edited = []
    from agentflow import github

    # The claim lanes are listed in order (building, triaging, drawing, resolving); only the
    # resolving lane holds the two research-claimed issues. The proof read shows the label gone.
    listings = iter([[], [], [], [{"number": 5, "updated_at": "2020-01-01T00:00:00Z"},
                                   {"number": 6, "updated_at": "2020-01-01T00:00:00Z"}]])
    monkeypatch.setattr(github, "api", lambda args, *, parse_json=False: next(listings))
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: edited.append(issue) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert coordinated_build.reconcile_orphaned_claims(RepoConfig(REPO, "/tmp")) == 1
    assert edited == [5]                                               # dead run released; live retained


# --- map breadcrumb helpers (pure) ------------------------------------------------------

def test_map_decision_append_is_idempotent_and_stays_in_section():
    body = ("# Map\n\n## Decisions so far\n\n- earlier (#3).\n\n"
            "## Open questions\n\n- something\n")
    assert not coordinated_research.decision_present(body, 5)
    line = coordinated_research.decision_line("Audit the widget path", 5)
    updated = coordinated_research.with_decision(body, line)
    assert coordinated_research.decision_present(updated, 5)
    assert updated.count("(#5)") == 1
    # the breadcrumb lands inside 'Decisions so far', above the next section
    assert updated.index("(#5)") < updated.index("## Open questions")


def test_with_decision_creates_the_section_when_absent():
    updated = coordinated_research.with_decision("# Map\n\nno section here\n", "- x (#9).")
    assert "## Decisions so far" in updated and "(#9)" in updated


# --- dispatch: claim the shared label before submitting -------------------------------

def test_research_dispatch_claims_then_enters_the_coordinator(monkeypatch):
    monkeypatch.setattr(loop, "_next_research_ticket",
                        lambda cfg, _log=None: {"number": 5, "title": "r", "body": "",
                                                "labels": [{"name": "wayfinder:research"}]})
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr("agentflow.coordinated_research.research_map_context",
                        lambda repo, n: "")
    events = []
    monkeypatch.setattr(dispatch, "claim",
                        lambda repo, n, _label: events.append("claim") or True)
    coord = SimpleNamespace(submit_stage=lambda s: events.append(s.stage))

    assert "submitted" in dispatch._submit_coordinated_research(
        RepoConfig(REPO, "/tmp"), coord, None)
    assert events == ["claim", "research"]                            # claim visible before submission


# --- worktree provisioning: the run gets a real repo to investigate ---------------------

def _repo_with_origin_main(tmp_path):
    from agentflow.loop import _run
    origin, workdir = tmp_path / "origin.git", tmp_path / "checkout"
    _run(["git", "init", "--bare", "-b", "main", str(origin)]).check_returncode()
    _run(["git", "clone", str(origin), str(workdir)]).check_returncode()
    _run(["git", "-C", str(workdir), "config", "user.email", "t@t"]).check_returncode()
    _run(["git", "-C", str(workdir), "config", "user.name", "t"]).check_returncode()
    (workdir / "README.md").write_text("the repo")
    _run(["git", "-C", str(workdir), "add", "-A"]).check_returncode()
    _run(["git", "-C", str(workdir), "commit", "-m", "init"]).check_returncode()
    _run(["git", "-C", str(workdir), "push", "-q", "origin", "main"]).check_returncode()
    return str(workdir)


def test_a_research_run_provisions_a_detached_worktree_and_reuses_it_on_resume(tmp_path):
    """prepare() materializes the run's isolated worktree — a detached ``origin/main`` checkout — so
    the session has a real repo to read and a place to write findings; a second prepare reuses it as
    it is, keeping partial findings a resumed run already wrote."""
    cfg = RepoConfig(REPO, _repo_with_origin_main(tmp_path))
    sub = coordinated_research.research_submission(cfg, _ticket(), "claude")
    adapter = ResearchStageAdapter(
        findings_ready=coordinated_research._findings_ready,
        worktree_ready=coordinated_research._research_worktree_ready)
    assert adapter.prepare(sub) is True
    wt = Path(sub.source)
    assert (wt / ".git").exists() and (wt / "README.md").read_text() == "the repo"
    _write_findings(sub, "partial findings")
    assert adapter.prepare(sub) is True                                # resume reuses as-is
    assert coordinated_research.read_findings(sub) == "partial findings"


def test_a_research_run_defers_when_its_worktree_cannot_be_provisioned(tmp_path):
    cfg = RepoConfig(REPO, str(tmp_path / "not-a-repo"))
    sub = coordinated_research.research_submission(cfg, _ticket(), "claude")
    adapter = ResearchStageAdapter(
        findings_ready=coordinated_research._findings_ready,
        worktree_ready=coordinated_research._research_worktree_ready)
    assert adapter.prepare(sub) is False


def test_research_dispatch_refuses_submission_when_the_claim_cannot_be_set(monkeypatch):
    monkeypatch.setattr(loop, "_next_research_ticket",
                        lambda cfg, _log=None: {"number": 5, "title": "r", "body": "",
                                                "labels": [{"name": "wayfinder:research"}]})
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr("agentflow.coordinated_research.research_map_context",
                        lambda repo, n: "")
    monkeypatch.setattr(dispatch, "claim", lambda repo, n, _label: False)
    coord = SimpleNamespace(submit_stage=lambda s: (_ for _ in ()).throw(
        AssertionError("must not submit without the claim")))

    assert "could not claim" in dispatch._submit_coordinated_research(
        RepoConfig(REPO, "/tmp"), coord, None)
