"""Independent Claude quota poll — the dispatch authority's *producer* (issues #309, #315).

`agentflow.coordinator.quota` owns how a window fact is validated, stored, and read; this
module owns getting fresh ones *without a session having run*. The provider's headless
``rate_limit_event`` only carries whichever window is currently in warning (often ``seven_day``),
so it cannot be relied on to ever report ``five_hour`` — a fleet that gates Claude on that event
alone never seeds a cold store and never notices a window reset while parked (both observed in
#307). This poll closes that hole: each dispatch pass it reads Anthropic's own OAuth usage
endpoint, which reports **both** the ``five_hour`` window (short-term headroom) and the
``seven_day`` window (the paced weekly allowance, #315) in one response, and records each as an
independent pool fact. Each window is validated and persisted on its own, so a partial response
that carries only one still-trustworthy window updates that window and leaves the other in place.

The subscription OAuth token is read locally (the same credential the daemon already launches
Claude sessions with) and used only to call the provider's own usage endpoint — never logged,
never persisted, never placed in the fact. Everything fails closed: a missing credential, an
unreachable endpoint, or an unparseable body simply leaves the prior fact in place, so the
balancer keeps failing closed on a missing/stale fact rather than the daemon crashing.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from agentflow.coordinator.quota import (FIVE_HOUR, SEVEN_DAY, build_fact, epoch_seconds,
                                          read_quota, record_quota)

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_KEYCHAIN_SERVICE = "Claude Code-credentials"
_CREDENTIAL_FILE = "~/.claude/.credentials.json"

# How fresh a persisted fact must be to skip the poll. A dispatch pass runs at most per heartbeat
# (or on a change probe), so this only bounds a burst of change-triggered passes from hitting the
# undocumented endpoint every few seconds. Env-overridable.
POLL_TTL_SECONDS = int(os.environ.get("AGENTFLOW_QUOTA_POLL_TTL_S", "60"))
_HTTP_TIMEOUT = 10
# Upper bound on the throttle wait we report, so a bogus/absent ``Retry-After`` still logs an
# actionable, finite number rather than an hours-long or missing value. The reset-aware
# roll-forward (#322), not this poll, is what unwedges the pool — the log is diagnostic only.
_MAX_RETRY_AFTER_SECONDS = 3600


def _access_token() -> str | None:
    """The subscription OAuth access token, from the macOS Keychain first (where Claude Code keeps
    it) then the on-disk credential file. Returns ``None`` — never raises — when neither yields a
    token, so a missing credential just leaves the fact untouched."""
    for blob in (_keychain_blob(), _file_blob()):
        if not blob:
            continue
        try:
            token = json.loads(blob)["claudeAiOauth"]["accessToken"]
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(token, str) and token:
            return token
    return None


def _keychain_blob() -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-w"],
            text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _file_blob() -> str | None:
    try:
        return Path(os.path.expanduser(_CREDENTIAL_FILE)).read_text()
    except OSError:
        return None


def _window_reading(window: dict) -> tuple[float, int] | None:
    """``(used_percent, resets_at)`` for one window object, or ``None`` for any malformed shape.

    The endpoint reports ``utilization`` as a **0..100 percent** and is used unscaled. This is the
    load-bearing assumption of the whole gate — if it were a 0..1 fraction, a busy pool would record
    a fraction-of-a-percent and the ceiling would silently never bite — so it is grounded in the
    live payload, not inferred: the endpoint returned ``9.0``/``10.0``/``26.0`` for a pool visibly
    tracking real spend (issue #309). It is deliberately *not* run through the stream extractor's
    ``value*100 if value<=1`` normalization, which would corrupt a genuine sub-1% reading here; the
    endpoint and the stream event share the field name ``utilization`` but not its scale."""
    if not isinstance(window, dict):
        return None
    used = window.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not 0 <= used <= 100:
        return None
    resets_at = epoch_seconds(window.get("resets_at") or window.get("resetsAt"))
    if resets_at is None:
        return None
    return float(used), resets_at


def _retry_after_seconds(value) -> int:
    """A bounded, actionable throttle wait parsed from a ``Retry-After`` header. The header may be
    absent or a delta-seconds integer; anything unparseable or out of range collapses to the
    bounded default so the logged wait is always a finite, sane number of seconds."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return _MAX_RETRY_AFTER_SECONDS
    if seconds < 0:
        return _MAX_RETRY_AFTER_SECONDS
    return min(seconds, _MAX_RETRY_AFTER_SECONDS)


def _fetch_windows(token: str, log=None) -> dict[str, tuple[float, int]] | None:
    """GET the OAuth usage endpoint once and return ``{window: (used_percent, resets_at)}`` for
    every window it reports in a trustworthy shape, or ``None`` on a transport/parse failure.

    Both the five-hour and the seven-day window come from this single response (#315). A window
    that is missing or malformed is simply absent from the returned mapping — the caller then
    leaves that window's prior fact untouched (a partial response never erases a still-good fact),
    while ``None`` (the whole request failed) leaves *both* untouched.

    A throttle (HTTP 429) still fails closed to ``None``, but is surfaced through ``log`` with its
    bounded ``Retry-After`` (#322) so a wedged pool's recovery is observable in daemon logs instead
    of a silent ``None``. The reset-aware roll-forward, not this poll, is what unwedges the pool."""
    request = urllib.request.Request(
        _USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        if error.code == 429 and log is not None:
            wait = _retry_after_seconds(error.headers.get("Retry-After"))
            log(f"claude quota poll throttled (HTTP 429); retry after ~{wait}s")
        return None
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    readings = {}
    for window in (FIVE_HOUR, SEVEN_DAY):
        reading = _window_reading(payload.get(window))
        if reading is not None:
            readings[window] = reading
    return readings


def _is_fresh(store_path: Path | str, pool: str, window: str, now: float, ttl: int) -> bool:
    fact = read_quota(store_path, pool, window)
    return fact is not None and now - fact.observed_at < ttl


def refresh_claude_quota(store_path: Path | str, *, now: float | None = None,
                         ttl: int = POLL_TTL_SECONDS, log=None):
    """Ensure the Claude pool has fresh provider facts for both windows, polling the OAuth usage
    endpoint when either persisted window is missing or older than ``ttl``. Records each window
    it reads independently. Returns the recorded five-hour
    :class:`~agentflow.coordinator.quota.QuotaFact` (the dispatch authority the balancer sizes
    headroom from), or ``None`` when no fresh five-hour reading could be obtained (each prior
    fact, if any, is left untouched). Never raises — a poll failure must not break a dispatch
    pass.

    ``log`` (if given) receives a diagnostic line when the endpoint throttles the poll (#322), so a
    429 lands its bounded ``Retry-After`` in daemon logs instead of a silent ``None``."""
    now = time.time() if now is None else now
    if (_is_fresh(store_path, "claude", FIVE_HOUR, now, ttl)
            and _is_fresh(store_path, "claude", SEVEN_DAY, now, ttl)):
        return read_quota(store_path, "claude", FIVE_HOUR)
    token = _access_token()
    if token is None:
        return None
    windows = _fetch_windows(token, log)
    if not windows:
        return None
    five_hour_fact = None
    for window, (used_percent, resets_at) in windows.items():
        fact = build_fact("claude", used_percent, resets_at, int(now),
                          f"oauth:{window}", window=window)
        if fact is None:
            continue
        record_quota(store_path, fact)
        if window == FIVE_HOUR:
            five_hour_fact = fact
    return five_hour_fact
