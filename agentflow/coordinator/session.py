"""Durable per-attempt provider session artifacts (ADR 0030).

A launched provider is exec-replaced by a tiny shell that redirects the provider's structured
stream to ``<token>.events`` and writes the provider's own exit status to ``<token>.exit``
under the store's ``sessions`` directory *before* the family ends. That is what makes the
full observation set durable across a daemon crash: the family runs and finishes writing its
artifacts even if the coordinator dies, and a fresh coordinator's provider adapter
reconstructs the observation by reading them — the launcher records that a provider ran, and
these files record *what it did*. Keyed by the reservation's launch token, so a recovered
attempt reads exactly the artifacts its own start produced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


def session_dir(store_path: Path | str) -> Path:
    """Where a store's per-attempt provider artifacts live, beside the records database."""
    return Path(store_path).parent / "sessions"


def events_path(store_path: Path | str, token: str) -> Path:
    return session_dir(store_path) / f"{token}.events"


def exit_path(store_path: Path | str, token: str) -> Path:
    return session_dir(store_path) / f"{token}.exit"


@dataclass(frozen=True)
class CapturedSession:
    """The durable facts one launched provider left behind: its structured events (one JSON
    object per line, unparsable lines preserved as raw partial output) and its exit status."""

    events: tuple[dict, ...] = ()
    exit_status: int | None = None
    partial_output: str = ""


def read_session(store_path: Path | str, token: str | None) -> CapturedSession:
    """Reconstruct one attempt's durable session facts. A missing file means the attempt left
    nothing to read (it never ran, or its artifacts were pruned) — an empty capture, never an
    error, so observation stays fail-open toward `unknown` rather than crashing recovery."""
    if not token:
        return CapturedSession()
    events: list[dict] = []
    partial: list[str] = []
    try:
        raw = events_path(store_path, token).read_text()
    except OSError:
        raw = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            partial.append(line)  # provider prose or a truncated tail — preserved, not parsed
            continue
        events.append(parsed if isinstance(parsed, dict) else {"value": parsed})
    exit_status: int | None = None
    try:
        exit_status = int(exit_path(store_path, token).read_text().strip())
    except (OSError, ValueError):
        exit_status = None
    return CapturedSession(tuple(events), exit_status, "\n".join(partial))
