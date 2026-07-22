"""The independent Claude quota poll — the facts' *producer* (#309, #315).

The balancer fails closed on a missing Claude window fact, and the provider's headless stream
only emits whichever window is in warning (often ``seven_day``), so nothing seeds a cold store or
notices a reset while the fleet is parked. This poll reads the provider's own usage endpoint each
dispatch pass and records fresh facts for *both* the five-hour and the seven-day window from one
response. These tests drive the public ``refresh_claude_quota`` with the credential read and HTTP
fetch stubbed, so no network or Keychain is touched.
"""

from __future__ import annotations

import json
import time
import urllib.error

from agentflow.coordinator import quota, quota_poll
from agentflow.coordinator.quota import FIVE_HOUR, SEVEN_DAY, epoch_seconds


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
    """Point ``_fetch_windows``'s HTTP call at a fixed body (str) or an exception instance."""
    def fake_urlopen(_request, timeout=None):
        if isinstance(body, Exception):
            raise body
        return _FakeResponse(body)
    monkeypatch.setattr(quota_poll.urllib.request, "urlopen", fake_urlopen)


def _stub(monkeypatch, *, token="tok", windows="default", now=None):
    """Stub the credential read and the endpoint fetch. ``windows`` is the dict the fetch returns
    (``None`` for a transport failure); ``"default"`` yields a fresh five-hour + seven-day pair."""
    now = int(time.time()) if now is None else int(now)
    if windows == "default":
        windows = {FIVE_HOUR: (12.0, now + 3 * 3600), SEVEN_DAY: (3.0, now + 3 * 24 * 3600)}
    monkeypatch.setattr(quota_poll, "_access_token", lambda: token)
    monkeypatch.setattr(quota_poll, "_fetch_windows", lambda _t: windows)
    return windows


def test_poll_records_a_fresh_fact_for_each_window(monkeypatch, tmp_path):
    """A cold store is seeded from the endpoint's five-hour AND seven-day readings in one pass —
    the bootstrap the stream event cannot provide, and the weekly fact the paced gate needs (#315)."""
    store = tmp_path / "records.db"
    now = int(time.time())
    _stub(monkeypatch, windows={FIVE_HOUR: (12.0, now + 3 * 3600),
                                SEVEN_DAY: (7.5, now + 3 * 24 * 3600)}, now=now)
    fact = quota_poll.refresh_claude_quota(store, now=now)
    assert fact is not None and fact.used_percent == 12.0
    assert fact.provenance == "oauth:five_hour"
    assert quota.read_quota(store, "claude", FIVE_HOUR) == fact
    weekly = quota.read_quota(store, "claude", SEVEN_DAY)
    assert weekly is not None and weekly.used_percent == 7.5
    assert weekly.provenance == "oauth:seven_day"


def test_a_partial_response_preserves_the_other_window(monkeypatch, tmp_path):
    """A response that carries only a fresh five-hour window updates that window and leaves the
    prior seven-day fact untouched — updating one window never erases the other (#315)."""
    store = tmp_path / "records.db"
    now = int(time.time())
    _stub(monkeypatch, windows={FIVE_HOUR: (12.0, now + 3 * 3600),
                                SEVEN_DAY: (7.5, now + 3 * 24 * 3600)}, now=now)
    quota_poll.refresh_claude_quota(store, now=now, ttl=60)
    # A later poll returns only the five-hour window (the seven-day one dropped from the payload).
    _stub(monkeypatch, windows={FIVE_HOUR: (40.0, now + 200 + 3 * 3600)}, now=now + 200)
    quota_poll.refresh_claude_quota(store, now=now + 200, ttl=60)
    assert quota.read_quota(store, "claude", FIVE_HOUR).used_percent == 40.0  # refreshed
    assert quota.read_quota(store, "claude", SEVEN_DAY).used_percent == 7.5   # preserved


def test_a_fresh_fact_is_not_re_polled(monkeypatch, tmp_path):
    """Within the TTL the persisted facts are reused, so a burst of passes cannot hammer the
    undocumented endpoint."""
    store = tmp_path / "records.db"
    _stub(monkeypatch, now=int(time.time()))
    quota_poll.refresh_claude_quota(store, ttl=600)
    calls = []
    monkeypatch.setattr(quota_poll, "_fetch_windows", lambda _t: calls.append(1) or {})
    quota_poll.refresh_claude_quota(store, ttl=600)
    assert calls == []  # both windows fresh, no second fetch


def test_a_stale_fact_is_refreshed(monkeypatch, tmp_path):
    """Past the TTL a new reading replaces the old one, so the balancer sizes headroom from the
    latest provider fact."""
    store = tmp_path / "records.db"
    now = time.time()
    _stub(monkeypatch, windows={FIVE_HOUR: (12.0, int(now) + 3 * 3600),
                                SEVEN_DAY: (3.0, int(now) + 3 * 24 * 3600)}, now=now)
    quota_poll.refresh_claude_quota(store, now=now, ttl=60)
    _stub(monkeypatch, windows={FIVE_HOUR: (40.0, int(now) + 120 + 3 * 3600),
                                SEVEN_DAY: (3.0, int(now) + 3 * 24 * 3600)}, now=now + 120)
    fact = quota_poll.refresh_claude_quota(store, now=now + 120, ttl=60)
    assert fact is not None and fact.used_percent == 40.0


def test_a_missing_credential_leaves_the_prior_facts(monkeypatch, tmp_path):
    """Fail closed: with no token the poll changes nothing — the balancer keeps whatever facts it
    had, rather than the daemon crashing."""
    store = tmp_path / "records.db"
    now = time.time()
    _stub(monkeypatch, now=now)
    quota_poll.refresh_claude_quota(store, now=now, ttl=60)
    monkeypatch.setattr(quota_poll, "_access_token", lambda: None)
    assert quota_poll.refresh_claude_quota(store, now=now + 120, ttl=60) is None
    assert quota.read_quota(store, "claude", FIVE_HOUR).used_percent == 12.0  # prior fact untouched
    assert quota.read_quota(store, "claude", SEVEN_DAY).used_percent == 3.0


def test_an_unreachable_endpoint_records_nothing(monkeypatch, tmp_path):
    """A transport/shape failure on a cold store persists no fact — the balancer stays fail-closed
    rather than dispatching blind."""
    store = tmp_path / "records.db"
    _stub(monkeypatch, windows=None)
    assert quota_poll.refresh_claude_quota(store) is None
    assert quota.read_quota(store, "claude", FIVE_HOUR) is None
    assert quota.read_quota(store, "claude", SEVEN_DAY) is None


# --- the endpoint's real payload parsing (the undocumented shape, exercised end to end) ---------

# Both windows as the live endpoint actually returns them (issues #309, #315): utilization is a
# 0..100 percent and resets_at is an ISO-8601 string.
_LIVE_BODY = json.dumps({
    "five_hour": {"utilization": 9.0, "resets_at": "2026-07-21T19:40:00.507502+00:00",
                  "limit_dollars": None, "used_dollars": None},
    "seven_day": {"utilization": 46.0, "resets_at": "2026-07-27T12:00:00+00:00"},
})


def test_fetch_parses_both_windows_from_one_response(monkeypatch):
    """The real payload yields each window's percent unscaled and its ISO reset as epoch seconds —
    the parse that the stubbed higher-level tests never reach."""
    _serve(monkeypatch, _LIVE_BODY)
    windows = quota_poll._fetch_windows("tok")
    assert windows[FIVE_HOUR] == (9.0, epoch_seconds("2026-07-21T19:40:00.507502+00:00"))
    assert windows[SEVEN_DAY] == (46.0, epoch_seconds("2026-07-27T12:00:00+00:00"))


def test_fetch_does_not_rescale_a_sub_one_percent_reading(monkeypatch):
    """The endpoint percent is used verbatim: a genuine 0.6% reading stays 0.6, never multiplied to
    60% the way the stream event's fraction would be. This is the assumption the gate rests on."""
    _serve(monkeypatch, json.dumps({"five_hour": {"utilization": 0.6, "resets_at": 4_000_000_000}}))
    assert quota_poll._fetch_windows("tok")[FIVE_HOUR] == (0.6, 4_000_000_000)


def test_fetch_drops_a_malformed_window_but_keeps_a_good_one(monkeypatch):
    """A percent outside 0..100, a boolean, or a missing reset makes only *that* window absent from
    the mapping — a good sibling window still comes back, so a partial payload updates what it can."""
    for bad in ({"utilization": 150, "resets_at": 4_000_000_000},
                {"utilization": True, "resets_at": 4_000_000_000},
                {"resets_at": 4_000_000_000},
                {"utilization": 9.0}):   # no reset
        _serve(monkeypatch, json.dumps({"five_hour": {"utilization": 9.0, "resets_at": 4_000_000_000},
                                        "seven_day": bad}))
        windows = quota_poll._fetch_windows("tok")
        assert windows == {FIVE_HOUR: (9.0, 4_000_000_000)}, bad


def test_fetch_returns_empty_when_no_window_is_trustworthy(monkeypatch):
    """A body with no usable window is an empty mapping (nothing to record), and unparseable JSON
    is ``None`` (a transport/shape failure)."""
    _serve(monkeypatch, json.dumps({"unrelated": {"utilization": 9.0}}))
    assert quota_poll._fetch_windows("tok") == {}
    _serve(monkeypatch, "not json at all")
    assert quota_poll._fetch_windows("tok") is None


def test_fetch_fails_closed_on_a_transport_error(monkeypatch):
    """A network failure is a ``None`` reading, never an exception that escapes into the cycle."""
    _serve(monkeypatch, urllib.error.URLError("boom"))
    assert quota_poll._fetch_windows("tok") is None


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
