"""Independent five-hour quota poll — the Claude dispatch authority's *producer* (issue #309).

`agentflow.coordinator.quota` owns how a five-hour fact is validated, stored, and read; this
module owns getting a fresh one *without a session having run*. The provider's headless
``rate_limit_event`` only carries whichever window is currently in warning (often ``seven_day``),
so it cannot be relied on to ever report ``five_hour`` — a fleet that gates Claude on that event
alone never seeds a cold store and never notices a window reset while parked (both observed in
#307). This poll closes that hole: each dispatch pass it reads Anthropic's own OAuth usage
endpoint, which reports ``five_hour`` unconditionally, and records it as the pool's fact.

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
from datetime import datetime
from pathlib import Path

from agentflow.coordinator.quota import build_fact, read_quota, record_quota

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


def _epoch(value) -> int | None:
    """Coerce the endpoint's ``resets_at`` (ISO-8601 or epoch seconds) to epoch seconds, or
    ``None`` for any other shape."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _fetch_five_hour(token: str) -> tuple[float, int] | None:
    """GET the OAuth usage endpoint and return ``(used_percent, resets_at)`` for the five-hour
    window, or ``None`` on any transport/shape failure. The endpoint reports ``utilization`` as a
    0..100 percent (unlike the stream event's 0..1 fraction), so it is used unscaled."""
    request = urllib.request.Request(
        _USAGE_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        return None
    five_hour = payload.get("five_hour") if isinstance(payload, dict) else None
    if not isinstance(five_hour, dict):
        return None
    used = five_hour.get("utilization")
    if isinstance(used, bool) or not isinstance(used, (int, float)):
        return None
    resets_at = _epoch(five_hour.get("resets_at") or five_hour.get("resetsAt"))
    if resets_at is None:
        return None
    return float(used), resets_at


def _is_fresh(store_path: Path | str, pool: str, now: float, ttl: int) -> bool:
    fact = read_quota(store_path, pool)
    return fact is not None and now - fact.observed_at < ttl


def refresh_claude_quota(store_path: Path | str, *, now: float | None = None,
                         ttl: int = POLL_TTL_SECONDS):
    """Ensure the Claude pool has a fresh provider five-hour fact, polling the OAuth usage endpoint
    when the persisted one is missing or older than ``ttl``. Returns the recorded
    :class:`~agentflow.coordinator.quota.QuotaFact`, or ``None`` when nothing fresh could be
    obtained (the prior fact, if any, is left untouched). Never raises — a poll failure must not
    break a dispatch pass."""
    now = time.time() if now is None else now
    if _is_fresh(store_path, "claude", now, ttl):
        return read_quota(store_path, "claude")
    token = _access_token()
    if token is None:
        return None
    fetched = _fetch_five_hour(token)
    if fetched is None:
        return None
    used_percent, resets_at = fetched
    fact = build_fact("claude", used_percent, resets_at, int(now), "oauth:five_hour")
    if fact is None:
        return None
    record_quota(store_path, fact)
    return fact
