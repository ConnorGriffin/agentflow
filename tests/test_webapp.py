"""The console server, exercised through its public surface (ADR 0023).

Covers the two clocks: a fast poll must reuse one GitHub-backed snapshot for ~15s,
and a concurrent burst must be single-flight (one production, not one per caller).
`dashboard_data.snapshot()` is patched to a counter so "how many gh rounds happened"
is directly observable through `/api/snapshot`.
"""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from agentflow import webapp


class FakeClock:
    """A hand-advanced monotonic clock so TTL expiry is deterministic, not timed."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _app(monkeypatch, clock, *, ttl=15.0):
    calls = {"n": 0}

    def fake_snapshot(repos, *, dispatch_enabled):
        calls["n"] += 1
        return {"dispatch": {"enabled": dispatch_enabled}, "pools": [], "repos": [],
                "round": calls["n"]}

    monkeypatch.setattr("agentflow.dashboard_data.snapshot", fake_snapshot)
    app = webapp.create_app([], lambda: True, ttl=ttl, clock=clock)
    return TestClient(app), calls


def test_burst_within_ttl_makes_one_gh_round(monkeypatch):
    clock = FakeClock()
    client, calls = _app(monkeypatch, clock)

    bodies = [client.get("/api/snapshot").json() for _ in range(8)]

    assert calls["n"] == 1, "a burst inside the cache window must hit gh exactly once"
    assert all(b["round"] == 1 for b in bodies), "every caller sees the same snapshot"


def test_snapshot_refreshes_after_ttl_expires(monkeypatch):
    clock = FakeClock()
    client, calls = _app(monkeypatch, clock, ttl=15.0)

    assert client.get("/api/snapshot").json()["round"] == 1
    clock.t += 20.0  # push past the TTL
    assert client.get("/api/snapshot").json()["round"] == 2
    assert calls["n"] == 2


def test_snapshot_preserves_todays_contract(monkeypatch):
    """The v2 endpoint returns the unchanged dispatch + pools + repos shape."""
    monkeypatch.setattr(
        "agentflow.dashboard_data.snapshot",
        lambda repos, *, dispatch_enabled: {
            "dispatch": {"enabled": dispatch_enabled}, "pools": [], "repos": []},
    )
    client = TestClient(webapp.create_app([], lambda: False))
    body = client.get("/api/snapshot").json()
    assert set(body) == {"dispatch", "pools", "repos"}
    assert body["dispatch"] == {"enabled": False}


def test_cache_is_single_flight_under_concurrency():
    """Many threads that all miss together trigger exactly one slow production."""
    calls = {"n": 0}

    def slow_produce():
        calls["n"] += 1
        time.sleep(0.05)  # widen the miss window so racers pile up on the lock
        return calls["n"]

    cache = webapp.SnapshotCache(slow_produce, ttl=15.0, clock=FakeClock())
    results: list[int] = []
    barrier = threading.Barrier(12)

    def worker():
        barrier.wait()  # release all callers at once
        results.append(cache.get())

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1, "a concurrent burst must be single-flight"
    assert results == [1] * 12
