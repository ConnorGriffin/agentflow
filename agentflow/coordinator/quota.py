"""Provider-authored quota facts — the Claude dispatch authority (issues #305, #315).

The balancer used to size Claude headroom from a *calibrated trailing-five-hour transcript
proxy*: a rough weighted-token sum over recent transcripts divided by a locally calibrated
peak. That proxy is reconstructed from usage events already written to disk, so it cannot
reserve demand for sessions just admitted and it keeps reporting a stale percentage after the
real provider window has already reset (docs/research/historical-session-demand.md). It is a
diagnostic, never a correctness boundary.

This module persists the *provider's own* quota facts instead: the utilization and reset time
the provider reports for each window. Claude has two independent windows the fleet gates on — a
five-hour window (short-term headroom) and a seven-day window (the paced weekly allowance, #315)
— and each is a separate durable fact: it carries its own utilization, reset time, observation
time, and provenance. One fact per ``(pool, window)`` is written durably beside the records
database, so the daemon makes the same admission decision after a restart without waiting for a
fresh session, and **updating one window never erases the other**.

Reads are validated and fail closed: a missing, malformed, or window-inconsistent fact returns
``None`` — never a fabricated zero-usage reading. :func:`effective_usage` is the one place the
reset-aware reading lives: once the observed window's ``resets_at`` has passed the fact reports
no usage (the window rolled over), so an expired capacity pause clears on the next daemon cycle
on its own.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from uuid import uuid4

# The short window both providers gate dispatch on is five hours; Claude's weekly allowance is
# paced across a seven-day window (#315). A fact's ``window`` names which one it describes, and
# its length is how far back the window's own start sits from ``resets_at``.
FIVE_HOUR_SECONDS = 5 * 60 * 60
SEVEN_DAY_SECONDS = 7 * 24 * 60 * 60
FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
_WINDOW_SECONDS = {FIVE_HOUR: FIVE_HOUR_SECONDS, SEVEN_DAY: SEVEN_DAY_SECONDS}


def epoch_seconds(value) -> int | None:
    """Coerce a provider ``resets_at`` (epoch seconds or an ISO-8601 string) into epoch seconds,
    or ``None`` for any other shape. Shared by both quota producers — the stream extractor
    (`providers`) and the independent poll (`quota_poll`) — which each read a provider reset time."""
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

# A little clock skew tolerance so a fact observed at the exact window edge is not judged
# temporally impossible by a sub-second scheduling difference.
_SKEW_SECONDS = 120


@dataclass(frozen=True)
class QuotaFact:
    """One pool+window's latest provider-authored quota reading.

    ``used_percent`` is the provider's utilization for this window (0..100). ``resets_at`` is
    the epoch the window rolls over; ``observed_at`` is when the fact was seen; ``provenance``
    names the surface it came from (e.g. ``oauth:five_hour``) so a diagnostic reader can tell a
    provider fact from a proxy. ``window`` names which independent window this fact describes
    (``five_hour`` by default; ``seven_day`` for the paced weekly allowance, #315) — its length
    is derived from ``window`` so an unknown window fails validation closed.
    """

    pool: str
    used_percent: float
    resets_at: int
    observed_at: int
    provenance: str
    window: str = FIVE_HOUR


def quota_dir(store_path: Path | str) -> Path:
    """Where per-pool quota facts live, beside the records database and telemetry."""
    return Path(store_path).parent / "quota"


def _fact_path(store_path: Path | str, pool: str, window: str) -> Path:
    return quota_dir(store_path) / f"{pool}-{window}.json"


def _finite_number(value) -> bool:
    return (not isinstance(value, bool)
            and isinstance(value, (int, float)) and math.isfinite(value))


def _valid(fact: QuotaFact) -> bool:
    """Whether a decoded fact is internally consistent enough to trust (fail closed)."""
    if not isinstance(fact.pool, str) or not fact.pool:
        return False
    if not isinstance(fact.provenance, str) or not fact.provenance:
        return False
    seconds = _WINDOW_SECONDS.get(fact.window) if isinstance(fact.window, str) else None
    if seconds is None:  # an unknown window has no defined length — cannot be trusted
        return False
    if not (_finite_number(fact.used_percent) and 0 <= fact.used_percent <= 100):
        return False
    if not (_finite_number(fact.resets_at) and fact.resets_at > 0):
        return False
    if not (_finite_number(fact.observed_at) and fact.observed_at > 0):
        return False
    # The observation must sit inside the window it describes: a fact observed before its own
    # window opened, or after it has already reset, is inconsistent and cannot be trusted. Each
    # window uses its own length, so a five-hour and a seven-day fact are validated independently.
    window_start = fact.resets_at - seconds
    if fact.observed_at < window_start - _SKEW_SECONDS:
        return False
    if fact.observed_at > fact.resets_at + _SKEW_SECONDS:
        return False
    return True


def build_fact(pool: str, used_percent, resets_at, observed_at, provenance,
               window: str = FIVE_HOUR) -> QuotaFact | None:
    """Construct a validated :class:`QuotaFact`, or ``None`` when the inputs are not a
    trustworthy provider reading. Extractors use this so a malformed provider field never
    becomes a persisted fact."""
    if not (_finite_number(used_percent) and _finite_number(resets_at)
            and _finite_number(observed_at)):
        return None
    fact = QuotaFact(pool=pool, used_percent=float(used_percent), resets_at=int(resets_at),
                     observed_at=int(observed_at), provenance=provenance, window=window)
    return fact if _valid(fact) else None


def record_quota(store_path: Path | str, fact: QuotaFact) -> None:
    """Persist one pool+window's latest quota fact atomically. The write replaces only *this*
    window's fact in place, so the freshest provider reading always wins and a sibling window
    (e.g. the seven-day fact next to the five-hour one) is never touched. A quota write must
    never break a dispatch cycle, so an unwritable directory is swallowed (the balancer then
    fails closed on the missing fact rather than the daemon crashing)."""
    if not _valid(fact):
        return
    path = _fact_path(store_path, fact.pool, fact.window)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        with tmp.open("w") as stream:
            json.dump(asdict(fact), stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except (OSError, NameError):
            pass


_FACT_FIELDS = {f.name for f in fields(QuotaFact)}


def read_quota(store_path: Path | str, pool: str,
               window: str = FIVE_HOUR) -> QuotaFact | None:
    """The pool+window's latest persisted quota fact, or ``None`` when it is missing, unreadable,
    or fails validation. Fail-closed by contract: a bad fact is *never* coerced into a zero-usage
    reading — the caller must treat ``None`` as "no trustworthy headroom fact", not "empty"."""
    path = _fact_path(store_path, pool, window)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    picked = {k: data.get(k) for k in _FACT_FIELDS}
    try:
        fact = QuotaFact(**picked)
    except TypeError:
        return None
    return fact if _valid(fact) else None


def effective_usage(fact: QuotaFact, now: float) -> float | None:
    """The five-hour utilization to gate on right now, or ``None`` when the fact cannot be
    trusted as a current reading (fail closed).

    Reset-aware: once ``now`` has reached the observed window's ``resets_at`` the provider window
    has rolled over, so the reported utilization no longer applies and the pool reads as fresh
    (``0.0``). A fact whose window opens more than its own length in the future is temporally
    impossible and fails closed; so does one observed outside the window it claims."""
    if not _valid(fact):
        return None
    if fact.resets_at > now + _WINDOW_SECONDS[fact.window] + _SKEW_SECONDS:
        return None
    if now >= fact.resets_at:
        return 0.0
    return fact.used_percent
