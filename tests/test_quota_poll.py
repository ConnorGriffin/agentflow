"""The independent five-hour quota poll — the fact's *producer* (#309).

The balancer fails closed on a missing Claude five-hour fact, and the provider's headless stream
only emits whichever window is in warning (often ``seven_day``), so nothing seeds a cold store or
notices a reset while the fleet is parked. This poll reads the provider's own usage endpoint each
dispatch pass and records a fresh five-hour fact. These tests drive the public
``refresh_claude_quota`` with the credential read and HTTP fetch stubbed, so no network or Keychain
is touched.
"""

from __future__ import annotations

import json
import time
import urllib.error

from agentflow.coordinator import quota, quota_poll
from agentflow.coordinator.quota import epoch_seconds


class _FakeResponse:
    """A urlopen stand-in — a context manager whose ``read()`` returns the encoded body."""

    def __init__(self, body: str):
        self._body = body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _serve(monkeypatch, body):
    """Point ``_fetch_five_hour``'s HTTP call at a fixed body (str) or an exception instance."""
    def fake_urlopen(_request, timeout=None):
        if isinstance(body, Exception):
            raise body
        return _FakeResponse(body)
    monkeypatch.setattr(quota_poll.urllib.request, "urlopen", fake_urlopen)


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


# --- the endpoint's real payload parsing (the undocumented shape, exercised end to end) ---------

# The five-hour window as the live endpoint actually returns it (issue #309): utilization is a
# 0..100 percent and resets_at is an ISO-8601 string.
_LIVE_BODY = json.dumps({"five_hour": {"utilization": 9.0,
                                       "resets_at": "2026-07-21T19:40:00.507502+00:00",
                                       "limit_dollars": None, "used_dollars": None}})


def test_fetch_parses_the_live_endpoint_shape(monkeypatch):
    """The real payload yields the five-hour percent unscaled and its ISO reset as epoch seconds —
    the parse that the stubbed higher-level tests never reach."""
    _serve(monkeypatch, _LIVE_BODY)
    used, resets_at = quota_poll._fetch_five_hour("tok")
    assert used == 9.0
    assert resets_at == epoch_seconds("2026-07-21T19:40:00.507502+00:00")


def test_fetch_does_not_rescale_a_sub_one_percent_reading(monkeypatch):
    """The endpoint percent is used verbatim: a genuine 0.6% reading stays 0.6, never multiplied to
    60% the way the stream event's fraction would be. This is the assumption the gate rests on."""
    _serve(monkeypatch, json.dumps({"five_hour": {"utilization": 0.6, "resets_at": 4_000_000_000}}))
    used, _ = quota_poll._fetch_five_hour("tok")
    assert used == 0.6


def test_fetch_rejects_an_out_of_range_or_malformed_utilization(monkeypatch):
    """A percent outside 0..100, a boolean, or a missing window is not a trustworthy reading."""
    for body in (json.dumps({"five_hour": {"utilization": 150, "resets_at": 4_000_000_000}}),
                 json.dumps({"five_hour": {"utilization": True, "resets_at": 4_000_000_000}}),
                 json.dumps({"five_hour": {"resets_at": 4_000_000_000}}),
                 json.dumps({"five_hour": {"utilization": 9.0}}),      # no reset
                 json.dumps({"seven_day": {"utilization": 9.0}}),       # wrong window
                 "not json at all"):
        _serve(monkeypatch, body)
        assert quota_poll._fetch_five_hour("tok") is None, body


def test_fetch_fails_closed_on_a_transport_error(monkeypatch):
    """A network failure is a ``None`` reading, never an exception that escapes into the cycle."""
    _serve(monkeypatch, urllib.error.URLError("boom"))
    assert quota_poll._fetch_five_hour("tok") is None


# --- credential sourcing (Keychain preferred, on-disk fallback, malformed skipped) --------------

_BLOB = json.dumps({"claudeAiOauth": {"accessToken": "secret-abc"}})


def test_token_prefers_the_keychain(monkeypatch):
    monkeypatch.setattr(quota_poll, "_keychain_blob", lambda: _BLOB)
    monkeypatch.setattr(quota_poll, "_file_blob",
                        lambda: json.dumps({"claudeAiOauth": {"accessToken": "from-file"}}))
    assert quota_poll._access_token() == "secret-abc"


def test_token_falls_back_to_the_file(monkeypatch):
    monkeypatch.setattr(quota_poll, "_keychain_blob", lambda: None)
    monkeypatch.setattr(quota_poll, "_file_blob", lambda: _BLOB)
    assert quota_poll._access_token() == "secret-abc"


def test_token_skips_a_malformed_blob(monkeypatch):
    """A garbage Keychain value doesn't abort the read — the on-disk credential is still tried."""
    monkeypatch.setattr(quota_poll, "_keychain_blob", lambda: "{not json")
    monkeypatch.setattr(quota_poll, "_file_blob", lambda: _BLOB)
    assert quota_poll._access_token() == "secret-abc"


def test_token_is_none_when_no_source_yields_one(monkeypatch):
    monkeypatch.setattr(quota_poll, "_keychain_blob", lambda: None)
    monkeypatch.setattr(quota_poll, "_file_blob", lambda: None)
    assert quota_poll._access_token() is None
