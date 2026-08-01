"""Regression coverage for the suite-wide state-isolation fixture (issue #396).

`agentflow.live`, `agentflow.daemon`, and `agentflow.ratchet` each bind their state paths at
import time — before `conftest.py`'s autouse `_isolated_agentflow_state` fixture ever runs — so
setting `AGENTFLOW_STATE` alone cannot redirect them. This file enumerates every already-bound
target the fixture must patch directly and proves a real stateful read/write lands inside the
private per-test directory. It also reproduces the exact poisoned-parent-state scenario the issue
found: a live fleet's durable ledger and daemon-status file sitting under `AGENTFLOW_STATE` when
the suite starts must never be read from or written to.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from agentflow import daemon, live, ratchet
from agentflow.coordinator.record import RUNNING, Record
from agentflow.coordinator.store import Store


ROOT = Path(__file__).parents[1]


def test_every_import_time_bound_state_target_resolves_under_the_private_test_directory(tmp_path):
    """Enumerate every target the issue named as bound at import time. This fails if the suite-wide
    fixture stops patching one of them — the module constant would then still point at whatever
    ``AGENTFLOW_STATE`` (or the ``~/.agentflow`` default) the test process started with."""
    targets = {
        "live.STATE_DIR": live.STATE_DIR,
        "live.LIVE_FILE": live.LIVE_FILE,
        "live.DAEMON_FILE": live.DAEMON_FILE,
        "live.SNAPSHOT_FILE": live.SNAPSHOT_FILE,
        "daemon.STATE_DIR": daemon.STATE_DIR,
        "daemon.ENABLE_FLAG": daemon.ENABLE_FLAG,
        "daemon.LOCK": daemon.LOCK,
        "ratchet.STATE": ratchet.STATE,
        "ratchet.record's default path": ratchet.record.__defaults__[0],
        "ratchet.record_once's default path": ratchet.record_once.__defaults__[0],
        "ratchet.status's default path": ratchet.status.__defaults__[0],
    }
    for name, path in targets.items():
        path = Path(path)
        assert path == tmp_path or tmp_path in path.parents, (
            f"{name} ({path}) is not inside the private test directory {tmp_path}")


def test_live_reads_and_writes_land_only_inside_the_private_directory(tmp_path):
    live.replace_projection([{"identity": "x"}])
    live.mark_cycle(15)
    live.write_snapshot({"ok": True})

    assert live.running() == [{"identity": "x"}]
    assert live.daemon_status()["poll_seconds"] == 15
    assert live.read_snapshot() == {"ok": True}
    assert {live.LIVE_FILE.parent, live.DAEMON_FILE.parent, live.SNAPSHOT_FILE.parent} == {tmp_path}


def test_ratchet_writes_land_only_inside_the_private_directory(tmp_path):
    ratchet.record("owner/repo", ratchet.CLEAN_MERGE)
    assert ratchet.STATE.parent == tmp_path
    assert ratchet.status("owner/repo")["samples"] == 1


_AFFECTED_TESTS = (
    "tests/test_build_tracer.py::test_interactive_start_leaves_the_background_pace_slot_intact",
    "tests/test_cli.py::test_daemon_once_starts_with_only_the_configured_repositories",
    "tests/test_cli.py::test_public_daemon_selects_the_bundled_capacity_helper",
)


def test_the_affected_tests_stay_green_and_do_not_touch_a_poisoned_parent_state(tmp_path):
    """Reproduce the issue's exact trap: start pytest with ``AGENTFLOW_STATE`` already pointing at
    a directory holding 5 running Claude permits (enough alone to overshoot the 85% ceiling — see
    the reserve arithmetic in ``test_interactive_start_leaves_the_background_pace_slot_intact``)
    and a sentinel ``daemon-status.json``. The affected tests must still pass, and the sentinel
    must come back byte-for-byte unchanged — proving the suite never opened that ledger or wrote
    through that file, regardless of what the parent process's state directory held."""
    parent_state = tmp_path / "poisoned-parent-state"
    store = Store(parent_state / "coordinator" / "records.db")
    try:
        store.upsert(Record(identity="poisoned-build", stage="build", pool="claude", demand=5,
                            repo="owner/repo", subject="1", state=RUNNING))
    finally:
        store.close()
    sentinel_path = parent_state / "daemon-status.json"
    sentinel_path.write_text(json.dumps({"sentinel": True, "poll_seconds": 999}))
    sentinel_before = sentinel_path.read_bytes()

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *_AFFECTED_TESTS],
        cwd=ROOT,
        env=os.environ | {"AGENTFLOW_STATE": str(parent_state)},
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel_path.read_bytes() == sentinel_before
