"""The two-pool headroom balancer (ADR 0006, 0025) — pick the builder/reviewer pair.

Both plans are prepaid, so the scarce resource is rate-limit headroom. The builder
goes to the pool with more headroom right now; the reviewer is the *other* tool
(cross-tool independence, ADR 0003, which also spreads load across both budgets).

Claude's dispatch authority is the provider's own five-hour quota fact — the utilization
and reset time Claude Code reports on its structured stream, persisted durably by the
coordinator (`agentflow.coordinator.quota`, issue #305). The retired trailing-5h transcript
proxy (`triage-gate.sh spend`) is a diagnostic only: it cannot gate dispatch, and it can
never keep the pool blocked after the provider's five-hour window has reset. Its one narrow
exception is *bootstrap* (issue #309): when no provider fact exists yet — a cold pool, or a
parked window that never observed its own reset — the balancer seeds dispatch from that proxy
as an explicit estimate rather than deadlocking the whole fleet closed until an operator
happens to run an interactive Claude turn. The estimate is never persisted as a provider fact,
and the moment a real provider fact lands it wins. Codex still
reports its rate-limit windows through the gate adapter. If only one pool has headroom, the
builder runs there and the reviewer is None: the caller must NOT auto-merge (single-tool
fallback, ADR 0003). If neither has headroom, no capacity this cycle.

**Paced weekly allowance (ADR 0006, issue #315).** Claude, like Codex, also gates on a *paced
weekly allowance*: the provider's seven-day window, released 80% in seven equal daily steps from
the window's own start. `_query_pool` reports the five-hour utilization (the load-balancing and
raw-usage signal — weekly pacing never replaces it) and carries the seven-day window along;
`_claude_dispatch_status` (mirroring `_codex_dispatch_status`) layers the weekly constraint on at
each dispatch decision — builder/reviewer assignment and the final launch recheck — so a queued
Claude session defers if its weekly allowance is spent between assignment and launch. The
seven-day window is a required fact: a missing or stale one fails closed independently of the
five-hour bootstrap estimate.

**Activity-adaptive ceiling (ADR 0025).** One named policy owns the effective ceiling: an idle
pool dispatches up to `IDLE_CEILING_PCT` utilization, an operator-active pool only up to
`ACTIVE_CEILING_PCT` and paced to `ACTIVE_PACE` new sessions/cycle (the pace is enforced by the
dispatcher, which counts per cycle — see `agentflow.dispatch`). For Claude the utilization is the
provider's own five-hour quota fact (#305); the personal-tooling gate contributes only the
operator active/idle fact and can no longer independently block Claude below this ceiling. Activity
is *derived* from that gate's check: a block that clears once the recent-activity guard is skipped
was an activity block, so it lowers the ceiling; genuine no-capacity for Claude is expressed by the
quota fact (over-ceiling utilization; a missing fact falls back to the bootstrap estimate above,
and only unknown gate output fails closed). The gate keeps
excluding agentflow's own sessions (`AGENTFLOW_WT_MARK`) so the fleet never reads *itself* as the
operator.

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

from agentflow.coordinator import quota
from agentflow.coordinator.store import default_store_path
from agentflow.runner import ClaudeRunner, CodexRunner, _WorktreeRunner

# The two pools this module balances between — the tools a build, review or revise may run on.
BUILD_POOLS = ("claude", "codex")

_GATE = (
    os.environ.get("AGENTFLOW_CAPACITY_HELPER")
    or os.environ.get("AGENTFLOW_TRIAGE_GATE")
)
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

# Conservative in-flight reservation (#305). Claude's provider quota fact is only observed after
# a session ends, so work admitted from one below-ceiling reading is not yet reflected in it. The
# reservation is charged *per running permit* (demand unit), not per running session: a heavier
# session already reserved more of the pool's five permits and consumes proportionally more of the
# five-hour quota, so a demand-4 deep build reserves 4x this before another session admits. This is
# deliberate — it errs on the safe side of the conservative-reservation criterion. It is also
# calibrated against the pool's own five-permit ledger: at the 15% default a fully-seated pool
# (five permits) reserves 75%, which stays just under the 85% idle ceiling, so the hard permit
# ledger — not this soft reservation — remains the real concurrency cap. Env-overridable.
CLAUDE_INFLIGHT_RESERVE_PCT = float(
    os.environ.get("AGENTFLOW_CLAUDE_INFLIGHT_RESERVE_PCT", "15"))

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


def _weekly_released(elapsed_seconds: float) -> float:
    """The unattended weekly allowance released ``elapsed_seconds`` into a seven-day window: 80%
    split into seven equal daily steps, the first available immediately and one more at each
    24-hour boundary measured from the window's own start (ADR 0006). Shared by both pools' weekly
    pacing (#315) so Claude and Codex release the same allowance the same way."""
    day = min(int(elapsed_seconds // _DAY_SECONDS), _WEEKLY_DAYS - 1)
    return _WEEKLY_UNATTENDED_PCT * (day + 1) / _WEEKLY_DAYS


def _weekly_over_pace(window: RateLimitWindow, now: float) -> str | None:
    """The deferral reason when a seven-day window is over its released allowance right now, or
    ``None`` when it permits unattended dispatch. Reported usage must be *strictly* below the
    released allowance — usage equal to it blocks.

    Reset-aware roll-forward (#322), mirroring `quota.effective_usage`: once ``now`` reaches the
    recorded ``resets_at`` the provider window has rolled over, so the reported utilization no
    longer applies. Rather than failing closed, advance to the current window (start = the latest
    ``resets_at + k·window`` at or before ``now``) at 0% used and pace from that fresh start, so a
    throttled pool recovers on the next normal dispatch pass with no operator, poll, or restart.
    This is decision-time compute only — nothing rolled forward is ever persisted. A window that
    has not yet opened (``now < starts_at``) is still untrustworthy and fails closed."""
    window_seconds = window.window_minutes * 60
    starts_at = window.resets_at - window_seconds
    if now < starts_at:
        return "weekly limit facts are stale"
    if now >= window.resets_at:
        elapsed_windows = int((now - window.resets_at) // window_seconds)
        starts_at = window.resets_at + elapsed_windows * window_seconds
        used_percent = 0.0
    else:
        used_percent = window.used_percent
    released = _weekly_released(now - starts_at)
    if used_percent >= released:
        return (f"weekly spend at {used_percent:g}% exceeds "
                f"{released:.1f}% released for unattended work")
    return None


def _normalize_short_window(window: RateLimitWindow, now: float) -> float | None:
    """Normalize a short (300-minute) Codex window's usage to the current moment (#319).
    Returns the effective usage (0..100), or ``None`` when the window is temporally impossible.

    - Expired (``now >= resets_at``): rolled over; read as 0%.
    - Future start (``starts_at > now``): impossible window; fail closed.
    - Live: return the reported usage unchanged."""
    starts_at = window.resets_at - window.window_minutes * 60
    if starts_at > now:
        return None
    if now >= window.resets_at:
        return 0.0
    return window.used_percent


def _codex_pacing(windows: tuple[RateLimitWindow, ...], now: float) -> tuple[bool, str]:
    for window in windows:
        if window.window_minutes == _WEEKLY_WINDOW_MIN:
            # The seven-day window's staleness/roll-forward is owned entirely by
            # `_weekly_over_pace` (#322), so an expired-but-reset weekly window rolls forward here
            # for Codex the same as for Claude.
            over = _weekly_over_pace(window, now)
            if over is not None:
                return False, over
            continue
        # Short window (#319): normalize expired windows to 0% (rolled over) rather than
        # failing closed as stale; a future-start window is still impossible and fails closed.
        effective = _normalize_short_window(window, now)
        if effective is None:
            return False, f"{window.window_minutes}-minute limit facts are stale"
    return True, ""


def _codex_spent_pct(windows: tuple[RateLimitWindow, ...], now: float) -> float:
    """The effective short-window utilization for headroom and ceiling checks, normalized to
    ``now`` so an expired 300-minute window does not keep the pool blocked after reset (#319)."""
    short = next((w for w in windows if w.window_minutes == _SHORT_WINDOW_MIN), None)
    target = short if short is not None else windows[0]
    effective = _normalize_short_window(target, now)
    return effective if effective is not None else 100.0


def _codex_dispatch_status(status: PoolStatus, now: float) -> PoolStatus:
    if status.windows is None:
        return PoolStatus(status.tool, False, 100.0, "limit facts unavailable", None)
    paced, pace_reason = _codex_pacing(status.windows, now)
    spent_pct = _codex_spent_pct(status.windows, now)
    # Prefer the pacing reason when it fires; fall back to the existing reason only when
    # pacing is fine or when it has no reason to offer.
    reason = (pace_reason if not paced and pace_reason
              else status.reason if not status.clear
              else pace_reason)
    return PoolStatus(
        status.tool,
        status.clear and paced,
        spent_pct,
        reason,
        status.windows,
        status.active,
        status.ceiling,
        status.observed_at,
    )


def _claude_dispatch_status(status: PoolStatus, now: float) -> PoolStatus:
    """Layer Claude's paced weekly allowance onto a five-hour `PoolStatus` (#315), the way
    `_codex_dispatch_status` does for Codex. The five-hour decision already in `status` sizes
    headroom and load-balances the pool; this ANDs in the independent seven-day constraint so a
    queued Claude session defers if its weekly allowance was consumed since assignment.

    The seven-day window is a *required* fact: a pool with no trustworthy weekly window fails
    closed here, independent of any five-hour bootstrap estimate — matching the fail-closed
    contract when a required window is unavailable or stale. A five-hour block keeps its own
    reason; only a pool that clears the five-hour gate surfaces the weekly reason, so a deferral
    line distinguishes a headroom block from a pacing block."""
    weekly = next((w for w in (status.windows or ())
                   if w.window_minutes == _WEEKLY_WINDOW_MIN), None)
    if weekly is None:
        # A five-hour block already blocking keeps its own reason (so a cold-pool bootstrap
        # estimate still reads as such); only a pool that cleared five-hour surfaces the weekly
        # fail-closed reason.
        reason = status.reason if not status.clear else "weekly limit facts unavailable"
        return PoolStatus(status.tool, False, status.spent_pct, reason,
                          status.windows, status.active, status.ceiling, status.observed_at)
    over = _weekly_over_pace(weekly, now)
    paced = over is None
    return PoolStatus(
        status.tool,
        status.clear and paced,
        status.spent_pct,
        status.reason if not status.clear else (over or ""),
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


def choose_reviewer(builder_tool: str, cs: PoolStatus, xs: PoolStatus, *,
                    allow_same_tool: bool = True) -> str | None:
    """Pure (ADR 0020): pick the reviewer for a completed builder's PR.

    Prefer the cross-tool reviewer (the *other* tool — the independence bar for a hands-off
    merge, ADR 0003). Callers serving a human-merge profile may allow immediate same-tool fallback;
    autonomous callers pass ``allow_same_tool=False`` and hold for independence. If no eligible
    pool has headroom, return None so the caller defers this cycle."""
    status = {"claude": cs, "codex": xs}
    opposite = "codex" if builder_tool == "claude" else "claude"
    if status[opposite].clear:
        return opposite
    if allow_same_tool and status[builder_tool].clear:
        return builder_tool
    return None


def pick_reviewer(builder_tool: str, *, allow_same_tool: bool = True) -> str | None:
    """Live: query both pools under the same unattended pacing `pick_pair` uses, then choose the
    reviewer per ADR 0020. See `choose_reviewer`. Returns None when no pool has headroom to
    review this cycle."""
    now = time.time()
    cs = _claude_dispatch_status(_query_pool("claude"), now)
    xs = _codex_dispatch_status(_query_pool("codex"), now)
    return choose_reviewer(builder_tool, cs, xs, allow_same_tool=allow_same_tool)


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


def _query_pool(tool: str, operator: bool = False, *,
                reserved_pct: float = 0.0, now: float | None = None) -> PoolStatus:
    now = time.time() if now is None else now
    env = {**os.environ, "TRIAGE_AGENT": tool}
    if operator:
        env["TRIAGE_SKIP_ACTIVITY"] = "1"
    if _GATE is None:
        if tool == "codex":
            return PoolStatus(
                tool, False, 100.0, "capacity helper not configured", None
            )
        limits = None
        blocked, active, block_reason, _ck_stdout = False, False, "", ""
    else:
        try:
            limits = (subprocess.run([_GATE, "limits"], env=env, text=True,
                                     capture_output=True, timeout=30)
                      if tool == "codex" else None)
            blocked, active, block_reason, _ck_stdout = _gate_facts(env, operator)
        except (OSError, subprocess.TimeoutExpired):
            return PoolStatus(tool, False, 100.0, "capacity helper unavailable")
    ceiling = ceiling_for(active)
    yield_reason = f"yielding to operator · ceiling {ceiling:.0f}%"
    # Known legacy spend/check text remains compatible. Structured Codex facts are
    # mandatory for unattended dispatch; an older adapter therefore fails closed.
    if tool == "codex":
        facts = (_parse_codex_facts(limits.stdout)
                 if limits is not None and limits.returncode == 0 else None)
        if facts is None:
            # Structured Codex facts are mandatory for unattended dispatch; an older or
            # unavailable adapter fails closed rather than dispatching blind.
            return PoolStatus(tool, False, 100.0, "limit facts unavailable", None, active, ceiling)
        windows, observed_at = facts
        pct = _codex_spent_pct(windows, now)
        under = pct < ceiling
        reason = block_reason if blocked else (yield_reason if active and not under else "")
        return PoolStatus(tool, (not blocked) and under, pct, reason,
                          windows, active, ceiling, observed_at)

    # Claude's dispatch authority is the provider's own five-hour quota fact (#305), read from the
    # durable store the coordinator writes — never the trailing transcript proxy, and never the
    # gate's own independent spend block. The single `ceiling_for` policy owns the effective
    # ceiling: no lower gate can silently override it. The transcript proxy stays a diagnostic.
    fact = quota.read_quota(default_store_path(), "claude")
    usage = quota.effective_usage(fact, now) if fact is not None else None
    observed_at = fact.observed_at if fact is not None else None
    bootstrapping = False
    if usage is None:
        if _GATE is None:
            return PoolStatus(
                tool,
                False,
                100.0,
                "capacity helper not configured and provider facts unavailable",
                (),
                active,
                ceiling,
            )
        # No trustworthy provider fact yet — a cold pool before any Claude session has emitted
        # one, or a parked window that never observed its own reset (issue #309). The provider
        # fact has a single producer (a completed Claude session), and the only session allowed
        # to run Claude *without* a fact is an interactive operator turn; an unattended fleet
        # never produces one, so failing closed here deadlocks the whole pool. Instead of
        # wedging, seed dispatch from a *bootstrap estimate*: the trailing-five-hour transcript
        # proxy the gate already computes. It is explicitly an estimate — never persisted as a
        # provider fact — and it is superseded the instant a real provider fact lands (the
        # provider fact always wins, above). This lets a cold/parked fleet dispatch and thereby
        # produce that first real fact instead of deadlocking on its absence. Unknown gate
        # output still fails closed (parse_pct → 100%).
        usage = parse_pct(_ck_stdout, 0)
        observed_at = None
        bootstrapping = True
    # Reserve conservative headroom for Claude work already admitted from the same observation, so
    # several launches cannot all pass one below-ceiling reading and collectively reach 100% (#305).
    under = usage + max(0.0, reserved_pct) < ceiling
    reason = (yield_reason if active and not under else
              ("" if under else
               f"five-hour utilization at {usage:.0f}% (ceiling {ceiling:.0f}%)"))
    if bootstrapping and reason:
        # Mark that this reading is the bootstrap estimate, not a provider fact, so a deferral
        # line reads "…· bootstrap estimate (no provider fact yet)" rather than looking like a
        # measured over-ceiling utilization.
        reason += " · bootstrap estimate (no provider fact yet)"
    # Carry the provider's seven-day window (the paced weekly allowance, #315) so a dispatch site
    # can layer weekly pacing on with `_claude_dispatch_status`. Raw `_query_pool` still reports the
    # five-hour utilization as `spent_pct` for load balancing and raw usage — weekly pacing never
    # replaces it (the dashboard layers the dispatch verdict on separately, #436). A missing
    # seven-day fact leaves the windows empty, so the wrapper fails
    # closed on it independently of the five-hour bootstrap.
    weekly_fact = quota.read_quota(default_store_path(), "claude", quota.SEVEN_DAY)
    weekly_windows = (
        (RateLimitWindow(weekly_fact.used_percent, _WEEKLY_WINDOW_MIN, weekly_fact.resets_at),)
        if weekly_fact is not None else ())
    return PoolStatus(tool, under, usage, reason, weekly_windows, active, ceiling, observed_at)


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
        now = time.time()
        cs = _claude_dispatch_status(cs, now)
        xs = _codex_dispatch_status(xs, now)
    builder, reviewer = choose_pair(cs, xs, {"claude": claude, "codex": codex})
    if builder is not None:
        return builder, reviewer, ""
    blocked = [s for s in (cs, xs) if not s.clear]
    block_msg = ", ".join(
        f"{s.tool}: {s.reason}" if s.reason else s.tool
        for s in blocked
    ) or "both at capacity"
    return None, None, block_msg
