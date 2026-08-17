"""A session cut off at its turn cap is a clock-class ending, named as such (#411).

Driven through the real surfaces: the Claude classifier reads the recorded shape of a turn-cap
ending exactly as the provider adapter does, the coordinator settles it through
``submit_stage``/``cycle``, and the two maintainer-facing handoffs compose their copy from the
persisted hold reason.
"""

from __future__ import annotations

from conftest import FakeSession, record_of

from agentflow.coordinated_build import _EXHAUSTED_STATUS, _TURN_CAP_STATUS, _hold_status
from agentflow.coordinator import Submission
from agentflow.coordinator.coordinator import (TURN_CAP_HOLD_CLAUSE, ended_at_turn_cap,
                                               permanent_hold_reason)
from agentflow.coordinator.providers import EndingReason, ProviderCause, classify_claude
from agentflow.coordinator.record import Record
from agentflow.pr_park import review_park_missing

# The closing record a session stopped at its ceiling really writes — copied from the review of
# PR #398 that parked overnight as "unknown": a reported failure, an explanatory message, and no
# HTTP-style status anywhere on it.
_TURN_CAP_RESULT = {
    "type": "result", "subtype": "error_max_turns", "is_error": True,
    "terminal_reason": "max_turns",
    "errors": ["Reached maximum number of turns (40)"],
    "result": "Reached maximum number of turns (40)",
}


def _turn_cap_stream() -> list[dict]:
    return [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Still working through the review checklist…"}]}},
        dict(_TURN_CAP_RESULT),
    ]


# --- the fact the adapter extracts ---------------------------------------------------------

def test_a_session_stopped_at_its_turn_cap_is_a_clock_class_ending():
    """Fails on today's code, where the reported failure with no status claims the record as
    unknown and the turn cap's own mapping is never consulted."""
    obs = classify_claude(_turn_cap_stream(), exit_status=1, has_end_fact=True)
    assert obs.cause is ProviderCause.TIMEOUT
    assert obs.classification() == "recoverable"
    assert obs.ending_reason is EndingReason.TURN_CAP     # which ceiling, not just "a ceiling"
    assert _TURN_CAP_RESULT not in obs.unrecognized       # recognized, not filed as unread


def test_the_wall_clock_deadline_stays_a_clock_class_ending_of_its_own_kind():
    # Both ceilings end in the same class; only the reason tells an operator which one fired.
    obs = classify_claude([], timed_out=True)
    assert obs.cause is ProviderCause.TIMEOUT
    assert obs.ending_reason is EndingReason.UNSPECIFIED


def test_a_typed_status_still_beats_the_subtype_on_the_same_record():
    # A session can be cut off *and* rate-limited; capacity is the fact that decides when to
    # retry, so it must keep precedence over the ending the subtype names.
    for status, cause in ((429, ProviderCause.CAPACITY), (403, ProviderCause.PERMANENT),
                          (503, ProviderCause.SERVER)):
        event = dict(_TURN_CAP_RESULT, api_error_status=status)
        obs = classify_claude([event])
        assert obs.cause is cause
        assert obs.ending_reason is not EndingReason.TURN_CAP


def test_a_failure_nothing_can_type_is_still_unknown_and_still_preserved():
    # The fail-safe is untouched: only a subtype the table actually models is recognized.
    event = {"type": "result", "subtype": "error_something_new", "is_error": True,
             "errors": ["a shape this build has never seen"]}
    obs = classify_claude([event])
    assert obs.cause is ProviderCause.UNKNOWN
    assert obs.classification() == "unknown"
    assert obs.unrecognized == (event,)                   # preserved verbatim


# --- what the pipeline does with it --------------------------------------------------------

class _TurnCapObserver:
    """Observes every ended family as the real classifier reads a turn-cap stream."""

    def observe(self, record):
        return classify_claude(_turn_cap_stream(), exit_status=1, has_end_fact=True)

    def verify(self, record, obs) -> bool:
        return False


def _run_to_hold(make_coord, subject: str, stage: str, lines: list[str]) -> tuple:
    fake = FakeSession()
    coord = make_coord(fake, adapter=_TurnCapObserver(), log=lines.append)
    identity = coord.submit_stage(Submission(repo="o/r", subject=subject, stage=stage,
                                             pool="claude", builder_lineage="claude"))
    for _ in range(8):
        outcomes = coord.cycle("claude")
        if any(o.identity == identity and o.status == "held" for o in outcomes):
            return coord, identity
        fake.kill(identity)
    raise AssertionError("record never reached a hold")


def test_the_daemon_log_names_the_turn_cap_instead_of_an_unknown_ending(make_coord):
    """Fails on today's code, which writes 'interrupted (unknown)' for the fleet's most common
    ending — and would read 'interrupted (timeout)' if the class alone were carried."""
    lines: list[str] = []
    _run_to_hold(make_coord, "398", "review", lines)

    assert any("interrupted (turn cap)" in line for line in lines)
    assert not any("interrupted (unknown)" in line for line in lines)
    assert not any("interrupted (timeout)" in line for line in lines)


def test_a_stage_that_spent_its_budget_at_the_turn_cap_records_which_ceiling_stopped_it(
        make_coord):
    coord, identity = _run_to_hold(make_coord, "399", "review", [])
    record = record_of(coord, identity)

    assert record.state == "held"
    assert record.hold_reason.startswith("continuation budget exhausted")
    assert ended_at_turn_cap(record.hold_reason)
    assert TURN_CAP_HOLD_CLAUSE in record.hold_reason


def test_a_stage_that_spent_its_budget_at_the_wall_clock_records_that_ceiling(make_coord):
    """The wall-clock deadline is the other clock-class ceiling (#737): the daemon log already
    told the two apart, but the durable record carried no clause for it, so a review the clock
    killed three times parked with the flat executions-failed sentence."""
    from agentflow.coordinator.coordinator import WALL_CLOCK_HOLD_CLAUSE, ended_at_wall_clock

    fake = FakeSession()
    coord = make_coord(fake, adapter=fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="401", stage="review",
                                             pool="claude"))
    for _ in range(8):
        outcomes = coord.cycle("claude")
        if any(o.identity == identity and o.status == "held" for o in outcomes):
            break
        fake.end(identity, cause=ProviderCause.TIMEOUT)
    record = record_of(coord, identity)

    assert record.state == "held"
    assert record.hold_reason.startswith("continuation budget exhausted")
    assert WALL_CLOCK_HOLD_CLAUSE in record.hold_reason
    assert ended_at_wall_clock(record.hold_reason)
    assert not ended_at_turn_cap(record.hold_reason)


def test_the_parked_review_names_the_wall_clock_when_that_ceiling_killed_it():
    from agentflow.coordinator.coordinator import WALL_CLOCK_HOLD_CLAUSE

    record = Record(identity="o/r|398|review|sha-a", stage="review", pool="claude", demand=1,
                    repo="o/r", subject="398", target="sha-a",
                    hold_reason="continuation budget exhausted" + WALL_CLOCK_HOLD_CLAUSE)
    missing = review_park_missing(record)

    assert "45-minute wall-clock limit" in missing
    assert "having produced nothing" in missing
    assert "the review executions failed" not in missing
    assert "Do not treat this as a clean review." in missing


def test_a_budget_spent_without_the_turn_cap_keeps_the_plain_exhaustion_reason(make_coord):
    # The control: an ending that is not the ceiling must not borrow the ceiling's words.
    fake = FakeSession()
    coord = make_coord(fake, adapter=fake)
    identity = coord.submit_stage(Submission(repo="o/r", subject="400", stage="review",
                                             pool="claude"))
    for _ in range(8):
        outcomes = coord.cycle("claude")
        if any(o.identity == identity and o.status == "held" for o in outcomes):
            break
        fake.end(identity, cause=ProviderCause.PROCESS)
    assert not ended_at_turn_cap(record_of(coord, identity).hold_reason)


# --- the two comments a maintainer reads ---------------------------------------------------

def test_the_build_hold_comment_tells_a_severed_session_from_a_spent_budget():
    """Fails on today's code, where a build whose sessions were cut off mid-work reads exactly
    like one that tried everything it could think of."""
    cut_off, headline = _hold_status("continuation budget exhausted" + TURN_CAP_HOLD_CLAUSE)
    spent, spent_headline = _hold_status("continuation budget exhausted")

    assert cut_off == _TURN_CAP_STATUS
    assert "turn ceiling" in cut_off                      # says which ceiling stopped it
    assert "cut off" in cut_off and "cut off" not in spent
    assert spent == _EXHAUSTED_STATUS                     # a genuinely spent budget is unchanged
    assert headline != spent_headline
    # A permanent condition still reads as itself, not as a ceiling.
    assert _hold_status(permanent_hold_reason(EndingReason.ACCESS))[0] not in (cut_off, spent)


def test_the_parked_review_says_it_was_cut_off_rather_than_out_of_ideas():
    """Fails on today's code, which tells the maintainer the review ran out of budget however it
    actually stopped — the exact line PR #393 parked overnight with."""
    def review(hold_reason):
        return Record(identity="o/r|398|review|sha-a", stage="review", pool="claude", demand=1,
                      repo="o/r", subject="398", target="sha-a", hold_reason=hold_reason)

    cut_off = review_park_missing(review("continuation budget exhausted" + TURN_CAP_HOLD_CLAUSE))
    spent = review_park_missing(review("continuation budget exhausted"))

    assert "turn ceiling" in cut_off and "cut off" in cut_off   # names which ceiling
    assert "turn ceiling" not in spent and "cut off" not in spent
    assert "Do not treat this as a clean review." in cut_off    # the fail-safe line survives both
    assert "Do not treat this as a clean review." in spent
