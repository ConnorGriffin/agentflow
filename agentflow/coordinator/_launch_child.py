"""The provider-launch bootstrap child (ADR 0030).

Run as
``python -m agentflow.coordinator._launch_child <store_path> <identity> <token> [argv...]``.
It double-forks so the provider family is reparented away from the daemon (and so an ended
provider never lingers as a zombie the daemon would misread as alive), then makes a *guarded*
durable ``started`` write with the detached supervisor's pid as the family — recorded only if
this reservation still holds ``token``. Recording the fact before provider spawn is the crash
boundary: the attempt is recoverable even if the provider exits immediately or the daemon
dies before observing it. The guard is the second half of that boundary: if the coordinator
already disowned this launch on a handshake timeout (rotating the token) or returned the
record to waiting, the write is refused and the child exits *without* becoming a provider, so
an uncancelled bootstrap can never start an unreserved, uncounted provider. With no provider
argv (the dormant slice) it exits after a successful start, which reconciliation reads as a
started-but-ended attempt.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from agentflow.coordinator.session import events_path, write_result
from agentflow.coordinator.store import Store


class _TerminationRequested(Exception):
    """Interrupt a provider wait so its supervisor can stop the process group cleanly."""


def _mark_active(working_dir: str) -> Path | None:
    """Mirror the detached supervisor pid into the legacy worktree-liveness marker.

    Startup recovery still consults this current-format marker before coordinator
    reconciliation. Keeping it for the supervisor's lifetime prevents that recovery pass from
    removing a clean coordinator-owned worktree while its provider is alive.
    """
    if not working_dir:
        return None
    from agentflow.runner import _active_marker
    marker = _active_marker(Path(working_dir))
    if marker is not None:
        marker.write_text(str(os.getpid()))
    return marker


def _clear_active(marker: Path | None) -> None:
    if marker is None:
        return
    try:
        if marker.read_text().strip() == str(os.getpid()):
            marker.unlink()
    except OSError:
        pass


def main(args: list[str]) -> None:
    store_path, identity, token, timeout, working_dir, *provider = args
    # Double-fork: the intermediate exits immediately so the daemon reaps it at once, while
    # the detached supervisor is reparented to init and cannot zombie under the daemon.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    store = Store(store_path)
    won = store.child_start(identity, token, os.getpid())
    store.close()
    if not won:
        os._exit(0)  # our reservation is gone; starting a provider now would be unreserved
    marker = _mark_active(working_dir)
    if not provider:
        _clear_active(marker)
        os._exit(0)  # dormant: no provider to become; a started-then-ended attempt
    # Remain as the recorded family supervisor while the provider runs in its own process
    # group. Output streams directly to its durable artifact, so partial output survives a
    # daemon crash. The supervisor records exit/signal/timeout facts after the whole provider
    # family ends; it can terminate that family without killing itself when the deadline fires.
    events = events_path(store_path, token)
    events.parent.mkdir(parents=True, exist_ok=True)
    timed_out = False
    with events.open("w") as output:
        try:
            process = subprocess.Popen(
                provider, cwd=working_dir or None, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            write_result(store_path, token, exit_status=None, signal=None, timed_out=False)
            _clear_active(marker)
            os._exit(0)

        def stop_provider() -> int:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return process.wait()

        def request_stop(_signum, _frame) -> None:
            raise _TerminationRequested

        # Reconciliation signals this supervisor, not the provider's separate session. Turn that
        # request into the same orderly process-group shutdown the deadline path uses, then keep
        # the supervisor alive to write the provider's durable end facts.
        signal.signal(signal.SIGTERM, request_stop)
        try:
            returncode = process.wait(timeout=float(timeout))
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = stop_provider()
        except _TerminationRequested:
            returncode = stop_provider()
        output.flush()
        os.fsync(output.fileno())
    ended_by_signal = -returncode if returncode < 0 else None
    exit_status = returncode if returncode >= 0 else None
    write_result(store_path, token, exit_status=exit_status,
                 signal=ended_by_signal, timed_out=timed_out)
    _clear_active(marker)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
