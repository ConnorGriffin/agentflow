"""The provider-launch bootstrap child (ADR 0030).

Run as ``python -m agentflow.coordinator._launch_child <store_path> <identity> [argv...]``.
It double-forks so the provider family is reparented away from the daemon (and so an ended
provider never lingers as a zombie the daemon would misread as alive), durably records
``started`` with the provider's own pid as the family, then ``exec``-replaces itself with
the provider argv. Recording the fact before replacement is the crash boundary: the attempt
is recoverable even if the provider exits immediately or the daemon dies before observing
it. With no provider argv (the dormant slice) it exits after recording the start, which
reconciliation reads as a started-but-ended attempt.
"""

from __future__ import annotations

import os
import sys

from agentflow.coordinator.launcher import STARTED
from agentflow.coordinator.store import Store


def main(args: list[str]) -> None:
    store_path, identity, *provider = args
    # Double-fork: the intermediate exits immediately so the daemon reaps it at once, while
    # the provider grandchild is reparented to init and cannot zombie under the daemon.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    store = Store(store_path)
    record = store.load().get(identity)
    if record is None:
        store.close()
        os._exit(0)  # nothing reserved this identity; there is nothing to start
    record.start_fact = STARTED
    record.family = str(os.getpid())
    record.process_alive = True
    store.upsert(record)
    store.close()
    if not provider:
        os._exit(0)  # dormant: no provider to become; a started-then-ended attempt
    os.execvp(provider[0], provider)


if __name__ == "__main__":
    main(sys.argv[1:])
