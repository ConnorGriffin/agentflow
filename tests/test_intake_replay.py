"""The offline Intake replay harness (Wayfinder #2, #228), tested through its public
interface: the three arms run over one identical pinned corpus, arm C's two-attempt spend is
summed, scoring stays blind to the arm, and unrun issues are logged as explicit gaps rather
than silently counted as covered.
"""

import json
from pathlib import Path

import pytest

from agentflow.coordinator.telemetry import AttemptUsage
from agentflow.experiments.replay import (Arm, AttemptReply, BlindBrief, CorpusEntry, Corpus,
                                          RecordedResponder, load_corpus, reference_scorer,
                                          run_arm, run_experiment)
from agentflow.experiments.spend import CLAUDE, headroom_weight

CORPUS_PATH = Path("agentflow/experiments/data/intake_replay_corpus.json")


def _usage(cost=1.0, out=1000):
    return AttemptUsage(input_tokens=500, cache_creation_tokens=0,
                        cached_input_tokens=0, output_tokens=out, cost_usd=cost)


def _reply(route, *, complexity="deep", effort="high", reasons=None, out=1000, cost=1.0):
    payload = {"route": route, "title": "t", "body": "brief body",
               "complexity": complexity, "effort": effort}
    if reasons is not None:
        payload["escalation_reasons"] = reasons
    return AttemptReply(payload=json.dumps(payload), usage=_usage(cost=cost, out=out))


def _entry(number, *, title="feature: ordinary widget", body="do the thing",
           route="ready", complexity="standard", effort="low"):
    return CorpusEntry(repo="o/r", repo_ref="sha", number=number, title=title, body=body,
                       reference_route=route, reference_complexity=complexity,
                       reference_effort=effort,
                       strata=(f"route:{route}", f"complexity:{complexity}"))


# --- the committed corpus is real, stratified, and pinned -------------------------------

def test_committed_corpus_is_stratified_and_pinned():
    corpus = load_corpus(CORPUS_PATH)
    assert 30 <= len(corpus.entries) <= 50
    # Every entry pins a repository SHA so the replay is reproducible.
    assert all(len(e.repo_ref) >= 7 for e in corpus.entries)
    mix = corpus.strata_mix()
    # Both complexity strata and the hold route are represented (auditable sample).
    assert mix.get("complexity:deep", 0) >= 5 and mix.get("complexity:standard", 0) >= 5
    assert mix.get("route:grill", 0) >= 1
    # The provenance / truncation note is recorded, never silent.
    assert any("truncation" in n.lower() for n in corpus.notes)


# --- all three arms run over the identical corpus ---------------------------------------

def _corpus():
    return Corpus(entries=(
        _entry(1),                                            # ordinary → standard/no escalate
        _entry(2, title="coordinator: race condition fix", body="concurrency + idempotency"),
    ))


def _responder():
    return RecordedResponder(replies={
        # issue 1: deep (arm A), standard (arm B/C) — standard settles, no escalation.
        (1, "deep", "deep"): _reply("ready", complexity="deep", cost=2.0, out=1000),
        (1, "standard", "standard"): _reply("ready", complexity="standard", cost=0.5, out=400),
        # issue 2: has a direct-deep trigger (race/concurrency) → arm C goes straight to deep.
        (2, "deep", "deep"): _reply("ready", complexity="deep", cost=2.0, out=1000),
        (2, "standard", "standard"): _reply("ready", complexity="deep", cost=0.5, out=400),
    })


def test_all_three_arms_run_over_the_same_corpus():
    runs = run_experiment(_corpus(), _responder())
    assert set(runs) == set(Arm)
    for arm, run in runs.items():
        assert len(run.issues) == 2, arm


def test_arm_c_direct_deep_trigger_spends_one_deep_attempt():
    # Issue 2 trips a deterministic direct-deep trigger, so arm C skips the standard attempt.
    run = run_arm(Arm.C_ESCALATE, _corpus(), _responder())
    issue2 = next(i for i in run.issues if i.number == 2)
    assert issue2.direct_deep is True
    assert [a.tier for a in issue2.attempts] == ["deep"]


def test_arm_c_escalation_sums_both_attempts_into_issue_spend():
    corpus = Corpus(entries=(_entry(10, title="plain", body="plain body"),))
    responder = RecordedResponder(replies={
        (10, "standard", "standard"): _reply("escalate", complexity="standard",
                                             reasons=["low-confidence-route"], cost=0.4),
        (10, "deep", "escalate"): _reply("ready", complexity="deep", cost=1.8),
    })
    run = run_arm(Arm.C_ESCALATE, corpus, responder)
    issue = run.issues[0]
    assert issue.escalated is True
    assert [a.kind for a in issue.attempts] == ["standard", "escalate"]
    # Per-issue spend is the sum of BOTH attempts (#228: arm C includes its two-attempt cost).
    expected = sum(headroom_weight(a.usage, CLAUDE) for a in issue.attempts)
    assert issue.headroom == expected
    assert issue.cost_usd == pytest.approx(0.4 + 1.8)


def test_arm_c_escalation_does_not_resubmit_the_original_prompt_blind():
    # The deep attempt in arm C must receive the bounded carry-forward prompt, not the original.
    seen = {}

    class SpyResponder(RecordedResponder):
        def respond(self, *, issue_number, tier, kind, prompt):
            seen[(kind, tier)] = prompt
            return super().respond(issue_number=issue_number, tier=tier, kind=kind, prompt=prompt)

    corpus = Corpus(entries=(_entry(11, title="plain", body="plain body"),))
    responder = SpyResponder(replies={
        (11, "standard", "standard"): _reply("escalate", complexity="standard",
                                             reasons=["evidence-gap"], cost=0.4),
        (11, "deep", "escalate"): _reply("ready", complexity="deep", cost=1.8),
    })
    run_arm(Arm.C_ESCALATE, corpus, responder)
    original = seen[("standard", "standard")]
    escalated = seen[("escalate", "deep")]
    assert escalated != original
    assert "PRIOR STANDARD ATTEMPT" in escalated and "evidence-gap" in escalated


# --- scoring stays blind ----------------------------------------------------------------

def test_scorer_receives_no_arm_label():
    # The scorer is handed an arm-free BlindBrief; there is no arm field to leak.
    entry = _entry(1, route="ready", complexity="standard")
    brief = BlindBrief(issue_number=1, route="ready", complexity="standard", effort="low",
                       body="b")
    assert not hasattr(brief, "arm")
    scores = reference_scorer(brief, entry)
    assert scores.route_correct == 1.0 and scores.complexity_correct == 1.0
    # The three judgment axes are left unscored for a blind human/LLM scorer.
    assert scores.verified_evidence is None


def test_reference_scorer_marks_a_wrong_route():
    entry = _entry(1, route="ready", complexity="deep")
    brief = BlindBrief(issue_number=1, route="grill", complexity="", effort="", body="b")
    scores = reference_scorer(brief, entry)
    assert scores.route_correct == 0.0


# --- median spend per arm ---------------------------------------------------------------

def test_spend_summary_reports_median_headroom_per_arm():
    runs = run_experiment(_corpus(), _responder())
    a = runs[Arm.A_DEEP].spend()
    b = runs[Arm.B_STANDARD].spend()
    assert a["issues"] == 2 and b["issues"] == 2
    assert a["median_headroom"] > 0 and b["median_headroom"] > 0
    # Arm B (all standard) spends less than arm A (all deep) on this fixture.
    assert b["total_headroom"] < a["total_headroom"]


# --- explicit gap logging ---------------------------------------------------------------

def test_missing_recording_is_logged_as_a_gap_not_silent_coverage():
    corpus = Corpus(entries=(_entry(1), _entry(2)))
    responder = RecordedResponder(replies={
        (1, "deep", "deep"): _reply("ready", complexity="deep"),
        # issue 2 has no deep recording → a gap for arm A.
    })
    run = run_arm(Arm.A_DEEP, corpus, responder)
    assert len(run.issues) == 1                      # only the covered issue is scored
    assert run.gaps == [(2, str((2, "deep", "deep")))]
    assert any("gaps" in n or "coverage" in n for n in run.notes)
