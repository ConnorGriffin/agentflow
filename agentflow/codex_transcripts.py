"""Facts read from Codex rollout transcripts.

Codex owns this JSONL format.  Callers ask this module where a rollout ran or
what it spent; they do not inspect transcript records or field names themselves.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


_TOKEN_USAGE_FIELDS = {
    "input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens"}


@dataclass(frozen=True)
class CodexSpend:
    """The last cumulative token total Codex recorded for one rollout."""

    model: str
    input_tokens: int | None
    cached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    unrecognized: tuple[str, ...] = ()


@dataclass(frozen=True)
class CodexRateLimitWindow:
    """One provider-reported remaining-capacity window."""

    used_percent: object
    window_minutes: object
    resets_at: object


@dataclass(frozen=True)
class CodexRateLimits:
    """The newest provider headroom fact Codex recorded."""

    observed_at: float
    windows: tuple[CodexRateLimitWindow, ...]


def codex_sessions_root() -> Path:
    """Return the fixed root where Codex stores rollout transcripts."""
    return Path.home() / ".codex" / "sessions"


def _records(path: Path) -> Iterator[dict]:
    try:
        with path.open(errors="replace") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _token(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def was_started_by_codex_exec(path: Path) -> bool:
    """Whether the rollout's required first metadata record names ``codex_exec``."""
    try:
        with path.open(errors="replace") as stream:
            record = json.loads(stream.readline())
    except (OSError, json.JSONDecodeError):
        return False
    payload = record.get("payload") if isinstance(record, dict) else None
    return isinstance(payload, dict) and payload.get("originator") == "codex_exec"


def rollout_paths(root: Path) -> Iterator[Path]:
    """Yield Codex rollout files below their sessions root."""
    try:
        yield from root.rglob("*.jsonl")
    except OSError:
        return


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def where_did_session_run(path: Path) -> str | None:
    """Return the rollout's recorded working directory, if it supplied one."""
    for record in _records(path):
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and isinstance(payload.get("cwd"), str):
            return payload["cwd"]
    return None


def session_identifier(path: Path) -> str | None:
    """Return Codex's rollout identifier when the transcript supplied one."""
    for record in _records(path):
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        for key in ("id", "session_id", "thread_id"):
            if isinstance(payload.get(key), str):
                return payload[key]
    return None


def what_did_session_spend(path: Path) -> CodexSpend | None:
    """Return a rollout's last cumulative token total, never treating absence as zero."""
    totals: dict | None = None
    model: str | None = None
    session_model: str | None = None
    for record in _records(path):
        payload = record.get("payload")
        if record.get("type") == "event_msg" and isinstance(payload, dict) \
                and payload.get("type") == "token_count":
            info = payload.get("info")
            candidate = info.get("total_token_usage") if isinstance(info, dict) else None
            if isinstance(candidate, dict):
                totals = candidate
        elif record.get("type") == "turn_context" and isinstance(payload, dict) \
                and isinstance(payload.get("model"), str):
            model = payload["model"]
        elif record.get("type") == "session_meta" and isinstance(payload, dict) \
                and isinstance(payload.get("model"), str) and session_model is None:
            session_model = payload["model"]
    if totals is None:
        return None
    return CodexSpend(
        model=model or session_model or "codex",
        input_tokens=_token(totals.get("input_tokens")),
        cached_input_tokens=_token(totals.get("cached_input_tokens")),
        output_tokens=_token(totals.get("output_tokens")),
        reasoning_output_tokens=_token(totals.get("reasoning_output_tokens")),
        unrecognized=tuple(sorted(key for key in totals if key not in _TOKEN_USAGE_FIELDS)),
    )


def latest_rate_limits(root: Path) -> CodexRateLimits | None:
    """Return Codex's newest usable provider-headroom observation under ``root``."""
    latest: tuple[float, dict] | None = None
    for path in rollout_paths(root):
        for record in _records(path):
            if record.get("type") != "event_msg":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("type") != "token_count":
                continue
            observed_at = _timestamp(record.get("timestamp"))
            limits = payload.get("rate_limits")
            if observed_at is not None and isinstance(limits, dict) \
                    and (latest is None or observed_at > latest[0]):
                latest = observed_at, limits
    if latest is None:
        return None
    observed_at, limits = latest
    windows = tuple(
        CodexRateLimitWindow(
            used_percent=window.get("used_percent"),
            window_minutes=window.get("window_minutes"),
            resets_at=window.get("resets_at"),
        )
        for name in ("primary", "secondary")
        if isinstance((window := limits.get(name)), dict)
    )
    return CodexRateLimits(observed_at, windows)
