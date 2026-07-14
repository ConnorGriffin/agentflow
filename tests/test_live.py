"""The live board (issue #70), tested through its interfaces.

Three surfaces meet here: the daemon's write seam (`worktree_session` records a session on
start and removes it on finish), the snapshot merge (`running[]` + the `daemon` block, with
per-pool counts derived from `running[]`), and the startup dead-session sweep (`reap`, and the
daemon's recovery pass that calls it). Each test would have failed before this change — there
was no live file to record to, no `running[]` in the snapshot, and a dead entry had nothing to
drop it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentflow import live, runner as runner_mod
from agentflow.runner import worktree_session

TEN_FIELDS = {"repo", "number", "title", "stage", "tool", "model",
              "branch", "worktree", "pid", "started_at"}


def _session(**kw) -> live.Session:
    base = dict(repo="ConnorGriffin/agentflow", number=70, title="Live board",
                stage="building", tool="codex", model="gpt-5.6-sol",
                branch="agentflow/codex/issue-70-live")
    base.update(kw)
    return live.Session(**base)


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Point the live board at a throwaway state dir so tests never touch ~/.agentflow."""
    monkeypatch.setattr(live, "LIVE_FILE", tmp_path / "live-sessions.json")
    monkeypatch.setattr(live, "DAEMON_FILE", tmp_path / "daemon-status.json")
    return tmp_path


def test_seam_records_on_start_and_removes_on_finish(state, tmp_path, monkeypatch):
    """The one write seam records a full entry as the session starts and clears it on exit."""
    monkeypatch.setattr(runner_mod, "_active_marker", lambda wt: None)  # no git needed
    wt = tmp_path / "wt"

    assert live.running() == []
    with worktree_session(wt, _session()):
        runs = live.running()
        assert len(runs) == 1
        entry = runs[0]
        assert set(entry) == TEN_FIELDS
        assert entry["stage"] == "building"
        assert entry["number"] == 70
        assert entry["tool"] == "codex"
        assert entry["model"] == "sol"                       # stored shortened for display
        assert entry["pid"] == os.getpid()                   # a real, live pid
        assert entry["branch"] == "agentflow/codex/issue-70-live"
        assert entry["worktree"] == os.path.realpath(wt)
        assert entry["started_at"]
    assert live.running() == []                              # gone when the session ends


def test_snapshot_merges_running_and_daemon_block(state, monkeypatch):
    from agentflow import dashboard_data

    monkeypatch.setattr(dashboard_data, "pools", lambda: [
        {"tool": "claude", "clear": True, "spent_pct": 10, "headroom_pct": 90},
        {"tool": "codex", "clear": True, "spent_pct": 20, "headroom_pct": 80}])
    live.record(_session(tool="codex"), "/tmp/wt-codex")
    live.mark_cycle(300)

    snap = dashboard_data.snapshot([], dispatch_enabled=True)

    assert len(snap["running"]) == 1 and snap["running"][0]["tool"] == "codex"
    # a pool's running count always equals its sessions in running[]
    assert {p["tool"]: p["running"] for p in snap["pools"]} == {"claude": 0, "codex": 1}
    daemon = snap["daemon"]
    assert daemon["enabled"] is True
    assert daemon["poll_seconds"] == 300
    assert daemon["last_cycle_at"] and daemon["gh_fresh_at"]


def test_missing_or_corrupt_file_reads_as_fleet_idle(state):
    assert live.running() == []                              # missing → idle
    live.LIVE_FILE.write_text("{ this is not json")
    assert live.running() == []                              # corrupt → idle, never a crash


def test_reap_drops_dead_session_and_keeps_the_live_one(state):
    live.record(_session(number=1), "/tmp/alive")
    live.record(_session(number=2), "/tmp/dead")

    dropped = live.reap(lambda wt: wt == "/tmp/alive")

    assert [e["number"] for e in live.running()] == [1]
    assert [e["number"] for e in dropped] == [2]


def test_daemon_startup_sweep_drops_a_dead_pid_entry(state):
    """The daemon's startup recovery pass sweeps the live board with the same liveness signal
    the worktree recovery uses — a session whose worktree is gone never reaches running[]."""
    from agentflow import daemon

    live.record(_session(), "/no/such/worktree/path")       # its owning worktree is gone
    daemon.recover_worktrees([], _log=lambda *a: None)      # no repos → only the live sweep

    assert live.running() == []
