"""Local provider facts used by the unattended capacity gate."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterator

from agentflow.codex_transcripts import (
    codex_sessions_root, latest_rate_limits, rollout_paths, where_did_session_run)


FIVE_HOURS = 5 * 60 * 60


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _records(root: Path) -> Iterator[dict]:
    for path in root.rglob("*.jsonl"):
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
            continue


def _codex_limits() -> dict:
    limits = latest_rate_limits(codex_sessions_root())
    if limits is None:
        raise ValueError("no Codex rate-limit event found")
    windows = [
        {"used_percent": window.used_percent, "window_minutes": window.window_minutes,
         "resets_at": window.resets_at}
        for window in limits.windows
    ]
    if not windows:
        raise ValueError("latest Codex rate-limit event has no windows")
    return {"observed_at": limits.observed_at, "windows": windows}


def _number(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _claude_events() -> list[tuple[float, float]]:
    events = []
    for record in _records(Path.home() / ".claude" / "projects"):
        if record.get("type") != "assistant":
            continue
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        timestamp = _timestamp(record.get("timestamp"))
        if timestamp is None or not isinstance(usage, dict):
            continue
        weight = (
            _number(usage.get("input_tokens"))
            + 1.25 * _number(usage.get("cache_creation_input_tokens"))
            + 5 * _number(usage.get("output_tokens"))
        )
        if weight:
            events.append((timestamp, weight))
    return sorted(events)


def _state_path() -> Path:
    root = Path(os.environ.get("AGENTFLOW_STATE", "~/.agentflow")).expanduser()
    return root / "capacity.json"


def _calibrate_claude() -> float:
    events = _claude_events()
    if not events:
        raise ValueError("no Claude transcript usage found")
    peak = 0.0
    total = 0.0
    start = 0
    for end, (timestamp, weight) in enumerate(events):
        total += weight
        while timestamp - events[start][0] > FIVE_HOURS:
            total -= events[start][1]
            start += 1
        peak = max(peak, total)
    state = _state_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"claude_peak_5h": round(peak)}, indent=2) + "\n")
    return peak


def _claude_usage() -> float:
    try:
        state = json.loads(_state_path().read_text())
        peak = float(state["claude_peak_5h"])
    except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError(
            "Claude capacity is not calibrated "
            "(run: agentflow capacity calibrate)"
        ) from None
    if peak <= 0:
        raise ValueError("Claude capacity calibration is invalid")
    cutoff = time.time() - FIVE_HOURS
    spent = sum(weight for timestamp, weight in _claude_events() if timestamp >= cutoff)
    return 100 * spent / peak


def _recent_claude_activity() -> bool:
    minutes = float(os.environ.get("TRIAGE_ACTIVE_WINDOW_MIN", "10"))
    cutoff = time.time() - minutes * 60
    root = Path.home() / ".claude" / "projects"
    for path in root.rglob("*.jsonl"):
        if "agentflow-worktrees" in path.parent.name:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                return True
        except OSError:
            continue
    return False


def _recent_codex_activity() -> bool:
    minutes = float(os.environ.get("TRIAGE_ACTIVE_WINDOW_MIN", "10"))
    cutoff = time.time() - minutes * 60
    root = codex_sessions_root()
    for path in rollout_paths(root):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        cwd = where_did_session_run(path)
        if cwd is not None and "/.agentflow/worktrees/" in cwd:
            continue
        return True
    return False


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentflow-capacity-helper")
    parser.add_argument("mode", choices=("calibrate", "check", "limits"))
    args = parser.parse_args(argv)
    try:
        if args.mode == "limits":
            if os.environ.get("TRIAGE_AGENT") != "codex":
                parser.error("limits is available only with TRIAGE_AGENT=codex")
            print(json.dumps(_codex_limits(), sort_keys=True))
        elif args.mode == "calibrate":
            if os.environ.get("TRIAGE_AGENT") != "claude":
                parser.error("calibrate is available only with TRIAGE_AGENT=claude")
            peak = _calibrate_claude()
            print(f"calibrated: Claude five-hour peak = {peak:.0f}")
        else:
            agent = os.environ.get("TRIAGE_AGENT")
            if agent == "claude":
                if (
                    not os.environ.get("TRIAGE_SKIP_ACTIVITY")
                    and _recent_claude_activity()
                ):
                    print("blocked: a Claude session was recently active")
                    sys.exit(1)
                usage = _claude_usage()
                print(
                    f"clear: trailing-5h spend at {usage:.0f}% of calibrated peak"
                )
            elif agent == "codex":
                if (
                    not os.environ.get("TRIAGE_SKIP_ACTIVITY")
                    and _recent_codex_activity()
                ):
                    print("blocked: a Codex session was recently active")
                    sys.exit(1)
                _codex_limits()
                print("clear: Codex limit facts available")
            else:
                parser.error("check requires TRIAGE_AGENT=claude or codex")
    except ValueError as exc:
        if args.mode == "check":
            print(f"blocked: {exc}")
            sys.exit(1)
        parser.error(str(exc))


if __name__ == "__main__":
    main()
