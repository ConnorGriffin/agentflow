"""The independent five-hour quota poll — the fact's *producer* (#309).

The balancer fails closed on a missing Claude five-hour fact, and the provider's headless stream
only emits whichever window is in warning (often ``seven_day``), so nothing seeds a cold store or
notices a reset while the fleet is parked. This poll reads the provider's own usage endpoint each
dispatch pass and records a fresh five-hour fact. These tests drive the public
``refresh_claude_quota`` with the credential read and HTTP fetch stubbed, so no network or Keychain
is touched.
"""

from __future__ import annotations

import time

from agentflow.coordinator import quota, quota_poll


def _stub(monkeypatch, *, token="tok", fetched=(12.0, None)):
    used, resets_at = fetched
    if resets_at is None:
        resets_at = int(time.time()) + 3 * 3600
    monkeypatch.setattr(quota_poll, "_access_token", lambda: token)
    monkeypatch.setattr(quota_poll, "_fetch_five_hour",
                        lambda _t: None if used is None else (used, resets_at))
    return resets_at


def test_poll_records_a_fresh_five_hour_fact(monkeypatch, tmp_path):
    """A cold store is seeded from the endpoint's five-hour reading — the bootstrap the stream
    event cannot provide."""
    store = tmp_path / "records.db"
    resets_at = _stub(monkeypatch, fetched=(12.0, int(time.time()) + 3 * 3600))
    fact = quota_poll.refresh_claude_quota(store)
    assert fact is not None and fact.used_percent == 12.0
    assert fact.resets_at == resets_at
    assert fact.provenance == "oauth:five_hour"
    assert quota.read_quota(store, "claude") == fact


def test_a_fresh_fact_is_not_re_polled(monkeypatch, tmp_path):
    """Within the TTL the persisted fact is reused, so a burst of passes cannot hammer the
    undocumented endpoint."""
    store = tmp_path / "records.db"
    _stub(monkeypatch, fetched=(12.0, int(time.time()) + 3 * 3600))
    quota_poll.refresh_claude_quota(store, ttl=600)
    calls = []
    monkeypatch.setattr(quota_poll, "_fetch_five_hour", lambda _t: calls.append(1) or (99.0, 0))
    quota_poll.refresh_claude_quota(store, ttl=600)
    assert calls == []  # the fresh fact was reused, no second fetch


def test_a_stale_fact_is_refreshed(monkeypatch, tmp_path):
    """Past the TTL a new reading replaces the old one, so the balancer sizes headroom from the
    latest provider fact."""
    store = tmp_path / "records.db"
    now = time.time()
    _stub(monkeypatch, fetched=(12.0, int(now) + 3 * 3600))
    quota_poll.refresh_claude_quota(store, now=now, ttl=60)
    _stub(monkeypatch, fetched=(40.0, int(now) + 3 * 3600))
    fact = quota_poll.refresh_claude_quota(store, now=now + 120, ttl=60)
    assert fact is not None and fact.used_percent == 40.0


def test_a_missing_credential_leaves_the_prior_fact(monkeypatch, tmp_path):
    """Fail closed: with no token the poll changes nothing — the balancer keeps whatever fact it
    had, rather than the daemon crashing."""
    store = tmp_path / "records.db"
    now = time.time()
    _stub(monkeypatch, fetched=(12.0, int(now) + 3 * 3600))
    quota_poll.refresh_claude_quota(store, now=now, ttl=60)
    monkeypatch.setattr(quota_poll, "_access_token", lambda: None)
    assert quota_poll.refresh_claude_quota(store, now=now + 120, ttl=60) is None
    assert quota.read_quota(store, "claude").used_percent == 12.0  # prior fact untouched


def test_an_unreachable_endpoint_records_nothing(monkeypatch, tmp_path):
    """A transport/shape failure on a cold store persists no fact — the balancer stays fail-closed
    rather than dispatching blind."""
    store = tmp_path / "records.db"
    _stub(monkeypatch, fetched=(None, None))
    assert quota_poll.refresh_claude_quota(store) is None
    assert quota.read_quota(store, "claude") is None
