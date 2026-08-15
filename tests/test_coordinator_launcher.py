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


def test_local_launcher_rejects_a_live_pid_that_is_not_its_process_family(monkeypatch):
    """Recovery must not adopt or signal a PID reused by an unrelated process group."""
    probes = []
    monkeypatch.setattr(launcher_mod.os, "kill",
                        lambda pid, signal: probes.append((pid, signal)))
    monkeypatch.setattr(launcher_mod.os, "getpgid", lambda _pid: 12345)

    assert not LocalLauncher.is_alive("89850")
    assert probes == [(89850, 0)]


def test_local_launcher_rejects_a_proven_missing_pid(monkeypatch):
    """A missing PID is definite evidence that the recorded family ended."""
    def missing(*_args):
        raise ProcessLookupError

    monkeypatch.setattr(launcher_mod.os, "kill", missing)
    monkeypatch.setattr(launcher_mod.os, "getpgid",
                        lambda _pid: pytest.fail("missing PID must not reach the group probe"))

    assert not LocalLauncher.is_alive("89850")


@pytest.mark.parametrize("probe", ["kill", "getpgid"])
def test_local_launcher_keeps_permission_denied_probe_conservatively_alive(monkeypatch, probe):
    """Recovery waits when the OS cannot determine whether the recorded family still exists."""
    def denied(*_args):
        raise PermissionError

    monkeypatch.setattr(launcher_mod.os, "kill", denied if probe == "kill"
                        else lambda _pid, _signal: None)
    monkeypatch.setattr(launcher_mod.os, "getpgid", denied if probe == "getpgid"
                        else lambda _pid: 89850)

    assert LocalLauncher.is_alive("89850")


def test_local_launcher_keeps_an_ambiguous_reused_group_leader_alive(monkeypatch):
    """Matching PID and PGID cannot prove the original process birth without new persistence."""
    monkeypatch.setattr(launcher_mod.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(launcher_mod.os, "getpgid", lambda _pid: 89850)

    assert LocalLauncher.is_alive("89850")


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


def test_recovery_settles_an_absent_family_from_its_durable_result(make_coord):
    """An ended family with a durable result completes immediately, never until its deadline."""
    fake = FakeSession()
    started = make_coord(fake)
    identity = started.submit_stage(review())
    started.cycle("codex")
    fake.end(identity, success=True)

    recovered = make_coord(fake)
    assert [outcome.status for outcome in recovered.cycle("codex")] == ["completed"]
    durable = record_of(recovered, identity)
    assert durable.state == "completed" and durable.attempts == 1
    assert not durable.process_alive
    assert recovered.cycle("codex") == []


def test_recovery_closes_an_absent_family_without_a_result_once(make_coord):
    """A vanished family follows the normal closed recovery path without a second attempt."""
    fake = FakeSession()
    started = make_coord(fake)
    identity = started.submit_stage(review())
    started.cycle("codex")
    fake.kill(identity)
    fake.gate_open = False

    recovered = make_coord(fake)
    assert recovered.cycle("codex") == []
    durable = record_of(recovered, identity)
    assert durable.state == "waiting" and durable.continuation
    assert durable.attempts == 1 and durable.claim
    assert not durable.process_alive


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


def _tracked_build(tmp_path, name="worker-build"):
    source = tmp_path / name
    source.mkdir()
    for command in (("git", "init", str(source)),
                    ("git", "-C", str(source), "config", "user.email", "test@example.com"),
                    ("git", "-C", str(source), "config", "user.name", "Test")):
        subprocess.run(command, check=True, capture_output=True)
    target = source / "implementation.py"
    target.write_text("before\n")
    subprocess.run(("git", "-C", str(source), "add", "."), check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "initial"),
                   check=True, capture_output=True)
    return source, target


def _bounded_worker_command(tmp_path, *, worker="luna", effort="medium", timeout="900"):
    prompt_file = tmp_path / f"worker-prompt-{worker}-{effort}-{timeout}"
    prompt_file.write_text("Implement the assigned slice")
    prompt_file.chmod(0o600)
    return (
        f"agentflow-codex-worker --worker {worker} --effort {effort} --timeout {timeout} "
        f'< "{prompt_file}"'
    )


def test_bounded_worker_durable_change_renews_build_silence(coord_state, tmp_path):
    """A real Build supervisor observes uncommitted work from its bounded Codex worker."""
    source, target = _tracked_build(tmp_path)
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.12)\n"
        "pathlib.Path(sys.argv[1]).write_text('after\\n')\n"
        "time.sleep(.45)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.50, 0.60, 1.2)))
    identity = coord.submit_stage(_build("codex", "bounded-worker-progress", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "bounded worker Build child did not exit")
    assert target.read_text() == "after\n"
    assert _build_observation(record).timed_out is False


def test_bounded_worker_untracked_addition_renews_build_silence(coord_state, tmp_path):
    """A non-ignored untracked implementation file is durable worker progress."""
    source, _target = _tracked_build(tmp_path)
    added = source / "new.py"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.35)\n"
        "pathlib.Path(sys.argv[1]).write_text('new implementation\\n')\n"
        "time.sleep(.65)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(added)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.75, 1.20, 1.5)))
    identity = coord.submit_stage(_build("codex", "worker-untracked-addition", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "bounded worker untracked addition did not exit")
    assert added.read_text() == "new implementation\n"
    assert _build_observation(record).timed_out is False


def test_bounded_worker_tracked_empty_addition_renews_build_silence(coord_state, tmp_path):
    """Adding a tracked empty implementation file is durable worker progress."""
    source, _target = _tracked_build(tmp_path)
    added = source / "empty.py"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,subprocess,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.35)\n"
        "pathlib.Path(sys.argv[1]).touch()\n"
        "subprocess.run(['git','-C',sys.argv[2],'add','empty.py'], check=True)\n"
        "time.sleep(.65)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(added), str(source)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.75, 1.20, 1.5)))
    identity = coord.submit_stage(_build("codex", "worker-tracked-empty-addition", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "bounded worker tracked empty addition did not exit")
    assert added.exists()
    assert _build_observation(record).timed_out is False


def test_bounded_worker_durable_deletion_renews_build_silence(coord_state, tmp_path):
    """Deleting a tracked implementation file is a durable worker progress state."""
    source, target = _tracked_build(tmp_path)
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.12)\n"
        "pathlib.Path(sys.argv[1]).unlink()\n"
        "time.sleep(.45)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.50, 0.60, 1.2)))
    identity = coord.submit_stage(_build("codex", "bounded-worker-deletion", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "bounded worker deletion child did not exit")
    assert not target.exists()
    assert _build_observation(record).timed_out is False


def test_bounded_worker_deletion_cannot_collide_with_tracked_content(
        coord_state, tmp_path, monkeypatch):
    """A deletion renews after content that formerly encoded to the same snapshot bytes."""
    source, target = _tracked_build(tmp_path)
    collided = source / "a"
    collided.write_text("initial")
    subprocess.run(("git", "-C", str(source), "add", "a"),
                   check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "track collision path"),
                   check=True, capture_output=True)
    snapshots = tmp_path / "collision-snapshots"
    custom = tmp_path / "collision-instrumentation"
    custom.mkdir()
    (custom / "sitecustomize.py").write_text(
        "import os\n"
        "_write=os.write\n"
        "def write(fd,data):\n"
        " if len(data)==32:\n"
        "  with open(os.environ['AGENTFLOW_TEST_SNAPSHOTS'],'ab') as stream: stream.write(b'x')\n"
        " return _write(fd,data)\n"
        "os.write=write\n")
    monkeypatch.setenv("AGENTFLOW_TEST_SNAPSHOTS", str(snapshots))
    monkeypatch.setenv(
        "PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,os,pathlib,sys,time\n"
        "started_at=time.monotonic()\n"
        "target=pathlib.Path(sys.argv[1])\n"
        "snapshots=pathlib.Path(os.environ['AGENTFLOW_TEST_SNAPSHOTS'])\n"
        "target.write_bytes(b'a\\0deleted\\0')\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "while not snapshots.exists() or snapshots.stat().st_size < 1: time.sleep(.002)\n"
        "target.unlink()\n"
        "while snapshots.stat().st_size < 2: time.sleep(.002)\n"
        "time.sleep(max(0, started_at + 1.10 - time.monotonic()))\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(collided)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(1.0, 1.50, 2.0)))
    identity = coord.submit_stage(_build("codex", "worker-snapshot-collision", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "collision-safe bounded worker did not exit")
    assert not collided.exists()
    assert target.read_text() == "before\n"
    assert _build_observation(record).timed_out is False


def test_bounded_worker_tracked_empty_deletion_renews_build_silence(coord_state, tmp_path):
    """Deleting a tracked empty implementation file is durable worker progress."""
    source, _target = _tracked_build(tmp_path)
    deleted = source / "empty.py"
    deleted.touch()
    subprocess.run(("git", "-C", str(source), "add", "empty.py"),
                   check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "track empty"),
                   check=True, capture_output=True)
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.35)\n"
        "pathlib.Path(sys.argv[1]).unlink()\n"
        "time.sleep(.65)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(deleted)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.75, 1.20, 1.5)))
    identity = coord.submit_stage(_build("codex", "worker-tracked-empty-deletion", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "bounded worker tracked empty deletion did not exit")
    assert not deleted.exists()
    assert _build_observation(record).timed_out is False


def test_bounded_worker_tracked_empty_path_change_renews_build_silence(
        coord_state, tmp_path):
    """Renaming a tracked empty implementation file is durable worker progress."""
    source, _target = _tracked_build(tmp_path)
    original = source / "empty.py"
    renamed = source / "renamed.py"
    original.touch()
    subprocess.run(("git", "-C", str(source), "add", "empty.py"),
                   check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "track empty"),
                   check=True, capture_output=True)
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,subprocess,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.35)\n"
        "subprocess.run(['git','-C',sys.argv[1],'mv','empty.py','renamed.py'], check=True)\n"
        "time.sleep(.65)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(source)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.75, 1.20, 1.5)))
    identity = coord.submit_stage(_build("codex", "worker-tracked-empty-path", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "bounded worker tracked empty rename did not exit")
    assert not original.exists() and renamed.exists()
    assert _build_observation(record).timed_out is False


def test_bounded_worker_unchanged_state_does_not_renew_build_silence(coord_state, tmp_path):
    """An active recognized worker is not progress without a new durable worktree state."""
    source, _target = _tracked_build(tmp_path)
    marker = tmp_path / "unchanged-worker-crossed-silence"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.35)\n"
        "pathlib.Path(sys.argv[1]).write_text('incorrectly renewed')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 0.50, 1.0)))
    identity = coord.submit_stage(_build("codex", "unchanged-worker", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "unchanged worker retained its Build lease")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_bounded_worker_chmod_only_does_not_renew_build_silence(coord_state, tmp_path):
    """Mode-only churn is not durable implementation progress."""
    source, target = _tracked_build(tmp_path)
    marker = tmp_path / "chmod-worker-crossed-silence"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.35)\n"
        "pathlib.Path(sys.argv[1]).chmod(0o755)\n"
        "time.sleep(.55)\n"
        "pathlib.Path(sys.argv[2]).write_text('incorrectly renewed')\n"
        "time.sleep(.50)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.75, 1.00, 1.5)))
    identity = coord.submit_stage(_build("codex", "chmod-worker", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "chmod-only worker retained Build silence")
    assert target.stat().st_mode & 0o777 == 0o755
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_bounded_worker_changes_after_completion_do_not_renew(coord_state, tmp_path):
    """A recognized invocation stops authorizing observation at its completion event."""
    source, target = _tracked_build(tmp_path)
    marker = tmp_path / "post-worker-change-crossed-silence"
    command = _bounded_worker_command(tmp_path)
    started = _codex_command_event(command)
    completed = _codex_command_event(command, completed=True)
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.08)\n"
        f"print(json.dumps({completed!r}), flush=True)\n"
        "time.sleep(.12)\n"
        "pathlib.Path(sys.argv[1]).write_text('after worker\\n')\n"
        "time.sleep(.20)\n"
        "pathlib.Path(sys.argv[2]).write_text('incorrectly renewed')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.28, 0.50, 1.0)))
    identity = coord.submit_stage(_build("codex", "post-worker-change", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "post-worker change retained Build silence")
    assert target.read_text() == "after worker\n"
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


@pytest.mark.parametrize("revocation", ["completion", "stale-completion"])
def test_bounded_worker_snapshot_rechecks_current_authorization(
        tmp_path, monkeypatch, revocation):
    """Completion or staleness during snapshot B wins before B can renew silence."""
    import threading

    from agentflow.coordinator import _launch_child
    from agentflow.coordinator.session import events_path, read_session

    class ChildExit(Exception):
        pass

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    command = _bounded_worker_command(tmp_path)
    started = _codex_command_event(command)
    revoked = _codex_command_event(
        command if revocation == "completion" else "different command", completed=True)

    class StartedStore:
        def __init__(self, _path):
            pass

        def child_start(self, identity, token, family):
            return True

        def close(self):
            pass

    class Provider:
        pid = 123

        def __init__(self, *args, **kwargs):
            output = kwargs["stdout"]
            output.write(json.dumps(started) + "\n")
            output.flush()

        def wait(self, timeout=None):
            if timeout is None or clock.now + timeout >= 0.22:
                clock.now = 0.22
                return 0
            clock.now += timeout
            raise subprocess.TimeoutExpired("provider", timeout)

        def poll(self):
            return None

    snapshot_calls = 0
    snapshot_b_started = threading.Event()
    snapshot_b_release = threading.Event()

    def snapshot(_working_dir):
        nonlocal snapshot_calls
        snapshot_calls += 1
        if snapshot_calls == 1:
            return b"A" * 32
        snapshot_b_started.set()
        assert snapshot_b_release.wait(1), "authorization revocation did not release snapshot B"
        return b"B" * 32

    def revoke_authorization():
        assert snapshot_b_started.wait(1), "snapshot B did not reach its barrier"
        with events.open("a") as output:
            output.write(json.dumps(revoked) + "\n")
        snapshot_b_release.set()

    store_path = tmp_path / "records.db"
    events = events_path(store_path, "token")
    monkeypatch.setattr(_launch_child.os, "fork", lambda: 0)
    monkeypatch.setattr(_launch_child.os, "setsid", lambda: None)
    monkeypatch.setattr(_launch_child.os, "_exit",
                        lambda code: (_ for _ in ()).throw(ChildExit(code)))
    monkeypatch.setattr(_launch_child.os, "killpg", lambda _pid, _signum: None)
    monkeypatch.setattr(_launch_child.signal, "signal", lambda _signum, _handler: None)
    monkeypatch.setattr(_launch_child, "Store", StartedStore)
    monkeypatch.setattr(_launch_child, "_mark_active", lambda _working_dir: None)
    monkeypatch.setattr(_launch_child, "_clear_active", lambda _marker: None)
    monkeypatch.setattr(_launch_child, "_head", lambda _working_dir: None)
    monkeypatch.setattr(_launch_child, "_worktree_snapshot", snapshot)
    monkeypatch.setattr(_launch_child, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(_launch_child.subprocess, "Popen", Provider)
    monkeypatch.chdir(tmp_path)

    args = [str(store_path), "attempt", "token", "5", "--build-lease", "codex",
            "0.20", "0.50", "1.0", _launch_child._INHERITED_WORKTREE, "provider"]
    revoker = threading.Thread(target=revoke_authorization)
    revoker.start()
    with pytest.raises(ChildExit) as exited:
        _launch_child.main(args)
    revoker.join(timeout=1)

    assert exited.value.args == (0,)
    assert not revoker.is_alive()
    session = read_session(store_path, "token")
    assert snapshot_calls == 2
    assert session.timed_out is True
    assert session.exit_status == 0 and session.signal is None


@pytest.mark.parametrize(
    "command_kind", ["unrelated", "unallowlisted", "composed", "malformed", "unquoted"])
def test_nonapproved_worker_command_cannot_turn_durable_churn_into_progress(
        coord_state, tmp_path, command_kind):
    """Only the launcher's exact approved worker command activates worktree observation."""
    source, target = _tracked_build(tmp_path)
    canonical = _bounded_worker_command(tmp_path)
    commands = {
        "unrelated": "sed -n '1,20p' implementation.py",
        "unallowlisted": _bounded_worker_command(tmp_path, worker="impostor"),
        "composed": canonical + " && echo extra",
        "malformed": canonical.split(" < ", 1)[0],
        "unquoted": canonical.replace('< "', "< ")[:-1],
    }
    marker = tmp_path / f"{command_kind}-crossed-silence"
    started = _codex_command_event(commands[command_kind])
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.18)\n"
        "pathlib.Path(sys.argv[1]).write_text('untrusted churn\\n')\n"
        "time.sleep(.28)\n"
        "pathlib.Path(sys.argv[2]).write_text('incorrectly renewed')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.32, 0.50, 1.0)))
    identity = coord.submit_stage(_build("codex", f"nonapproved-{command_kind}", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "nonapproved worker command retained Build silence")
    assert target.read_text() == "untrusted churn\n"
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_prior_unapproved_change_is_not_reclassified_when_worker_starts(coord_state, tmp_path):
    """A worker starts from the durable barrier left by the preceding parent command."""
    source, target = _tracked_build(tmp_path)
    marker = tmp_path / "prior-unapproved-change-renewed"
    unapproved = "python -c 'write implementation.py'"
    worker = _bounded_worker_command(tmp_path)
    unapproved_started = _codex_command_event(unapproved)
    unapproved_completed = _codex_command_event(unapproved, completed=True)
    worker_started = _codex_command_event(worker)
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({unapproved_started!r}), flush=True)\n"
        "time.sleep(.12)\n"
        "pathlib.Path(sys.argv[1]).write_text('unapproved change\\n')\n"
        f"print(json.dumps({unapproved_completed!r}), flush=True)\n"
        f"print(json.dumps({worker_started!r}), flush=True)\n"
        "time.sleep(.18)\n"
        "pathlib.Path(sys.argv[2]).write_text('incorrectly renewed')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.22, 0.50, 1.0)))
    identity = coord.submit_stage(_build("codex", "prior-unapproved-change", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "prior unapproved change retained Build silence")
    assert target.read_text() == "unapproved change\n"
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


@pytest.mark.parametrize("churn_kind", ["outside", "internal", "generated"])
def test_bounded_worker_ignores_outside_internal_and_generated_churn(
        coord_state, tmp_path, churn_kind):
    """Worker observation excludes state outside Git's implementation-change surface."""
    source, _target = _tracked_build(tmp_path)
    empty_global_excludes = tmp_path / "empty-global-excludes"
    empty_global_excludes.write_text("")
    subprocess.run(("git", "-C", str(source), "config", "core.excludesFile",
                    str(empty_global_excludes)), check=True, capture_output=True)
    (source / ".gitignore").write_text("generated/\n")
    subprocess.run(("git", "-C", str(source), "add", ".gitignore"),
                   check=True, capture_output=True)
    subprocess.run(("git", "-C", str(source), "commit", "-m", "ignore generated"),
                   check=True, capture_output=True)
    churn = {
        "outside": tmp_path / "outside.py",
        "internal": source / ".agentflow" / "supervisor-state",
        "generated": source / "generated" / "cache.py",
    }[churn_kind]
    marker = tmp_path / f"{churn_kind}-crossed-silence"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.18)\n"
        "target=pathlib.Path(sys.argv[1])\n"
        "target.parent.mkdir(parents=True, exist_ok=True)\n"
        "target.write_text('churn\\n')\n"
        "time.sleep(.28)\n"
        "pathlib.Path(sys.argv[2]).write_text('incorrectly renewed')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(churn), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.32, 0.50, 1.0)))
    identity = coord.submit_stage(_build("codex", f"ignored-{churn_kind}", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "nonimplementation churn retained Build silence")
    assert churn.exists()
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_bounded_worker_progress_cannot_cross_the_immutable_cap(coord_state, tmp_path):
    """Repeated worker-owned durable states renew silence but never the attempt cap."""
    source, target = _tracked_build(tmp_path)
    marker = tmp_path / "worker-crossed-absolute-cap"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "target=pathlib.Path(sys.argv[1])\n"
        "for n in range(1,6):\n"
        " time.sleep(.10)\n"
        " target.write_text(f'{n}\\n')\n"
        "pathlib.Path(sys.argv[2]).write_text('incorrectly crossed cap')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.20, 0.50, 0.42)))
    identity = coord.submit_stage(_build("codex", "worker-absolute-cap", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "worker progress crossed the immutable cap")
    assert target.read_text() != "before\n"
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_bounded_worker_snapshot_cannot_strand_the_supervisor(
        coord_state, tmp_path, monkeypatch):
    """A blocking Git status helper cannot delay the immutable cap or provider teardown."""
    source, _target = _tracked_build(tmp_path)
    invoked = tmp_path / "blocking-status-invoked"
    marker = tmp_path / "provider-survived-blocking-status"
    fake_bin = tmp_path / "fake-status-bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    real_git = shutil.which("git")
    assert real_git
    fake_git.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        f"  *\" status \"*) printf invoked > {shlex.quote(str(invoked))}; sleep 2; exit 1 ;;\n"
        f"  *) exec {shlex.quote(real_git)} \"$@\" ;;\n"
        "esac\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")

    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.70)\n"
        "pathlib.Path(sys.argv[1]).write_text('not cleaned up')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(marker)]
    absolute_cap = 0.35
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(5.0, 5.0, absolute_cap)))
    identity = coord.submit_stage(_build("codex", "blocking-worker-snapshot", str(source)))

    started_at = time.monotonic()
    coord.cycle("codex")
    record = _wait_for_real_child(identity, "worker snapshot stranded the supervisor")
    elapsed = time.monotonic() - started_at

    assert invoked.exists(), "the worker snapshot did not exercise the blocking Git adapter"
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert elapsed < 1.2, f"{absolute_cap:.2f}s absolute cap took {elapsed:.3f}s"
    assert not marker.exists()


def test_bounded_worker_snapshot_cleanup_permission_denial_cannot_renew(
        coord_state, tmp_path, monkeypatch):
    """A digest is invalid when its still-running helper cannot be cleanly reaped."""
    source, target = _tracked_build(tmp_path)
    cleanup_denied = tmp_path / "snapshot-cleanup-denied"
    marker = tmp_path / "provider-crossed-silence"
    custom = tmp_path / "snapshot-cleanup-instrumentation"
    custom.mkdir()
    (custom / "sitecustomize.py").write_text(
        "import os,pathlib,signal,time\n"
        "_write=os.write\n"
        "def write(fd,data):\n"
        " result=_write(fd,data)\n"
        " if len(data)==32: time.sleep(2)\n"
        " return result\n"
        "os.write=write\n"
        "_killpg=os.killpg\n"
        "def killpg(pid,signum):\n"
        " if signum==signal.SIGKILL:\n"
        "  pathlib.Path(os.environ['AGENTFLOW_TEST_CLEANUP_DENIED']).write_text('denied')\n"
        "  raise PermissionError('deterministic snapshot cleanup denial')\n"
        " return _killpg(pid,signum)\n"
        "os.killpg=killpg\n")
    monkeypatch.setenv("AGENTFLOW_TEST_CLEANUP_DENIED", str(cleanup_denied))
    monkeypatch.setenv(
        "PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")

    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.35)\n"
        "pathlib.Path(sys.argv[1]).write_text('after cleanup denial\\n')\n"
        "time.sleep(.65)\n"
        "pathlib.Path(sys.argv[2]).write_text('incorrectly renewed')\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target), str(marker)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.75, 1.20, 1.5)))
    identity = coord.submit_stage(_build("codex", "worker-cleanup-denial", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "cleanup-denied worker retained Build silence")
    assert cleanup_denied.exists(), "snapshot cleanup did not exercise permission denial"
    assert target.read_text() == "after cleanup denial\n"
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


@pytest.mark.parametrize("status, numstat", [
    (b"?? ../../outside\x00", b""),
    (b"?? /outside\x00", b""),
    (b"?? linked/secret\x00", b""),
    (b"R  renamed\x00../../outside\x00", b"0\t0\trenamed\x00"),
    (b"R  ../../outside\x00renamed\x00", b"0\t0\t../../outside\x00"),
], ids=["traversal", "absolute", "symlink", "rename-source", "rename-destination"])
def test_bounded_worktree_snapshot_rejects_paths_outside_its_worktree(
        tmp_path, monkeypatch, status, numstat):
    """A malformed Git record cannot read external bytes or renew Build silence."""
    from agentflow.coordinator import _launch_child

    root = tmp_path / "worktree"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_bytes(b"external bytes must remain unread")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "renamed").write_bytes(b"inside")

    class Process:
        def __init__(self, output):
            self.stdout = SimpleNamespace(read=lambda _limit: output)

        def wait(self):
            return 0

        def kill(self):
            pass

    outputs = iter([status, numstat])
    monkeypatch.setattr(_launch_child.subprocess, "Popen",
                        lambda *_args, **_kwargs: Process(next(outputs)))

    assert _launch_child._worktree_snapshot(str(root), timeout=0.5) is None


@pytest.mark.parametrize("status, numstat", [
    ("?? ../../outside\\0", ""),
    ("?? /outside\\0", ""),
    ("?? linked/secret\\0", ""),
    ("R  renamed\\0../../outside\\0", "0\\t0\\trenamed\\0"),
    ("R  ../../outside\\0renamed\\0", "0\\t0\\t../../outside\\0"),
], ids=["traversal", "absolute", "symlink", "rename-source", "rename-destination"])
def test_malicious_git_paths_cannot_renew_build_silence(
        coord_state, tmp_path, monkeypatch, status, numstat):
    """The public Build supervisor ignores snapshots that would leave its worktree."""
    source, _target = _tracked_build(tmp_path)
    fake_bin = tmp_path / "malicious-git"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    real_git = shutil.which("git")
    assert real_git
    fake_git.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        f" *\" status \"*) printf '{status}' ;;\n"
        f" *\" diff \"*) printf '{numstat}' ;;\n"
        f" *) exec {shlex.quote(real_git)} \"$@\" ;;\n"
        "esac\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    marker = tmp_path / "malicious-snapshot-renewed"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.60)\n"
        "pathlib.Path(sys.argv[1]).write_text('renewed')\n")
    coord = Coordinator(launcher=LocalLauncher(
        lambda _record: [sys.executable, "-c", script, str(marker)], timeout=5,
        build_lease=(0.20, 0.30, 0.45)))
    identity = coord.submit_stage(_build("codex", "malicious-snapshot", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "malicious snapshot retained Build silence")
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_DIRECTORY"])
def test_missing_containment_primitive_cannot_renew_build_silence(
        coord_state, tmp_path, monkeypatch, missing_flag):
    """Build fails closed before observing worktree state without either containment primitive."""
    source, target = _tracked_build(tmp_path)
    changed = source / "changed.py"
    outside = tmp_path / "outside"
    outside.write_bytes(b"external bytes must remain unread")
    target.unlink()
    target.symlink_to(outside)
    custom = tmp_path / "missing-primitive-instrumentation"
    custom.mkdir()
    external_read = tmp_path / "external-read"
    (custom / "sitecustomize.py").write_text(
        "import os,pathlib\n"
        "_open=os.open\n"
        "_read=os.read\n"
        "external_fds=set()\n"
        "delattr(os, os.environ['AGENTFLOW_TEST_MISSING_FLAG'])\n"
        "def open(path,flags,*args,**kwargs):\n"
        " result=_open(path,flags,*args,**kwargs)\n"
        " if path == os.environ['AGENTFLOW_TEST_EXTERNAL_NAME'] and flags & os.O_NONBLOCK:\n"
        "  external_fds.add(result)\n"
        " return result\n"
        "def read(fd,size):\n"
        " if fd in external_fds: pathlib.Path(os.environ['AGENTFLOW_TEST_EXTERNAL_READ']).write_text('read')\n"
        " return _read(fd,size)\n"
        "os.open=open\n"
        "os.read=read\n")
    monkeypatch.setenv("AGENTFLOW_TEST_MISSING_FLAG", missing_flag)
    monkeypatch.setenv("AGENTFLOW_TEST_EXTERNAL_NAME", target.name)
    monkeypatch.setenv("AGENTFLOW_TEST_EXTERNAL_READ", str(external_read))
    monkeypatch.setenv(
        "PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    marker = tmp_path / "missing-primitive-renewed"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.08)\n"
        "pathlib.Path(sys.argv[1]).write_text('changed')\n"
        "time.sleep(.16)\n"
        "pathlib.Path(sys.argv[2]).write_text('renewed')\n")
    coord = Coordinator(launcher=LocalLauncher(
        lambda _record: [sys.executable, "-c", script, str(changed), str(marker)], timeout=5,
        build_lease=(0.20, 0.30, 0.80)))
    identity = coord.submit_stage(_build("codex", "missing-primitive", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "missing primitive retained Build silence")
    assert not external_read.exists()
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_file_replaced_with_external_symlink_cannot_renew_build_silence(
        coord_state, tmp_path, monkeypatch):
    """A replacement after lstat cannot make the bounded reader consume external bytes."""
    source, target = _tracked_build(tmp_path)
    outside = tmp_path / "outside"
    outside.write_bytes(b"external bytes must remain unread")
    custom = tmp_path / "replacement-instrumentation"
    custom.mkdir()
    replaced = tmp_path / "replacement-observed"
    external_read = tmp_path / "external-read"
    (custom / "sitecustomize.py").write_text(
        "import os,pathlib\n"
        "_open=os.open\n"
        "_read=os.read\n"
        "external_fds=set()\n"
        "def open(path,flags,*args,**kwargs):\n"
        " if (path == os.environ['AGENTFLOW_TEST_REPLACED_NAME']\n"
        "     and flags & os.O_NONBLOCK):\n"
        "  target=pathlib.Path(os.environ['AGENTFLOW_TEST_REPLACED_TARGET'])\n"
        "  target.unlink()\n"
        "  target.symlink_to(os.environ['AGENTFLOW_TEST_REPLACED_OUTSIDE'])\n"
        "  pathlib.Path(os.environ['AGENTFLOW_TEST_REPLACED_MARKER']).write_text('replaced')\n"
        " result=_open(path,flags,*args,**kwargs)\n"
        " if path == os.environ['AGENTFLOW_TEST_REPLACED_NAME']: external_fds.add(result)\n"
        " return result\n"
        "def read(fd,size):\n"
        " if fd in external_fds: pathlib.Path(os.environ['AGENTFLOW_TEST_EXTERNAL_READ']).write_text('read')\n"
        " return _read(fd,size)\n"
        "os.open=open\n"
        "os.read=read\n")
    monkeypatch.setenv("AGENTFLOW_TEST_REPLACED_NAME", target.name)
    monkeypatch.setenv("AGENTFLOW_TEST_REPLACED_TARGET", str(target))
    monkeypatch.setenv("AGENTFLOW_TEST_REPLACED_OUTSIDE", str(outside))
    monkeypatch.setenv("AGENTFLOW_TEST_REPLACED_MARKER", str(replaced))
    monkeypatch.setenv("AGENTFLOW_TEST_EXTERNAL_READ", str(external_read))
    monkeypatch.setenv(
        "PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    marker = tmp_path / "replacement-renewed"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.12)\n"
        "pathlib.Path(sys.argv[1]).write_text('changed')\n"
        "time.sleep(.46)\n"
        "pathlib.Path(sys.argv[2]).write_text('renewed')\n")
    coord = Coordinator(launcher=LocalLauncher(
        lambda _record: [sys.executable, "-c", script, str(target), str(marker)], timeout=5,
        build_lease=(0.20, 0.30, 0.80)))
    identity = coord.submit_stage(_build("codex", "replacement-race", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "replacement race retained Build silence")
    assert replaced.exists(), "snapshot did not reach the replacement race"
    assert target.is_symlink()
    assert not external_read.exists()
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def test_failed_rename_prior_path_closes_worktree_descriptors_and_cannot_renew_silence(
        coord_state, tmp_path, monkeypatch):
    """A rejected rename prior path releases both parent descriptors before Build times out."""
    source, target = _tracked_build(tmp_path)
    fake_bin = tmp_path / "rename-failure-git"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    real_git = shutil.which("git")
    assert real_git
    fake_git.write_text(
        "#!/bin/sh\n"
        "case \" $* \" in\n"
        " *\" status \"*) printf 'R  implementation.py\\0nested/file\\0' ;;\n"
        " *\" diff \"*) printf '0\\t0\\timplementation.py\\0' ;;\n"
        f" *) exec {shlex.quote(real_git)} \"$@\" ;;\n"
        "esac\n")
    fake_git.chmod(0o755)
    custom = tmp_path / "rename-failure-instrumentation"
    custom.mkdir()
    closed = tmp_path / "closed-root-descriptors"
    (custom / "sitecustomize.py").write_text(
        "import os,pathlib\n"
        "_open=os.open\n"
        "_close=os.close\n"
        "roots=set()\n"
        "def open(path,flags,*args,**kwargs):\n"
        " if os.fspath(path) == 'nested': raise OSError('deterministic prior-path failure')\n"
        " result=_open(path,flags,*args,**kwargs)\n"
        " if os.fspath(path) == os.environ['AGENTFLOW_TEST_WORKTREE_ROOT']: roots.add(result)\n"
        " return result\n"
        "def close(fd):\n"
        " if fd in roots:\n"
        "  with pathlib.Path(os.environ['AGENTFLOW_TEST_CLOSED_ROOTS']).open('a') as stream: stream.write('x')\n"
        " return _close(fd)\n"
        "os.open=open\n"
        "os.close=close\n")
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("AGENTFLOW_TEST_WORKTREE_ROOT", str(source))
    monkeypatch.setenv("AGENTFLOW_TEST_CLOSED_ROOTS", str(closed))
    monkeypatch.setenv(
        "PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    marker = tmp_path / "rename-failure-renewed"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.05)\n"
        "pathlib.Path(sys.argv[1]).write_text('changed')\n"
        "time.sleep(.19)\n"
        "pathlib.Path(sys.argv[2]).write_text('renewed')\n")
    coord = Coordinator(launcher=LocalLauncher(
        lambda _record: [sys.executable, "-c", script, str(target), str(marker)], timeout=5,
        build_lease=(0.20, 0.30, 0.80)))
    identity = coord.submit_stage(_build("codex", "rename-fd-cleanup", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "failed rename prior path retained Build silence")
    assert closed.read_text().count("x") >= 2
    assert _build_observation(record).cause is ProviderCause.TIMEOUT
    assert not marker.exists()


def _run_clocked_supervisor(
        tmp_path, monkeypatch, *, build_lease, exit_at, heads=(None,)):
    """Run the production supervisor interface against an exact monotonic timeline."""
    from agentflow.coordinator import _launch_child
    from agentflow.coordinator.session import read_session

    class ChildExit(Exception):
        pass

    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()

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
            assert timeout is not None
            if clock.now + timeout >= exit_at:
                clock.now = exit_at
                return 0
            clock.now += timeout
            raise subprocess.TimeoutExpired("provider", timeout)

        def poll(self):
            return None

    observed_heads = iter(heads)
    last_head = heads[-1]

    def head(_working_dir):
        nonlocal last_head
        last_head = next(observed_heads, last_head)
        return last_head

    monkeypatch.setattr(_launch_child.os, "fork", lambda: 0)
    monkeypatch.setattr(_launch_child.os, "setsid", lambda: None)
    monkeypatch.setattr(_launch_child.os, "_exit",
                        lambda code: (_ for _ in ()).throw(ChildExit(code)))
    monkeypatch.setattr(_launch_child.signal, "signal", lambda _signum, _handler: None)
    monkeypatch.setattr(_launch_child, "Store", StartedStore)
    monkeypatch.setattr(_launch_child, "_mark_active", lambda _working_dir: None)
    monkeypatch.setattr(_launch_child, "_clear_active", lambda _marker: None)
    monkeypatch.setattr(_launch_child, "_head", head)
    monkeypatch.setattr(_launch_child, "time", SimpleNamespace(monotonic=clock))
    monkeypatch.setattr(_launch_child.subprocess, "Popen", lambda *args, **kwargs: Provider())
    monkeypatch.chdir(tmp_path)

    silent, test_grace, absolute = build_lease
    store_path = tmp_path / "records.db"
    args = [str(store_path), "attempt", "token", "5", "--build-lease", "claude",
            str(silent), str(test_grace), str(absolute), _launch_child._INHERITED_WORKTREE,
            "provider"]
    with pytest.raises(ChildExit) as exited:
        _launch_child.main(args)

    assert exited.value.args == (0,)
    return read_session(store_path, "token")


def test_build_head_progress_renews_its_child_local_silent_lease(tmp_path, monkeypatch):
    """A HEAD change extends silence past the original lease on an exact clock (#570)."""
    session = _run_clocked_supervisor(
        tmp_path, monkeypatch, build_lease=(0.25, 0.50, 1.0), exit_at=0.30,
        heads=("before", "after"))

    assert session.timed_out is False
    assert session.exit_status == 0 and session.signal is None


def test_active_bounded_worker_durable_change_renews_build_silence(coord_state, tmp_path):
    """An approved bounded worker's uncommitted work keeps its Build parent alive."""
    source, target = _tracked_build(tmp_path)
    worker = _bounded_worker_command(tmp_path, effort="low")
    started = _codex_command_event(worker)
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        # Leave enough time for the 100ms-bounded baseline helper plus its next poll on
        # slower CI hosts. The provider still exits after the original silent lease, so
        # this remains a renewal test rather than a natural-exit test.
        "time.sleep(.35)\n"
        "pathlib.Path(sys.argv[1]).write_text('after\\n')\n"
        "time.sleep(.65)\n"
    )
    provider = lambda record: [sys.executable, "-c", script, str(target)]
    coord = Coordinator(launcher=LocalLauncher(
        provider, timeout=5, build_lease=(0.75, 1.20, 1.5)))
    identity = coord.submit_stage(_build("codex", "worker-progress", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "bounded worker Build child did not exit")
    assert target.read_text() == "after\n"
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


def test_natural_exit_observed_at_absolute_cap_is_durable_timeout(tmp_path, monkeypatch):
    """An exit at the immutable cap keeps its natural status but is classified timeout."""
    session = _run_clocked_supervisor(
        tmp_path, monkeypatch, build_lease=(5.0, 5.0, 0.30), exit_at=0.30)

    assert session.has_end_fact is True and session.timed_out is True
    assert session.exit_status == 0 and session.signal is None


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


def test_natural_exit_observed_at_silent_deadline_is_durable_timeout(tmp_path, monkeypatch):
    """An exit at the silent deadline keeps its natural status but is classified timeout."""
    session = _run_clocked_supervisor(
        tmp_path, monkeypatch, build_lease=(0.12, 1.0, 1.0), exit_at=0.12)

    assert session.has_end_fact is True and session.timed_out is True
    assert session.exit_status == 0 and session.signal is None


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
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ChildExit) as exited:
        _launch_child.main([str(tmp_path / "records.db"), "attempt", "token", "30",
                            _launch_child._INHERITED_WORKTREE, "provider"])

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


def test_local_launcher_passes_only_the_inherited_worktree_sentinel(tmp_path, monkeypatch):
    """The public launcher carries source authority in cwd, never in child argv."""
    from agentflow.coordinator import _launch_child
    from agentflow.coordinator import launcher as launcher_mod
    from agentflow.coordinator.launcher import STARTED

    source = tmp_path / "authorized-worktree"
    source.mkdir()
    observed = {}

    class Child:
        def wait(self, timeout):
            assert timeout == 1

    class Store:
        path = tmp_path / "records.db"

        def record_of(self, _identity):
            return SimpleNamespace(start_fact=STARTED, launch_token="token", family="123")

    record = SimpleNamespace(identity="attempt", launch_token="token", stage="review",
                             source=str(source))

    def launch(argv, **kwargs):
        observed["argv"] = argv
        observed["cwd"] = kwargs["cwd"]
        return Child()

    monkeypatch.setattr(launcher_mod.subprocess, "Popen", launch)
    result = launcher_mod.LocalLauncher(lambda _record: ["provider"], timeout=1,
                                        session_timeout=5).start(record, Store())

    assert result.family == "123"
    assert observed["cwd"] == str(source)
    assert _launch_child._INHERITED_WORKTREE in observed["argv"]
    assert str(source) not in observed["argv"]


def test_public_launcher_ignores_forged_provider_root_for_snapshot_authority(
        coord_state, tmp_path, monkeypatch):
    """A provider path cannot redirect the snapshot away from LocalLauncher's inherited cwd."""
    from agentflow.coordinator import _launch_child

    source, target = _tracked_build(tmp_path)
    forged = tmp_path / "forged-worktree"
    forged.mkdir()
    external = forged / "external"
    external.write_bytes(b"external bytes must remain unread")
    custom = tmp_path / "forged-root-instrumentation"
    custom.mkdir()
    external_read = tmp_path / "external-read"
    bootstrap = tmp_path / "bootstrap-authority"
    (custom / "sitecustomize.py").write_text(
        "import json,os,pathlib,sys\n"
        "_open=os.open\n"
        "_read=os.read\n"
        "external_fds=set()\n"
        "with pathlib.Path(os.environ['AGENTFLOW_TEST_BOOTSTRAP_AUTHORITY']).open('a') as stream:\n"
        " stream.write(json.dumps([os.getcwd(),sys.argv])+ '\\n')\n"
        "def open(path,flags,*args,**kwargs):\n"
        " result=_open(path,flags,*args,**kwargs)\n"
        " if isinstance(path,(str,bytes,os.PathLike)) and os.fspath(path) == os.environ['AGENTFLOW_TEST_FORGED_EXTERNAL']:\n"
        "  external_fds.add(result)\n"
        " return result\n"
        "def read(fd,size):\n"
        " if fd in external_fds: pathlib.Path(os.environ['AGENTFLOW_TEST_EXTERNAL_READ']).write_text('read')\n"
        " return _read(fd,size)\n"
        "os.open=open\n"
        "os.read=read\n")
    monkeypatch.setenv("AGENTFLOW_TEST_BOOTSTRAP_AUTHORITY", str(bootstrap))
    monkeypatch.setenv("AGENTFLOW_TEST_FORGED_EXTERNAL", str(external))
    monkeypatch.setenv("AGENTFLOW_TEST_EXTERNAL_READ", str(external_read))
    monkeypatch.setenv(
        "PYTHONPATH", f"{custom}{os.pathsep}{os.environ.get('PYTHONPATH', '')}")
    marker = tmp_path / "forged-root-renewed"
    started = _codex_command_event(_bounded_worker_command(tmp_path))
    script = (
        "import json,pathlib,sys,time\n"
        f"print(json.dumps({started!r}), flush=True)\n"
        "time.sleep(.05)\n"
        "pathlib.Path(sys.argv[1]).write_text('changed')\n"
        "time.sleep(.19)\n"
        "pathlib.Path(sys.argv[2]).write_text('renewed')\n")
    coord = Coordinator(launcher=LocalLauncher(
        lambda _record: [sys.executable, "-c", script, str(target), str(marker), str(forged)],
        timeout=5, build_lease=(0.50, 0.60, 1.2)))
    identity = coord.submit_stage(_build("codex", "forged-root", str(source)))
    coord.cycle("codex")

    record = _wait_for_real_child(identity, "forged root redirected the worktree snapshot")
    starts = [json.loads(line) for line in bootstrap.read_text().splitlines()]
    cwd, argv = next(start for start in starts if _launch_child._INHERITED_WORKTREE in start[1])
    assert cwd == str(source)
    assert _launch_child._INHERITED_WORKTREE in argv
    assert not external_read.exists()
    assert marker.exists()
    assert _build_observation(record).timed_out is False


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
