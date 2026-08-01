"""A session whose shell never started is an environment fault, not a retried budget (#386).

Driven through the real surfaces: the Claude classifier reads a fixture stream exactly as the
provider adapter does, the coordinator settles it through ``submit_stage``/``cycle``, and the
two stage handoffs compose their maintainer-facing copy from the persisted hold reason.
"""

from __future__ import annotations

from conftest import FakeSession, permits, record_of

from agentflow.coordinated_build import (_ENVIRONMENT_STATUS, _EXHAUSTED_STATUS, _hold_status,
                                         _marker_status, resume_if_held)
from agentflow.coordinator import Submission
from agentflow.coordinator.coordinator import (PERMANENT_HOLD_REASON, parse_permanent_hold_reason,
                                               permanent_hold_reason)
from agentflow.coordinator.providers import (EndingReason, ProviderCause, classify_claude,
                                             classify_codex)
from agentflow.intake import _provider_failed
from agentflow.shell_crib import SHELL_CRIB

# The harness's own line when it cannot bring the shell process into existence — the refusal
# that took out four consecutive sessions on ciq-autotune #493 (ADR 0050).
_REFUSED_AT_SPAWN = (
    "Could not start /bin/zsh: the command line plus environment exceed the OS exec argument "
    "limit (E2BIG). At spawn: command line 2.4MB across 3 args.")


def _shell_call(call_id: str, command: str) -> dict:
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": call_id, "name": "Bash", "input": {"command": command}}]}}


def _shell_result(call_id: str, text: str, *, is_error: bool) -> dict:
    return {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": call_id, "is_error": is_error,
         "content": text}]}}


def _dead_shell_stream() -> list[dict]:
    """A session that asked for a shell twice, was refused at spawn both times, and gave up."""
    return [
        {"type": "system", "subtype": "init"},
        _shell_call("toolu_1", "git -C /w status"),
        _shell_result("toolu_1", _REFUSED_AT_SPAWN, is_error=True),
        _shell_call("toolu_2", "ls /w"),
        _shell_result("toolu_2", _REFUSED_AT_SPAWN, is_error=True),
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "My shell will not start; I cannot reach the work."}]}},
        {"type": "result", "subtype": "success", "result": "stopped: no shell"},
    ]


# --- the fact the adapter extracts ---------------------------------------------------------

def test_a_session_whose_shell_never_started_is_an_environment_fault():
    # The distinguishing fact: it ends permanently — a human has to act, nothing lifts on its
    # own — for a reason that names the environment rather than the coding agent's provider.
    obs = classify_claude(_dead_shell_stream(), exit_status=0)
    assert obs.classification() == "permanent"
    assert obs.ending_reason is EndingReason.ENVIRONMENT


def test_a_clean_incomplete_session_is_not_an_environment_fault():
    # The control: the same clean ending with no shell refusal in it stays exactly what it was.
    stream = [
        _shell_call("toolu_1", "uv run pytest -q"),
        _shell_result("toolu_1", "3 passed", is_error=False),
        {"type": "result", "subtype": "success", "result": "done"},
    ]
    obs = classify_claude(stream, exit_status=0)
    assert obs.cause is ProviderCause.NONE
    assert obs.ending_reason is EndingReason.UNSPECIFIED


def test_a_session_that_ran_one_command_is_an_ordinary_rejection_not_a_dead_shell():
    # A shell that started once and then refused a command is the adjustable kind the shell crib
    # teaches a session to work around. Only "no command in this session ever ran" is the fault.
    stream = [
        _shell_call("toolu_1", "ls /w"),
        _shell_result("toolu_1", "AGENTS.md", is_error=False),
        _shell_call("toolu_2", "cd /w && ls"),
        _shell_result("toolu_2", _REFUSED_AT_SPAWN, is_error=True),
        {"type": "result", "subtype": "success", "result": "done"},
    ]
    assert classify_claude(stream, exit_status=0).classification() != "permanent"


def test_an_error_from_a_tool_that_is_not_the_shell_never_becomes_an_environment_fault():
    # Correlating each result back to its own tool-use block is what keeps this anchored: the
    # same words arriving from a non-shell tool prove nothing about the shell.
    stream = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"file_path": "/w/x"}}]}},
        _shell_result("toolu_1", _REFUSED_AT_SPAWN, is_error=True),
        {"type": "result", "subtype": "success", "result": "done"},
    ]
    assert classify_claude(stream, exit_status=0).classification() != "permanent"


def test_a_provider_condition_the_stream_reported_still_wins_over_a_dead_shell():
    # The provider's own typed fact is the real story; the environment fault only claims endings
    # that would otherwise read as an ordinary incomplete or interrupted session.
    stream = _dead_shell_stream() + [{"type": "assistant", "error": {"type": "rate_limit_error"}}]
    assert classify_claude(stream, exit_status=0).cause is ProviderCause.CAPACITY


def test_a_timed_out_session_with_a_dead_shell_is_still_an_environment_fault():
    # A session that hung because it had no shell must not be waited on: no reset lifts this.
    obs = classify_claude(_dead_shell_stream(), timed_out=True)
    assert obs.ending_reason is EndingReason.ENVIRONMENT


def test_a_codex_dead_shell_keeps_todays_classification():
    # Deliberate asymmetry (ADR 386): the Codex surface carries no typed tool-result fact to
    # correlate a refusal back to a shell call, and its prose never diagnoses.
    obs = classify_codex(exit_status=1, final_message=_REFUSED_AT_SPAWN)
    assert obs.cause is ProviderCause.UNKNOWN


# --- what the pipeline does with it --------------------------------------------------------

class _DeadShellObserver:
    """Observes every ended family as the real classifier reads a dead-shell stream."""

    def observe(self, record):
        return classify_claude(_dead_shell_stream(), exit_status=0, has_end_fact=True)

    def verify(self, record, obs) -> bool:
        return False


class _PendingDeadShellObserver(_DeadShellObserver):
    """Makes the external handoff fail once, exposing the restart boundary."""

    def __init__(self, handoff):
        self._handoff = handoff

    def finalize_hold(self, record):
        return self._handoff(record)


def test_a_dead_shell_holds_the_dispatched_stage_at_once_with_its_attempt(make_coord):
    """Fails on today's code: a dead shell reads as an ordinary incomplete ending, so the stage
    consumes its attempt, continues, and — at the budget — is recorded as having run out of
    tries. It must instead hold on the first ending while preserving the dispatched attempt's
    durable accounting."""
    fake = FakeSession()
    coord = make_coord(fake, adapter=_DeadShellObserver())
    identity = coord.submit_stage(Submission(repo="o/r", subject="493", stage="intake",
                                             pool="claude"))
    assert coord.cycle("claude") == []          # one attempt admitted and running
    assert record_of(coord, identity).attempts == 1
    fake.kill(identity)

    outcomes = coord.cycle("claude")
    assert [(o.identity, o.status) for o in outcomes] == [(identity, "held")]

    record = record_of(coord, identity)
    assert record.attempts == 1                  # the dispatched session is not erased on park
    assert record.hold_reason == permanent_hold_reason(EndingReason.ENVIRONMENT)
    assert record.hold_reason != "continuation budget exhausted"
    assert permits(coord, "claude") == 0         # the reservation is released, not looping


def test_a_dead_shell_never_requeues_a_continuation(make_coord):
    # A parked environment fault is terminal, so cycling again never launches a second provider.
    fake = FakeSession()
    coord = make_coord(fake, adapter=_DeadShellObserver())
    identity = coord.submit_stage(Submission(repo="o/r", subject="494", stage="intake",
                                             pool="claude"))
    coord.cycle("claude")
    fake.kill(identity)
    coord.cycle("claude")

    for _ in range(3):
        assert coord.cycle("claude") == []
    assert record_of(coord, identity).state == "held"
    assert record_of(coord, identity).attempts == 1


def test_a_maintainer_resume_of_an_environment_hold_starts_a_fresh_budget(make_coord):
    # Resume creates a distinct execution, so the human gets a fresh bounded run without
    # rewriting what the parked, dispatched session actually consumed.
    fake = FakeSession()
    coord = make_coord(fake, adapter=_DeadShellObserver())
    sub = Submission(repo="o/r", subject="495", stage="build", pool="claude",
                     complexity="deep", builder_lineage="claude")
    identity = coord.submit_stage(sub)
    coord.cycle("claude")
    fake.kill(identity)
    coord.cycle("claude")

    held = record_of(coord, identity)
    assert held.state == "held" and held.attempts == 1

    resumed = resume_if_held(sub, list(coord._store.load().values()))
    assert resumed.resume == 1
    successor = coord.submit_stage(resumed)
    assert successor != identity
    assert record_of(coord, successor).attempts == 0     # the full ATTEMPT_BUDGET is available


def test_a_restart_finalizes_a_pending_dead_shell_hold_with_its_dispatched_attempt(make_coord):
    """The pending durable boundary keeps the attempt when the external handoff needs a retry."""
    fake = FakeSession()
    proofs = iter((None, "issue-proof"))
    adapter = _PendingDeadShellObserver(handoff=lambda record: next(proofs))
    coord = make_coord(
        fake,
        adapter=adapter,
    )
    identity = coord.submit_stage(Submission(repo="o/r", subject="496", stage="build",
                                             pool="claude", complexity="deep",
                                             builder_lineage="claude"))
    coord.cycle("claude")
    fake.kill(identity)

    assert coord.cycle("claude") == []
    pending = record_of(coord, identity)
    assert pending.hold_pending is True and pending.attempts == 1

    restarted = make_coord(fake, adapter=adapter)
    assert [outcome.status for outcome in restarted.cycle("claude")] == ["held"]
    held = record_of(restarted, identity)
    assert held.attempts == 1 and held.handoff_proof == "issue-proof"


# --- the two comments a maintainer reads ---------------------------------------------------

def test_the_intake_hold_for_an_environment_fault_names_the_fault_and_its_remedy():
    # Exactly the two lines the intake handoff runs to pick its copy from the persisted reason.
    reason = permanent_hold_reason(EndingReason.ENVIRONMENT)
    assert reason.startswith(PERMANENT_HOLD_REASON)         # keeps the existing hold path
    body = _provider_failed(reason, parse_permanent_hold_reason(reason).value).body

    assert "couldn't give it a working command line" in body
    assert "leftover session checkouts" in body             # the remedy, in the maintainer's terms
    assert "Reclaim" in body
    assert "budget" not in body and "attempt" not in body   # never blames the agent's persistence
    # Fixed text: a restarted daemon recomposes it byte-identically, so the handoff posts once.
    assert body == _provider_failed("a different detail string", "environment").body


def test_each_permanent_condition_keeps_its_own_intake_diagnosis():
    # The environment body is a fifth fixed body, not a replacement for ADR 342's four.
    bodies = {reason: _provider_failed("d", reason).body
              for reason in ("access", "rejected-request", "spend", "unspecified", "environment")}
    assert len(set(bodies.values())) == 5
    assert "Re-authenticate" in bodies["access"]


def test_the_build_hold_comment_no_longer_collapses_every_ending_into_exhaustion():
    """Fails on today's code, where every non-collision hold reads as a spent budget."""
    environment, _ = _hold_status(permanent_hold_reason(EndingReason.ENVIRONMENT))
    access, _ = _hold_status(permanent_hold_reason(EndingReason.ACCESS))
    exhausted, _ = _hold_status("continuation budget exhausted")
    collision, _ = _hold_status("integration collision")

    assert environment == _ENVIRONMENT_STATUS
    assert "leftover session checkouts" in environment
    assert "sign-in" in access
    assert len({environment, access, exhausted, collision}) == 4
    assert exhausted == _EXHAUSTED_STATUS            # a genuinely spent budget is unchanged


def test_an_already_held_build_composes_the_same_handoff_marker_as_before():
    """The post-once marker must not move for any reason a record can already carry, or every
    held issue would look unheld and get a second comment on deploy."""
    collision_status = ("could not rebase past a collision with newer changes on the main branch "
                        "and stopped without resolving it")

    def todays_status(reason):
        # The expression this module used before #386, kept literal as the regression anchor.
        return (collision_status if reason == "integration collision"
                else "continuation budget exhausted")

    for reason in ("integration collision", "continuation budget exhausted",
                   "no new recovery state to act on", "completed stage has no successor",
                   permanent_hold_reason(EndingReason.ACCESS),
                   permanent_hold_reason(EndingReason.UNSPECIFIED), None):
        assert _marker_status(reason) == todays_status(reason)

    # And the wording a permanent hold now displays is genuinely different from what it keys on,
    # which is the whole reason the two are separate.
    permanent = permanent_hold_reason(EndingReason.ACCESS)
    assert _hold_status(permanent)[0] != _marker_status(permanent)


# --- what the session is told --------------------------------------------------------------

def test_the_shell_crib_tells_a_session_a_dead_shell_is_not_adjustable():
    assert "could not START your shell" in SHELL_CRIB
    assert "NOT an adjustable rejection" in SHELL_CRIB
    assert "Do not try variants." in SHELL_CRIB
    # The prompts carrying the crib are str.format-rendered, so a brace here breaks every render.
    assert "{" not in SHELL_CRIB and "}" not in SHELL_CRIB
