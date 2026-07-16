"""The two-pool headroom balancer (ADR 0006, 0025) — pick the builder/reviewer pair.

Both plans are prepaid, so the scarce resource is rate-limit headroom. The builder
goes to the pool with more headroom right now; the reviewer is the *other* tool
(cross-tool independence, ADR 0003, which also spreads load across both budgets).

It reuses the existing per-agent gate `triage-gate.sh` (the reuse map's balancer
source): that adapter reports Claude's calibrated trailing-5h spend and Codex's
reported rate-limit windows. If only one pool has headroom, the builder runs there
and the reviewer is None: the caller must NOT auto-merge (single-tool fallback,
ADR 0003). If neither has headroom, no capacity this cycle.

**Activity-adaptive ceiling (ADR 0025).** The gate no longer *hard-stops* dispatch when
the operator is working interactively — that guard now only *selects the ceiling*. Facts
live in the gate (trailing-5h spend, operator active/idle); the policy lives here: an idle
pool dispatches up to `IDLE_CEILING_PCT` spent, an operator-active pool only up to
`ACTIVE_CEILING_PCT` and paced to `ACTIVE_PACE` new sessions/cycle (the pace is enforced
by the dispatcher, which counts per cycle — see `agentflow.dispatch`). Until the gate
grows an explicit activity fact, activity is *derived* from the gate's own check: a block
that clears once the recent-activity guard is skipped was an activity block, so it lowers
the ceiling instead of stopping the pool; any other block (genuine no-capacity) still
defers. Unknown facts fail toward the idle ceiling. The gate keeps excluding agentflow's
own sessions (`AGENTFLOW_WT_MARK`) so the fleet never reads *itself* as the operator.

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
_DAY_SECONDS = 24 * 60 * 60
_WEEKLY_DAYS = _WEEKLY_WINDOW_MIN * 60 // _DAY_SECONDS

# ADR 0025 spend-ceiling policy (named config, env-overridable). An idle pool dispatches
# until it is this % spent; an operator-active pool yields down to the lower ceiling and
# the dispatcher paces new sessions on it to ACTIVE_PACE per cycle.
IDLE_CEILING_PCT = float(os.environ.get("AGENTFLOW_IDLE_CEILING_PCT", "85"))
ACTIVE_CEILING_PCT = float(os.environ.get("AGENTFLOW_ACTIVE_CEILING_PCT", "50"))
ACTIVE_PACE = int(os.environ.get("AGENTFLOW_ACTIVE_PACE", "1"))

def ceiling_for(active: bool) -> float:
    """The spend ceiling a pool dispatches under, given whether the operator is active on
    it (ADR 0025). Pure — the one place the idle/active policy lives."""
    return ACTIVE_CEILING_PCT if active else IDLE_CEILING_PCT


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
    active: bool = False           # operator working interactively on this pool (ADR 0025)
    ceiling: float = IDLE_CEILING_PCT   # the spend ceiling this pool dispatched under
    observed_at: float | None = None    # when the latest limit fact was seen (Codex)


def _parse_codex_facts(
        stdout: str) -> tuple[tuple[RateLimitWindow, ...], float | None] | None:
    """Parse the gate adapter's structured facts into `(windows, observed_at)`.
    Unknown shapes fail closed (None). `observed_at` is when the newest fact was seen;
    an older adapter omits it (None — the pool then simply never actively refreshes),
    but a present-yet-malformed value fails the whole parse closed."""
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
            if not math.isfinite(raw_minutes):
                return None
            minutes = int(raw_minutes)
            if (raw_minutes != minutes
                    or minutes not in (_SHORT_WINDOW_MIN, _WEEKLY_WINDOW_MIN)
                    or not math.isfinite(used) or not 0 <= used <= 100
                    or not math.isfinite(resets_at) or resets_at <= 0):
                return None
            windows.append(RateLimitWindow(used, minutes, resets_at))
        if len({window.window_minutes for window in windows}) != len(windows):
            return None
        observed_at = raw.get("observed_at")
        if observed_at is not None:
            if (isinstance(observed_at, bool)
                    or not isinstance(observed_at, (int, float))
                    or not math.isfinite(observed_at) or observed_at <= 0):
                return None
            observed_at = float(observed_at)
        return tuple(sorted(windows, key=lambda window: window.window_minutes)), observed_at
    except (KeyError, TypeError, ValueError, OverflowError, json.JSONDecodeError):
        return None


def _codex_pacing(windows: tuple[RateLimitWindow, ...], now: float) -> tuple[bool, str]:
    for window in windows:
        starts_at = window.resets_at - window.window_minutes * 60
        if not starts_at <= now < window.resets_at:
            return False, f"{window.window_minutes}-minute limit facts are stale"
        if window.window_minutes != _WEEKLY_WINDOW_MIN:
            continue
        elapsed = now - starts_at
        day = min(int(elapsed // _DAY_SECONDS), _WEEKLY_DAYS - 1)
        released = _WEEKLY_UNATTENDED_PCT * (day + 1) / _WEEKLY_DAYS
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
        status.active,
        status.ceiling,
        status.observed_at,
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


def _gate_facts(env: dict, operator: bool) -> tuple[bool, bool, str, str]:
    """Report the gate's dispatch facts for a pool (ADR 0025):
    `(blocked, active, reason, check_stdout)`.

    `active` is the operator-activity fact, derived (until the gate exposes it directly)
    by re-running `check` with the recent-activity guard skipped: a block that clears is
    an activity block, which no longer stops the pool — it lowers the ceiling. `blocked`
    is any *other* (genuine no-capacity) block that still defers. A by-hand `operator` run
    skips the activity guard outright — the operator IS the live session and asked for it —
    so it reports no activity. Raises through to the caller's fail-closed handling on error."""
    ck = subprocess.run([_GATE, "check"], env=env, text=True, capture_output=True, timeout=30)
    raw = ck.stdout.strip()
    reason = raw[len("blocked: "):] if raw.startswith("blocked: ") else raw
    if ck.returncode == 0 or operator:
        return ck.returncode != 0, False, reason, ck.stdout
    skip = subprocess.run([_GATE, "check"], env={**env, "TRIAGE_SKIP_ACTIVITY": "1"},
                          text=True, capture_output=True, timeout=30)
    if skip.returncode == 0:
        return False, True, "", ck.stdout   # the block was purely the operator being active
    return True, False, reason, ck.stdout    # a real block remains once activity is skipped


def _query_pool(tool: str, operator: bool = False) -> PoolStatus:
    env = {**os.environ, "TRIAGE_AGENT": tool}
    if operator:
        env["TRIAGE_SKIP_ACTIVITY"] = "1"
    try:
        # `spend` reports the REAL trailing-5h % even when the interactive-use guard
        # would block dispatch — so headroom is honest while you're active.
        sp = subprocess.run([_GATE, "spend"], env=env, text=True, capture_output=True, timeout=30)
        limits = (subprocess.run([_GATE, "limits"], env=env, text=True,
                                 capture_output=True, timeout=30)
                  if tool == "codex" else None)
        blocked, active, block_reason, ck_stdout = _gate_facts(env, operator)
    except (OSError, subprocess.TimeoutExpired):
        return PoolStatus(tool, False, 100.0, "gate unavailable")
    ceiling = ceiling_for(active)
    yield_reason = f"yielding to operator · ceiling {ceiling:.0f}%"
    # Known legacy spend/check text remains compatible. Structured Codex facts are
    # mandatory for unattended dispatch; an older adapter therefore fails closed.
    if tool == "codex":
        facts = (_parse_codex_facts(limits.stdout)
                 if limits is not None and limits.returncode == 0 else None)
        if facts is not None:
            windows, observed_at = facts
            pct = _codex_spent_pct(windows)
            under = pct < ceiling
            reason = block_reason if blocked else (yield_reason if active and not under else "")
            return PoolStatus(tool, (not blocked) and under, pct, reason,
                              windows, active, ceiling, observed_at)

    legacy = sp.stdout if sp.stdout.strip().startswith("spend:") else ck_stdout
    pct = parse_pct(legacy, 0)
    parsed = _PCT_RE.search(legacy) is not None
    under = parsed and pct < ceiling
    reason = (block_reason if blocked else
              ("limit facts unavailable" if not parsed else
               (yield_reason if active and not under else "")))
    return PoolStatus(tool, (not blocked) and under, pct, reason,
                      None if tool == "codex" else (), active, ceiling)


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
