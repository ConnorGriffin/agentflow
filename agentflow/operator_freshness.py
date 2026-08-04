"""How old the operator briefing is allowed to claim it is — the one rule, on both sides of
the state file (ADR 0036, amended by ADR 376).

Two entry points, one rule. :func:`component` is what the daemon stamps onto a repository's
map read as it publishes it. :func:`serve` is what the console server puts the published body
through before answering a browser: it re-ages every component against the reader's own clock,
**downgrade-only**, and stamps the briefing's display state, its label prefix, its banner
sentence, and the machine-readable timestamp the browser words an age from.

Why aging happens at read time at all: publication stamps freshness once and then stops. If the
daemon dies, that body keeps being served, and without this it keeps claiming a verified
decision frontier forever. Why downgrade-only: a *failed* read preserves the last verified
``fresh_at``, so timestamp age alone would promote a known failure back to fresh.

Stdlib only, and no I/O: the web server must not inherit :mod:`agentflow.operator_projection`'s
GitHub-facing imports.
"""

from __future__ import annotations

from datetime import datetime

SCHEMA_VERSION = 2

FRESH = "fresh"
STALE = "stale"
UNAVAILABLE = "unavailable"

# The daemon stamps its own heartbeat into the body under this key, and the reader takes the
# window from there rather than from anything in its own process. The daemon and the console
# are separate long-lived launch agents installed together from one runtime checkout
# (`macos_service.install`); each loads its code at start, and ADR 0051 defers the daemon's
# restart while fleet work is live — so for a while the two can be running different code.
# A window inferred server-side would then be a number the published body never agreed to.
HEARTBEAT_FIELD = "heartbeat_seconds"

# What the console shows instead of a briefing when the body cannot be trusted: the v2 fields
# replaced wholesale, so no map, frontier or queue survives, while every v1 field the Inbox,
# Live, Fleet and History tabs read passes through untouched.
UNAVAILABLE_V2 = {
    "schema_version": SCHEMA_VERSION,
    "generated_at": None,
    "repositories": [],
    "fleet": {"recent_landed": []},
    "attention": {"rows": [], "total": 0},
}

# The masthead's four label prefixes. The browser appends the age itself, from `verified_at`,
# with the relative-time vocabulary it already ships (`just now`, `4m ago`, `2h ago`, `2d ago`);
# there is deliberately no age formatter here to drift from it.
_VERIFIED = "Verified"
_LAST_VERIFIED = "Last verified"
_PARTIAL = "Partial projection · verified"
_UNAVAILABLE_LABEL = "Projection unavailable"

_STALE_MESSAGE = "This projection is stale. Open GitHub before acting on a frontier."
_PARTIAL_MESSAGE = "Dependency data is incomplete. A decision frontier is not verified."
_NEVER_VERIFIED_MESSAGE = (
    "One or more repositories have never published a verified Decision Map read. "
    "Open GitHub for authoritative state.")
_NO_PROJECTION_MESSAGE = (
    "No daemon-published projection yet. Open GitHub for authoritative state.")
_UNREADABLE_MESSAGE = (
    "The operator projection could not be read. Open GitHub for authoritative state.")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _instant(value) -> datetime | None:
    """One timestamp out of the state file, or None when it cannot be compared to a clock.

    The file is durable state a crash, a hand edit or a partial write can perturb, so every
    field here is untrusted: a missing, non-string, naive or unparseable stamp is infinitely
    old rather than an error, and never an exception that escapes as an HTTP 500."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def component(
    *, previous: dict | None, now: datetime, heartbeat_seconds: int,
    attempted: bool, success: bool, error: str | None,
) -> dict:
    """One repository's maps-component freshness stamp (ADR 0036): ``fresh`` only when the
    latest attempt succeeded and the verified read is no older than two heartbeats; ``stale``
    when the latest attempt failed or that window has passed; ``unavailable`` when no read has
    ever succeeded. Always preserves the last verified ``fresh_at`` — a failure never invents
    one, and it never discards a still-honest earlier success."""
    prev = previous or {}
    if success:
        return {"status": FRESH, "attempted_at": _iso(now), "fresh_at": _iso(now), "error": None}
    fresh_at = prev.get("fresh_at")
    attempted_at = _iso(now) if attempted else prev.get("attempted_at")
    if fresh_at is None:
        return {"status": UNAVAILABLE, "attempted_at": attempted_at, "fresh_at": None,
                "error": error}
    if attempted:  # the latest attempt ran this cycle and failed
        return {"status": STALE, "attempted_at": attempted_at, "fresh_at": fresh_at,
                "error": error}
    parsed = _instant(fresh_at)
    age = (now - parsed).total_seconds() if parsed else float("inf")
    if age <= 2 * heartbeat_seconds:  # not attempted this cycle, but still within the window
        return {"status": FRESH, "attempted_at": attempted_at, "fresh_at": fresh_at,
                "error": None}
    return {"status": STALE, "attempted_at": attempted_at, "fresh_at": fresh_at, "error": error}


def carry_forward(body: dict | None) -> dict | None:
    """The previous published body the daemon is allowed to preserve last-verified map data
    from. Narrower than the serving guard on purpose: a body whose schema version is absent or
    unrecognised is not this projection and carries nothing, but a recognised body that predates
    the stamped heartbeat window still carries — otherwise the first publish after an upgrade
    would throw away every repository's last verified read and republish it as never-loaded."""
    if not isinstance(body, dict) or body.get("schema_version") != SCHEMA_VERSION:
        return None
    return body


def _window(body: dict) -> float | None:
    """The read-time freshness window in seconds — two of the heartbeats the *publisher*
    stamped — or None when this body is not a v2 projection that stamped one."""
    if body.get("schema_version") != SCHEMA_VERSION:
        return None
    heartbeat = body.get(HEARTBEAT_FIELD)
    if not isinstance(heartbeat, int) or isinstance(heartbeat, bool) or heartbeat <= 0:
        return None
    return 2.0 * heartbeat


def _aged(github: dict, *, now: datetime, window: float) -> dict:
    """One published component, re-aged against the reader's clock. The published status is a
    ceiling: only ``fresh`` can move, and only downward.

    An unusable ``fresh_at`` is infinitely old rather than absent — there *was* a verified read,
    this reader just cannot date it, so the component goes stale. No ``fresh_at`` at all means
    there was never one, whatever status the body claims alongside it."""
    status = github.get("status")
    if github.get("fresh_at") is None:
        return github if status == UNAVAILABLE else {**github, "status": UNAVAILABLE}
    if status == FRESH:
        verified = _instant(github.get("fresh_at"))
        if verified is not None and (now - verified).total_seconds() <= window:
            return github
        return {**github, "status": STALE}
    if status == STALE:
        return github
    # Published unavailable, or a status this reader does not recognise: nothing here can
    # establish a verified read, so it keeps the state that claims the least.
    return {**github, "status": UNAVAILABLE}


def _stamp(state: str, prefix: str, message: str | None, verified_at: datetime | None) -> dict:
    return {"state": state, "label_prefix": prefix, "message": message,
            "verified_at": None if verified_at is None else _iso(verified_at)}


def _rollup(entries: list[dict], generated_at) -> dict:
    """The whole briefing's display state, from the aged components. ``verified_at`` is the
    *oldest* component that was ever verified — the masthead may not claim an age any newer
    than the least current thing under it — and falls back to the publish stamp only when no
    component carries a usable one."""
    statuses = [entry["github"].get("status") for entry in entries]
    verified = [t for t in (_instant(e["github"].get("fresh_at")) for e in entries)
                if t is not None]
    oldest = min(verified) if verified else None
    if all(status == FRESH for status in statuses):
        return _stamp(FRESH, _VERIFIED, None, oldest or _instant(generated_at))
    if UNAVAILABLE in statuses:
        if oldest is None:
            return _stamp("incomplete", _UNAVAILABLE_LABEL, _NEVER_VERIFIED_MESSAGE, None)
        return _stamp("incomplete", _PARTIAL, _PARTIAL_MESSAGE, oldest)
    return _stamp(STALE, _LAST_VERIFIED, _STALE_MESSAGE, oldest or _instant(generated_at))


def _fail_closed(body: dict, message: str) -> dict:
    return {**body, **UNAVAILABLE_V2,
            "freshness": _stamp("incomplete", _UNAVAILABLE_LABEL, message, None)}


def serve(body: dict | None, *, now: datetime, never_ran: dict) -> dict:
    """The reader's front door: the daemon-published body as the browser should see it *now*.

    Adds nothing the daemon did not already read and removes nothing it did — the only fields
    recomputed are each repository's map-read status, aged downward against ``now``, and the
    briefing's own display state. Never queries anything.

    Fails closed on everything it cannot vouch for: an absent body, a body that is not this
    schema version, a body published before the heartbeat window travelled in it, and a body
    with no repositories at all (the configuration requires at least one, so zero of them is a
    perturbed file, not an empty fleet). Each of those keeps the v1 fields the rest of the
    console reads and replaces the v2 briefing with its unavailable shape."""
    if body is None:
        return _fail_closed(never_ran, _NO_PROJECTION_MESSAGE)
    window = _window(body)
    repositories = body.get("repositories")
    if window is None or not isinstance(repositories, list) or not repositories:
        return _fail_closed(body, _UNREADABLE_MESSAGE)
    aged = []
    for entry in repositories:
        if not isinstance(entry, dict) or not isinstance(entry.get("github"), dict):
            return _fail_closed(body, _UNREADABLE_MESSAGE)
        aged.append({**entry, "github": _aged(entry["github"], now=now, window=window)})
    return {**body, "repositories": aged, "freshness": _rollup(aged, body.get("generated_at"))}
