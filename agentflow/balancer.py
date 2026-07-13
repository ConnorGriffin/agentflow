"""The two-pool headroom balancer (ADR 0006) — pick the builder/reviewer pair.

Both plans are prepaid, so the scarce resource is rate-limit headroom. The builder
goes to the pool with more headroom right now; the reviewer is the *other* tool
(cross-tool independence, ADR 0003, which also spreads load across both budgets).

It reuses the existing per-agent gate `triage-gate.sh` (the reuse map's balancer
source): that adapter reports Claude's calibrated trailing-5h spend and Codex's
reported rate-limit windows. If only one pool has headroom, the builder runs there
and the reviewer is None: the caller must NOT auto-merge (single-tool fallback,
ADR 0003). If neither has headroom, no capacity this cycle.

`pick_pair` is the public dispatch test surface; `choose_pair` and `parse_pct`
remain pure compatibility interfaces.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass

from agentflow.runner import ClaudeRunner, CodexRunner, _WorktreeRunner

_GATE = os.environ.get(
    "AGENTFLOW_TRIAGE_GATE",
    os.path.expanduser("~/Code/ConnorGriffin/dotfiles/scripts/triage-gate.sh"))
_PCT_RE = re.compile(r"at (\d+(?:\.\d+)?)% of (?:peak|limit)")
_SHORT_WINDOW_MIN = 300
_WEEKLY_WINDOW_MIN = 10080
_WEEKLY_UNATTENDED_PCT = 80.0


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: float
    window_minutes: int
    resets_at: float


@dataclass(frozen=True)
class PoolStatus:
    tool: str
    clear: bool
    spent_pct: float   # 0..100; lower = more headroom
    reason: str = ""   # gate's check output when blocked, stripped of "blocked: " prefix
    windows: tuple[RateLimitWindow, ...] | None = ()


def _parse_codex_windows(stdout: str) -> tuple[RateLimitWindow, ...] | None:
    """Parse the gate adapter's structured facts. Unknown shapes fail closed."""
    try:
        raw = json.loads(stdout)
        facts = raw["windows"]
        if not isinstance(facts, list) or not facts:
            return None
        windows = []
        for fact in facts:
            values = (fact["used_percent"], fact["window_minutes"], fact["resets_at"])
            if any(isinstance(value, bool) or not isinstance(value, (int, float))
                   for value in values):
                return None
            used, raw_minutes, resets_at = map(float, values)
            minutes = int(raw_minutes)
            if (raw_minutes != minutes
                    or minutes not in (_SHORT_WINDOW_MIN, _WEEKLY_WINDOW_MIN)
                    or not math.isfinite(used) or not 0 <= used <= 100
                    or not math.isfinite(resets_at) or resets_at <= 0):
                return None
            windows.append(RateLimitWindow(used, minutes, resets_at))
        if len({window.window_minutes for window in windows}) != len(windows):
            return None
        return tuple(sorted(windows, key=lambda window: window.window_minutes))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _codex_pacing(windows: tuple[RateLimitWindow, ...], now: float) -> tuple[bool, str]:
    for window in windows:
        starts_at = window.resets_at - window.window_minutes * 60
        if not starts_at <= now < window.resets_at:
            return False, f"{window.window_minutes}-minute limit facts are stale"
        if window.window_minutes != _WEEKLY_WINDOW_MIN:
            continue
        elapsed = now - starts_at
        released = min(
            _WEEKLY_UNATTENDED_PCT,
            _WEEKLY_UNATTENDED_PCT * elapsed / (window.window_minutes * 60),
        )
        if window.used_percent >= released:
            return False, (
                f"weekly spend at {window.used_percent:g}% exceeds "
                f"{released:.1f}% released for unattended work"
            )
    return True, ""


def _codex_spent_pct(windows: tuple[RateLimitWindow, ...]) -> float:
    short = next((window for window in windows
                  if window.window_minutes == _SHORT_WINDOW_MIN), None)
    return short.used_percent if short else windows[0].used_percent


def _codex_dispatch_status(status: PoolStatus, now: float) -> PoolStatus:
    if status.windows is None:
        return PoolStatus(status.tool, False, 100.0, "limit facts unavailable", None)
    paced, pace_reason = _codex_pacing(status.windows, now)
    return PoolStatus(
        status.tool,
        status.clear and paced,
        status.spent_pct,
        status.reason if not status.clear else pace_reason,
        status.windows,
    )


def parse_pct(stdout: str, _returncode: int) -> float:
    """Pull the trailing-5h spend % from a `triage-gate.sh check` line. Pure.
    Unknown legacy output fails closed at 100%."""
    m = _PCT_RE.search(stdout)
    if m:
        return float(m.group(1))
    return 100.0


def choose_pair(cs: PoolStatus, xs: PoolStatus, runners: dict) -> tuple:
    """Pure: given both pools' status, return (builder, reviewer) runners.
    reviewer is None in single-tool mode; (None, None) if neither is clear."""
    clear = [s for s in (cs, xs) if s.clear]
    if not clear:
        return None, None
    if len(clear) == 1:
        return runners[clear[0].tool], None            # single-tool: no auto-merge
    builder = min(clear, key=lambda s: s.spent_pct)     # more headroom builds
    other = xs if builder.tool == "claude" else cs
    return runners[builder.tool], runners[other.tool]


def _query_pool(tool: str, operator: bool = False) -> PoolStatus:
    env = {**os.environ, "TRIAGE_AGENT": tool}
    if operator:
        # By-hand dispatch: the operator IS the live session and asked for this
        # run, so tell the gate to skip its recent-activity block. The spend
        # ceiling still applies — `check` keeps failing when a pool is genuinely
        # rate-limited, so the gate stays the single source of truth for it.
        env["TRIAGE_SKIP_ACTIVITY"] = "1"
    try:
        # `spend` reports the REAL trailing-5h % even when the interactive-use gate
        # would block dispatch — so headroom is honest while you're active. `check`
        # is the separate dispatch-availability question (clear vs busy).
        sp = subprocess.run([_GATE, "spend"], env=env, text=True, capture_output=True, timeout=30)
        ck = subprocess.run([_GATE, "check"], env=env, text=True, capture_output=True, timeout=30)
        limits = (subprocess.run([_GATE, "limits"], env=env, text=True,
                                 capture_output=True, timeout=30)
                  if tool == "codex" else None)
    except (OSError, subprocess.TimeoutExpired):
        return PoolStatus(tool, False, 100.0, "gate unavailable")
    # Known legacy spend/check text remains compatible. Structured Codex facts are
    # mandatory for unattended dispatch; an older adapter therefore fails closed.
    raw = ck.stdout.strip()
    reason = raw[len("blocked: "):] if raw.startswith("blocked: ") else raw
    if tool == "codex":
        windows = (_parse_codex_windows(limits.stdout)
                   if limits is not None and limits.returncode == 0 else None)
        if windows is not None:
            return PoolStatus(tool, ck.returncode == 0, _codex_spent_pct(windows),
                              reason, windows)

    legacy = sp.stdout if sp.stdout.strip().startswith("spend:") else ck.stdout
    pct = parse_pct(legacy, ck.returncode)
    parsed = _PCT_RE.search(legacy) is not None
    return PoolStatus(tool, ck.returncode == 0 and parsed, pct,
                      reason if parsed or ck.returncode != 0 else "limit facts unavailable",
                      None if tool == "codex" else ())


def pick_pair(claude: _WorktreeRunner | None = None,
              codex: _WorktreeRunner | None = None,
              operator: bool = False) -> tuple:
    """Live: query both pools and choose the pair. See `choose_pair`. `operator=True`
    marks an explicit by-hand dispatch, which skips the pools' recent-activity guard
    while still honoring their spend ceiling — the pair-vs-single decision is unchanged.

    Returns (builder, reviewer, block_msg). block_msg is "" when builder is not None;
    when both pools are blocked it names each pool and its gate reason."""
    claude = claude or ClaudeRunner()
    codex = codex or CodexRunner()
    cs = _query_pool("claude", operator)
    xs = _query_pool("codex", operator)
    if not operator:
        xs = _codex_dispatch_status(xs, time.time())
    builder, reviewer = choose_pair(cs, xs, {"claude": claude, "codex": codex})
    if builder is not None:
        return builder, reviewer, ""
    blocked = [s for s in (cs, xs) if not s.clear]
    block_msg = ", ".join(
        f"{s.tool}: {s.reason}" if s.reason else s.tool
        for s in blocked
    ) or "both at capacity"
    return None, None, block_msg
