"""Test the hardening rounds through their interface — the fail-safe objection parser, the pure
round-chain mappings, and the settlement that decides publish / redraft / hold (ADR 380/418).

The point of the loop is that a brief nobody argued with never reaches a builder, so the
load-bearing assertions here are the negative ones: a ready draft is *not* published while an
attacker is still owed one, an unreadable answer never reads as the draft surviving, and a draft
whose argument still needs a human goes to the maintainer rather than to the build queue.

The equally load-bearing positive one is newer: an objection that came with its own fix is work,
not a decision, and running out of attackers must not turn it into a human hold. The gate spent
its whole production life publishing nothing because it could not tell those apart (#418).

The GitHub-touching tests state facts through the shared `github` module's interface (ADR 0040) —
a canned typed read, or a recorded typed write — never a `gh` argument vector.
"""

import json

import pytest

from agentflow import coordinated_attack, coordinated_intake
from agentflow import intake as intake_mod
from agentflow.attack import (ATTACK_RESULT_SCHEMA, MAX_ATTACK_ROUNDS, AttackResult, attack_prompt,
                              hardening_note, max_rounds, parse_attack)
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
    original = parse_attack('{"objections": "1. unverifiable claim", "forks": "1. yours to call"}')
    assert decode_result(encode_result(original)) == original
    assert decode_result(encode_result(original)).forked, \
        "which objections need a human must survive the trip — the gate branches on it"
    unreadable = parse_attack("not json")
    assert decode_result(encode_result(unreadable)) == unreadable, \
        "the *reason* a round was spent must survive the trip too"


# --- the answer says what KIND of objection each one is ---------------------------------------

def test_the_attacker_is_asked_which_of_its_objections_need_a_human():
    # The whole fix lives here: the gate can only tell an edit from a decision if the answer
    # carries the difference. Nothing downstream re-reads the objection prose to guess.
    assert set(ATTACK_RESULT_SCHEMA["required"]) == {"objections", "forks"}


def test_an_objection_that_names_its_own_fix_is_the_drafters_to_answer():
    result = parse_attack('{"objections": "1. Reword criterion 3.", "forks": ""}')
    assert result.parsed and not result.survived
    assert result.answerable and not result.forked


def test_a_named_fork_is_nobody_in_the_loops_to_answer():
    result = parse_attack('{"objections": "1. Two defensible shapes here.", '
                          '"forks": "1. Which store owns the row?"}')
    assert result.forked and not result.answerable


def test_a_missing_forks_field_reads_as_no_fork_rather_than_as_unreadable():
    # Silence about forks is the ordinary answer — most rounds have none — and its safe
    # direction is the drafter answering the objections, which is what every non-final round
    # already does with them.
    result = parse_attack('{"objections": "1. Reword criterion 3."}')
    assert result.parsed and result.answerable and not result.forked


def test_an_unreadable_answer_is_never_answerable():
    # The fail-safe direction holds for the new field too: nobody read the draft, so nobody
    # can vouch that what it says is only editing.
    assert not parse_attack("not json").answerable


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


def test_the_prompt_asks_for_the_forks_and_shuts_the_lever_it_opens():
    prompt = attack_prompt("o/r", 418, "a plan", "body")
    assert '"forks"' in prompt
    assert "A FORK IS NOT A STRONGER OBJECTION" in prompt, \
        "an attacker that can escalate by relabelling would use it — say so where it is asked"
    assert "fail to answer under `## Answered objections`" in prompt, \
        "a finding the drafter kept dismissing is exactly what the maintainer is for"


def test_the_redraft_regrounds_from_the_same_base_prompt():
    prompt = redraft_prompt("THE GROUNDING PROMPT", "a plan", "the draft body",
                            "1. an objection", round=2, max_rounds=3)
    assert prompt.startswith("THE GROUNDING PROMPT")
    assert "the draft body" in prompt and "1. an objection" in prompt
    assert "round 2 of at most 3" in prompt
    assert "## Answered objections" in prompt, \
        "standing its ground must leave a trace inside the brief"
    assert 'route "grill"' in prompt, "a genuine fork still escapes to the maintainer"


def test_the_last_redraft_is_told_nothing_will_attack_its_answer():
    prompt = redraft_prompt("THE GROUNDING PROMPT", "a plan", "the draft body",
                            "1. an objection", round=4, max_rounds=3, final=True)
    assert "last cold reader" in prompt and "round 4 of at most 3" not in prompt
    assert 'route "grill"' in prompt, \
        "the last redraft is the last chance to escalate, so the escape must still be there"


def test_the_published_brief_says_what_the_argument_cost():
    assert hardening_note(0) == ""
    assert "once" in hardening_note(1)
    assert "twice" in hardening_note(2)
    assert "3 times" in hardening_note(3)


def test_a_brief_published_after_applying_the_last_objections_says_so():
    assert "nothing left to object to" in hardening_note(3)
    assert "with those applied" in hardening_note(3, answered=True), \
        "the maintainer reads the brief, not our records — the two endings must read differently"


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


def test_publication_no_longer_requires_an_empty_objection_list(monkeypatch):
    # THE defect. The gate published nothing in its entire production life because the only way
    # through it was an attacker with nothing to say, and a cold reader given a real brief always
    # has something. An objection that came with its own fix is work, and running out of attackers
    # does not turn work into a decision — it gets the same redraft every other round's gets.
    outcome, calls = _settle(monkeypatch, _attack_record(round=3),
                             AttackResult("1. Reword criterion 3.", ""))
    assert outcome is None and not calls, \
        "the last round's objections are answered, and that answer is what gets published"


def test_a_draft_out_of_rounds_with_a_real_fork_is_held_never_published(monkeypatch):
    result = AttackResult("1. Two defensible shapes.", "1. Which store owns the row?")
    outcome, calls = _settle(monkeypatch, _attack_record(round=3), result)
    assert outcome == "url" and "publish" not in calls
    assert calls["hold"][1].forks == "1. Which store owns the row?"


def test_a_standard_draft_gets_exactly_one_round(monkeypatch):
    # The cap is untouched: one attacker for a standard draft, and no second one buys itself a
    # round by objecting.
    record = _attack_record(_draft(Complexity.STANDARD), round=1)
    outcome, calls = _settle(monkeypatch, record, AttackResult("1. wrong", "1. yours to call"))
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
    # The gate itself: triage deciding "ready" no longer touches GitHub while an attacker is
    # still owed one. If this regressed, every brief would publish un-attacked and the whole
    # loop would be decoration.
    fake = _install(monkeypatch, FakeGH())
    settled = coordinated_intake.apply_route(_intake_record(), _draft())
    assert settled is None
    assert fake.posted_comment is None and fake.written_body is None and fake.title is None
    assert not fake.added and not fake.removed, "a draft leaves the issue untouched"


def test_the_redraft_with_no_attacker_left_is_published_by_intakes_own_settlement(monkeypatch):
    # The argument ends on a redraft, not on an attacker: the last round's objections were
    # answered, and nothing is left to argue with the answer.
    published = {}
    monkeypatch.setattr(coordinated_attack, "publish_brief",
                        lambda r, d, note: (published.setdefault("note", note), "url")[1])
    assert coordinated_intake.apply_route(_intake_record(round=3), _draft()) == "url"
    assert published["note"] == hardening_note(3, answered=True)


def test_a_redraft_that_re_sizes_itself_deeper_earns_the_attacker_it_asks_for(monkeypatch):
    # The dial is the classifier, and a redraft may move it. A standard draft that has already
    # had its one attacker but comes back sized deep is owed more scrutiny, not a publish.
    monkeypatch.setattr(coordinated_attack, "publish_brief",
                        lambda r, d, note: "url")
    assert coordinated_intake.apply_route(_intake_record(round=1),
                                          _draft(Complexity.STANDARD)) == "url"
    assert coordinated_intake.apply_route(_intake_record(round=1), _draft()) is None


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

    result = AttackResult("1. Reword criterion 3.\n2. Two defensible shapes.",
                          "2. Which store owns the row?")
    url = coordinated_attack.hold_contested(_attack_record(round=3), _draft(), result)
    assert url is not None
    assert captured["stage"] == "attack-contested"
    assert GRILLING in fake.added and READY in fake.removed, \
        "a contested draft leaves the build queue and waits for the maintainer"
    body = fake.posted_comment
    assert "## Agent Brief\nthe plan" in body, "the draft is not lost"
    assert "a call only you can make" in body
    assert body.index("Which store owns the row?") < body.index("Reword criterion 3."), \
        "the maintainer is here for the fork, not for a numbered list of edits with patches"
    assert released == ["agentflow:triaging"]


def test_an_unread_last_round_is_still_handed_over_as_unread(monkeypatch):
    from agentflow import handoff as handoff_mod

    fake = _install(monkeypatch, FakeGH())
    monkeypatch.setattr(handoff_mod.DurableHandoff, "hand_off",
                        lambda self, subject, **kw: (kw["action"](), "url")[1])
    monkeypatch.setattr(coordinated_attack.github, "issue_headline",
                        lambda repo, n: IssueView(title="a title", body="", state="OPEN",
                                                  url="", labels={READY}, comments=[]))
    monkeypatch.setattr(coordinated_attack, "release", lambda repo, n, label: True)

    coordinated_attack.hold_contested(_attack_record(round=3), _draft(), parse_attack("not json"))
    assert "answer I couldn't read" in fake.posted_comment, \
        "an unread draft is not a settled one — that is a different question for the maintainer"


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


def test_the_last_rounds_objections_still_open_their_redraft(monkeypatch):
    record = _attack_record(round=3, outcome=encode_result(AttackResult("1. Reword it.", "")))
    coord = _drive_attack_opener(monkeypatch, record)
    assert len(coord.submitted) == 1
    assert coord.submitted[0].stage == "intake", "the cap on attackers is untouched"
    assert "last cold reader" in json.loads(coord.submitted[0].input_ptr)["prompt"]


def test_a_fork_out_of_rounds_is_settlements_not_another_round(monkeypatch):
    # The guard that keeps a transiently unsettled contested hold from buying itself an
    # extra round beyond the cap.
    record = _attack_record(round=3,
                            outcome=encode_result(AttackResult("1. wrong", "1. yours to call")))
    assert not _drive_attack_opener(monkeypatch, record).submitted


def test_an_unread_last_round_is_settlements_not_another_round(monkeypatch):
    record = _attack_record(round=3, outcome=encode_result(parse_attack("not json")))
    assert not _drive_attack_opener(monkeypatch, record).submitted


def test_a_redraft_with_no_attacker_left_opens_no_attacker(monkeypatch):
    # The mirror of the guard above, on the intake side: a redraft whose publish failed
    # transiently must retry the publish, never buy itself a fourth attacker.
    record = _intake_record(round=3, outcome=encode_draft(_draft()))
    assert not _drive_intake_opener(monkeypatch, record).submitted


# --- replaying what the gate actually did in production ---------------------------------------

# Excerpts — verbatim — from the contested holds this gate really posted. Each is the attacker's
# own objection heading followed by its own `Cheapest fix`. All three issues were handed to the
# maintainer to arbitrate edits that arrived with their own wording, which is #418.
RECORDED_OBJECTIONS = {
    401: (
        "## 1. Criterion 3 cannot tell a correct implementation from one that leaves a wrong "
        "sign-off unrepaired\n\n"
        "**Cheapest fix.** Restate criterion 3 in two halves: *a commit already carrying the "
        "author-matching sign-off gets exactly one; a commit carrying only a non-matching "
        "sign-off still gets the correct one appended.* That one sentence forces "
        "`addIfDifferent` and is testable with the machinery criterion 2 already builds."
    ),
    411: (
        '4. **"The set of reason texts is identical before and after, pinned by a test" is not '
        "testable without inventing something the charter would reject.**\n\n"
        "   *Cheapest fix.* Replace it with the two concrete observations it is standing in "
        "for, both of which are testable through the existing interfaces: (a) a turn-capped "
        "intake and a turn-capped attack hold under exactly `\"continuation budget exhausted\"` "
        "(already AC 11), and (b) `proof_marker(identity, reason, tag=\"intake-hold\")` / "
        '`tag="attack-hold"` are byte-identical before and after the change for each of the two '
        "existing reason strings — no registry required."
    ),
    417: (
        "## 2. Criterion 8 passes on today's code, so it cannot judge the new read\n\n"
        "**Cheapest fix.** Reword to isolate the new read: with the comment thread, PR facts "
        "and PR content all readable and **only the check-status read** returning unknown, "
        "settlement posts no clean summary, does not merge, and posts no park comment; "
        "repeating that N times leaves the PR with no new agentflow comment and the record "
        "unsettled."
    ),
}


def _replay(monkeypatch, answer, *, complexity=Complexity.DEEP):
    """Drive one attacker answer through the whole gate at the round cap, end to end.

    Settlement first; if the argument continues, the opener drives the redraft and that
    redraft's own settlement decides. Returns whichever ending fired.
    """
    ending = {}
    monkeypatch.setattr(coordinated_attack, "hold_contested",
                        lambda r, d, res: (ending.setdefault("held", res), "url")[1])
    monkeypatch.setattr(coordinated_attack, "publish_brief",
                        lambda r, d, note: (ending.setdefault("published", d), "url")[1])
    result = parse_attack(json.dumps(answer))
    draft = _draft(complexity)
    record = _attack_record(draft, round=max_rounds(complexity), outcome=encode_result(result))
    if coordinated_attack.apply_objections(record, result) is not None:
        return ending
    redraft = _drive_attack_opener(monkeypatch, record).submitted[0]
    coordinated_intake.apply_route(
        _Record("intake", round=redraft.round, source=redraft.source,
                input_ptr=redraft.input_ptr, identity="o/r|380|intake|last"),
        draft)
    return ending


@pytest.mark.parametrize("issue", [401, 411, 417])
def test_the_holds_this_gate_really_posted_would_now_be_published(monkeypatch, issue):
    assert "Cheapest fix" in RECORDED_OBJECTIONS[issue], \
        "these are the recorded objections precisely because each arrived with its own remedy"
    ending = _replay(monkeypatch, {"objections": RECORDED_OBJECTIONS[issue], "forks": ""})
    assert "published" in ending and "held" not in ending


def test_a_draft_carrying_a_real_either_or_still_holds(monkeypatch):
    ending = _replay(monkeypatch, {
        "objections": "1. The plan gives the retry clock to the daemon without saying why.",
        "forks": "1. Does the retry clock belong to the daemon or to the runner? Both are "
                 "defensible and no amount of reading the code decides it."})
    assert "held" in ending and "published" not in ending


@pytest.mark.parametrize("complexity", [Complexity.STANDARD, Complexity.DEEP])
def test_one_round_and_three_rounds_end_the_same_way(monkeypatch, complexity):
    # A standard draft reaches the cap after a single attacker, so if the two paths diverged it
    # would be the standard one that kept parking issues on the maintainer.
    ending = _replay(monkeypatch, {"objections": RECORDED_OBJECTIONS[401], "forks": ""},
                     complexity=complexity)
    assert "published" in ending and "held" not in ending


@pytest.mark.parametrize("complexity", [Complexity.STANDARD, Complexity.DEEP])
def test_one_round_and_three_rounds_escalate_the_same_way(monkeypatch, complexity):
    ending = _replay(monkeypatch, {"objections": "1. Two defensible shapes.",
                                   "forks": "1. Which one do you want?"},
                     complexity=complexity)
    assert "held" in ending and "published" not in ending
