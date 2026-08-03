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

import pytest

from conftest import FakeSession, permits, record_of

from agentflow import coordinated_research, dispatch, loop, pipeline
from agentflow.coordinator import ResearchStageAdapter, StageRouter, tracer
from agentflow.coordinator.providers import ProviderCause
from agentflow.coordinator.record import HELD, RUNNING, WAITING, Record
from agentflow.loop import RepoConfig

REPO = "o/r"


def _R(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


class FakeGitHub:
    """A stateful stand-in for the ticket, its parent Decision Map, and the shared claim. The
    finalizer's GitHub reads/writes are stated through the GitHub module's helpers (ADR 0040) —
    the typed ``issue_view`` read, the typed ``comment``/``close``/``edit_body`` writes and the
    ``api`` escape hatch — never by matching a ``gh`` argument vector. ``run`` stands in for ``coordinated_research._run`` for the git worktree
    cleanup that remains loop-owned."""

    def __init__(self, *, state="OPEN", title="Audit the widget path",
                 labels=("wayfinder:research", "wayfinder:resolving"),
                 map_number=4, map_body="# Map\n\n## Decisions so far\n\n- earlier (#3).\n",
                 fail_once_at=None):
        self.state = state
        self.title = title
        self.labels = list(labels)
        self.map_number = map_number
        self.map_body = map_body
        self.comments: list[dict] = []
        self.fail_once_at = fail_once_at
        self.failed = False
        self.mutations: list[str] = []

    def _response(self, boundary):
        if self.fail_once_at == boundary and not self.failed:
            self.failed = True
            return False
        return True

    # --- GitHub module seam (ADR 0040) ------------------------------------------------
    def api(self, args, *, parse_json=False):
        # The one escape-hatch read the finalizer still reaches through: the GraphQL parent-map
        # lookup, which no typed single-fact method covers.
        assert args[0] == "api", f"unexpected escape-hatch call: {args}"
        return {"data": {"repository": {"issue": {"parent": {
            "number": self.map_number, "body": self.map_body,
            "labels": {"nodes": [{"name": "wayfinder:map"}]}}}}}}

    def issue_view(self, repo, number):
        from agentflow import github
        return github.IssueView(
            title=self.title, body="", state=self.state,
            url=f"https://github.com/{REPO}/issues/{number}",
            labels=frozenset(self.labels),
            comments=[github.Comment(body=c["body"], created_at="") for c in self.comments])

    def issue_comments(self, repo, number):
        from agentflow import github
        return [github.Comment(body=c["body"], created_at="") for c in self.comments]

    def issue_url(self, repo, number):             # the release's durable proof-of-release
        return f"https://github.com/{REPO}/issues/{number}"

    def comment(self, repo, number, body):
        self.comments.append({"body": body})
        self.mutations.append("comment")
        return self._response("comment")

    def close(self, repo, number):
        if self.state != "CLOSED":
            self.state = "CLOSED"
            self.mutations.append("close")
        return self._response("close")

    def edit_body(self, repo, number, body):       # the parent map's breadcrumb edit
        if self.map_body != body:
            self.map_body = body
            self.mutations.append("map")
        return self._response("map")

    def add_label(self, repo, number, label):
        if label not in self.labels:
            self.labels.append(label)
            self.mutations.append("label")
        return self._response("label")

    def create_label(self, repo, label, color, description=""):
        return self._response("label")

    def issue_labels(self, repo, number):
        return frozenset(self.labels)

    def release(self, repo, number, _label):       # stands in for coordinated_research.release_claim
        if "wayfinder:resolving" in self.labels:
            self.labels.remove("wayfinder:resolving")
            self.mutations.append("release")
        return self._response("release")

    def run(self, argv):                           # coordinated_research._run: only the git worktree cleanup remains
        assert argv and argv[0] == "git", f"unexpected non-git coordinated_research._run call: {argv}"
        return _R(0)

    def install(self, monkeypatch):
        from agentflow import github
        monkeypatch.setattr(github, "api", self.api)
        monkeypatch.setattr(github, "issue_view", self.issue_view)
        monkeypatch.setattr(github, "issue_comments", self.issue_comments)
        monkeypatch.setattr(github, "issue_url", self.issue_url)
        monkeypatch.setattr(github, "comment", self.comment)
        monkeypatch.setattr(github, "close", self.close)
        monkeypatch.setattr(github, "edit_body", self.edit_body)
        monkeypatch.setattr(coordinated_research, "release_claim", self.release)
        monkeypatch.setattr(coordinated_research, "_run", self.run)
        monkeypatch.setattr(github, "create_label", self.create_label)
        monkeypatch.setattr(github, "add_label", self.add_label)
        monkeypatch.setattr(github, "issue_labels", self.issue_labels)


def _adapter(fake):
    return ResearchStageAdapter(
        findings_ready=coordinated_research._findings_ready,
        resolve=coordinated_research.resolve,
        park=coordinated_research.park,
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


def _artifact(kind, summary, **details):
    payload = {"disposition": kind, "summary": summary, **details}
    return (
        f"## Findings\n\nInvestigated the widget path.\n\n"
        f"## Disposition\n\n```json\n{json.dumps(payload)}\n```\n"
    )


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
    assert not loop._research_eligible(
        {"labels": [{"name": "wayfinder:research"},
                    {"name": "wayfinder:awaiting-disposition"}]})


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

@pytest.mark.parametrize("artifact", [
    "Findings without a disposition.",
    (
        "## Disposition\n\n```json\n"
        '{"disposition":"no_build","summary":"No implementation is warranted for this path."}'
        "\n```\n\n## Disposition\n\n```json\n"
        '{"disposition":"no_build","summary":"A second ruling must make the artifact invalid."}'
        "\n```\n"
    ),
    "## Disposition\n\n```json\n{not valid json}\n```\n",
    (
        "## Disposition\n\n```json\n"
        '{"disposition":"handoff_required","disposition":"no_build",'
        '"summary":"Conflicting rulings must not collapse into the last JSON key."}'
        "\n```\n"
    ),
    _artifact("no_build", "No implementation work is needed."),
    _artifact("no_build", "The answer requires no implementation change."),
    _artifact("no_build", "No build is needed for the overall project direction."),
    _artifact(
        "handoff_required",
        "The widget audit exposes a candidate that needs operator judgment.",
        candidates=[{
            "title": "A build is required",
            "build": "Some implementation work is needed",
        }],
    ),
    _artifact(
        "deferred",
        "The widget route may become useful after upstream work.",
        trigger="A meaningful future event occurs.",
        verification="Confirm that the meaningful event occurred.",
    ),
    _artifact(
        "deferred",
        "The widget route may become useful after upstream work.",
        trigger="maybe later",
        verification="Check whether the upstream route exists in the published schema.",
    ),
    _artifact(
        "deferred",
        "The widget route may become useful after upstream work.",
        trigger="When the team decides it is time.",
        verification="Check whether the upstream route exists in the published schema.",
    ),
    _artifact(
        "deferred",
        "The widget route may become useful after upstream work.",
        trigger="The upstream schema publishes the shared widget route.",
        verification="Ask the team what they think about it later.",
    ),
])
def test_an_invalid_disposition_stays_within_the_research_recovery_budget(
        artifact, make_coord, coord_state, tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.install(monkeypatch)
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    cfg = RepoConfig(REPO, str(tmp_path / "wd"))
    ident = coord.submit_stage(coordinated_research.research_submission(cfg, _ticket(), "claude"))
    coord.cycle("claude")
    record = record_of(coord, ident)
    original_map = gh.map_body
    _write_findings(record, artifact)

    fake.end(ident, cause=ProviderCause.PROCESS)
    assert coord.cycle("claude") == []

    current = record_of(coord, ident)
    assert current.retired is False
    assert current.state != "completed"
    assert gh.state == "OPEN"
    assert gh.comments == []
    assert gh.map_body == original_map


def test_no_build_fails_closed_when_its_existing_comment_does_not_carry_the_ruling(
        tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.comments.append({
        "body": coordinated_research._findings_marker(5) + "\n\nOlder findings without the ruling.",
    })
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    pings = []
    from agentflow import notify
    monkeypatch.setattr(notify, "notify", lambda *args: pings.append(args) or True)
    _write_findings(record, _artifact(
        "no_build",
        "The existing router already covers the widget path, so no implementation is warranted.",
    ))

    assert coordinated_research.resolve(record) is None
    assert gh.state == "OPEN"
    assert not coordinated_research.decision_present(gh.map_body, 5)
    assert gh.mutations == []
    assert pings == []


def test_handoff_required_fails_closed_when_its_existing_comment_has_different_findings(
        tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.comments.append({
        "body": coordinated_research._findings_marker(5) + "\n\nOlder, different findings.",
    })
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    pings = []
    from agentflow import notify
    monkeypatch.setattr(notify, "notify", lambda *args: pings.append(args) or True)
    _write_findings(record, _artifact(
        "handoff_required",
        "The widget path exposes one independently shippable build.",
        candidates=[{
            "title": "Route widgets through the shared router",
            "build": "Replace the widget-only dispatch path with the shared router.",
        }],
    ))

    assert coordinated_research.resolve(record) is None
    assert gh.state == "OPEN"
    assert "wayfinder:awaiting-disposition" not in gh.labels
    assert "wayfinder:resolving" in gh.labels
    assert gh.mutations == []
    assert pings == []


def test_no_build_fails_closed_when_the_map_contains_a_different_ruling(
        tmp_path, monkeypatch):
    gh = FakeGitHub(map_body=(
        "# Map\n\n## Decisions so far\n\n"
        "- **Audit the widget path** — no build: An older, different ruling. (#5).\n"
    ))
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    _write_findings(record, _artifact(
        "no_build",
        "The existing router already covers the widget path, so no implementation is warranted.",
    ))

    assert coordinated_research.resolve(record) is None
    assert gh.state == "OPEN"
    assert gh.map_body.count("(#5)") == 1
    assert "wayfinder:resolving" in gh.labels


def test_a_handoff_result_retires_the_run_but_parks_the_ticket_open(make_coord, coord_state,
                                                                    tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.install(monkeypatch)
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    cfg = RepoConfig(REPO, str(tmp_path / "wd"))
    ident = coord.submit_stage(coordinated_research.research_submission(cfg, _ticket(), "claude"))
    coord.cycle("claude")
    record = record_of(coord, ident)
    _write_findings(record, _artifact(
        "handoff_required",
        "The widget path exposes one independently shippable build.",
        candidates=[{
            "title": "Route widgets through the shared router",
            "build": "Replace the widget-only dispatch path with the shared router.",
        }, {
            "title": "Remove the retired widget dispatcher",
            "build": "Delete the independently removable widget-only dispatch path.",
        }],
    ))

    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    coord.cycle("claude")

    assert record_of(coord, ident).retired is True
    assert gh.state == "OPEN"
    assert "wayfinder:awaiting-disposition" in gh.labels
    assert "wayfinder:resolving" not in gh.labels
    assert "## Awaiting disposition" in gh.map_body
    assert "Audit the widget path" in gh.map_body
    assert gh.map_body.count("(#5)") == 1
    assert not coordinated_research.decision_present(gh.map_body, 5)
    assert "Remove the retired widget dispatcher" in gh.comments[0]["body"]


def test_a_concrete_defer_closes_with_its_trigger_and_verification_on_the_map(
        make_coord, coord_state, tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.install(monkeypatch)
    fake = FakeSession()
    coord = _coord(make_coord, fake)
    cfg = RepoConfig(REPO, str(tmp_path / "wd"))
    ident = coord.submit_stage(coordinated_research.research_submission(cfg, _ticket(), "claude"))
    coord.cycle("claude")
    record = record_of(coord, ident)
    _write_findings(record, _artifact(
        "deferred",
        "The widget route depends on an upstream schema capability that does not exist yet.",
        trigger="The published upstream schema adds a versioned widget route.",
        verification="Confirm the route in the upstream schema and its generated client.",
    ))

    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    coord.cycle("claude")

    assert record_of(coord, ident).retired is True
    assert gh.state == "CLOSED"
    assert "deferred: The widget route depends on an upstream schema capability" in gh.map_body
    assert "Trigger: The published upstream schema adds a versioned widget route." in gh.map_body
    assert "Verification: Confirm the route in the upstream schema" in gh.map_body
    assert "resolved by unattended research" not in gh.map_body


def test_ciq_autotune_469_through_472_keep_build_findings_open_and_close_the_evidence_gate(
        tmp_path, monkeypatch):
    fixtures = [
        (469, _artifact(
            "handoff_required",
            "The audit exposes one independently shippable cache build.",
            candidates=[{"title": "Index the cache result",
                         "build": "Persist the cache result in the ordinary result index."}],
        )),
        (470, _artifact(
            "handoff_required",
            "The audit exposes one independently shippable matching build.",
            candidates=[{"title": "Match the initial pump result",
                         "build": "Make initial pump matching use the settled ranking rule."}],
        )),
        (471, _artifact(
            "deferred",
            "No current build is justified until the direction-only evidence gate opens.",
            trigger="A completed trial records direction-only evidence for the affected profile.",
            verification="Confirm the evidence in the durable trial result and profile history.",
        )),
        (472, _artifact(
            "handoff_required",
            "The audit exposes two independently shippable builds.",
            candidates=[
                {"title": "Surface the first independent recommendation",
                 "build": "Deliver the first recommendation without depending on the second."},
                {"title": "Surface the second independent recommendation",
                 "build": "Deliver the second recommendation without depending on the first."},
            ],
        )),
    ]
    states = []
    maps = []

    for number, artifact in fixtures:
        gh = FakeGitHub(title=f"ciq-autotune research {number}")
        gh.install(monkeypatch)
        record = SimpleNamespace(
            identity=f"{REPO}:{number}:research", repo=REPO, subject=str(number),
            source=str(tmp_path / f"wt-{number}"))
        _write_findings(record, artifact)

        assert coordinated_research.resolve(record) is not None
        states.append(gh.state)
        maps.append(gh.map_body)
        if number == 472:
            assert "first independent recommendation" in gh.comments[0]["body"]
            assert "second independent recommendation" in gh.comments[0]["body"]

    assert states == ["OPEN", "OPEN", "CLOSED", "OPEN"]
    assert all("## Awaiting disposition" in maps[index] for index in (0, 1, 3))
    assert "deferred: No current build is justified" in maps[2]
    assert all("resolved by unattended research" not in body for body in maps)


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

    _write_findings(record, _artifact(
        "no_build",
        "The existing router already covers the widget path, so no implementation is warranted.",
    ))
    fake.end(ident, cause=ProviderCause.PROCESS)
    assert [o.status for o in coord.cycle("claude")] == ["completed"]
    coord.cycle("claude")                                              # settle → finalize resolves
    assert record_of(coord, ident).retired is True

    assert gh.state == "CLOSED"
    findings = [c for c in gh.comments if "agentflow-research-findings" in c["body"]]
    assert len(findings) == 1
    assert "no implementation is warranted" in findings[0]["body"]
    assert "wayfinder:resolving" not in gh.labels                      # shared claim released
    assert coordinated_research.decision_present(gh.map_body, 5)       # one titled breadcrumb
    assert gh.map_body.count("(#5)") == 1
    assert "no build: The existing router already covers the widget path" in gh.map_body
    assert "resolved by unattended research" not in gh.map_body

    # A restart re-observes the retired record and never resolves a second time.
    _coord(make_coord, fake).cycle("claude")
    assert len([c for c in gh.comments if "research-findings" in c["body"]]) == 1
    assert gh.map_body.count("(#5)") == 1


def test_resolution_replays_without_a_duplicate_comment_or_map_line(tmp_path, monkeypatch):
    """A crash between the comment and the map edit (or after the ticket is closed) must not
    double-write: replaying the finalizer over the already-closed ticket is a no-op."""
    gh = FakeGitHub()
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    pings = []
    from agentflow import notify
    monkeypatch.setattr(notify, "notify", lambda *args: pings.append(args) or True)
    _write_findings(record, _artifact(
        "no_build",
        "The existing router already covers the widget path, so no implementation is warranted.",
    ))

    assert coordinated_research.resolve(record) is not None
    assert coordinated_research.resolve(record) is not None            # replay
    assert coordinated_research.resolve(record) is not None            # and again

    assert gh.state == "CLOSED"
    assert len([c for c in gh.comments if "research-findings" in c["body"]]) == 1
    assert gh.map_body.count("(#5)") == 1
    assert "wayfinder:resolving" not in gh.labels
    assert pings == []


@pytest.mark.parametrize("boundary", ["comment", "map", "close", "release"])
def test_no_build_replay_converges_after_each_durable_write(boundary, tmp_path, monkeypatch):
    gh = FakeGitHub(fail_once_at=boundary)
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    _write_findings(record, _artifact(
        "no_build",
        "The existing router already covers the widget path, so no implementation is warranted.",
    ))

    assert coordinated_research.resolve(record) is None
    assert coordinated_research.resolve(record) is not None

    assert gh.state == "CLOSED"
    assert gh.mutations.count("comment") == 1
    assert gh.mutations.count("map") == 1
    assert gh.mutations.count("close") == 1
    assert gh.mutations.count("release") == 1
    assert gh.map_body.count("(#5)") == 1


@pytest.mark.parametrize("boundary", ["comment", "map", "label", "release"])
def test_pending_replay_converges_after_each_durable_write(boundary, tmp_path, monkeypatch):
    gh = FakeGitHub(fail_once_at=boundary)
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    _write_findings(record, _artifact(
        "handoff_required",
        "The widget path exposes one independently shippable build.",
        candidates=[{
            "title": "Route widgets through the shared router",
            "build": "Replace the widget-only dispatch path with the shared router.",
        }],
    ))

    first = coordinated_research.resolve(record)
    if boundary == "comment":
        assert first is not None, "the envelope re-read proves that the reported-failed write landed"
    else:
        assert first is None
    assert coordinated_research.resolve(record) is not None

    assert gh.state == "OPEN"
    assert gh.mutations.count("comment") == 1
    assert gh.mutations.count("map") == 1
    assert gh.mutations.count("label") == 1
    assert gh.mutations.count("release") == 1
    assert gh.map_body.count("(#5)") == 1
    assert "wayfinder:awaiting-disposition" in gh.labels
    assert "wayfinder:resolving" not in gh.labels


def test_handoff_required_resolution_notifies_with_stable_ticket_context(tmp_path, monkeypatch):
    gh = FakeGitHub()
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    _write_findings(record, _artifact(
        "handoff_required",
        "The widget path exposes one independently shippable build.",
        candidates=[{
            "title": "Route widgets through the shared router",
            "build": "Replace the widget-only dispatch path with the shared router.",
        }],
    ))
    pings = []
    from agentflow import notify
    monkeypatch.setattr(notify, "notify", lambda *args: pings.append(args) or True)
    adapter = ResearchStageAdapter(
        findings_ready=coordinated_research._findings_ready,
        resolve=coordinated_research.resolve,
    )

    assert adapter.finalize_completed(record) == "https://github.com/o/r/issues/5"
    assert len(pings) == 1
    assert pings[0][:3] == (
        "agentflow needs you",
        "o/r #5: Research findings await disposition",
        "https://github.com/o/r/issues/5",
    )
    assert pings[0][3]

    assert adapter.finalize_completed(record) == "https://github.com/o/r/issues/5"
    assert len([c for c in gh.comments if "agentflow-research-findings" in c["body"]]) == 1
    assert len(pings) == 2
    assert pings[1] == pings[0]


def test_pending_resolution_replaces_a_stale_untitled_map_entry(tmp_path, monkeypatch):
    gh = FakeGitHub(map_body=(
        "# Map\n\n## Awaiting disposition\n\n"
        "- awaiting operator disposition (#5)\n"
        "- **Audit the widget path** — awaiting operator disposition (#5).\n"
    ))
    gh.install(monkeypatch)
    record = SimpleNamespace(
        identity=f"{REPO}:5:research", repo=REPO, subject="5", source=str(tmp_path / "wt"))
    _write_findings(record, _artifact(
        "handoff_required",
        "The widget path exposes one independently shippable build.",
        candidates=[{
            "title": "Route widgets through the shared router",
            "build": "Replace the widget-only dispatch path with the shared router.",
        }],
    ))

    assert coordinated_research.resolve(record) is not None

    assert gh.map_body.count("(#5)") == 1
    assert (
        "- **Audit the widget path** — awaiting operator disposition (#5)."
        in gh.map_body
    )


def test_exhaustion_parks_the_ticket_for_a_human_instead_of_releasing_it_silently(
        make_coord, coord_state, tmp_path, monkeypatch):
    """Exhaustion is the research stage's own operator-facing handoff (ADR 362). It used to drop
    the shared claim and say nothing, which left the ticket looking untouched while no later
    unattended attempt could ever run."""
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
    assert outcome.handoff == "ticket:parked"
    assert gh.state == "OPEN"                                          # never closed, never judged
    assert "wayfinder:parked" in gh.labels                             # out of unattended selection
    assert "wayfinder:resolving" not in gh.labels                      # the shared claim is released
    parked = [c for c in gh.comments if "agentflow-research-park" in c["body"]]
    assert len(parked) == 1
    assert "recorded no findings at all" in parked[0]["body"]
    assert "will not try this ticket again" in parked[0]["body"]


# --- capacity: research reserves its own lane/cap and shows in the live board -----------

def test_research_reserves_its_own_stage_lane_and_cap():
    from agentflow.pipeline import _ProductionGate
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
    monkeypatch.setattr(pipeline.tracer, "load_records", lambda: [dead, live])
    edited = []
    from agentflow import github

    # The claim lanes are listed in order (building, triaging, drawing, resolving); only the
    # resolving lane holds the two research-claimed issues. The proof read shows the label gone.
    listings = iter([[], [], [], [github.ClaimedIssue(5, "2020-01-01T00:00:00Z"),
                                  github.ClaimedIssue(6, "2020-01-01T00:00:00Z")]])
    monkeypatch.setattr(github, "claimed_issues", lambda repo, label: next(listings))
    monkeypatch.setattr(github, "remove_label",
                        lambda repo, issue, label: edited.append(issue) or True)
    monkeypatch.setattr(github, "issue_labels", lambda repo, issue: frozenset())

    assert pipeline.reconcile_orphaned_claims(RepoConfig(REPO, "/tmp")) == 1
    assert edited == [5]                                               # dead run released; live retained


# --- map breadcrumb helpers (pure) ------------------------------------------------------

def test_map_decision_append_is_idempotent_and_stays_in_section():
    body = ("# Map\n\n## Decisions so far\n\n- earlier (#3).\n\n"
            "## Open questions\n\n- something\n")
    assert not coordinated_research.decision_present(body, 5)
    line = coordinated_research.decision_line(
        "Audit the widget path",
        5,
        coordinated_research.ResearchDisposition(
            kind="no_build",
            summary="The existing router already covers the widget path.",
        ),
    )
    updated = coordinated_research.with_decision(body, line)
    assert coordinated_research.decision_present(updated, 5)
    assert updated.count("(#5)") == 1
    # the breadcrumb lands inside 'Decisions so far', above the next section
    assert updated.index("(#5)") < updated.index("## Open questions")


def test_with_decision_creates_the_section_when_absent():
    updated = coordinated_research.with_decision("# Map\n\nno section here\n", "- x (#9).")
    assert "## Decisions so far" in updated and "(#9)" in updated


# --- dispatch: submit, inspect the record, then claim only a runnable run ----------------

def _dispatch_ticket(monkeypatch):
    monkeypatch.setattr(loop, "_next_research_ticket",
                        lambda cfg, _log=None: {"number": 5, "title": "r", "body": "",
                                                "labels": [{"name": "wayfinder:research"}]})
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda: (SimpleNamespace(tool="claude"), None, ""))
    monkeypatch.setattr("agentflow.coordinated_research.research_map_context",
                        lambda repo, n: "")


class _FakeCoordinator:
    """Just enough coordinator for dispatch: a submission resolves to whatever record the stable
    (repo, ticket, research) identity already points at, and a withdrawal is observable."""

    def __init__(self, record):
        self.record = record
        self.events: list[str] = []
        self.withdrawn: list[str] = []

    def submit_stage(self, submission):
        self.events.append(submission.stage)
        return f"{submission.repo}:{submission.subject}:{submission.stage}"

    def stage_record(self, _identity):
        return self.record

    def withdraw_stage(self, identity):
        self.withdrawn.append(identity)


def test_research_dispatch_submits_then_claims_a_runnable_run(monkeypatch):
    _dispatch_ticket(monkeypatch)
    coord = _FakeCoordinator(
        SimpleNamespace(state=WAITING, hold_pending=False, retired=False))
    monkeypatch.setattr(dispatch, "claim",
                        lambda repo, n, _label: coord.events.append("claim") or True)

    assert "submitted" in dispatch._submit_coordinated_research(
        RepoConfig(REPO, "/tmp"), coord, None)
    assert coord.events == ["research", "claim"]   # the record is inspected before it is claimed
    assert coord.withdrawn == []


def test_research_dispatch_claims_nothing_when_the_ticket_is_already_parked(monkeypatch):
    """A parked ticket's stable identity resolves to a terminal held record, so nothing new was
    created to run. Claiming there stamped a shared claim for a session that never started, which
    orphan reconciliation stripped an hour later and the next cycle restamped forever (ADR 362)."""
    _dispatch_ticket(monkeypatch)
    coord = _FakeCoordinator(
        SimpleNamespace(state=HELD, hold_pending=False, retired=False))
    monkeypatch.setattr(dispatch, "claim", lambda repo, n, _label: (_ for _ in ()).throw(
        AssertionError("a terminal record must never be claimed")))

    report = dispatch._submit_coordinated_research(RepoConfig(REPO, "/tmp"), coord, None)

    assert "parked" in report
    assert "submitted to coordinator" not in report   # no session started; the log must not say one did


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


def test_research_dispatch_withdraws_the_submission_when_the_claim_cannot_be_set(monkeypatch):
    """A runnable submission whose claim mutation fails leaves no unowned research work behind."""
    _dispatch_ticket(monkeypatch)
    coord = _FakeCoordinator(
        SimpleNamespace(state=WAITING, hold_pending=False, retired=False))
    monkeypatch.setattr(dispatch, "claim", lambda repo, n, _label: False)

    assert "could not claim" in dispatch._submit_coordinated_research(
        RepoConfig(REPO, "/tmp"), coord, None)
    assert coord.withdrawn == [f"{REPO}:5:research"]
