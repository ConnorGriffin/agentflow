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


# --- workspace projection (file-only, ADR 0033) ----------------------------------------

def _workspace_client(*, workspace=None, available=True, enqueue=None):
    return TestClient(webapp.create_app(
        lambda: None,
        read_workspace=workspace or (lambda: None),
        available=lambda: available,
        enqueue=enqueue or (lambda command: None)))


def test_workspace_endpoint_serves_the_published_projection():
    published = {"workspace": {"revision": 7, "available": True},
                 "projects": [{"id": "p", "repo": "o/r", "conversations": []}]}
    body = _workspace_client(workspace=lambda: published).get("/api/workspace").json()
    assert body == published, "the endpoint is the projection file's contents, verbatim"


def test_workspace_endpoint_reads_as_empty_before_any_publish():
    body = _workspace_client(workspace=lambda: None).get("/api/workspace").json()
    assert body["projects"] == [] and body["workspace"]["available"] is False


# --- command transport (POST → daemon channel; never a direct write, ADR 0033) ---------

def test_command_is_transported_with_its_key_when_the_daemon_is_up():
    sent = []
    client = _workspace_client(available=True, enqueue=sent.append)
    cmd = {"key": "k1", "kind": "open_ask", "repo": "o/r", "conversation_id": "c1", "prompt": "hi"}
    res = client.post("/api/command", json=cmd)
    assert res.status_code == 202 and res.json()["key"] == "k1"
    assert sent == [cmd], "the web layer only transports — it enqueues the command verbatim"


def test_command_fails_unavailable_when_the_daemon_is_down_with_no_direct_write():
    sent = []
    client = _workspace_client(available=False, enqueue=sent.append)
    res = client.post("/api/command", json={
        "key": "k1", "kind": "open_ask", "repo": "o/r", "conversation_id": "c1", "prompt": "hi"})
    assert res.status_code == 503 and res.json()["status"] == "unavailable"
    assert sent == [], "no direct-write fallback — nothing is enqueued when the daemon is down"


def test_command_with_a_missing_field_is_rejected_as_a_transport_error():
    sent = []
    client = _workspace_client(available=True, enqueue=sent.append)
    res = client.post("/api/command", json={
        "key": "k1", "kind": "send_turn", "repo": "o/r", "conversation_id": "c1", "prompt": "hi"})
    assert res.status_code == 400          # send_turn requires expected_revision
    assert sent == []


def test_unknown_command_kind_is_rejected():
    client = _workspace_client(available=True)
    assert client.post("/api/command", json={"key": "k", "kind": "delete_everything"}).status_code == 400
