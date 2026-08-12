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
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

_EVENT_ARTIFACT_BYTES = 64 * 1024 * 1024
_EVENT_RECORD_BYTES = 16 * 1024 * 1024
_EVENT_RECORDS = 100_000
_PARTIAL_OUTPUT_BYTES = 16 * 1024 * 1024
_RESULT_ARTIFACT_BYTES = 4 * 1024
_EXIT_ARTIFACT_BYTES = 64


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


def _read_regular_bytes(path: Path, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes from one regular durable artifact, else fail closed."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                return None
            chunks: list[bytes] = []
            remaining = limit + 1
            while remaining:
                chunk = os.read(fd, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            return raw if len(raw) <= limit else None
        finally:
            os.close(fd)
    except (OSError, ValueError):
        return None


class _DuplicateResultKey(ValueError):
    pass


def _unique_result_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateResultKey(key)
        result[key] = value
    return result


def _result_fields(result: object) -> tuple[int | None, int | None, bool] | None:
    """Return terminal fields only for the exact shape :func:`write_result` publishes."""
    if not isinstance(result, dict) or set(result) != {"exit_status", "signal", "timed_out"}:
        return None
    exit_status, signal, timed_out = (
        result["exit_status"], result["signal"], result["timed_out"])
    if not ((exit_status is None or type(exit_status) is int)
            and (signal is None or type(signal) is int)
            and type(timed_out) is bool):
        return None
    return exit_status, signal, timed_out


def read_session(store_path: Path | str, token: str | None) -> CapturedSession:
    """Reconstruct one attempt's durable session facts. A missing file means the attempt left
    nothing to read (it never ran, or its artifacts were pruned) — an empty capture, never an
    error, so observation stays fail-open toward `unknown` rather than crashing recovery."""
    if not token:
        return CapturedSession()
    events: list[dict] = []
    partial: list[str] = []
    partial_bytes = 0
    raw = _read_regular_bytes(events_path(store_path, token), _EVENT_ARTIFACT_BYTES)
    lines = raw.splitlines() if raw is not None else []
    if len(lines) > _EVENT_RECORDS or any(len(line) > _EVENT_RECORD_BYTES for line in lines):
        lines = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        # The provider owns this line. Deep JSON can raise RecursionError rather than the
        # documented JSONDecodeError; both are malformed durable output, not a reader failure.
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
            # Provider prose or malformed/truncated bytes are preserved, never interpreted.
            if partial_bytes + len(line) <= _PARTIAL_OUTPUT_BYTES:
                partial.append(line.decode("utf-8", errors="replace"))
                partial_bytes += len(line)
            continue
        events.append(parsed if isinstance(parsed, dict) else {"value": parsed})
    exit_status: int | None = None
    signal: int | None = None
    timed_out = False
    has_end_fact = False
    result_raw = _read_regular_bytes(
        result_path(store_path, token), _RESULT_ARTIFACT_BYTES)
    try:
        # This file is durable cross-process state and may be truncated or human-edited. Decode
        # it under the same narrow trust boundary as events, then require its canonical object.
        result = json.loads(result_raw, object_pairs_hook=_unique_result_object) \
            if result_raw is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, _DuplicateResultKey):
        result = None
    fields = _result_fields(result)
    if fields is not None:
        exit_status, signal, timed_out = fields
        has_end_fact = True
    else:
        # Compatibility with artifacts written before the full supervisor result existed.
        try:
            legacy = _read_regular_bytes(exit_path(store_path, token), _EXIT_ARTIFACT_BYTES)
            exit_status = int(legacy.decode("utf-8").strip()) if legacy is not None else None
            if exit_status is None:
                raise ValueError("missing legacy exit")
            has_end_fact = True
        except (UnicodeDecodeError, ValueError):
            exit_status = None
    return CapturedSession(tuple(events), exit_status, signal, timed_out,
                           "\n".join(partial), has_end_fact)
