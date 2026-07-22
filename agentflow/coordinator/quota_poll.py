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


def _fetch_windows(token: str) -> dict[str, tuple[float, int]] | None:
    """GET the OAuth usage endpoint once and return ``{window: (used_percent, resets_at)}`` for
    every window it reports in a trustworthy shape, or ``None`` on a transport/parse failure.

    Both the five-hour and the seven-day window come from this single response (#315). A window
    that is missing or malformed is simply absent from the returned mapping — the caller then
    leaves that window's prior fact untouched (a partial response never erases a still-good fact),
    while ``None`` (the whole request failed) leaves *both* untouched."""
    request = urllib.request.Request(
        _USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
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
                         ttl: int = POLL_TTL_SECONDS):
    """Ensure the Claude pool has fresh provider facts for both windows, polling the OAuth usage
    endpoint when either persisted window is missing or older than ``ttl``. Records each window
    it reads independently. Returns the recorded five-hour
    :class:`~agentflow.coordinator.quota.QuotaFact` (the dispatch authority the balancer sizes
    headroom from), or ``None`` when no fresh five-hour reading could be obtained (each prior
    fact, if any, is left untouched). Never raises — a poll failure must not break a dispatch
    pass."""
    now = time.time() if now is None else now
    if (_is_fresh(store_path, "claude", FIVE_HOUR, now, ttl)
            and _is_fresh(store_path, "claude", SEVEN_DAY, now, ttl)):
        return read_quota(store_path, "claude", FIVE_HOUR)
    token = _access_token()
    if token is None:
        return None
    windows = _fetch_windows(token)
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
