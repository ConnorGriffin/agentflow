"""Test the plan audit through its interface — the build queue's selection, the fail-safe
verdict parser, and the projection each verdict makes on GitHub (ADR 380).

The point of the step is that a brief nobody re-read never reaches a builder, so the load-bearing
assertions here are the *negative* ones: an un-audited ready issue is not dispatchable, and
anything we cannot read as a confident countersign bounces to the maintainer rather than falling
through to a build.

The GitHub-touching tests state facts through the shared `github` module's interface (ADR 0040) —
a canned typed read, or a recorded typed write — never a `gh` argument vector.
"""

import pytest

from agentflow import intake as intake_mod
from agentflow import loop as loop_mod
from agentflow import plan_audit
from agentflow.coordinator.admission import ADMISSION_MATRIX, STAGE_CAPS, admission_demand
from agentflow.coordinator.plan_audit_stage import decode_result, encode_result
from agentflow.coordinator.profiles import WITHHELD_EDIT_TOOLS, profile_for
from agentflow.coordinator.tracer import CLAIM_LANE, ENABLED_STAGES
from agentflow.labels import _CLAIM_LABELS
from agentflow.github import Comment, IssueRow, IssueView
from agentflow.intake import COUNTERSIGNED, IntakeRoute, apply_intake, intake_result_is_durable
from agentflow.loop import RepoConfig, _audit_pending, _next_audit_candidate, _next_ready_issue
from agentflow.plan_audit import (PlanAuditVerdict, bounce_body, bounce_result, countersigned,
                                  parse_plan_audit, plan_audit_prompt)
from agentflow.worktree_ref import WorktreeKind, WorktreeRef

READY = "ready-for-agent"
GRILLING = "agentflow:needs-grilling"
TRIAGING = "agentflow:triaging"
BUILDING = "agentflow:building"
AUDITING = "agentflow:auditing"
DIALS = ["agentflow:complexity:deep", "agentflow:effort:high"]


# --- the verdict parser: fail-safe toward the bounce ---------------------------------------

def test_countersign_parses():
    v = parse_plan_audit('{"verdict": "countersign", "objections": ""}')
    assert v.verdict is PlanAuditVerdict.COUNTERSIGN and v.parsed


def test_bounce_carries_its_objections():
    v = parse_plan_audit('{"verdict": "bounce", "objections": "1. The premise is wrong."}')
    assert v.verdict is PlanAuditVerdict.BOUNCE and v.parsed
    assert v.objections == "1. The premise is wrong."


@pytest.mark.parametrize("payload", [
    "",
    "   ",
    "null",
    '["countersign"]',
    "not json at all",
    '{"objections": "1. no verdict field"}',
    '{"verdict": "looks-fine", "objections": "1. an invented verdict"}',
    'Here you go:\n```json\n{"verdict": "countersign", "objections": ""}\n```',
])
def test_an_unreadable_verdict_bounces_and_never_countersigns(payload):
    # THE fail-safe direction. An audit we cannot read is not an audit: it must never leave the
    # issue silently dispatchable, and it must never be mistaken for a countersign — not even
    # when a countersign is the thing buried in the prose we refused to scavenge.
    v = parse_plan_audit(payload)
    assert v.verdict is PlanAuditVerdict.BOUNCE
    assert v.parsed is False
    assert v.objections.strip(), "a bounce must give the maintainer something to answer"


def test_a_bounce_with_no_objections_still_bounces_with_something_answerable():
    v = parse_plan_audit('{"verdict": "bounce", "objections": "   "}')
    assert v.verdict is PlanAuditVerdict.BOUNCE and v.parsed is False
    assert "did not come back" in v.objections


def test_a_countersign_discards_stray_objection_text():
    # A countersign posts nothing, so text written beside it has nowhere to go and must not be
    # carried around as if it were a finding.
    v = parse_plan_audit('{"verdict": "countersign", "objections": "1. some musing"}')
    assert v.objections == ""


def test_the_verdict_survives_a_durable_round_trip():
    original = parse_plan_audit('{"verdict": "bounce", "objections": "1. unverifiable claim"}')
    assert decode_result(encode_result(original)) == original


# --- the bounce comment must read as ours ---------------------------------------------------

def test_the_bounce_comment_carries_our_marker():
    # The held-issue sweep decides "the maintainer answered" by looking for our marker. Objections
    # posted without it would read as a maintainer reply and re-run triage instantly, in a loop
    # with nobody in it.
    body = bounce_body("1. The brief claims a function exists; it does not.")
    assert intake_mod.INTAKE_MARK in body
    assert intake_mod.awaiting_recheck([{"body": body}]) is False


def test_the_bounce_is_projected_as_a_grilling_route_that_never_retitles():
    result = bounce_result(parse_plan_audit(
        '{"verdict": "bounce", "objections": "1. unverifiable"}'))
    assert result.route is IntakeRoute.GRILL
    assert result.title == "", "the audit judges the plan; it never rewrites it"
    assert "1. unverifiable" in result.body


# --- the build queue only takes countersigned briefs -----------------------------------------

class FakeQueueGH:
    """Stand-in for the `github` reads queue selection makes: a canned issue listing, no open
    PRs, and no blocker edges — so the only thing under test is the countersign gate."""

    def __init__(self, issues):
        self._issues = issues

    def list_issues(self, repo, *, label=None, limit=100):
        return self._issues

    def list_open_prs(self, repo, limit=100):
        return []

    def api(self, argv, parse_json=False):
        return []          # no native blocked-by edges

    def issue_state(self, repo, number):
        return "CLOSED"


def _install_queue(monkeypatch, *issues):
    fake = FakeQueueGH(list(issues))
    for name in ("list_issues", "list_open_prs", "api", "issue_state"):
        monkeypatch.setattr(loop_mod.github, name, getattr(fake, name))
    return fake


def _row(number, *labels, title="a ready issue"):
    return IssueRow(number=number, title=title, body="", labels=set(labels))


_CFG = RepoConfig(repo="owner/repo", workdir="/tmp/does-not-matter")


def test_an_unaudited_ready_issue_is_not_dispatchable(monkeypatch):
    # The keystone. Before the plan audit existed this issue dispatched straight to a builder;
    # now a brief nobody re-read is simply not in the queue.
    _install_queue(monkeypatch, _row(41, READY, *DIALS))
    assert _next_ready_issue(_CFG) is None


def test_a_countersigned_ready_issue_is_dispatchable(monkeypatch):
    _install_queue(monkeypatch, _row(41, READY, COUNTERSIGNED, *DIALS))
    picked = _next_ready_issue(_CFG)
    assert picked is not None and picked["number"] == 41


def test_countersigned_issues_still_dispatch_oldest_first(monkeypatch):
    # The gate filters the queue; it must not reorder what survives it.
    _install_queue(monkeypatch,
                   _row(70, READY, COUNTERSIGNED),
                   _row(41, READY),                    # un-audited — skipped, not merely deferred
                   _row(55, READY, COUNTERSIGNED))
    assert _next_ready_issue(_CFG)["number"] == 55


@pytest.mark.parametrize("claim", [AUDITING, TRIAGING])
def test_an_issue_a_read_only_session_owns_is_not_dispatched_to_build(monkeypatch, claim):
    # An audit in flight holds its own claim and has stamped no countersign yet, so the build
    # queue cannot pick the issue up underneath it; nor can it while triage still owns it.
    _install_queue(monkeypatch, _row(41, READY, claim, *DIALS))
    assert _next_ready_issue(_CFG) is None


# --- the audit queue: one audit per un-audited ready issue ------------------------------------

def test_the_audit_queue_takes_the_oldest_unaudited_ready_issue(monkeypatch):
    _install_queue(monkeypatch, _row(70, READY), _row(41, READY, COUNTERSIGNED), _row(55, READY))
    assert _next_audit_candidate(_CFG)["number"] == 55


def test_the_audit_queue_skips_an_issue_a_session_already_owns():
    # Not double-claiming is the whole reason this predicate exists.
    assert _audit_pending({"labels": [{"name": READY}]}) is True
    assert _audit_pending({"labels": [{"name": READY}, {"name": TRIAGING}]}) is False
    assert _audit_pending({"labels": [{"name": READY}, {"name": BUILDING}]}) is False
    assert _audit_pending({"labels": [{"name": READY}, {"name": COUNTERSIGNED}]}) is False


def test_the_audit_queue_reserves_issues_already_taken_this_cycle(monkeypatch):
    _install_queue(monkeypatch, _row(41, READY), _row(55, READY))
    assert _next_audit_candidate(_CFG, reserved={41})["number"] == 55


# --- the projection each verdict makes --------------------------------------------------------

class FakeGH:
    """Records the typed writes and serves canned typed reads, like `tests/test_intake.py`."""

    def __init__(self, *, body="", comment_bodies=(), issue=None):
        self._read_body = body
        self._comment_bodies = comment_bodies
        self._durability_issue = issue
        self.added: list[str] = []
        self.removed: list[str] = []
        self.created: list[str] = []
        self.title = None
        self.written_body = None
        self.posted_comment = None

    def issue_body(self, repo, issue):
        return self._read_body

    def issue_comments(self, repo, issue):
        return [Comment(body=b, created_at="") for b in self._comment_bodies]

    def issue_view(self, repo, issue):
        return self._durability_issue

    def list_issues(self, repo, *, label=None, limit=100):
        return []

    def create_label(self, repo, name, color, description=""):
        self.created.append(name)
        return True

    def add_label(self, repo, issue, label):
        self.added.append(label)
        return True

    def remove_label(self, repo, issue, label):
        self.removed.append(label)
        return True

    def edit_title(self, repo, issue, title):
        self.title = title
        return True

    def edit_body(self, repo, issue, body):
        self.written_body = body
        return True

    def comment(self, repo, issue, body):
        self.posted_comment = body
        return True


_GH_NAMES = ("issue_body", "issue_comments", "list_issues", "issue_view", "create_label",
             "add_label", "remove_label", "edit_title", "edit_body", "comment")


def _install(monkeypatch, fake):
    for name in _GH_NAMES:
        monkeypatch.setattr(intake_mod.github, name, getattr(fake, name))
    return fake


def test_a_bounce_leaves_the_build_queue_and_holds_for_the_maintainer(monkeypatch):
    fake = _install(monkeypatch, FakeGH())
    bounced = bounce_result(parse_plan_audit(
        '{"verdict": "bounce", "objections": "1. The premise does not hold."}'))
    apply_intake("owner/repo", 380, "a ready issue", [READY, *DIALS], bounced)

    assert GRILLING in fake.added
    assert READY in fake.removed, "a bounced brief must leave the build queue"
    assert set(DIALS) <= set(fake.removed), "a bounced brief carries no dials"
    assert fake.posted_comment is not None
    assert "1. The premise does not hold." in fake.posted_comment
    assert intake_mod.INTAKE_MARK in fake.posted_comment
    assert fake.title is None and fake.written_body is None


def test_a_countersign_marker_is_cleared_when_the_issue_is_re_triaged(monkeypatch):
    # Requirement: a countersign belongs to the brief that earned it. An issue bounced to grilling
    # and later promoted to ready again must be audited fresh, not inherit the old verdict.
    from agentflow.runner import Complexity, Effort
    from agentflow.intake import IntakeResult

    fake = _install(monkeypatch, FakeGH(body="as filed"))
    promoted = IntakeResult(IntakeRoute.READY, "## Agent Brief\nthe new plan", "",
                            Complexity.DEEP, Effort.HIGH)
    apply_intake("owner/repo", 380, "a ready issue", [GRILLING, COUNTERSIGNED], promoted)
    assert COUNTERSIGNED in fake.removed


def test_a_promotion_to_ready_still_proves_durable_with_no_countersign_yet(monkeypatch):
    # Triage's own durability proof runs *before* the audit ever stamps its marker, so the marker
    # being absent at that moment must remain perfectly normal.
    from agentflow.runner import Complexity, Effort
    from agentflow.intake import IntakeResult, compose_ready_body

    brief = "## Agent Brief\nthe plan"
    composed = compose_ready_body(brief, "as filed")
    result = IntakeResult(IntakeRoute.READY, brief, "", Complexity.DEEP, Effort.HIGH)
    view = IssueView(title="a ready issue", body=composed, state="OPEN",
                     url="https://github.com/owner/repo/issues/380",
                     labels={READY, "agentflow:complexity:deep", "agentflow:effort:high"},
                     comments=[Comment(body=intake_mod._READY_COMMENT, created_at="")])
    _install(monkeypatch, FakeGH(issue=view))
    assert intake_result_is_durable("owner/repo", 380, result, "a ready issue", "as filed")


def test_a_stale_countersign_is_a_managed_label_the_proof_rejects(monkeypatch):
    # The mechanism behind the requirement above: the marker is managed, so a projection that
    # left one behind is not durable and is repaired rather than accepted.
    from agentflow.runner import Complexity, Effort
    from agentflow.intake import IntakeResult, compose_ready_body

    brief = "## Agent Brief\nthe plan"
    result = IntakeResult(IntakeRoute.READY, brief, "", Complexity.DEEP, Effort.HIGH)
    view = IssueView(title="a ready issue", body=compose_ready_body(brief, "as filed"),
                     state="OPEN", url="https://github.com/owner/repo/issues/380",
                     labels={READY, "agentflow:complexity:deep", "agentflow:effort:high",
                             COUNTERSIGNED},
                     comments=[Comment(body=intake_mod._READY_COMMENT, created_at="")])
    _install(monkeypatch, FakeGH(issue=view))
    assert not intake_result_is_durable("owner/repo", 380, result, "a ready issue", "as filed")


def test_countersigned_reads_the_marker_off_a_label_set():
    assert countersigned({READY, COUNTERSIGNED}) is True
    assert countersigned({READY}) is False


# --- the session the audit runs in -------------------------------------------------------------

class _Record:
    def __init__(self, stage, complexity=None, effort=None):
        self.stage = stage
        self.complexity = complexity
        self.effort = effort
        self.builder_complexity = None


def test_the_audit_runs_read_only_under_intakes_ceiling():
    profile = profile_for(_Record("audit"))
    assert profile.read_only
    assert not set(WITHHELD_EDIT_TOOLS) & set(profile.allowed_tools)
    assert profile.wall_ceiling_s == 20 * 60 and profile.turn_ceiling == 40
    assert profile_for(_Record("intake")) == profile, "the audit is intake-shaped"


def test_the_audit_takes_one_permit_on_either_pool():
    assert admission_demand("audit", "claude", "opus", "deep") == 1
    assert admission_demand("audit", "codex", "sol", "deep") == 1
    assert ("audit", "claude", "opus", "deep", None) in ADMISSION_MATRIX


def test_the_audit_holds_its_own_lane_and_cap_beside_triage():
    # Intake-shaped, but never contending with intake for the same slots: the audit is what
    # stands between a settled brief and a builder, so a busy triage queue must not stall it.
    from agentflow.pipeline import _ProductionGate

    limits = _ProductionGate.reservation_limits(_Record("audit"))
    assert limits.stage_lane == "audit"
    assert limits.stage_cap == STAGE_CAPS["audit"]
    assert _ProductionGate.reservation_limits(_Record("intake")).stage_lane == "triage"


def test_the_audit_is_an_enabled_stage_on_its_own_claim():
    # Its own claim label, never Intake's and never Build's: the two other lanes own an issue at
    # different moments, and a shared label would let one lane's live record shield the other's
    # stale claim from reclamation.
    assert "audit" in ENABLED_STAGES
    assert CLAIM_LANE["audit"] == "auditing"
    assert CLAIM_LANE["audit"] not in (CLAIM_LANE["intake"], CLAIM_LANE["build"])
    assert AUDITING in _CLAIM_LABELS, "the claim needs a colour and a description like any lane"


def test_the_reconciler_can_reclaim_a_stranded_audit_claim(monkeypatch):
    # A claim label no lane reconciles is a claim that strands an issue forever.
    from agentflow import pipeline as pipeline_mod

    seen = []
    monkeypatch.setattr(pipeline_mod.tracer, "load_records", lambda: [])
    monkeypatch.setattr(pipeline_mod.github, "claimed_issues",
                        lambda repo, label: seen.append(label) or [])
    pipeline_mod.reconcile_orphaned_claims(RepoConfig("o/r", "/tmp"))
    assert AUDITING in seen


def test_the_audit_checkout_is_not_mistakable_for_the_intake_checkout():
    audit = WorktreeRef.for_plan_audit("/w", "claude", 380)
    intake = WorktreeRef.for_intake("/w", "claude", 380)
    assert audit.path != intake.path and audit.branch != intake.branch
    assert WorktreeRef.parse(audit.path) == audit
    assert WorktreeRef.parse(audit.path).kind is WorktreeKind.AUDIT


@pytest.mark.parametrize("ref", [
    WorktreeRef.for_build("/w", "claude", 380, "a-slug"),
    WorktreeRef.for_review("/w", "codex", 12, "a-slug"),
    WorktreeRef.for_intake("/w", "claude", 380),
    WorktreeRef.for_research("/w", "codex", 9),
    WorktreeRef.for_mockup("/w", "claude", 7, "a-slug"),
    WorktreeRef.for_converse("/w", "codex", "token"),
    WorktreeRef.for_plan_audit("/w", "claude", 380),
])
def test_every_checkout_kind_still_round_trips(ref):
    assert WorktreeRef.parse(ref.path) == ref


# --- a re-audit is a different audit ----------------------------------------------------------

def test_a_rewritten_brief_is_a_different_audit():
    # The stall this prevents: an issue bounces, the maintainer answers, triage writes a *new*
    # brief and promotes it back to ready — and the second audit silently reuses the first one's
    # retired record, stamps no countersign, and leaves the issue sitting ready-for-agent forever,
    # never audited and never built. The audit's identity has to move when the plan does.
    from agentflow.coordinated_plan_audit import brief_fingerprint

    first = {"number": 380, "title": "a ready issue", "body": "## Agent Brief\nthe first plan"}
    rewritten = {"number": 380, "title": "a ready issue", "body": "## Agent Brief\nthe new plan"}
    retitled = {"number": 380, "title": "a rescoped issue", "body": first["body"]}

    assert brief_fingerprint(first) != brief_fingerprint(rewritten)
    assert brief_fingerprint(first) != brief_fingerprint(retitled)
    assert brief_fingerprint(first) == brief_fingerprint(dict(first)), \
        "the same brief is the same audit — resubmitting it must not open a second one"


def test_a_re_promoted_issue_opens_a_fresh_audit_and_is_dispatchable_again(monkeypatch):
    # The same stall, end to end and through the interface that actually decides it. After the
    # bounce → answer → re-scope round trip the issue is ready again with its countersign
    # cleared: it must be *audit-pending* (so an audit is submitted at all) and that audit must
    # carry a different record identity from the retired first one (so the submission is
    # runnable rather than a reused terminal record that stamps nothing forever).
    from agentflow import coordinated_plan_audit as audit_mod

    monkeypatch.setattr(audit_mod, "_run",
                        lambda argv: type("R", (), {"returncode": 0, "stdout": "abc123\n"})())
    first = {"number": 380, "title": "a ready issue", "body": "the first plan",
             "labels": [{"name": READY}, {"name": COUNTERSIGNED}]}
    re_promoted = {"number": 380, "title": "a ready issue", "body": "the re-scoped plan",
                   "labels": [{"name": READY}]}          # the re-route cleared the countersign

    assert _audit_pending(first) is False
    assert _audit_pending(re_promoted) is True
    assert (audit_mod.plan_audit_submission(_CFG, re_promoted, "claude").target
            != audit_mod.plan_audit_submission(_CFG, first, "claude").target)


def test_a_hand_edited_brief_keeps_its_countersign(monkeypatch):
    # A maintainer editing the brief by hand is a human authorizing it, and the countersign
    # attests to a plan a human is free to change. Only an intake re-route invalidates it —
    # anything that noticed the edit would be body-hash attestation, which this pipeline
    # deliberately does not build.
    edited = {"number": 380, "title": "a ready issue", "body": "the plan, with my edit",
              "labels": [{"name": READY}, {"name": COUNTERSIGNED}]}
    _install_queue(monkeypatch, _row(380, READY, COUNTERSIGNED))

    assert countersigned({READY, COUNTERSIGNED})
    assert _audit_pending(edited) is False, "a hand-edited brief is not re-audited"
    assert _next_ready_issue(_CFG)["number"] == 380, "and it still builds"


# --- what each verdict does to the issue --------------------------------------------------------

class _AuditRecord:
    def __init__(self, repo="owner/repo", subject="380"):
        self.repo = repo
        self.subject = subject
        self.identity = "id-380-audit"
        self.pool = "claude"
        self.source = ""
        self.input_ptr = ""
        self.hold_reason = ""


class ThreadedGH(FakeGH):
    """`FakeGH`, but the comment it posts joins the thread it later reads back.

    The exhaustion hold only reports success once it can *prove* its own comment landed, so a
    recorder that swallows the post would read as "nothing was written" and the hold would look
    like it had failed when it had not.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self._thread = list(kw.get("comment_bodies", ()))

    def issue_comments(self, repo, issue):
        return [Comment(body=b, created_at="") for b in self._thread]

    def comment(self, repo, issue, body):
        self._thread.append(body)
        return super().comment(repo, issue, body)


def _install_audit_gh(monkeypatch, fake, labels):
    """Point the projection's own module at the recorder, and canned live labels at it too."""
    from agentflow import coordinated_plan_audit as audit_mod

    _install(monkeypatch, fake)
    monkeypatch.setattr(audit_mod.github, "issue_labels", lambda repo, n: set(labels))
    fake.released = []
    monkeypatch.setattr(audit_mod, "release",
                        lambda repo, n, label: fake.released.append(label) or True)
    return fake


def test_a_countersign_marks_the_brief_audited_and_changes_nothing_else(monkeypatch):
    # A countersign is the quiet verdict: the issue was already ready, and the audit's only
    # finding was that it deserved to stay that way. Anything louder would be noise on an issue
    # nobody needs to look at.
    from agentflow.coordinated_plan_audit import apply_verdict
    from agentflow.plan_audit import PlanAuditResult

    fake = _install_audit_gh(monkeypatch, FakeGH(),
                             {READY, COUNTERSIGNED, *DIALS})
    url = apply_verdict(_AuditRecord(), PlanAuditResult(PlanAuditVerdict.COUNTERSIGN))

    assert url is not None
    assert COUNTERSIGNED in fake.added
    assert fake.posted_comment is None, "a countersign posts nothing"
    assert fake.title is None and fake.written_body is None
    assert fake.removed == [], "a countersign takes nothing off the issue"


def test_a_countersign_is_not_stamped_on_an_issue_that_stopped_being_ready(monkeypatch):
    # A maintainer (or a resumed triage) moved the issue while the audit ran. Stamping now would
    # leave a stale "may be built" mark on something nobody cleared.
    from agentflow.coordinated_plan_audit import apply_verdict
    from agentflow.plan_audit import PlanAuditResult

    fake = _install_audit_gh(monkeypatch, FakeGH(), {GRILLING})
    url = apply_verdict(_AuditRecord(), PlanAuditResult(PlanAuditVerdict.COUNTERSIGN))

    assert url is not None, "there is nothing to project and nothing to retry"
    assert COUNTERSIGNED not in fake.added


def test_a_session_that_never_spoke_is_retried_rather_than_answered_for(monkeypatch):
    # The quiet failure mode. A session killed by infrastructure said nothing at all, which is
    # not a bounce and not a countersign: capturing nothing leaves the attempt budget to retry
    # it silently, with no comment and no label touched. Fabricating a fail-safe bounce here
    # would send a good brief back to the maintainer over a dead shell.
    from agentflow.coordinator.plan_audit_stage import PlanAuditStageAdapter

    adapter = PlanAuditStageAdapter(worktree_reset=lambda _r: True,
                                    apply_verdict=lambda _r, _v: pytest.fail("nothing to apply"))
    record = _AuditRecord()
    assert adapter.capture(record, _Obs("")) is None
    assert adapter.capture(record, _Obs("   ")) is None
    assert adapter.capture(record, _Obs('{"verdict": "bounce", "objections": "1. no"}')) \
        is not None, "a session that *did* answer is captured"


class _Obs:
    def __init__(self, final_message):
        self.final_message = final_message


def test_the_verdict_detail_survives_the_durable_round_trip():
    # Why a fail-safe bounced has to reach the projection: a bounce whose reason was dropped
    # leaves nothing anywhere saying whether the auditor rejected the plan or never answered.
    unreadable = parse_plan_audit("not json at all")
    assert unreadable.detail
    assert decode_result(encode_result(unreadable)) == unreadable


def test_an_exhausted_audit_holds_without_unwinding_the_settled_decision(monkeypatch,
                                                                        coord_state):
    # Exhaustion is not silence — but it is also not a finding about the brief. A session that
    # ran out of room never read the plan, so the settlement stands: the issue keeps `ready` and
    # its dials, no grilling route is projected, and our own spend cap never costs the
    # maintainer a round-trip on a brief that may be perfectly good.
    from agentflow import coordinated_plan_audit as audit_mod
    from agentflow.github import IssueHeadline

    fake = _install_audit_gh(monkeypatch, ThreadedGH(), {READY, *DIALS})
    monkeypatch.setattr(audit_mod.github, "issue_headline",
                        lambda repo, n: IssueHeadline(title="a ready issue",
                                                      labels=frozenset({READY, *DIALS})))
    record = _AuditRecord()
    record.hold_reason = "continuation budget exhausted"

    assert audit_mod.hold_audit(record) is not None
    assert READY not in fake.removed, "a spend-cap trip must not unwind a settled decision"
    assert not (set(DIALS) & set(fake.removed)), \
        "the dials survive a hold — nothing about them is in doubt"
    assert GRILLING not in fake.added, "the auditor failed, so the brief is not what is in doubt"
    assert AUDITING in fake.released, "the audit's own claim is released when it stops running"
    assert fake.posted_comment is not None
    assert intake_mod.INTAKE_MARK in fake.posted_comment, \
        "the hold must read as ours, or a later sweep treats it as a maintainer reply"


def test_a_held_audit_names_the_by_hand_resume(monkeypatch, coord_state):
    # Because the hold leaves the issue ready with no held label, the resume sweep will never
    # wake on a reply to it. The comment is the only thing that can tell the maintainer how to
    # restart it, so it has to say so in plain words.
    from agentflow import coordinated_plan_audit as audit_mod

    fake = _install_audit_gh(monkeypatch, ThreadedGH(), {READY, *DIALS})
    record = _AuditRecord()
    record.hold_reason = "continuation budget exhausted"

    assert audit_mod.hold_audit(record) is not None
    assert "build" in fake.posted_comment and "pickup" in fake.posted_comment


# --- the rubric is the step ----------------------------------------------------------------

def test_the_prompt_carries_all_five_rubric_axes_and_demands_real_objections():
    prompt = plan_audit_prompt("owner/repo", {"number": 380, "title": "t", "body": "the brief"})
    for axis in ("Grounding", "Acceptance", "Interface shape",
                 "Scope and complexity budget", "Cost"):
        assert axis in prompt
    assert "evidence" in prompt
    assert "why it breaks the build if unfixed" in prompt.lower()
    assert "cheapest fix" in prompt
    assert "Number them." in prompt
    assert "TASTE IS NOT AN OBJECTION" in prompt
    assert "the brief" in prompt and "#380" in prompt
