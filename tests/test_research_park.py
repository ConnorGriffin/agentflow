"""Research exhaustion parks the ticket where the operator can see it (issue #362).

An unattended run that spends its whole recovery budget without recording a ruling the disposition
contract accepts used to drop its shared claim in silence: nothing was said on the ticket, and the
daemon then re-claimed it every cycle for a session that never started. Exhaustion is now the
research stage's own operator-facing handoff — one comment naming the check that failed, one durable
label that takes the ticket out of unattended selection, and the claim released.

These tests fail on the pre-#362 code: `release` posted nothing, labelled nothing, and dispatch
stamped a claim for a terminal record.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import coordinated_research, loop
from agentflow.labels import AWAITING_DISPOSITION, RESEARCH_PARKED, RESEARCH_TICKET, RESOLVING


def _R(returncode=0, stdout=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout)


def _record(tmp_path, number=7):
    """A held research record whose source points at the run's real worktree directory."""
    wt = tmp_path / ".agentflow" / "worktrees" / "claude" / f"research-{number}"
    wt.mkdir(parents=True)
    return SimpleNamespace(repo="o/r", subject=str(number), pool="claude", source=str(wt))


def _plant(record, text):
    """Plant the findings artifact the exhausted session left behind."""
    findings = Path(coordinated_research.findings_path(record))
    findings.parent.mkdir(parents=True, exist_ok=True)
    findings.write_text(text)


# An artifact that reads like real research but carries no ruling at all — the ordinary exhaustion
# shape: the session wrote up what it found and never produced the machine-checkable section.
_NO_DISPOSITION = "## Findings\n\nThe widget path is reached from three call sites.\n"


class _FakeTicket:
    """The ticket, its labels, and its comments, stated through the GitHub module's helpers
    (ADR 0040) — never by matching a `gh` argument vector. Individual writes can be made to fail so
    a half-applied park can be observed converging on a later pass."""

    def __init__(self, number=7):
        self.number = number
        self.state = "OPEN"
        self.title = "Audit the widget path"
        self.comments: list[str] = []
        self.labels = [RESEARCH_TICKET, RESOLVING]
        self.git_calls: list[list[str]] = []
        self.fail_comment = False
        self.fail_add_label = False
        self.fail_release = False

    def issue_view(self, repo, number):
        from agentflow import github
        return github.IssueView(
            title=self.title, body="", state=self.state,
            url=f"https://github.com/o/r/issues/{self.number}",
            labels=frozenset(self.labels),
            comments=[github.Comment(body=b, created_at="") for b in self.comments])

    def comment(self, repo, number, body):
        if self.fail_comment:
            return False
        self.comments.append(body)
        return True

    def create_label(self, repo, label, color, description=""):
        return True

    def add_label(self, repo, number, label):
        if self.fail_add_label:
            return False
        if label not in self.labels:
            self.labels.append(label)
        return True

    def issue_labels(self, repo, number):
        return frozenset(self.labels)

    def release(self, repo, number, label):        # stands in for coordinated_research.release_claim
        if self.fail_release:
            return False
        if label in self.labels:
            self.labels.remove(label)
        return True

    def run(self, argv):                           # coordinated_research._run: worktree cleanup only
        assert argv and argv[0] == "git", f"unexpected non-git call: {argv}"
        self.git_calls.append(list(argv))
        return _R(0)

    @property
    def worktree_removals(self):
        return [c for c in self.git_calls if "worktree" in c and "remove" in c]

    @property
    def park_comments(self):
        return [c for c in self.comments if "agentflow-research-park:#" in c]

    def install(self, monkeypatch):
        from agentflow import github
        monkeypatch.setattr(github, "issue_view", self.issue_view)
        monkeypatch.setattr(github, "comment", self.comment)
        monkeypatch.setattr(github, "create_label", self.create_label)
        monkeypatch.setattr(github, "add_label", self.add_label)
        monkeypatch.setattr(github, "issue_labels", self.issue_labels)
        monkeypatch.setattr(coordinated_research, "release_claim", self.release)
        monkeypatch.setattr(coordinated_research, "_run", self.run)


# --- the park itself ------------------------------------------------------------------

def test_exhausted_research_parks_the_ticket_visibly(tmp_path, monkeypatch):
    """The whole visible outcome: open ticket, one comment, park label, no claim."""
    record = _record(tmp_path)
    _plant(record, _NO_DISPOSITION)
    ticket = _FakeTicket()
    ticket.install(monkeypatch)

    proof = coordinated_research.park(record)

    assert proof is not None, "a converged park must return durable proof"
    assert ticket.state == "OPEN", "a park must never close the ticket"
    assert RESEARCH_PARKED in ticket.labels
    assert RESOLVING not in ticket.labels, "the shared claim must be released"
    assert len(ticket.park_comments) == 1


def test_the_park_comment_says_what_it_is_and_what_happens_next(tmp_path, monkeypatch):
    record = _record(tmp_path)
    _plant(record, _NO_DISPOSITION)
    ticket = _FakeTicket()
    ticket.install(monkeypatch)

    coordinated_research.park(record)
    body = ticket.park_comments[0]

    assert "unattended session (AI)" in body, "the research disclaimer must front the comment"
    assert "<!-- agentflow-research-park:#7 -->" in body, "the dedup marker must be present"
    assert "will not try this ticket again" in body, "the comment must say there is no retry"
    assert "The widget path is reached from three call sites." in body, \
        "the recorded findings must be carried over so the evidence is not lost"


@pytest.mark.parametrize("artifact,expected", [
    ("## Findings\n\nProse only.\n",
     "no `## Disposition` section"),
    ("## Disposition\n\n```json\n{\"disposition\":\"no_build\",\"summary\":\"The widget path is reached from three call sites.\"}"
     "\n```\n\n## Disposition\n\n```json\n{}\n```\n",
     "2 `## Disposition` sections"),
    ("## Disposition\n\nno_build, probably.\n",
     "not exactly one fenced `json` block"),
    ("## Disposition\n\n```json\n{\"disposition\": oops}\n```\n",
     "not valid JSON"),
    ("## Disposition\n\n```json\n{\"disposition\":\"no_build\",\"summary\":\"Too short.\"}\n```\n",
     "shorter than 12 characters"),
    ("## Disposition\n\n```json\n{\"disposition\":\"no_build\",\"summary\":\"The widget path is reached from three call sites.\","
     "\"trigger\":\"Something observable happens here.\"}\n```\n",
     "must carry exactly `disposition`, `summary`"),
    ("## Disposition\n\n```json\n{\"disposition\":\"deferred\",\"summary\":\"The widget path is reached from three call sites.\","
     "\"trigger\":\"when ready\",\"verification\":\"The widget counter reaches ten.\"}\n```\n",
     "trigger did not name an observable event"),
    ("## Disposition\n\n```json\n{\"disposition\":\"build_it\",\"summary\":\"The widget path is reached from three call sites.\"}"
     "\n```\n",
     "must be one of `no_build`"),
    ("## Disposition\n\n```json\n{\"disposition\":\"no_build\",\"summary\":\"The widget path is reached from three call sites.\","
     "\"summary\":\"Second one.\"}\n```\n",
     "named the same field more than once"),
])
def test_the_park_comment_names_the_check_that_failed(tmp_path, monkeypatch, artifact, expected):
    """Not a generic 'unusable disposition' — the specific reason the artifact was refused, so the
    maintainer knows whether to rewrite the question or just the ruling."""
    record = _record(tmp_path)
    _plant(record, artifact)
    ticket = _FakeTicket()
    ticket.install(monkeypatch)

    assert coordinated_research.park(record) is not None
    assert expected in ticket.park_comments[0]


def test_a_run_that_recorded_nothing_is_parked_too(tmp_path, monkeypatch):
    """Exhaustion with no artifact at all is the other half of the case — it must still be said."""
    record = _record(tmp_path)          # no findings planted
    ticket = _FakeTicket()
    ticket.install(monkeypatch)

    proof = coordinated_research.park(record)

    assert proof is not None
    assert "recorded no findings at all" in ticket.park_comments[0]
    assert RESEARCH_PARKED in ticket.labels


def test_a_usable_ruling_reaching_the_park_says_the_run_was_held_first(tmp_path, monkeypatch):
    """A parseable ruling belongs to resolve(), so reaching the park with one means the run was
    held before it could be recorded. The comment must say that, not invent a rejected check."""
    record = _record(tmp_path)
    _plant(record, "## Disposition\n\n```json\n{\"disposition\":\"no_build\","
                   "\"summary\":\"The widget path already routes through the shared router.\"}\n```\n")
    ticket = _FakeTicket()
    ticket.install(monkeypatch)

    assert coordinated_research.park(record) is not None
    assert "held before the daemon could record the ruling" in ticket.park_comments[0]


def test_parking_twice_leaves_one_comment_and_one_label(tmp_path, monkeypatch):
    """Crash-replay safety: the second park re-proves the first rather than repeating it."""
    record = _record(tmp_path)
    _plant(record, _NO_DISPOSITION)
    ticket = _FakeTicket()
    ticket.install(monkeypatch)

    first = coordinated_research.park(record)
    second = coordinated_research.park(record)

    assert first == second, "a replayed park must re-prove the same result"
    assert len(ticket.park_comments) == 1
    assert ticket.labels.count(RESEARCH_PARKED) == 1


def test_the_park_removes_the_run_worktree(tmp_path, monkeypatch):
    """Nothing will ever resume this run, so the retained worktree is only a leak now."""
    record = _record(tmp_path)
    _plant(record, _NO_DISPOSITION)
    ticket = _FakeTicket()
    ticket.install(monkeypatch)

    assert coordinated_research.park(record) is not None
    assert ticket.worktree_removals, "the isolated worktree must be removed on a park"


# --- proof is withheld until the whole park is durable ----------------------------------

def test_no_proof_while_the_comment_cannot_be_posted(tmp_path, monkeypatch):
    record = _record(tmp_path)
    _plant(record, _NO_DISPOSITION)
    ticket = _FakeTicket()
    ticket.fail_comment = True
    ticket.install(monkeypatch)

    assert coordinated_research.park(record) is None
    assert RESEARCH_PARKED not in ticket.labels


def test_no_proof_while_the_park_label_cannot_be_read_back(tmp_path, monkeypatch):
    """A half-applied park must be retried, not recorded as done — the one-hour orphan-claim grace
    means the flap would otherwise only show up an hour later."""
    record = _record(tmp_path)
    _plant(record, _NO_DISPOSITION)
    ticket = _FakeTicket()
    ticket.fail_add_label = True
    ticket.install(monkeypatch)

    assert coordinated_research.park(record) is None
    assert RESOLVING in ticket.labels, "the claim must not be dropped by an unfinished park"

    # The next pass converges: one comment total, and the park is now proved.
    ticket.fail_add_label = False
    proof = coordinated_research.park(record)

    assert proof is not None
    assert len(ticket.park_comments) == 1
    assert RESEARCH_PARKED in ticket.labels
    assert RESOLVING not in ticket.labels


def test_no_proof_while_the_claim_survives(tmp_path, monkeypatch):
    record = _record(tmp_path)
    _plant(record, _NO_DISPOSITION)
    ticket = _FakeTicket()
    ticket.fail_release = True
    ticket.install(monkeypatch)

    assert coordinated_research.park(record) is None
    assert not ticket.worktree_removals, "an unproved park must not tear down the run"


# --- selection: a parked ticket is out of the unattended queue --------------------------

def _issue(*labels):
    return {"number": 7, "labels": [{"name": n} for n in labels]}


def test_a_parked_ticket_is_never_selected_again():
    assert loop._research_eligible(_issue(RESEARCH_TICKET)) is True
    assert loop._research_eligible(_issue(RESEARCH_TICKET, RESEARCH_PARKED)) is False
    # The two settled states stay distinct: awaiting-disposition means research succeeded.
    assert loop._research_eligible(_issue(RESEARCH_TICKET, AWAITING_DISPOSITION)) is False


# --- dispatch: no claim, and no false "submitted" line, for a run that will not start ----

class _TerminalCoordinator:
    """A coordinator whose stable research identity already points at a terminal held record — the
    state an exhausted, parked run leaves behind."""

    def __init__(self):
        self.withdrawn: list[str] = []

    def submit_stage(self, submission):
        return f"{submission.repo}:{submission.subject}:{submission.stage}"

    def stage_record(self, identity):
        return SimpleNamespace(state="held", hold_pending=False, retired=False)

    def withdraw_stage(self, identity):
        self.withdrawn.append(identity)


def test_dispatch_claims_nothing_for_a_ticket_whose_run_already_parked(tmp_path, monkeypatch):
    """The phantom loop: dispatch used to stamp the shared claim before submitting, so a terminal
    record produced an hourly claim/reclaim flap and a log line saying a session had started."""
    from agentflow import dispatch

    cfg = SimpleNamespace(repo="o/r", workdir=str(tmp_path))
    ticket = {"number": 7, "title": "Audit the widget path", "body": "",
              "labels": [{"name": RESEARCH_TICKET}]}
    claims: list[tuple] = []

    monkeypatch.setattr(loop, "_next_research_ticket", lambda _cfg, _log=None: ticket)
    monkeypatch.setattr(dispatch, "pick_pair",
                        lambda: (SimpleNamespace(tool="claude"), SimpleNamespace(tool="codex"), ""))
    monkeypatch.setattr(dispatch, "claim",
                        lambda *a: claims.append(a) or True)
    monkeypatch.setattr(coordinated_research, "research_map_context", lambda *a, **k: "")

    coordinator = _TerminalCoordinator()
    report = dispatch._submit_coordinated_research(cfg, coordinator, lambda _m: None)

    assert claims == [], "a terminal record must never be claimed"
    assert "submitted to coordinator" not in report, \
        "no session started, so the log must not say one did"
    assert "parked" in report


# --- the disposition contract itself is untouched ---------------------------------------

_PARSEABLE = [
    '{"disposition":"no_build","summary":"The widget path already routes through the shared router."}',
    '{"disposition":"deferred","summary":"The rollout waits on the new counter.",'
    '"trigger":"The widget counter reaches ten thousand.",'
    '"verification":"The dashboard shows the counter above ten thousand."}',
    '{"disposition":"handoff_required","summary":"The widget path exposes one independently shippable build.",'
    '"candidates":[{"title":"Route widgets through the shared router",'
    '"build":"Replace the widget-only path with the shared router."}]}',
]


@pytest.mark.parametrize("payload", _PARSEABLE)
def test_artifacts_that_parse_today_still_parse(payload):
    findings = f"## Findings\n\nSome prose.\n\n## Disposition\n\n```json\n{payload}\n```\n"
    assert coordinated_research.parse_disposition(findings) is not None
    assert coordinated_research.rejection_reason(findings) is None, \
        "a usable ruling has no rejection reason to give"


@pytest.mark.parametrize("payload", [
    '{"disposition":"no_build","summary":"Maybe later."}',
    '{"disposition":"no_build"}',
    '{"disposition":"deferred","summary":"The rollout waits on something.",'
    '"trigger":"The widget counter reaches ten thousand.",'
    '"verification":"the widget counter reaches ten thousand."}',
    '{"disposition":"handoff_required","summary":"The finding needs disposition.","candidates":[]}',
    '["not","an","object"]',
])
def test_artifacts_that_fail_today_still_fail_and_now_say_why(payload):
    findings = f"## Findings\n\nSome prose.\n\n## Disposition\n\n```json\n{payload}\n```\n"
    assert coordinated_research.parse_disposition(findings) is None
    reason = coordinated_research.rejection_reason(findings)
    assert reason and not reason.endswith("."), "reasons read as one clause the comment punctuates"
