"""Durable per-attempt provider session artifacts (ADR 0030).

A launched provider runs beneath a detached supervisor that streams structured output to
``<token>.events`` and atomically publishes exit, signal, and timeout facts to
``<token>.result`` under the store's ``sessions`` directory. That is what makes the full
observation set durable across a daemon crash: the family runs and finishes writing its
artifacts even if the coordinator dies, and a fresh coordinator's provider adapter
reconstructs the observation by reading them. Keyed by the reservation's launch token, so a
recovered attempt reads exactly the artifacts its own start produced.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


def session_dir(store_path: Path | str) -> Path:
    """Where a store's per-attempt provider artifacts live, beside the records database."""
    return Path(store_path).parent / "sessions"


def events_path(store_path: Path | str, token: str) -> Path:
    return session_dir(store_path) / f"{token}.events"


def exit_path(store_path: Path | str, token: str) -> Path:
    return session_dir(store_path) / f"{token}.exit"


def result_path(store_path: Path | str, token: str) -> Path:
    return session_dir(store_path) / f"{token}.result"


@dataclass(frozen=True)
class CapturedSession:
    """The durable facts one launched provider left behind: its structured events (one JSON
    object per line, unparsable lines preserved as raw partial output) and its terminal
    exit, signal, and supervisor-timeout facts."""

    events: tuple[dict, ...] = ()
    exit_status: int | None = None
    signal: int | None = None
    timed_out: bool = False
    partial_output: str = ""
    has_end_fact: bool = False   # a supervisor end fact (`.result`/`.exit`) existed for this token —
                                 # the durable proof the provider family ended on its own, not with
                                 # the daemon. Absence is what distinguishes a restart-caused death.


def write_result(store_path: Path | str, token: str, *, exit_status: int | None,
                 signal: int | None, timed_out: bool) -> None:
    """Atomically publish the supervisor facts after the provider family has ended."""
    path = result_path(store_path, token)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with tmp.open("w") as stream:
            json.dump({"exit_status": exit_status, "signal": signal,
                       "timed_out": timed_out}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def read_session(store_path: Path | str, token: str | None) -> CapturedSession:
    """Reconstruct one attempt's durable session facts. A missing file means the attempt left
    nothing to read (it never ran, or its artifacts were pruned) — an empty capture, never an
    error, so observation stays fail-open toward `unknown` rather than crashing recovery."""
    if not token:
        return CapturedSession()
    events: list[dict] = []
    partial: list[str] = []
    try:
        raw = events_path(store_path, token).read_bytes()
    except OSError:
        raw = b""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        # The provider owns this line. Deep JSON can raise RecursionError rather than the
        # documented JSONDecodeError; both are malformed durable output, not a reader failure.
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            # Provider prose or malformed/truncated bytes are preserved, never interpreted.
            partial.append(line.decode("utf-8", errors="replace"))
            continue
        events.append(parsed if isinstance(parsed, dict) else {"value": parsed})
    exit_status: int | None = None
    signal: int | None = None
    timed_out = False
    has_end_fact = False
    try:
        result = json.loads(result_path(store_path, token).read_text())
        exit_status = result.get("exit_status")
        signal = result.get("signal")
        timed_out = result.get("timed_out") is True
        has_end_fact = True
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # Compatibility with artifacts written before the full supervisor result existed.
        try:
            exit_status = int(exit_path(store_path, token).read_text().strip())
            has_end_fact = True
        except (OSError, ValueError):
            exit_status = None
    return CapturedSession(tuple(events), exit_status, signal, timed_out,
                           "\n".join(partial), has_end_fact)
