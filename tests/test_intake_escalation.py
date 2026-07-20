"""The standard-first escalation design (Wayfinder #2, #228), tested through its public
interface: the deterministic direct-deep triggers, the typed enumerated escalate result, and
the bounded evidence carry-forward that must never degenerate into a blind rerun.
"""

import pytest

from agentflow.experiments.escalation import (EscalationEvidence, direct_deep_triggers,
                                              escalation_prompt, evidence_from_result)
from agentflow.intake import (EscalationReason, INTAKE_RESULT_SCHEMA,
                              STANDARD_FIRST_INTAKE_SCHEMA, IntakeResult, IntakeRoute,
                              parse_intake)
from agentflow.runner import Complexity


# --- typed, enumerated escalate result --------------------------------------------------

def test_escalate_is_a_typed_result_with_enumerated_reasons():
    v = parse_intake('{"route": "escalate", "title": "t", "body": "tentative",'
                     ' "complexity": "standard", "effort": null,'
                     ' "escalation_reasons": ["evidence-gap", "low-confidence-route"]}')
    assert v.route is IntakeRoute.ESCALATE
    # Reasons are members of the closed enum, not free text.
    assert set(v.escalation_reasons) == {EscalationReason.EVIDENCE_GAP,
                                         EscalationReason.LOW_CONFIDENCE_ROUTE}
    assert all(isinstance(r, EscalationReason) for r in v.escalation_reasons)


def test_escalate_drops_unknown_reason_strings_never_coerces_free_text():
    v = parse_intake('{"route": "escalate", "title": "t", "body": "b", "complexity": null,'
                     ' "effort": null, "escalation_reasons": ["evidence-gap", "vibes"]}')
    assert v.escalation_reasons == (EscalationReason.EVIDENCE_GAP,)


def test_escalate_with_no_typed_reason_fails_safe_to_a_hold():
    # A step-up carrying no reason hands the deep attempt nothing to act on — that would be a
    # blind rerun, so it must fall back to a human hold rather than escalate.
    v = parse_intake('{"route": "escalate", "title": "t", "body": "b", "complexity": null,'
                     ' "effort": null, "escalation_reasons": []}')
    assert v.route is IntakeRoute.GRILL and v.parsed is False


def test_production_schema_forbids_escalate_standard_first_schema_allows_it():
    # Production deep Intake can never emit escalate — the terminal default is #232's call.
    assert "escalate" not in INTAKE_RESULT_SCHEMA["properties"]["route"]["enum"]
    assert "escalation_reasons" not in INTAKE_RESULT_SCHEMA["properties"]
    assert "escalate" in STANDARD_FIRST_INTAKE_SCHEMA["properties"]["route"]["enum"]
    reasons_enum = STANDARD_FIRST_INTAKE_SCHEMA["properties"]["escalation_reasons"]["items"]["enum"]
    assert set(reasons_enum) == {r.value for r in EscalationReason}


# --- deterministic direct-deep triggers -------------------------------------------------

def test_direct_deep_triggers_are_deterministic_same_input_same_decision():
    issue = {"title": "coordinator: fix a race condition in the merge machinery",
             "body": "This touches concurrency and idempotency."}
    first = direct_deep_triggers(issue)
    second = direct_deep_triggers(issue)
    assert first == second
    assert EscalationReason.COMPLEXITY_SIGNALS in first


def test_direct_deep_trigger_on_adr_contradiction():
    issue = {"title": "revisit intake routing", "body": "This argues against ADR 0016."}
    assert EscalationReason.UNRESOLVED_FORK in direct_deep_triggers(issue)


def test_ordinary_issue_has_no_direct_deep_trigger():
    issue = {"title": "docs: fix a typo in the readme", "body": "One-word change."}
    assert direct_deep_triggers(issue) == frozenset()


def test_long_body_triggers_evidence_gap():
    issue = {"title": "big", "body": "x" * 6000}
    assert EscalationReason.EVIDENCE_GAP in direct_deep_triggers(issue)


# --- bounded evidence carry-forward -----------------------------------------------------

def test_evidence_digest_is_bounded_at_construction():
    ev = EscalationEvidence(reasons=(EscalationReason.EVIDENCE_GAP,),
                            evidence_digest="y" * 5000, open_question="q" * 5000)
    assert len(ev.evidence_digest) <= 1200
    assert len(ev.open_question) <= 400


def test_escalation_prompt_is_not_a_blind_rerun_of_the_original():
    base = "ORIGINAL INTAKE PROMPT for issue #1"
    ev = EscalationEvidence(reasons=(EscalationReason.LOW_CONFIDENCE_ROUTE,),
                            tentative_route="ready", tentative_complexity="standard",
                            evidence_digest="found the change lives in module X; premise holds",
                            open_question="ready vs grill on the edge case")
    prompt = escalation_prompt(base, ev)
    # It carries the original, but is strictly more — the bounded carry-forward block — so it is
    # a refinement, never a verbatim resubmission.
    assert base in prompt
    assert prompt != base and len(prompt) > len(base)
    assert "low-confidence-route" in prompt
    assert "found the change lives in module X" in prompt
    assert "do not re-ground" in prompt.lower() or "refine" in prompt.lower()


def test_escalation_refuses_to_build_a_blind_rerun_without_evidence():
    # This is the test that fails if escalation degenerates to a blind rerun: no typed reason or
    # no prior grounding means there is nothing to refine, so the builder must refuse.
    with pytest.raises(ValueError):
        escalation_prompt("base", EscalationEvidence(reasons=(), evidence_digest="something"))
    with pytest.raises(ValueError):
        escalation_prompt("base", EscalationEvidence(
            reasons=(EscalationReason.EVIDENCE_GAP,), evidence_digest="   "))


def test_evidence_from_result_carries_the_typed_reasons_and_tentative_dials():
    std = IntakeResult(IntakeRoute.ESCALATE, body="tentative brief",
                       complexity=Complexity.STANDARD, detail="unsure on route",
                       escalation_reasons=(EscalationReason.UNRESOLVED_FORK,))
    ev = evidence_from_result(std)
    assert ev.reasons == (EscalationReason.UNRESOLVED_FORK,)
    assert ev.tentative_complexity == "standard"
    assert ev.evidence_digest == "tentative brief"
    assert ev.is_actionable
