"""The provider-launch bootstrap child (ADR 0030).

Run as
``python -m agentflow.coordinator._launch_child <store_path> <identity> <token> [argv...]``.
It double-forks so the provider family is reparented away from the daemon (and so an ended
provider never lingers as a zombie the daemon would misread as alive), then makes a *guarded*
durable ``started`` write with the provider's own pid as the family — recorded only if this
reservation still holds ``token``. Recording the fact before replacement is the crash
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
import sys

from agentflow.coordinator.session import events_path, exit_path
from agentflow.coordinator.store import Store


def main(args: list[str]) -> None:
    store_path, identity, token, *provider = args
    # Double-fork: the intermediate exits immediately so the daemon reaps it at once, while
    # the provider grandchild is reparented to init and cannot zombie under the daemon.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    store = Store(store_path)
    won = store.child_start(identity, token, os.getpid())
    store.close()
    if not won:
        os._exit(0)  # our reservation is gone; starting a provider now would be unreserved
    if not provider:
        os._exit(0)  # dormant: no provider to become; a started-then-ended attempt
    # Exec a tiny shell that becomes the provider, redirecting its structured stream to a
    # durable events file and writing its exit status to a durable exit file. The shell owns
    # the reservation's family and finishes writing both even if the daemon dies mid-run, so
    # the full observation set is durable. `"$@"` runs the provider argv verbatim — the prompt
    # never reaches the shell as code.
    events = events_path(store_path, token)
    events.parent.mkdir(parents=True, exist_ok=True)
    os.environ["AGENTFLOW_SESSION_EVENTS"] = str(events)
    os.environ["AGENTFLOW_SESSION_EXIT"] = str(exit_path(store_path, token))
    os.execvp("sh", [
        "sh", "-c",
        '"$@" >"$AGENTFLOW_SESSION_EVENTS" 2>/dev/null; echo $? >"$AGENTFLOW_SESSION_EXIT"',
        "sh", *provider])


if __name__ == "__main__":
    main(sys.argv[1:])
