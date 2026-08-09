"""The provider-launch bootstrap child (ADR 0030).

Run as
``python -m agentflow.coordinator._launch_child <store_path> <identity> <token> <timeout>
<working_dir> <role_overrides> <worker_effort> [argv...]``. ``role_overrides`` (``"1"`` or
empty) and ``worker_effort`` (a reasoning-effort string, or empty) are the narrow role-override
*intent* :mod:`agentflow.coordinator.launcher` already verified — never a directory or a file
path. When set, this supervisor recomputes the exact same routes from the routing table itself
(:func:`agentflow.routing.CapabilityRouting.codex_worker_roles`) and generates its own role
directory (:func:`agentflow.codex_native_helpers.build_role_overrides`) immediately before it
spawns the provider, so that directory's path is a local value this one process both creates and
removes — it never crosses argv, an environment variable, or the durable record (#509).

It double-forks so the provider family is reparented away from the daemon (and so an ended
provider never lingers as a zombie the daemon would misread as alive), then makes a *guarded*
durable ``started`` write with the detached supervisor's pid as the family — recorded only if
this reservation still holds ``token``. Recording the fact before provider spawn is the crash
boundary: the attempt is recoverable even if the provider exits immediately or the daemon
dies before observing it. The guard is the second half of that boundary: if the coordinator
already disowned this launch on a handshake timeout (rotating the token) or returned the
record to waiting, the write is refused and the child exits *without* becoming a provider — and
without ever having generated a role directory, so there is nothing to clean up in that case.
With no provider argv (the dormant slice) it exits after a successful start, which reconciliation
reads as a started-but-ended attempt.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from agentflow import codex_native_helpers
from agentflow.coordinator.session import events_path, write_result
from agentflow.coordinator.store import Store
from agentflow.routing import routing


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


def _prepare_provider_argv(
        provider: list[str], role_overrides: bool,
        worker_effort: str | None) -> tuple[list[str], Path | None]:
    """Generate this launch's role overrides (#509), immediately before ``Popen``, and splice
    their CLI argv into *provider* right before its final positional element (the prompt) —
    order among ``-c`` flags ahead of that positional argument does not matter to ``codex exec``.
    Returns the resulting argv and the local directory this call owns (``None`` for a launch
    that declared no role overrides, or one with no provider argv to append ahead of). The
    directory is created fresh here, read once to build this argv, and never returned any
    further than this function's caller — the same process that also cleans it up."""
    if not role_overrides or not provider:
        return provider, None
    overrides = codex_native_helpers.build_role_overrides(
        routing.codex_worker_roles(worker_effort))
    if not overrides.argv:
        return provider, overrides.directory
    return provider[:-1] + overrides.argv + provider[-1:], overrides.directory


def main(args: list[str]) -> None:
    (store_path, identity, token, timeout, working_dir, role_overrides_flag,
     worker_effort, *provider) = args
    role_overrides = bool(role_overrides_flag)
    worker_effort = worker_effort or None
    # Double-fork: the intermediate exits immediately so the daemon reaps it at once, while
    # the detached supervisor is reparented to init and cannot zombie under the daemon.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    stop_requested = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    # Once child_start publishes this pid, reconciliation may signal it immediately. Install
    # the handler before that family becomes externally visible; it only records intent so a
    # request arriving inside store or process startup cannot strand an async exception there.
    signal.signal(signal.SIGTERM, request_stop)
    store = Store(store_path)
    won = store.child_start(identity, token, os.getpid())
    store.close()
    if not won:
        # Our reservation is gone; starting a provider now would be unreserved. Role-override
        # generation only ever happens further down, immediately before this supervisor's own
        # Popen call — nothing has been generated yet at this point, so there is nothing to
        # clean up here.
        os._exit(0)
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
    # Own the role directory (#509), if any, for its whole lifetime: this supervisor is the one
    # process that both generates it (immediately below, right before Popen) and observes the
    # provider actually end, so it is the one that removes it — on a role-generation failure, a
    # spawn failure, a clean exit, or a provider failure alike — rather than leaving every
    # launch's directory for the 24h stale sweep. ``os._exit`` bypasses ``finally`` blocks, so
    # every exit below calls this explicitly instead of relying on one wrapping ``try``.
    owned_role_dir: Path | None = None
    with events.open("w") as output:
        try:
            provider, owned_role_dir = _prepare_provider_argv(
                provider, role_overrides, worker_effort)
            process = subprocess.Popen(
                provider, cwd=working_dir or None, stdout=output,
                stderr=subprocess.STDOUT, start_new_session=True)
        except OSError:
            # Either role-override generation or the provider spawn itself failed; either way no
            # provider family ever came into existence for this attempt.
            write_result(store_path, token, exit_status=None, signal=None, timed_out=False)
            _clear_active(marker)
            codex_native_helpers.cleanup_role_dirs(owned_role_dir)
            os._exit(0)

        def stop_provider() -> int:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                # Do not claim this attempt ended while its provider may still be running.
                # The event artifact is the durable operator-facing record of why this
                # supervisor remains until it can observe the provider's real exit.
                print("agentflow: permission denied stopping provider process group; "
                      "waiting for provider exit", file=output, flush=True)
            try:
                return process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    print("agentflow: permission denied force-stopping provider process group; "
                          "waiting for provider exit", file=output, flush=True)
                return process.wait()

        # Reconciliation signals this supervisor, not the provider's separate session. Turn that
        # request into the same orderly process-group shutdown the deadline path uses, then keep
        # the supervisor alive to write the provider's durable end facts.
        deadline = time.monotonic() + float(timeout)
        while True:
            if stop_requested:
                returncode = stop_provider()
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                returncode = stop_provider()
                break
            try:
                returncode = process.wait(timeout=min(remaining, 0.1))
                break
            except subprocess.TimeoutExpired:
                pass
        output.flush()
        os.fsync(output.fileno())
    ended_by_signal = -returncode if returncode < 0 else None
    exit_status = returncode if returncode >= 0 else None
    write_result(store_path, token, exit_status=exit_status,
                 signal=ended_by_signal, timed_out=timed_out)
    _clear_active(marker)
    codex_native_helpers.cleanup_role_dirs(owned_role_dir)
    os._exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
