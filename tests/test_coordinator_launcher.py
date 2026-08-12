"""The crash-safe launcher handshake survives daemon death at every boundary (ADR 0030).

Every boundary is driven through the coordinator's public ``submit_stage`` / ``cycle`` seam:
a launch is admitted, the world (an injected :class:`FakeSession`) is left in the state a
crash would leave it, and a fresh coordinator over the same durable store reconciles. A
reservation that never durably started releases its permits and keeps the full attempt
budget; a durable ``started`` consumes exactly one attempt and keeps its reservation while
its family is alive. A separate test exercises the real spawning launcher end to end.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import (FakeSession, NeverStartsLauncher, permits, record_of,
                      starts_until_held)

from agentflow.coordinator import Coordinator, Submission
from agentflow.coordinator.admission import ATTEMPT_BUDGET
from agentflow.coordinator.coordinator import RESTART_RESUME_CAP
from agentflow.coordinator import launcher as launcher_mod
from agentflow.coordinator.launcher import LocalLauncher, NOT_STARTED, pid_family_alive
from agentflow.coordinator.record import Record
from agentflow.coordinator.providers import ProviderCause


def review(subject: str = "7", pool: str = "codex") -> Submission:
    return Submission(repo="o/r", subject=subject, stage="review", pool=pool)


def test_reservation_that_never_started_recovers_with_the_full_budget(make_coord):
    fake = FakeSession()
    fake.crash_start = True
    crashed = make_coord(fake)
    identity = crashed.submit_stage(review())
    with pytest.raises(RuntimeError):
        crashed.cycle("codex")
    assert permits(crashed, "codex") == 2  # ambiguous running reservation fails closed

    fake.crash_start = False
    recovered = make_coord(fake)
    # No durable start existed, so no attempt was consumed: the record still has all three.
    assert starts_until_held(recovered, fake, identity, "codex") == 3


def test_durable_started_then_dead_recovery_consumes_exactly_one_attempt(make_coord):
    fake = FakeSession()
    started = make_coord(fake)
    identity = started.submit_stage(review())
    started.cycle("codex")            # a durable `started` is written and the family is alive
    fake.kill(identity)               # the provider died before the daemon could observe it

    recovered = make_coord(fake)
    # The durable `started` counts, so only two attempts remain before the hold.
    assert starts_until_held(recovered, fake, identity, "codex") == 2


@pytest.mark.parametrize(("result", "legacy_exit"), [
    ({"exit_status": 17, "signal": None, "timed_out": False}, None),
    (None, 17),
    ({}, 17),
    ({"exit_status": 17, "signal": None}, 17),
    ({"exit_status": "17", "signal": None, "timed_out": False}, 17),
    ({"exit_status": 17, "signal": None, "timed_out": False, "unknown": True}, 17),
    ("recursive", 17),
    ("oversized", 17),
    ("duplicate", 17),
])
def test_public_coordinator_recovery_accepts_only_current_or_legacy_terminal_facts(
        coord_state, result, legacy_exit):
    """Fresh recovery accepts writer schema or `.exit`, never an invalid result object."""
    from agentflow.coordinator.launcher import STARTED, StartResult
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.session import exit_path, result_path

    class CorruptEndedLauncher:
        starts = 0

        def start(self, record, store):
            self.starts += 1
            terminal = result_path(store.path, record.launch_token)
            terminal.parent.mkdir(parents=True, exist_ok=True)
            if result == "recursive":
                terminal.write_bytes(b"[" * 10000 + b"0" + b"]" * 10000)
            elif result == "oversized":
                terminal.write_bytes(b" " * (4096 + 1))
            elif result == "duplicate":
                terminal.write_bytes(
                    b'{"exit_status":0,"exit_status":99,"signal":null,"timed_out":false}')
            elif result is not None:
                terminal.write_text(json.dumps(result))
            if legacy_exit is not None:
                exit_path(store.path, record.launch_token).write_text(f"{legacy_exit}\n")
            return StartResult(STARTED, "corrupt-ended-family")

        @staticmethod
        def is_alive(_family):
            return False

    launcher = CorruptEndedLauncher()
    admission = {"open": True}
    coord = Coordinator(launcher=launcher, adapter=ClaudeProviderAdapter(),
                        gate=lambda _record: admission["open"])
    identity = coord.submit_stage(review(subject="recursive-result", pool="claude"))
    assert coord.cycle("claude") == []
    assert launcher.starts == 1

    admission["open"] = False
    recovered = Coordinator(launcher=launcher, adapter=ClaudeProviderAdapter(),
                            gate=lambda _record: admission["open"])
    recovered.cycle("claude")
    assert launcher.starts == 1, f"recovery replayed ended attempt {identity}"


def test_durable_started_and_alive_recovery_keeps_the_reservation(make_coord):
    fake = FakeSession()
    started = make_coord(fake)
    identity = started.submit_stage(review())
    started.cycle("codex")            # started and alive

    recovered = make_coord(fake)
    assert recovered.cycle("codex") == []       # a live family is neither released nor duplicated
    assert permits(recovered, "codex") == 2
    assert recovered.cycle("codex") == []       # idempotent across repeated reconciliation
    assert permits(recovered, "codex") == 2


# --- ADR 0030 / issue #175: a daemon restart resumes an attempt without charging it ---------

def test_daemon_bounce_that_leaves_the_family_alive_is_recovered_not_resumed(make_coord):
    """A plain SIGTERM bounce leaves the family alive (ADR 0030's setsid). Even under a fresh
    daemon generation, an alive family is the healthy recovered-running path: its attempt is
    unchanged, its reservation retained, and it is never treated as a restart resume."""
    fake = FakeSession()
    started = make_coord(fake, daemon_generation="daemon-1")
    identity = started.submit_stage(review())
    started.cycle("codex")                       # attempt 1 running, family alive

    restarted = make_coord(fake, daemon_generation="daemon-2")
    assert restarted.cycle("codex") == []        # the live family is observed, never re-launched
    durable = record_of(restarted, identity)
    assert durable.attempts == 1 and not durable.continuation
    assert durable.restart_resumes == 0          # a live family is recovered, not resumed
    assert permits(restarted, "codex") == 2      # its reservation is retained, not released


def test_daemon_restart_that_kills_the_family_resumes_without_charging_an_attempt(make_coord):
    """A restart/reboot that also kills the running family leaves no supervisor end fact. A fresh
    daemon (new generation) attributes that death to the daemon lifecycle and re-runs the *same*
    attempt in place: the attempt count stays flat, it is not a consumed continuation, and its
    permits are released and cleanly re-reserved. This fails if the death is charged as before."""
    fake = FakeSession()
    started = make_coord(fake, daemon_generation="daemon-1")
    identity = started.submit_stage(review())
    started.cycle("codex")                       # attempt 1 running under daemon-1
    assert record_of(started, identity).attempts == 1
    fake.kill(identity)                          # a reboot takes the family down — no end fact

    restarted = make_coord(fake, daemon_generation="daemon-2")
    restarted.cycle("codex")
    durable = record_of(restarted, identity)
    assert durable.attempts == 1                 # same attempt, not charged again
    assert durable.state == "running" and not durable.continuation
    assert durable.restart_resumes == 1
    assert permits(restarted, "codex") == 2      # permits released and cleanly re-reserved


def test_repeated_daemon_bounces_keep_attempts_flat_then_genuine_retries_park(make_coord):
    """Repeated restarts mid-session never advance the attempt count, but genuine provider
    failures (each leaving an end fact) still consume attempts and park at the budget — the two
    edges the fix must keep separate."""
    fake = FakeSession()
    coord = make_coord(fake, daemon_generation="d0")
    identity = coord.submit_stage(review())
    coord.cycle("codex")
    for i in range(1, 4):
        fake.kill(identity)                      # a restart kills the family, no end fact
        coord = make_coord(fake, daemon_generation=f"d{i}")
        coord.cycle("codex")
        durable = record_of(coord, identity)
        assert durable.attempts == 1             # flat across every bounce
        assert durable.restart_resumes == i

    # Attempt 1 is running again. Now genuine failures (end fact present) exhaust the budget.
    parked = False
    for _ in range(ATTEMPT_BUDGET + 2):
        if record_of(coord, identity).state == "running":
            fake.end(identity, cause=ProviderCause.UNKNOWN)  # a real failure — leaves an end fact
        outcomes = coord.cycle("codex")
        if any(o.identity == identity and o.status == "held" for o in outcomes):
            parked = True
            break
    assert parked
    assert record_of(coord, identity).attempts == ATTEMPT_BUDGET  # parked exactly at the budget


def test_clean_but_incomplete_run_still_consumes_even_across_a_restart(make_coord):
    """A clean exit-0 incomplete run leaves a supervisor end fact even though its cause is NONE.
    Because the resume keys on end-fact *absence*, not on the cause, a generation change must not
    let this masquerade as a restart kill — it still consumes an attempt and continues."""
    fake = FakeSession()
    started = make_coord(fake, daemon_generation="daemon-1")
    identity = started.submit_stage(review())
    started.cycle("codex")                       # attempt 1 running under daemon-1
    fake.end(identity, cause=ProviderCause.NONE)  # clean-but-incomplete: an end fact is present

    restarted = make_coord(fake, daemon_generation="daemon-2")
    restarted.cycle("codex")
    durable = record_of(restarted, identity)
    assert durable.restart_resumes == 0          # never resumed — the end fact overrides the restart
    assert durable.attempts == 2 and durable.continuation  # consumed and continued, as today


def test_restart_resume_is_bounded_so_a_persistent_crash_loop_still_parks(make_coord):
    """A family that keeps dying with no end fact under a fresh daemon each time cannot resume
    forever: after the cap it stops resuming, consumes attempts, and parks."""
    fake = FakeSession()
    coord = make_coord(fake, daemon_generation="g-start")
    identity = coord.submit_stage(review())
    coord.cycle("codex")
    parked = False
    for i in range(RESTART_RESUME_CAP + ATTEMPT_BUDGET + 2):
        fake.kill(identity)                      # a restart kills the family, no end fact
        coord = make_coord(fake, daemon_generation=f"g{i}")
        outcomes = coord.cycle("codex")
        if any(o.identity == identity and o.status == "held" for o in outcomes):
            parked = True
            break
    assert parked
    assert record_of(coord, identity).restart_resumes == RESTART_RESUME_CAP


def test_launch_that_never_creates_a_family_records_not_started(make_coord):
    fake = FakeSession()
    coord = make_coord(fake, launcher=NeverStartsLauncher())
    identity = coord.submit_stage(review())
    assert coord.cycle("codex") == []
    assert permits(coord, "codex") == 0  # nothing started, so nothing reserved and no attempt


def test_real_launcher_spawns_a_provider_and_the_start_is_durable(coord_state):
    """The production launcher genuinely spawns a child that records a durable ``started``
    before ``exec``-replacing itself, so a fresh coordinator recovers a real, live family."""
    alive_provider = lambda record: [sys.executable, "-c", "import time; time.sleep(30)"]
    coord = Coordinator(launcher=LocalLauncher(alive_provider, timeout=5))
    identity = coord.submit_stage(review(pool="claude"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 1  # a real provider family is alive and reserved

    # A fresh coordinator over the same store reconciles the durable start and real liveness.
    recovered = Coordinator(launcher=LocalLauncher(alive_provider, timeout=5))
    assert recovered.cycle("claude") == []
    assert permits(recovered, "claude") == 1



def test_real_launcher_releases_when_the_spawned_provider_exits(coord_state):
    """A provider that exits is detected as a dead family and its permit is released."""
    gate = {"open": True}
    exiting_provider = lambda record: [sys.executable, "-c", ""]
    coord = Coordinator(launcher=LocalLauncher(exiting_provider, timeout=5),
                        gate=lambda record: gate["open"])
    coord.submit_stage(review(pool="claude"))
    assert coord.cycle("claude") == []
    assert permits(coord, "claude") == 1  # started

    time.sleep(0.5)                      # the provider exits
    gate["open"] = False                 # do not immediately re-admit the continuation
    coord.cycle("claude")
    assert permits(coord, "claude") == 0  # the dead family's reservation is released


def test_launched_session_is_observed_from_its_durable_artifacts(coord_state):
    """The wired provider path is real, not a no-op: a launched Claude family redirects its
    structured stream and exit to durable per-attempt artifacts, and the default provider
    observer reconstructs the full observation from them — a typed capacity cause with its
    reset, not a guess — which then drives the seam's continuation decision (ADR 0030)."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    emitted = [{"type": "assistant", "text": "working"},
               {"type": "rate_limit_event",
                "rate_limit_info": {"status": "rejected", "resetsAt": 900}}]
    script = ("import json,sys\n"
              + "\n".join(f"print(json.dumps({e!r}))" for e in emitted) + "\nsys.exit(0)\n")
    provider = lambda record: [sys.executable, "-c", script]

    coord = Coordinator(launcher=LocalLauncher(provider, timeout=5))
    identity = coord.submit_stage(Submission(repo="o/r", subject="obs", stage="review",
                                             pool="claude", input_ptr="a-brief"))
    assert coord.cycle("claude") == []       # launched; a real family recorded started
    time.sleep(0.5)                          # the provider emits its events and exits

    record = Store(default_store_path()).load()[identity]
    obs = ClaudeProviderAdapter().observe(record)
    assert obs.cause is ProviderCause.CAPACITY and obs.reset_at == 900
    assert any(e.get("type") == "assistant" for e in obs.events)   # events preserved
    assert obs.exit_status == 0

    # The same observation, through the seam, defers the stage as an eligible continuation.
    assert coord.cycle("claude", now=0) == []
    assert permits(coord, "claude") == 0     # released, waiting for its reset


def test_real_supervisor_preserves_partial_output_signal_and_timeout(coord_state):
    """The production path, not a fixture classifier, retains output written before a
    deadline and records both the supervisor timeout and the terminating signal."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    script = (
        "import sys,time\n"
        "print('partial stdout', flush=True)\n"
        "print('partial stderr', file=sys.stderr, flush=True)\n"
        "time.sleep(30)\n"
    )
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, session_timeout=0.1))
    identity = coord.submit_stage(review(subject="timed-out", pool="claude"))
    coord.cycle("claude")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = Store(default_store_path()).load()[identity]
        if not pid_family_alive(record.family):
            break
        time.sleep(0.02)
    else:
        pytest.fail("provider supervisor did not finish after its timeout")

    observation = ClaudeProviderAdapter().observe(record)
    assert observation.timed_out is True
    assert observation.signal in {15, 9}
    assert "partial stdout" in observation.partial_output
    assert "partial stderr" in observation.partial_output
    assert observation.cause is ProviderCause.TIMEOUT


def _wait_for_real_child(identity: str, message: str):
    from agentflow.coordinator.store import Store, default_store_path

    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        record = Store(default_store_path()).load()[identity]
        if not pid_family_alive(record.family):
            return record
        time.sleep(.02)
    pytest.fail(message)


def _build_observation(record):
    from agentflow.coordinator.providers import ClaudeProviderAdapter, CodexProviderAdapter

    if record.pool == "codex":
        return CodexProviderAdapter(account_of=lambda _record: None).observe(record)
    return ClaudeProviderAdapter().observe(record)


def _build(pool: str, subject: str, source: str) -> Submission:
    return Submission(repo="o/r", subject=subject, stage="build", pool=pool, source=source,
                      complexity="standard", effort="low", input_ptr="build")


def _codex_command_event(command: str, *, completed: bool = False) -> dict:
    return {"type": "item.completed" if completed else "item.started", "item": {
        "id": "t1", "type": "command_execution",
        "command": shlex.join(["/bin/zsh", "-lc", command]),
        "aggregated_output": "read output" if completed else "",
        "exit_code": 0 if completed else None,
        "status": "completed" if completed else "in_progress"}}


def test_build_head_progress_renews_its_child_local_silent_lease(coord_state, tmp_path):
    """A real Build child survives a short silent lease after committing new work (#570)."""
    from agentflow.coordinator.store import Store, default_store_path

    source = tmp_path / "build"
    source.mkdir()
    for command in (("git", "init", str(source)),
                    ("git", "-C", str(source), "config", "user.email", "test@example.com"),
                    ("git", "-C", str(source), "config", "user.name", "Test")):
        subprocess.run(command, check=True, capture_output=True)
    (source / "initial").write_text("initial")
    subprocess.run(("git", "-C", str(source), "add", "."), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "initial"),
                   check=True, capture_output=True)

    script = (
        "import pathlib,subprocess,sys,time\n"
        "time.sleep(.12)\n"
        "pathlib.Path(sys.argv[1], 'progress').write_text('done')\n"
        "subprocess.run(['git','-C',sys.argv[1],'add','.'], check=True)\n"
        "subprocess.run(['git','-C',sys.argv[1],'commit','-m','progress'], check=True)\n"
        "time.sleep(.24)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, record.source]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.25, 0.50, 1.0)))
    identity = coord.submit_stage(_build("claude", "head-progress", str(source)))
    coord.cycle("claude")

    deadline = time.monotonic() + 2
    while not (source / "progress").exists() and time.monotonic() < deadline:
        time.sleep(.01)
    assert (source / "progress").exists()
    time.sleep(.18)  # past the original silent lease, but after the new HEAD
    record = Store(default_store_path()).load()[identity]
    assert pid_family_alive(record.family)

    record = _wait_for_real_child(identity, "Build child did not exit")
    assert _build_observation(record).timed_out is False


@pytest.mark.parametrize("pool", ["claude", "codex"])
def test_build_recognized_test_crosses_silence_then_renews_on_success(
        coord_state, tmp_path, pool):
    """Both real provider stream shapes supervise a test, then renew only on success."""
    from agentflow.coordinator.store import Store, default_store_path

    if pool == "claude":
        started = {"type": "assistant", "message": {"type": "message", "role": "assistant",
                   "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                "input": {"command": "uv run pytest -q"}}]}}
        completed = {"type": "user", "message": {"type": "message", "role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "is_error": False, "content": "1 passed"}]}}
    else:
        command = '/bin/zsh -lc "uv run pytest -q"'
        started = {"type": "item.started", "item": {"id": "t1",
                   "type": "command_execution", "command": command,
                   "aggregated_output": "", "exit_code": None, "status": "in_progress"}}
        completed = {"type": "item.completed", "item": {"id": "t1",
                     "type": "command_execution", "command": command,
                     "aggregated_output": "1 passed", "exit_code": 0,
                     "status": "completed"}}
    script = (
        "import json,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.30)\n"
        f"print(json.dumps({completed!r}), flush=True)\n"
        "time.sleep(.12)\n"
    )
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 0.60, 1.0)))
    identity = coord.submit_stage(_build(pool, f"test-grace-{pool}", str(tmp_path)))
    coord.cycle(pool)

    time.sleep(.25)
    record = Store(default_store_path()).load()[identity]
    assert pid_family_alive(record.family)  # the in-flight test crossed silence

    record = _wait_for_real_child(identity, "Build test child did not exit")
    assert _build_observation(record).timed_out is False


def test_build_test_grace_cannot_cross_the_immutable_absolute_cap(coord_state, tmp_path):
    """A recognized in-flight test is still stopped at Build's absolute attempt cap (#570)."""
    from agentflow.coordinator.store import Store, default_store_path

    script = (
        "import json,time\n"
        "print(json.dumps({'type':'assistant','message':{'type':'message','role':'assistant','content':[{'type':'tool_use','id':'t1','name':'Bash','input':{'command':'pytest -q'}}]}}), flush=True)\n"
        "time.sleep(.60)\n"
    )
    gate = {"open": True}
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 1.0, 0.35)), gate=lambda _record: gate["open"])
    identity = coord.submit_stage(_build("claude", "absolute-cap", str(tmp_path)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "Build test child did not stop at its absolute cap")
    observation = _build_observation(record)
    assert observation.cause is ProviderCause.TIMEOUT
    gate["open"] = False
    coord.cycle("claude")
    assert Store(default_store_path()).load()[identity].attempts == 1


def test_build_prose_and_repeated_structured_facts_do_not_renew(coord_state, tmp_path):
    """Only one completed edit is progress; chat, usage, and its replay cannot extend silence."""
    edit = {'type': 'assistant', 'message': {'type': 'message', 'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': 'w1', 'name': 'Write',
         'input': {'file_path': str(tmp_path / 'x')}}]}}
    completed = {'type': 'user', 'message': {'type': 'message', 'role': 'user', 'content': [
        {'type': 'tool_result', 'tool_use_id': 'w1', 'is_error': False, 'content': 'done'}]}}
    script = (
        "import json,time\n"
        f"print(json.dumps({edit!r}), flush=True)\n"
        f"print(json.dumps({completed!r}), flush=True)\n"
        "print(json.dumps({'type':'assistant','message':{'type':'message','role':'assistant','content':[{'type':'text','text':'still working'}]}}), flush=True)\n"
        "print(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'allowed'}}), flush=True)\n"
        "print('partial output only', flush=True)\n"
        "time.sleep(.20)\n"
        f"print(json.dumps({completed!r}), flush=True)\n"
        "time.sleep(.40)\n"
    )
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.30, 0.60, 1.0)))
    identity = coord.submit_stage(_build("claude", "no-chatter-lease", str(tmp_path)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "Build child did not expire after one silent lease")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT


@pytest.mark.parametrize(("pool", "event"), [
    ("claude", {"type": "item.started", "item": {"id": "t1",
                "type": "command_execution", "command": '/bin/zsh -lc "pytest -q"',
                "aggregated_output": "", "exit_code": None, "status": "in_progress"}}),
    ("codex", {"type": "assistant", "message": {"type": "message", "role": "assistant",
               "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                            "input": {"command": "pytest -q"}}]}}),
    ("codex", {"type": "item.started", "item": {"id": "t1",
               "type": "command_execution", "command": '/bin/zsh -lc "pytest -q"'}}),
    ("claude", {"type": "assistant", "message": {"type": "message", "role": "assistant",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                             "input": {"command": "pytest -q && echo done"}}]}}),
    ("codex", {"type": "item.started", "item": {"id": "t1",
               "type": "command_execution", "command": '/bin/zsh -lc "pytest -q | tail -1"',
               "aggregated_output": "", "exit_code": None, "status": "in_progress"}}),
])
def test_build_progress_stream_fails_closed_for_wrong_malformed_or_composed_events(
        coord_state, tmp_path, pool, event):
    """Provider-specific, malformed, and command-shaped lookalikes never gain test grace."""
    script = ("import json,time\n"
              f"print(json.dumps({event!r}), flush=True)\n"
              "time.sleep(.50)\n")
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.25, 0.80, 1.0)))
    identity = coord.submit_stage(_build(pool, "fail-closed", str(tmp_path)))
    coord.cycle(pool)

    record = _wait_for_real_child(identity, "unrecognized Build event retained supervision")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT


@pytest.mark.parametrize(("pool", "command"), [
    ("claude", "pytest $(sleep 100)"),
    ("codex", "pytest $(sleep 100)"),
    ("claude", "pytest `sleep 100`"),
    ("codex", "pytest `sleep 100`"),
    ("claude", "pytest > result.txt"),
    ("codex", "pytest 2>&1"),
    ("claude", "pytest $TEST_ARGS"),
    ("codex", "pytest ${TEST_ARGS}"),
    ("claude", "pytest tests/*.py"),
    ("codex", "pytest tests/[ab].py"),
    ("claude", "pytest tests/{a,b}.py"),
    ("codex", "pytest ~/tests"),
    ("claude", "pytest <(cat test-list)"),
    ("codex", "pytest (sleep 100)"),
    ("claude", "pytest # ignore the rest"),
    ("codex", "pytest\\ -q"),
    ("claude", "pytest\nsleep 100"),
    ("codex", "pytest; sleep 100"),
])
def test_shell_interpretation_shapes_never_gain_test_grace(
        coord_state, tmp_path, pool, command):
    """Every composition/substitution/redirect/expansion form in ADR 570 fails closed."""
    marker = tmp_path / "gained-test-grace"
    if pool == "codex":
        event = _codex_command_event(command)
    else:
        event = {"type": "assistant", "message": {"type": "message", "role": "assistant",
                 "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                              "input": {"command": command}}]}}
    script = ("import json,pathlib,sys,time\n"
              f"print(json.dumps({event!r}), flush=True)\n"
              "time.sleep(.35)\n"
              "pathlib.Path(sys.argv[1]).write_text('incorrectly supervised')\n")
    provider = lambda record: [sys.executable, "-c", script, str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 0.80, 1.0)))
    identity = coord.submit_stage(_build(pool, "shell-composition", str(tmp_path)))
    coord.cycle(pool)

    record = _wait_for_real_child(identity, "shell composition retained test supervision")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_codex_read_only_command_events_do_not_renew(coord_state, tmp_path):
    """A paired successful Codex read is durable output, but never a Build progress fact."""
    marker = tmp_path / "read-renewed-silence"
    started = _codex_command_event("sed -n '1,80p' README.md")
    completed = _codex_command_event("sed -n '1,80p' README.md", completed=True)
    script = ("import json,pathlib,sys,time\n"
              f"print(json.dumps({started!r}), flush=True)\n"
              "time.sleep(.12)\n"
              f"print(json.dumps({completed!r}), flush=True)\n"
              "time.sleep(.20)\n"
              "pathlib.Path(sys.argv[1]).write_text('incorrectly renewed')\n")
    provider = lambda record: [sys.executable, "-c", script, str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 0.80, 1.0)))
    identity = coord.submit_stage(_build("codex", "read-only-events", str(tmp_path)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "Codex read-only events renewed Build silence")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


@pytest.mark.parametrize("pool", ["claude", "codex"])
def test_malformed_test_completion_ends_supervision_without_renewing(
        coord_state, tmp_path, pool):
    """A valid start cannot lend test grace to an ambiguous provider completion."""
    if pool == "claude":
        started = {"type": "assistant", "message": {"type": "message", "role": "assistant",
                   "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                "input": {"command": "pytest -q"}}]}}
        malformed = {"type": "user", "message": {"type": "message", "role": "user",
                     "content": [{"type": "tool_result", "tool_use_id": "t1",
                                  "content": "looks green"}]}}
    else:
        command = '/bin/zsh -lc "pytest -q"'
        started = {"type": "item.started", "item": {"id": "t1",
                   "type": "command_execution", "command": command,
                   "aggregated_output": "", "exit_code": None, "status": "in_progress"}}
        malformed = {"type": "item.completed", "item": {"id": "t1",
                     "type": "command_execution", "command": command,
                     "aggregated_output": "looks green", "status": "completed"}}
    script = ("import json,time\n"
              f"print(json.dumps({started!r}), flush=True)\n"
              "time.sleep(.15)\n"
              f"print(json.dumps({malformed!r}), flush=True)\n"
              "time.sleep(.40)\n")
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.25, 0.80, 1.0)))
    identity = coord.submit_stage(_build(pool, f"malformed-completion-{pool}", str(tmp_path)))
    coord.cycle(pool)

    record = _wait_for_real_child(identity, "malformed completion retained test supervision")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT


def test_overlapping_tests_are_each_bounded_by_the_test_cap(coord_state, tmp_path):
    """A second recognized test cannot move the first test's immutable supervision deadline."""
    marker = tmp_path / "past-first-cap"
    one = '/bin/zsh -lc "pytest -q tests/one"'
    two = '/bin/zsh -lc "pytest -q tests/two"'
    start_one = {"type": "item.started", "item": {"id": "t1", "type": "command_execution",
                 "command": one, "aggregated_output": "", "exit_code": None,
                 "status": "in_progress"}}
    start_two = {"type": "item.started", "item": {"id": "t2", "type": "command_execution",
                 "command": two, "aggregated_output": "", "exit_code": None,
                 "status": "in_progress"}}
    script = ("import json,pathlib,sys,time\n"
              f"print(json.dumps({start_one!r}), flush=True)\n"
              "time.sleep(.25)\n"
              f"print(json.dumps({start_two!r}), flush=True)\n"
              "time.sleep(.45)\n"
              "pathlib.Path(sys.argv[1]).write_text('too late')\n")
    provider = lambda record: [sys.executable, "-c", script, str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.30, 0.50, 1.2)))
    identity = coord.submit_stage(_build("codex", "overlapping-tests", str(tmp_path)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "first overlapping test exceeded its test cap")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_repeated_head_progress_cannot_cross_the_absolute_cap(coord_state, tmp_path):
    """Commits can renew silence, but the attempt still ends at its original absolute cap."""
    source = tmp_path / "capped-build"
    source.mkdir()
    for command in (("git", "init", str(source)),
                    ("git", "-C", str(source), "config", "user.email", "test@example.com"),
                    ("git", "-C", str(source), "config", "user.name", "Test")):
        subprocess.run(command, check=True, capture_output=True)
    (source / "progress").write_text("0")
    subprocess.run(("git", "-C", str(source), "add", "."), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "initial"),
                   check=True, capture_output=True)

    marker = source / "past-absolute-cap"
    script = (
        "import pathlib,subprocess,sys,time\n"
        "root=pathlib.Path(sys.argv[1])\n"
        "for n in range(1,5):\n"
        " time.sleep(.12)\n"
        " (root/'progress').write_text(str(n))\n"
        " subprocess.run(['git','-C',str(root),'add','.'], check=True)\n"
        " subprocess.run(['git','-C',str(root),'commit','-m',f'progress-{n}'], check=True)\n"
        "time.sleep(.10)\n"
        "(root/'past-absolute-cap').write_text('too late')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, record.source]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.25, 0.80, 0.45)))
    identity = coord.submit_stage(_build("claude", "head-absolute-cap", str(source)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "Build commits crossed the absolute cap")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_head_observation_cannot_block_past_the_absolute_cap(
        coord_state, tmp_path, monkeypatch):
    """A real child ignores a blocking `git` executable and stops at its immutable cap."""
    source = tmp_path / "timed-build"
    source.mkdir()
    for command in (("git", "init", str(source)),
                    ("git", "-C", str(source), "config", "user.email", "test@example.com"),
                    ("git", "-C", str(source), "config", "user.name", "Test")):
        subprocess.run(command, check=True, capture_output=True)
    (source / "initial").write_text("initial")
    subprocess.run(("git", "-C", str(source), "add", "."), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "initial"),
                   check=True, capture_output=True)

    invoked = tmp_path / "fake-git-invoked"
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    real_git = shutil.which("git")
    assert real_git
    fake_git.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        f"  *\" rev-parse HEAD \"*) printf invoked > {shlex.quote(str(invoked))}; "
        "sleep 2; exit 1 ;;\n"
        f"  *) exec {shlex.quote(real_git)} \"$@\" ;;\n"
        "esac\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    event = {"type": "assistant", "message": {"type": "message", "role": "assistant",
             "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                          "input": {"command": "pytest -q"}}]}}
    script = ("import json,time\n"
              f"print(json.dumps({event!r}), flush=True)\n"
              "time.sleep(5)\n")
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 5.0, 0.35)))
    identity = coord.submit_stage(_build("claude", "nonblocking-head", str(source)))

    started_at = time.monotonic()
    coord.cycle("claude")
    record = _wait_for_real_child(identity, "HEAD observation crossed the absolute cap")
    elapsed = time.monotonic() - started_at

    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert elapsed < 1.2, f"0.35s absolute cap took {elapsed:.3f}s"
    assert not invoked.exists(), "HEAD observation executed the blocking git adapter"


@pytest.mark.parametrize("bad_ref", ["invalid-utf8-ref", "fifo-ref"])
def test_bad_or_special_git_metadata_cannot_strand_real_provider(
        coord_state, tmp_path, bad_ref):
    """HEAD observation fails closed on hostile ref storage while the real child is cleaned."""
    source = tmp_path / bad_ref
    source.mkdir()
    subprocess.run(("git", "init", str(source)), check=True, capture_output=True)
    head = (source / ".git" / "HEAD").read_text().strip()
    ref = source / ".git" / head.removeprefix("ref: ")
    ref.parent.mkdir(parents=True, exist_ok=True)
    ref.unlink(missing_ok=True)
    if bad_ref == "invalid-utf8-ref":
        ref.write_bytes(b"\xff\xfe\n")
    else:
        os.mkfifo(ref)

    provider_pid = tmp_path / f"{bad_ref}-pid"
    marker = tmp_path / f"{bad_ref}-survived"
    script = (
        "import os,pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(.70)\n"
        "pathlib.Path(sys.argv[2]).write_text('not stopped')\n"
    )
    provider = lambda record: [
        sys.executable, "-c", script, str(provider_pid), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(5.0, 5.0, 0.35)))
    identity = coord.submit_stage(_build("claude", bad_ref, str(source)))

    started_at = time.monotonic()
    coord.cycle("claude")
    record = _wait_for_real_child(identity, "hostile Git ref stranded the provider")
    elapsed = time.monotonic() - started_at
    observation = _build_observation(record)

    assert provider_pid.exists()
    assert not pid_family_alive(provider_pid.read_text())
    assert observation.has_end_fact is True and observation.cause is ProviderCause.TIMEOUT
    assert elapsed < 1.2
    assert not marker.exists()


def test_post_decode_deadline_cannot_be_renewed_by_late_progress(
        coord_state, tmp_path, monkeypatch):
    """A real child rolls back a completion whose decoder crosses the silent deadline."""
    custom = tmp_path / "slow-json"
    custom.mkdir()
    (custom / "sitecustomize.py").write_text(
        "import json,time\n"
        "_loads=json.loads\n"
        "def loads(value,*args,**kwargs):\n"
        " data=value if isinstance(value,bytes) else str(value).encode()\n"
        " if b'\\\"slow_decode\\\":true' in data: time.sleep(.08)\n"
        " return _loads(value,*args,**kwargs)\n"
        "json.loads=loads\n")
    monkeypatch.setenv("PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")

    target = tmp_path / "changed.py"
    marker = tmp_path / "late-progress-renewed"
    started = {"type": "assistant", "message": {"type": "message", "role": "assistant",
               "content": [{"type": "tool_use", "id": "w1", "name": "Write",
                            "input": {"file_path": str(target)}}]}}
    completed = {"slow_decode": True, "type": "user",
                 "message": {"type": "message", "role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "w1", "is_error": False}]}}
    script = (
        "import pathlib,sys,time\n"
        f"print({json.dumps(json.dumps(started))}, flush=True)\n"
        "time.sleep(.07)\n"
        f"print({json.dumps(json.dumps(completed, separators=(',', ':')))}, flush=True)\n"
        "time.sleep(.13)\n"
        "pathlib.Path(sys.argv[1]).write_text('expired progress renewed')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.15, 1.0, 0.60)))
    identity = coord.submit_stage(_build("claude", "post-decode-deadline", str(tmp_path)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "late decoder progress renewed an expired lease")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_natural_exit_observed_at_absolute_cap_is_durable_timeout(coord_state, tmp_path):
    """An exit racing the immutable cap keeps its natural status but is classified timeout."""
    absolute_cap = 0.30
    provider = lambda record: [
        sys.executable, "-c", f"import time; time.sleep({absolute_cap - 0.025})"]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(5.0, 5.0, absolute_cap)))
    identity = coord.submit_stage(_build("claude", "natural-at-cap", str(tmp_path)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "natural cap-edge exit was not published")
    observation = _build_observation(record)
    assert observation.has_end_fact is True
    assert observation.timed_out is True and observation.cause is ProviderCause.TIMEOUT
    assert observation.exit_status == 0 and observation.signal is None


def _delay_supervisor_wait(
        tmp_path, monkeypatch, delay: float, *, decoded_marker=None,
        wait_returned_marker=None) -> None:
    """Make the launched supervisor observe a natural exit after a deterministic delay."""
    custom = tmp_path / "delayed-wait"
    custom.mkdir()
    instrumentation = ""
    if decoded_marker is not None:
        instrumentation += (
            "import json,pathlib\n"
            "_loads=json.loads\n"
            "def loads(value,*args,**kwargs):\n"
            " result=_loads(value,*args,**kwargs)\n"
            " data=value if isinstance(value,(bytes,bytearray)) else str(value).encode()\n"
            " if b'\\\"command\\\": \\\"pytest -q\\\"' in data:\n"
            "  pathlib.Path(os.environ['AGENTFLOW_TEST_DECODED_MARKER']).write_text('decoded')\n"
            " return result\n"
            "json.loads=loads\n")
    wait_marker = ""
    if wait_returned_marker is not None:
        wait_marker = (
            "  pathlib.Path(os.environ['AGENTFLOW_TEST_WAIT_RETURNED_MARKER']).write_text(str(timeout))\n")
    sitecustomize = (
        "import os,subprocess,time\n"
        + instrumentation
        + "_wait=subprocess.Popen.wait\n"
        "def wait(self,timeout=None):\n"
        " result=_wait(self,timeout=timeout)\n"
        " if timeout is not None:\n"
        + wait_marker
        + "  time.sleep(float(os.environ['AGENTFLOW_TEST_WAIT_DELAY']))\n"
        " return result\n"
        "subprocess.Popen.wait=wait\n")
    (custom / "sitecustomize.py").write_text(sitecustomize)
    monkeypatch.setenv("AGENTFLOW_TEST_WAIT_DELAY", str(delay))
    if decoded_marker is not None:
        monkeypatch.setenv("AGENTFLOW_TEST_DECODED_MARKER", os.fspath(decoded_marker))
    if wait_returned_marker is not None:
        monkeypatch.setenv(
            "AGENTFLOW_TEST_WAIT_RETURNED_MARKER", os.fspath(wait_returned_marker))
    monkeypatch.setenv(
        "PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")


def test_natural_exit_observed_after_silent_deadline_is_durable_timeout(
        coord_state, tmp_path, monkeypatch):
    """Post-exit observation cannot turn an expired silent lease into a clean exit."""
    _delay_supervisor_wait(tmp_path, monkeypatch, 0.08)
    provider = lambda record: [sys.executable, "-c", "import time; time.sleep(.08)"]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.12, 1.0, 1.0)))
    identity = coord.submit_stage(_build("claude", "natural-silent-edge", str(tmp_path)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "silent-edge natural exit was not published")
    observation = _build_observation(record)
    assert observation.has_end_fact is True
    assert observation.timed_out is True and observation.cause is ProviderCause.TIMEOUT
    assert observation.exit_status == 0 and observation.signal is None


def test_natural_exit_observed_after_active_test_deadline_is_durable_timeout(
        coord_state, tmp_path, monkeypatch):
    """An active test's own cap governs post-exit classification ahead of silence."""
    decoded_marker = tmp_path / "test-event-decoded"
    wait_returned_marker = tmp_path / "provider-wait-returned"
    _delay_supervisor_wait(
        tmp_path, monkeypatch, 0.25, decoded_marker=decoded_marker,
        wait_returned_marker=wait_returned_marker)
    event = {"type": "assistant", "message": {"type": "message", "role": "assistant",
             "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                          "input": {"command": "pytest -q"}}]}}
    script = ("import json,pathlib,sys,time\n"
              f"print(json.dumps({event!r}), flush=True)\n"
              "deadline=time.monotonic()+2\n"
              "marker=pathlib.Path(sys.argv[1])\n"
              "while not marker.exists() and time.monotonic()<deadline: time.sleep(.002)\n"
              "if not marker.exists(): raise RuntimeError('test event was not decoded')\n"
              "time.sleep(.03)\n")
    provider = lambda record: [sys.executable, "-c", script, str(decoded_marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.80, 0.20, 1.5)))
    identity = coord.submit_stage(_build("claude", "natural-test-edge", str(tmp_path)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "test-edge natural exit was not published")
    observation = _build_observation(record)
    assert decoded_marker.exists(), "supervisor never decoded the recognized test event"
    assert wait_returned_marker.exists(), "provider did not exit inside the wait window"
    assert observation.has_end_fact is True
    assert observation.timed_out is True and observation.cause is ProviderCause.TIMEOUT
    assert observation.exit_status == 0 and observation.signal is None


def test_natural_exit_before_effective_deadline_remains_clean(
        coord_state, tmp_path, monkeypatch):
    """The post-exit clock check does not convert a genuinely early natural exit."""
    _delay_supervisor_wait(tmp_path, monkeypatch, 0.02)
    provider = lambda record: [sys.executable, "-c", "import time; time.sleep(.02)"]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 1.0, 1.0)))
    identity = coord.submit_stage(_build("claude", "natural-before-deadline", str(tmp_path)))
    coord.cycle("claude")

    record = _wait_for_real_child(identity, "early natural exit was not published")
    observation = _build_observation(record)
    assert observation.has_end_fact is True and observation.timed_out is False
    assert observation.exit_status == 0 and observation.signal is None


def test_large_slow_event_burst_cannot_delay_the_absolute_cap(coord_state, tmp_path):
    """A real child cannot hold teardown inside a large, expensive JSONL append (#570)."""
    burst_delivered = tmp_path / "burst-delivered"
    marker = tmp_path / "past-absolute-cap"
    script = (
        "import json,pathlib,sys,time\n"
        "time.sleep(.22)\n"
        "line=json.dumps({'type':'rate_limit_event','items':list(range(1000))})+'\\n'\n"
        "sys.stdout.write(line*12000)\n"
        "sys.stdout.flush()\n"
        "pathlib.Path(sys.argv[1]).write_text(str(time.monotonic()))\n"
        "time.sleep(.60)\n"
        "pathlib.Path(sys.argv[2]).write_text('too late')\n"
    )
    provider = lambda record: [
        sys.executable, "-c", script, str(burst_delivered), str(marker)]
    absolute_cap = 0.55
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(5.0, 5.0, absolute_cap)))
    identity = coord.submit_stage(_build("claude", "bounded-event-burst", str(tmp_path)))

    started_at = time.monotonic()
    coord.cycle("claude")
    record = _wait_for_real_child(identity, "event parsing crossed the absolute cap")
    elapsed = time.monotonic() - started_at

    assert burst_delivered.exists(), "provider did not deliver the large pre-cap event burst"
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert elapsed < 1.2, f"{absolute_cap:.2f}s absolute cap took {elapsed:.3f}s"
    assert not marker.exists()


def test_deeply_nested_json_cannot_strand_provider_or_timeout_result(coord_state, tmp_path):
    """A decoder recursion failure stays non-progress and cannot escape the supervisor."""
    provider_pid = tmp_path / "provider-pid"
    marker = tmp_path / "provider-survived-cap"
    script = (
        "import os,pathlib,sys,time\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "print('['*10000+'0'+']'*10000, flush=True)\n"
        "time.sleep(.70)\n"
        "pathlib.Path(sys.argv[2]).write_text('not cleaned up')\n"
    )
    provider = lambda record: [
        sys.executable, "-c", script, str(provider_pid), str(marker)]
    absolute_cap = 0.35
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(5.0, 5.0, absolute_cap)))
    identity = coord.submit_stage(_build("claude", "recursive-json", str(tmp_path)))

    started_at = time.monotonic()
    coord.cycle("claude")
    record = _wait_for_real_child(identity, "recursive JSON stranded the Build supervisor")
    elapsed = time.monotonic() - started_at
    observation = _build_observation(record)

    assert provider_pid.exists(), "real provider did not emit its malformed event"
    assert not pid_family_alive(provider_pid.read_text()), "provider process was not reaped"
    assert observation.has_end_fact is True
    assert observation.timed_out is True
    assert observation.cause is ProviderCause.TIMEOUT
    assert observation.signal in {signal.SIGTERM, signal.SIGKILL}
    assert observation.events == ()
    assert observation.partial_output.startswith("[[[[")
    assert elapsed < 1.2, f"{absolute_cap:.2f}s absolute cap took {elapsed:.3f}s"
    assert not marker.exists()


def test_real_supervisor_forwards_reconciler_sigterm_to_its_provider_group(coord_state):
    """A reconciliation SIGTERM reaches the provider group, while the supervisor stays long
    enough to write the normal durable end facts for the stopped attempt (#220)."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    provider = lambda record: [sys.executable, "-c", "import time; time.sleep(30)"]
    coord = Coordinator(launcher=LocalLauncher(provider, timeout=5, session_timeout=30))
    identity = coord.submit_stage(review(subject="reconciled", pool="claude"))
    coord.cycle("claude")

    record = Store(default_store_path()).load()[identity]
    assert pid_family_alive(record.family)
    os.kill(int(record.family), signal.SIGTERM)

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = Store(default_store_path()).load()[identity]
        if not pid_family_alive(record.family):
            break
        time.sleep(0.02)
    else:
        pytest.fail("provider supervisor did not finish after reconciliation SIGTERM")

    observation = ClaudeProviderAdapter().observe(record)
    assert observation.signal in {15, 9}
    assert observation.timed_out is False


def test_real_supervisor_remembers_sigterm_from_provider_spawn(coord_state):
    """A provider can run immediately after Popen creates it, before the supervisor reaches
    its wait. Its SIGTERM request is remembered, reaches the real provider group, and still
    leaves the durable end facts reconciliation depends on."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    script = (
        "import os,signal,time\n"
        "os.kill(os.getppid(), signal.SIGTERM)\n"
        "time.sleep(30)\n"
    )
    provider = lambda record: [sys.executable, "-c", script]
    coord = Coordinator(launcher=LocalLauncher(provider, timeout=5, session_timeout=30))
    identity = coord.submit_stage(review(subject="spawn-term", pool="claude"))
    coord.cycle("claude")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = Store(default_store_path()).load()[identity]
        if not pid_family_alive(record.family):
            break
        time.sleep(0.02)
    else:
        pytest.fail("provider supervisor did not finish after spawn-time SIGTERM")

    observation = ClaudeProviderAdapter().observe(record)
    assert observation.signal in {signal.SIGTERM, signal.SIGKILL}
    assert observation.timed_out is False
    assert observation.has_end_fact is True


def test_child_stop_permission_denial_records_a_durable_reason(tmp_path, monkeypatch):
    """The child-stop command boundary keeps supervising when the OS denies its signal.

    It only publishes the normal terminal result after it observes the provider exit, so the
    coordinator never mistakes a still-running provider for an ended attempt.
    """
    from agentflow.coordinator import _launch_child
    from agentflow.coordinator.session import read_session

    class ChildExit(Exception):
        pass

    class StartedStore:
        def __init__(self, _path):
            pass

        def child_start(self, identity, token, family):
            return True

        def close(self):
            pass

    class Provider:
        pid = 123

        def wait(self, timeout=None):
            return 0

    handlers = {}
    monkeypatch.setattr(_launch_child.os, "fork", lambda: 0)
    monkeypatch.setattr(_launch_child.os, "setsid", lambda: None)
    monkeypatch.setattr(_launch_child.os, "_exit",
                        lambda code: (_ for _ in ()).throw(ChildExit(code)))
    monkeypatch.setattr(_launch_child.signal, "signal",
                        lambda signum, handler: handlers.setdefault(signum, handler))
    monkeypatch.setattr(_launch_child, "Store", StartedStore)
    monkeypatch.setattr(_launch_child, "_mark_active", lambda _working_dir: None)
    monkeypatch.setattr(_launch_child, "_clear_active", lambda _marker: None)

    def start_provider(*args, **kwargs):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return Provider()

    monkeypatch.setattr(_launch_child.subprocess, "Popen", start_provider)
    monkeypatch.setattr(_launch_child.os, "killpg",
                        lambda _pid, _signum: (_ for _ in ()).throw(PermissionError()))

    with pytest.raises(ChildExit) as exited:
        _launch_child.main([str(tmp_path / "records.db"), "attempt", "token", "30", "", "", "",
                            "provider"])

    assert exited.value.args == (0,)
    session = read_session(tmp_path / "records.db", "token")
    assert session.has_end_fact is True
    assert session.exit_status == 0
    assert "permission denied stopping provider process group" in session.partial_output


def test_real_supervisor_starts_provider_in_the_submitted_source(coord_state, tmp_path):
    """The path named in the boundary is also the provider process's real working directory,
    which is what the Claude project settings and OS workspace sandbox confine."""
    from agentflow.coordinator.providers import ClaudeProviderAdapter
    from agentflow.coordinator.store import Store, default_store_path

    source = tmp_path / "owned-worktree"
    source.mkdir()
    provider = lambda record: [sys.executable, "-c", "import os; print(os.getcwd())"]
    coord = Coordinator(launcher=LocalLauncher(provider, timeout=5))
    identity = coord.submit_stage(Submission(
        repo="o/r", subject="cwd", stage="review", pool="claude", source=str(source)))
    coord.cycle("claude")

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        record = Store(default_store_path()).load()[identity]
        if not pid_family_alive(record.family):
            break
        time.sleep(0.02)
    else:
        pytest.fail("provider supervisor did not finish")

    observation = ClaudeProviderAdapter().observe(record)
    assert observation.partial_output == str(source)


def test_coordinator_supervisor_marks_its_worktree_active(tmp_path, monkeypatch):
    """Startup recovery sees a coordinator provider through the same PID marker it already
    trusts for legacy sessions, so it cannot remove a clean worktree before reconciliation."""
    from agentflow import runner
    from agentflow.coordinator._launch_child import _clear_active, _mark_active

    marker = tmp_path / "agentflow-active"
    monkeypatch.setattr(runner, "_active_marker", lambda path: marker)

    written = _mark_active(str(tmp_path))
    assert written == marker
    assert marker.read_text().strip().isdigit()

    _clear_active(marker)
    assert not marker.exists()
