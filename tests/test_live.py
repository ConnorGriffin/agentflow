"""The generated coordinator live-board projection and fleet snapshot."""

from __future__ import annotations

import pytest

from agentflow import live

TEN_FIELDS = {"repo", "number", "title", "stage", "tool", "model",
              "branch", "worktree", "pid", "started_at"}


def _session(**kw) -> dict:
    base = dict(repo="ConnorGriffin/agentflow", number=70, title="Live board",
                stage="building", tool="codex", model="gpt-5.6-sol",
                branch="agentflow/codex/issue-70-live", worktree="/tmp/wt", pid=123,
                started_at="2026-07-16T00:00:00Z")
    base.update(kw)
    return base


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Point the live board at a throwaway state dir so tests never touch ~/.agentflow."""
    monkeypatch.setattr(live, "LIVE_FILE", tmp_path / "live-sessions.json")
    monkeypatch.setattr(live, "DAEMON_FILE", tmp_path / "daemon-status.json")
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    return tmp_path


def test_snapshot_merges_running_and_daemon_block(state, monkeypatch):
    from agentflow import dashboard_data

    monkeypatch.setattr(dashboard_data, "pools", lambda: [
        {"tool": "claude", "clear": True, "spent_pct": 10, "headroom_pct": 90},
        {"tool": "codex", "clear": True, "spent_pct": 20, "headroom_pct": 80}])
    live.replace_projection([_session(tool="codex", worktree="/tmp/wt-codex")])
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


def test_projection_replaces_every_prior_entry(state):
    live.replace_projection([_session(number=1, worktree="/tmp/old")])
    replacement = _session(number=2, worktree="/tmp/coordinator-build", pid=222)

    live.replace_projection([replacement])

    entries = {entry["worktree"]: entry for entry in live.running()}
    assert set(entries) == {"/tmp/coordinator-build"}
    assert entries["/tmp/coordinator-build"]["pid"] == 222


def test_projection_overwrites_ambiguous_derived_state(state):
    live.LIVE_FILE.write_text("{ partial live state")
    live.replace_projection([])
    assert live.running() == []


def test_snapshot_roundtrips_and_fails_soft(state):
    """The published snapshot reads back verbatim; before any daemon has published —
    or after a corrupt write — the reader says None, never raises (ADR 0026)."""
    assert live.read_snapshot() is None                      # no daemon ever ran

    live.write_snapshot({"dispatch": {"enabled": True}, "repos": []})
    assert live.read_snapshot() == {"dispatch": {"enabled": True}, "repos": []}

    live.SNAPSHOT_FILE.write_text("{ this is not json")
    assert live.read_snapshot() is None                      # corrupt → never an error
