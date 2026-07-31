"""Test the hardening rounds through their interface — the fail-safe objection parser, the pure
round-chain mappings, and the settlement that decides publish / redraft / hold (ADR 380).

The point of the loop is that a brief nobody argued with never reaches a builder, so the
load-bearing assertions here are the negative ones: a ready draft is *not* published by intake's
own settlement, an unreadable answer never reads as the draft surviving, and a draft out of rounds
still contested goes to the maintainer rather than to the build queue.

The GitHub-touching tests state facts through the shared `github` module's interface (ADR 0040) —
a canned typed read, or a recorded typed write — never a `gh` argument vector.
"""

import json

import pytest

from agentflow import coordinated_attack, coordinated_intake
from agentflow import intake as intake_mod
from agentflow.attack import (MAX_ATTACK_ROUNDS, AttackResult, attack_prompt, hardening_note,
                              max_rounds, parse_attack)
from agentflow.coordinator.admission import ADMISSION_MATRIX, admission_demand
from agentflow.coordinator.attack_stage import decode_result, encode_result
from agentflow.coordinator.intake_stage import encode_result as encode_draft
from agentflow.coordinator.profiles import WITHHELD_EDIT_TOOLS, profile_for
from agentflow.coordinator.providers import PROVIDER_INPUT_V1
from agentflow.coordinator.tracer import CLAIM_LANE, ENABLED_STAGES
from agentflow.github import Comment, IssueView
from agentflow.intake import IntakeResult, IntakeRoute, redraft_prompt
from agentflow.runner import Complexity, Effort
from agentflow.worktree_ref import WorktreeKind, WorktreeRef

READY = "ready-for-agent"
GRILLING = "agentflow:needs-grilling"


# --- the objection parser: fail-safe, but never toward "survived" ---------------------------

def test_objections_parse():
    result = parse_attack('{"objections": "1. The premise is wrong."}')
    assert result.parsed and result.objections == "1. The premise is wrong."
    assert not result.survived


def test_an_empty_objection_list_is_the_draft_surviving():
    result = parse_attack('{"objections": ""}')
    assert result.parsed and result.survived


@pytest.mark.parametrize("payload", [
    "",
    "   ",
    "null",
    '["1. an objection in the wrong shape"]',
    "not json at all",
    '{"verdict": "no objections field"}',
    'Here you go:\n```json\n{"objections": ""}\n```',
])
def test_an_unreadable_answer_spends_the_round_but_never_clears_the_draft(payload):
    # THE fail-safe direction. An answer we cannot read means nobody attacked the draft, which
    # must never be mistaken for nobody finding anything wrong with it — not even when a clean
    # bill of health is the thing buried in the prose we refused to scavenge.
    result = parse_attack(payload)
    assert result.parsed is False
    assert not result.survived


def test_the_answer_survives_a_durable_round_trip():
    original = parse_attack('{"objections": "1. unverifiable claim"}')
    assert decode_result(encode_result(original)) == original
    unreadable = parse_attack("not json")
    assert decode_result(encode_result(unreadable)) == unreadable, \
        "the *reason* a round was spent must survive the trip too"


# --- the round cap comes from the draft's own dial -------------------------------------------

def test_the_dial_is_the_intensity_classifier():
    assert max_rounds(Complexity.STANDARD) == 1
    assert max_rounds(Complexity.DEEP) == 3
    assert max_rounds("standard") == 1 and max_rounds("deep") == 3


def test_a_draft_with_no_dial_attacks_at_the_deep_cap():
    # Missing sizing is a reason for more scrutiny, not less.
    assert max_rounds(None) == MAX_ATTACK_ROUNDS


def test_the_attack_takes_one_permit_on_either_pool_at_either_tier():
    assert admission_demand("attack", "claude", "opus", "deep") == 1
    assert admission_demand("attack", "claude", "sonnet", "standard") == 1
    assert admission_demand("attack", "codex", "sol", "deep") == 1
    assert admission_demand("attack", "codex", "terra", "standard") == 1
    assert ("attack", "claude", "sonnet", "standard", None) in ADMISSION_MATRIX


# --- the attacker is cold: newest draft, nothing else ----------------------------------------

def test_the_prompt_carries_only_the_newest_draft():
    prompt = attack_prompt("o/r", 380, "a plan", "## Agent Brief\nthe body",
                           round=2, max_rounds=3)
    assert "## Agent Brief\nthe body" in prompt
    assert "round 2 of at most 3" in prompt
    # Structurally cold: there is no history parameter at all, so a settled objection can only
    # reach the next attacker written into the draft itself.
    assert "Answered objections" in prompt, \
        "the prompt must tell the attacker how a settlement inside the draft is to be judged"


def test_taste_is_not_an_objection_and_empty_is_a_success():
    prompt = attack_prompt("o/r", 380, "a plan", "body")
    assert "TASTE IS NOT AN OBJECTION" in prompt
    assert "EMPTY objection list is a SUCCESSFUL" in prompt


def test_the_redraft_regrounds_from_the_same_base_prompt():
    prompt = redraft_prompt("THE GROUNDING PROMPT", "a plan", "the draft body",
                            "1. an objection", round=2, max_rounds=3)
    assert prompt.startswith("THE GROUNDING PROMPT")
    assert "the draft body" in prompt and "1. an objection" in prompt
    assert "## Answered objections" in prompt, \
        "standing its ground must leave a trace inside the brief"
    assert 'route "grill"' in prompt, "a genuine fork still escapes to the maintainer"


def test_the_published_brief_says_what_the_argument_cost():
    assert hardening_note(0) == ""
    assert "once" in hardening_note(1)
    assert "twice" in hardening_note(2)
    assert "3 times" in hardening_note(3)


# --- the round chain: pure record-to-record mappings ------------------------------------------

class _Record:
    def __init__(self, stage, *, round=0, source="", input_ptr="", pool="claude",
                 identity="o/r|380|x|-", outcome=None):
        self.repo = "o/r"
        self.subject = "380"
        self.stage = stage
        self.round = round
        self.source = source
        self.input_ptr = input_ptr
        self.pool = pool
        self.identity = identity
        self.outcome = outcome
        self.complexity = None
        self.effort = None
        self.builder_complexity = None
        self.hold_reason = None


def _draft(complexity=Complexity.DEEP):
    return IntakeResult(IntakeRoute.READY, "## Agent Brief\nthe plan", "a sharpened title",
                        complexity, Effort.HIGH)


def _intake_record(**kwargs):
    payload = {"format": PROVIDER_INPUT_V1, "snapshot": {"title": "as filed", "body": "raw ask"},
               "source_ref": "abc123", "prompt": "THE GROUNDING PROMPT"}
    return _Record("intake", source=str(WorktreeRef.for_intake("/w", "claude", 380).path),
                   input_ptr=json.dumps(payload), identity="o/r|380|intake|-", **kwargs)


def _attack_record(draft=None, *, round=1, **kwargs):
    payload = {"format": PROVIDER_INPUT_V1, "snapshot": {"title": "as filed", "body": "raw ask"},
               "source_ref": "abc123", "prompt": "ATTACK PROMPT",
               "base_prompt": "THE GROUNDING PROMPT",
               "draft": encode_draft(draft or _draft())}
    return _Record("attack", round=round,
                   source=str(WorktreeRef.for_attack("/w", "claude", 380).path),
                   input_ptr=json.dumps(payload), identity=f"o/r|380|attack|{round}", **kwargs)


def test_a_ready_draft_opens_a_cold_attack_that_assumes_the_claim():
    submission = coordinated_attack.attack_submission(_intake_record(), _draft(), "codex")
    assert submission.stage == "attack" and submission.round == 1
    assert submission.transfer_from == "o/r|380|intake|-"
    payload = json.loads(submission.input_ptr)
    assert payload["base_prompt"] == "THE GROUNDING PROMPT", \
        "round 0's own prompt seeds the chain every redraft re-grounds from"
    assert "history" not in payload, "an attacker sees the newest draft and nothing else"
    assert "## Agent Brief\nthe plan" in payload["prompt"]
    ref = WorktreeRef.parse(submission.source)
    assert ref.kind is WorktreeKind.ATTACK and ref.tool == "codex"


def test_the_attack_runs_at_the_drafts_own_dial():
    deep = coordinated_attack.attack_submission(_intake_record(), _draft(), "claude")
    standard = coordinated_attack.attack_submission(
        _intake_record(), _draft(Complexity.STANDARD), "claude")
    assert deep.complexity == "deep" and standard.complexity == "standard"
    assert "at most 1" in json.loads(standard.input_ptr)["prompt"]
    assert "at most 3" in json.loads(deep.input_ptr)["prompt"]


def test_objections_open_a_redraft_that_carries_the_argument():
    result = AttackResult("1. The premise is wrong.")
    submission = coordinated_attack.redraft_submission(_attack_record(), result, "claude")
    assert submission.stage == "intake" and submission.round == 1
    assert submission.transfer_from == "o/r|380|attack|1"
    prompt = json.loads(submission.input_ptr)["prompt"]
    assert prompt.startswith("THE GROUNDING PROMPT")
    assert "1. The premise is wrong." in prompt
    assert "history" not in json.loads(submission.input_ptr)


def test_an_unreadable_answer_renews_the_attack_on_the_same_draft():
    record = _attack_record(round=1)
    submission = coordinated_attack.renewed_attack_submission(record, "claude")
    assert submission.stage == "attack" and submission.round == 2
    assert json.loads(submission.input_ptr)["draft"] == json.loads(record.input_ptr)["draft"], \
        "there is nothing to answer, so the same draft faces the next round"


@pytest.mark.parametrize("mapper", [
    lambda r: coordinated_attack.attack_submission(r, _draft(), "claude"),
    lambda r: coordinated_attack.redraft_submission(r, AttackResult("1. x"), "claude"),
    lambda r: coordinated_attack.renewed_attack_submission(r, "claude"),
])
def test_an_unreadable_chain_payload_opens_nothing(mapper):
    broken = _attack_record()
    broken.input_ptr = "not json"
    broken.source = str(WorktreeRef.for_attack("/w", "claude", 380).path)
    assert mapper(broken) is None


# --- settlement: publish, next round, or hold — never a contested publish ---------------------

def _settle(monkeypatch, record, result):
    calls = {}
    monkeypatch.setattr(coordinated_attack, "publish_brief",
                        lambda r, d, note: (calls.setdefault("publish", (d, note)), "url")[1])
    monkeypatch.setattr(coordinated_attack, "hold_contested",
                        lambda r, d, res: (calls.setdefault("hold", (d, res)), "url")[1])
    outcome = coordinated_attack.apply_objections(record, result)
    return outcome, calls


def test_a_survived_draft_is_published_with_its_hardening_note(monkeypatch):
    outcome, calls = _settle(monkeypatch, _attack_record(round=2), AttackResult(""))
    assert outcome == "url" and "hold" not in calls
    _draft_arg, note = calls["publish"]
    assert note == hardening_note(2)


def test_objections_with_rounds_left_keep_the_argument_going(monkeypatch):
    outcome, calls = _settle(monkeypatch, _attack_record(round=1), AttackResult("1. wrong"))
    assert outcome is None and not calls, \
        "None is how a round says the argument continues — the next opener assumes the claim"


def test_a_draft_out_of_rounds_still_contested_is_held_never_published(monkeypatch):
    outcome, calls = _settle(monkeypatch, _attack_record(round=3), AttackResult("1. still wrong"))
    assert outcome == "url" and "publish" not in calls
    assert calls["hold"][1].objections == "1. still wrong"


def test_a_standard_draft_gets_exactly_one_round(monkeypatch):
    record = _attack_record(_draft(Complexity.STANDARD), round=1)
    outcome, calls = _settle(monkeypatch, record, AttackResult("1. wrong"))
    assert "hold" in calls and "publish" not in calls


def test_an_unreadable_final_round_is_held_not_published(monkeypatch):
    unreadable = parse_attack("not json")
    outcome, calls = _settle(monkeypatch, _attack_record(round=3), unreadable)
    assert "hold" in calls and "publish" not in calls, \
        "an unread draft is not a settled one — publishing it on our own say-so is refused"


def test_an_unreadable_early_round_waits_for_its_renewed_attack(monkeypatch):
    outcome, calls = _settle(monkeypatch, _attack_record(round=1), parse_attack("not json"))
    assert outcome is None and not calls


# --- intake's own settlement never publishes a draft ------------------------------------------

class FakeGH:
    def __init__(self, issue=None):
        self._issue = issue
        self.added, self.removed, self.created = [], [], []
        self.title = self.written_body = self.posted_comment = None

    def issue_body(self, repo, issue):
        return "raw ask"

    def issue_comments(self, repo, issue):
        return []

    def issue_view(self, repo, issue):
        return self._issue

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


def test_a_ready_route_is_a_draft_intake_never_projects(monkeypatch):
    # The gate itself: triage deciding "ready" no longer touches GitHub. If this regressed, every
    # brief would publish un-attacked and the whole loop would be decoration.
    fake = _install(monkeypatch, FakeGH())
    settled = coordinated_intake.apply_route(_intake_record(), _draft())
    assert settled is None
    assert fake.posted_comment is None and fake.written_body is None and fake.title is None
    assert not fake.added and not fake.removed, "a draft leaves the issue untouched"


def test_the_hardening_note_rides_inside_the_one_ready_comment(monkeypatch):
    fake = _install(monkeypatch, FakeGH())
    hardened = IntakeResult(IntakeRoute.READY, "## Agent Brief\nthe plan", "", Complexity.DEEP,
                            Effort.HIGH, hardening=hardening_note(2))
    intake_mod.apply_intake("o/r", 380, "a title", [GRILLING], hardened)
    assert READY in fake.added
    assert hardening_note(2) in fake.posted_comment
    assert intake_mod._comment_matches_result(fake.posted_comment, hardened), \
        "the durability proof must match the comment the note rides in"


def test_the_contested_hold_hands_over_the_draft_and_the_objections(monkeypatch):
    from agentflow import handoff as handoff_mod

    fake = _install(monkeypatch, FakeGH())
    captured = {}

    def hand_off(self, subject, *, identity, stage, marker, action, notification):
        captured["stage"] = stage
        captured["notification"] = notification
        action()
        return "https://github.com/o/r/issues/380"

    monkeypatch.setattr(handoff_mod.DurableHandoff, "hand_off", hand_off)
    monkeypatch.setattr(coordinated_attack.github, "issue_headline",
                        lambda repo, n: IssueView(title="a title", body="", state="OPEN",
                                                  url="", labels={READY}, comments=[]))
    released = []
    monkeypatch.setattr(coordinated_attack, "release",
                        lambda repo, n, label: released.append(label) or True)

    url = coordinated_attack.hold_contested(
        _attack_record(round=3), _draft(), AttackResult("1. still wrong"))
    assert url is not None
    assert captured["stage"] == "attack-contested"
    assert GRILLING in fake.added and READY in fake.removed, \
        "a contested draft leaves the build queue and waits for the maintainer"
    assert "## Agent Brief\nthe plan" in fake.posted_comment, "the draft is not lost"
    assert "1. still wrong" in fake.posted_comment, "the objections are the question"
    assert released == ["agentflow:triaging"]


# --- the remedied ending: the cap no longer converts fixes into a human hold (#418) ------------

# The shape of the recorded objections that motivated this: every surviving objection on #401,
# #411 and #417 carried the attacker's own "Cheapest fix" and named no fork. Excerpted, not
# invented — these are the fixtures the gate is judged against.
_REMEDIED_SETS = {
    401: ("1. Criterion 3 as written can pass without driving addIfDifferent. "
          "Cheapest fix: restate it in two halves so a test cannot go green without both.\n"
          "2. The ~100-line helper has no named home. Cheapest fix: name the module so the "
          "interface stays deep."),
    411: ("1. The turn-cap clause never reaches the handoff copy. Cheapest fix: thread the "
          "clause through the hold reason and pin the copy by test."),
    417: ("1. Criterion 4's notification clause cannot discriminate. Cheapest fix: delete the "
          "clause and keep the byte-identical-park half.\n"
          "2. Criterion 8 passes on today's code. Cheapest fix: isolate the check-status read."),
}


def test_parse_reads_the_typed_remedied_and_fork_pair():
    result = parse_attack(json.dumps(
        {"objections": "1. x. Cheapest fix: y.", "remedied": True, "fork": ""}))
    assert result.remedied and not result.fork and result.remedied_only


def test_an_answer_without_the_typed_pair_stays_contested():
    """Old recorded answers predate the pair; their defaults must hold, never publish."""
    result = parse_attack(json.dumps({"objections": "1. x"}))
    assert not result.remedied and not result.remedied_only
    assert not decode_result(encode_result(result)).remedied_only


def test_remedied_only_needs_all_three_facts():
    assert not AttackResult("", remedied=True).remedied_only, "empty list is survival, not remedy"
    assert not AttackResult("1. x", remedied=False).remedied_only
    assert not AttackResult("1. x", remedied=True, fork="A or B?").remedied_only
    assert not AttackResult("1. x", parsed=False, remedied=True).remedied_only
    assert AttackResult("1. x", remedied=True).remedied_only


def test_the_typed_pair_survives_the_durable_round_trip():
    original = AttackResult("1. x", remedied=True, fork="ship A or ship B?")
    assert decode_result(encode_result(original)) == original


@pytest.mark.parametrize("issue", sorted(_REMEDIED_SETS))
def test_replayed_remedied_objection_sets_publish_at_the_cap(monkeypatch, issue):
    """The recorded #401/#411/#417 shapes — every objection remedied, no fork — publish with the
    fixes riding in the brief, instead of holding for the maintainer."""
    result = AttackResult(_REMEDIED_SETS[issue], remedied=True)
    outcome, calls = _settle(monkeypatch, _attack_record(round=3), result)
    assert outcome == "url" and "hold" not in calls
    published, note = calls["publish"]
    assert "## Final-round objections — apply these fixes" in published.body
    assert "Cheapest fix" in published.body
    assert note == hardening_note(3, remedied=True) and "folded into the brief" in note


def test_publication_no_longer_requires_an_empty_objection_list(monkeypatch):
    """The regression pin the issue asks for: a non-empty, all-remedied final round publishes.
    If the gate ever again publishes only on survived (empty list), this fails."""
    outcome, calls = _settle(monkeypatch, _attack_record(round=3),
                             AttackResult("1. real, with its fix.", remedied=True))
    assert "publish" in calls and "hold" not in calls


def test_a_genuine_fork_still_holds_even_when_marked_remedied(monkeypatch):
    result = AttackResult("1. x. Cheapest fix: y.", remedied=True,
                          fork="Should enrollment refuse or repair on drift?")
    outcome, calls = _settle(monkeypatch, _attack_record(round=3), result)
    assert "hold" in calls and "publish" not in calls


def test_the_single_round_standard_path_publishes_remedied_objections_too(monkeypatch):
    record = _attack_record(_draft(Complexity.STANDARD), round=1)
    outcome, calls = _settle(monkeypatch, record,
                             AttackResult("1. x. Cheapest fix: y.", remedied=True))
    assert "publish" in calls and "hold" not in calls


def test_mid_round_remedied_objections_still_redraft_not_publish(monkeypatch):
    """With rounds left, the loop keeps absorbing fixes the normal way — the remedied ending
    exists only at the cap, so the cap still bounds the argument and its value is unchanged."""
    outcome, calls = _settle(monkeypatch, _attack_record(round=1),
                             AttackResult("1. x. Cheapest fix: y.", remedied=True))
    assert outcome is None and not calls


def test_an_unreadable_final_round_is_still_held_despite_the_new_ending(monkeypatch):
    outcome, calls = _settle(monkeypatch, _attack_record(round=3), parse_attack("not json"))
    assert "hold" in calls and "publish" not in calls


def test_the_contested_hold_leads_with_the_fork(monkeypatch):
    from agentflow import handoff as handoff_mod

    fake = _install(monkeypatch, FakeGH())
    monkeypatch.setattr(handoff_mod.DurableHandoff, "hand_off",
                        lambda self, subject, *, identity, stage, marker, action, notification:
                        (action(), "https://github.com/o/r/issues/380")[1])
    monkeypatch.setattr(coordinated_attack.github, "issue_headline",
                        lambda repo, n: IssueView(title="a title", body="", state="OPEN",
                                                  url="", labels={READY}, comments=[]))
    monkeypatch.setattr(coordinated_attack, "release", lambda repo, n, label: True)

    result = AttackResult("1. detail. Cheapest fix: z.",
                          fork="Should enrollment refuse or repair on drift?")
    url = coordinated_attack.hold_contested(_attack_record(round=3), _draft(), result)
    assert url is not None
    fork_at = fake.posted_comment.index("refuse or repair")
    objections_at = fake.posted_comment.index("1. detail")
    assert fork_at < objections_at, "the maintainer's question is the fork, not the edit list"
    assert "question only you can settle" in fake.posted_comment


def test_the_attacker_is_told_the_stakes_of_the_typed_pair():
    prompt = attack_prompt("o/r", 380, "t", "b")
    assert '"remedied"' in prompt and '"fork"' in prompt
    assert "applied to the published brief verbatim" in prompt
    assert "A wording preference is never a fork" in prompt


# --- the session the attack runs in ------------------------------------------------------------

def test_the_attack_runs_read_only_under_intakes_ceiling():
    profile = profile_for(_Record("attack"))
    assert profile.read_only
    assert not set(WITHHELD_EDIT_TOOLS) & set(profile.allowed_tools)
    assert profile_for(_Record("intake")) == profile, "the attack is intake-shaped"


def test_the_attack_is_an_enabled_stage_on_intakes_own_claim():
    # Deliberately *shared* with intake — the rounds are one continuous ownership of an issue
    # that is still being decided, transferred record-to-record down the chain.
    assert "attack" in ENABLED_STAGES
    assert CLAIM_LANE["attack"] == CLAIM_LANE["intake"] == "triaging"


def test_the_attack_shares_triages_lane_and_cap():
    from agentflow.coordinator.admission import STAGE_CAPS
    from agentflow.pipeline import _ProductionGate

    limits = _ProductionGate.reservation_limits(_Record("attack"))
    assert limits.stage_lane == "triage"
    assert limits.stage_cap == STAGE_CAPS["triage"]


def test_the_attack_checkout_is_not_mistakable_for_the_intake_checkout():
    attack = WorktreeRef.for_attack("/w", "claude", 380)
    intake = WorktreeRef.for_intake("/w", "claude", 380)
    assert attack.path != intake.path and attack.branch != intake.branch
    assert WorktreeRef.parse(attack.path) == attack
    assert WorktreeRef.parse(attack.path).kind is WorktreeKind.ATTACK


# --- the openers drive the chain from durable records ------------------------------------------

class _Coord:
    def __init__(self):
        self.submitted = []

    def submit_stage(self, submission):
        self.submitted.append(submission)
        return "identity"


class _Builder:
    tool = "claude"


def _drive_intake_opener(monkeypatch, record, *, headroom=True):
    from agentflow import pipeline as pipeline_mod

    coord = _Coord()
    monkeypatch.setattr(pipeline_mod.tracer, "load_records", lambda: [record])
    monkeypatch.setattr(pipeline_mod, "pick_pair",
                        lambda: (_Builder() if headroom else None, None, "busy"))
    pipeline_mod._open_attack_on_completed_intake(coord, record.identity)
    return coord


def test_a_completed_ready_draft_opens_its_attacker(monkeypatch):
    record = _intake_record(outcome=encode_draft(_draft()))
    coord = _drive_intake_opener(monkeypatch, record)
    assert len(coord.submitted) == 1 and coord.submitted[0].stage == "attack"


def test_a_grill_route_never_opens_an_attacker(monkeypatch):
    grill = IntakeResult(IntakeRoute.GRILL, "a question")
    record = _intake_record(outcome=encode_draft(grill))
    assert not _drive_intake_opener(monkeypatch, record).submitted


def test_no_headroom_leaves_the_draft_for_the_next_cycle(monkeypatch):
    record = _intake_record(outcome=encode_draft(_draft()))
    assert not _drive_intake_opener(monkeypatch, record, headroom=False).submitted


def _drive_attack_opener(monkeypatch, record):
    from agentflow import pipeline as pipeline_mod

    coord = _Coord()
    monkeypatch.setattr(pipeline_mod.tracer, "load_records", lambda: [record])
    monkeypatch.setattr(pipeline_mod, "pick_pair", lambda: (_Builder(), None, ""))
    pipeline_mod._open_next_round_on_completed_attack(coord, record.identity)
    return coord


def test_objections_open_the_redraft_round(monkeypatch):
    record = _attack_record(round=1, outcome=encode_result(AttackResult("1. wrong")))
    coord = _drive_attack_opener(monkeypatch, record)
    assert len(coord.submitted) == 1 and coord.submitted[0].stage == "intake"


def test_an_unreadable_answer_opens_the_renewed_attack(monkeypatch):
    record = _attack_record(round=1, outcome=encode_result(parse_attack("not json")))
    coord = _drive_attack_opener(monkeypatch, record)
    assert len(coord.submitted) == 1 and coord.submitted[0].stage == "attack"
    assert coord.submitted[0].round == 2


def test_a_survived_draft_is_settlements_not_another_round(monkeypatch):
    record = _attack_record(round=1, outcome=encode_result(AttackResult("")))
    assert not _drive_attack_opener(monkeypatch, record).submitted


def test_a_draft_out_of_rounds_is_settlements_not_another_round(monkeypatch):
    # The guard that keeps a transiently unsettled contested hold from buying itself an
    # extra round beyond the cap.
    record = _attack_record(round=3, outcome=encode_result(AttackResult("1. wrong")))
    assert not _drive_attack_opener(monkeypatch, record).submitted


# --- the pre-admission claim proof weighs the label and the issue's state as one ------------

def test_attack_claim_proof_refuses_a_closed_issue_still_carrying_the_label(monkeypatch):
    # Closing an issue does not strip its labels (#438): a chain whose issue closed
    # mid-argument must not launch another round on the label alone.
    from agentflow.labels import TRIAGING
    monkeypatch.setattr(coordinated_attack.github, "issue_standing",
                        lambda repo, n: coordinated_attack.github.IssueStanding(
                            labels=frozenset({TRIAGING}), state="CLOSED"))
    assert coordinated_attack.attack_claim_ready(_attack_record()) is False


def test_attack_claim_proof_admits_an_open_issue_with_the_label_and_fails_closed(monkeypatch):
    from agentflow.labels import TRIAGING
    monkeypatch.setattr(coordinated_attack.github, "issue_standing",
                        lambda repo, n: coordinated_attack.github.IssueStanding(
                            labels=frozenset({TRIAGING}), state="OPEN"))
    assert coordinated_attack.attack_claim_ready(_attack_record()) is True
    monkeypatch.setattr(coordinated_attack.github, "issue_standing", lambda repo, n: None)
    assert coordinated_attack.attack_claim_ready(_attack_record()) is False
