"""The console server, exercised through its public surface (ADR 0026).

The server is a pure reader of the daemon-published snapshot: `/api/snapshot`
serves exactly what the daemon last wrote, never queries GitHub, and renders a
daemon that has never run as an empty fleet — not an error.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agentflow import live, webapp


def _client(read):
    return TestClient(webapp.create_app(read))


def test_serves_the_daemon_published_snapshot():
    published = {"dispatch": {"enabled": True}, "daemon": {"gh_fresh_at": "2026-07-13T00:00:00+00:00"},
                 "pools": [], "running": [], "repos": [{"repo": "o/r"}]}
    body = _client(lambda: published).get("/api/snapshot").json()
    assert body == published, "the endpoint is the file's contents, verbatim"


def test_never_ran_daemon_reads_as_an_empty_fleet():
    body = _client(lambda: None).get("/api/snapshot").json()
    assert body["repos"] == [] and body["running"] == []
    assert body["dispatch"] == {"enabled": False}
    assert body["daemon"]["gh_fresh_at"] is None, "no freshness stamp to lie with"


def test_endpoint_reads_fresh_every_poll(tmp_path, monkeypatch):
    """A new publish shows up on the next poll — the reader holds no cache of its own,
    exercised end-to-end through the real state file the daemon writes."""
    monkeypatch.setattr(live, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    client = _client(live.read_snapshot)

    assert client.get("/api/snapshot").json()["repos"] == [], "missing file = empty fleet"
    live.write_snapshot({"dispatch": {"enabled": True}, "repos": [{"repo": "o/r"}]})
    assert client.get("/api/snapshot").json()["dispatch"] == {"enabled": True}
    live.write_snapshot({"dispatch": {"enabled": False}, "repos": []})
    assert client.get("/api/snapshot").json()["dispatch"] == {"enabled": False}
