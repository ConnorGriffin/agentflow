"""The two-pool headroom balancer (ADR 0006) — pick the builder/reviewer pair.

Both plans are prepaid, so the scarce resource is rate-limit headroom. The builder
goes to the pool with more headroom right now; the reviewer is the *other* tool
(cross-tool independence, ADR 0003, which also spreads load across both budgets).

It reuses the existing per-agent gate `triage-gate.sh` (the reuse map's balancer
source): that script already models both pools — Claude's calibrated trailing-5h
spend, Codex's primary rate-limit `used_percent`. If only one pool has headroom, the
builder runs there and the reviewer is None: the caller must NOT auto-merge
(single-tool fallback, ADR 0003). If neither has headroom, no capacity this cycle.

`choose_pair` and `parse_pct` are pure — the test surface.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

from agentflow.runner import ClaudeRunner, CodexRunner, _WorktreeRunner

_GATE = os.environ.get(
    "AGENTFLOW_TRIAGE_GATE",
    os.path.expanduser("~/Code/ConnorGriffin/dotfiles/scripts/triage-gate.sh"))
_PCT_RE = re.compile(r"at (\d+(?:\.\d+)?)% of (?:peak|limit)")


@dataclass(frozen=True)
class PoolStatus:
    tool: str
    clear: bool
    spent_pct: float   # 0..100; lower = more headroom
    reason: str = ""   # gate's check output when blocked, stripped of "blocked: " prefix


def parse_pct(stdout: str, returncode: int) -> float:
    """Pull the trailing-5h spend % from a `triage-gate.sh check` line. Pure.
    Falls back to 0 when clear-but-unparsed, 100 when blocked-and-unparsed."""
    m = _PCT_RE.search(stdout)
    if m:
        return float(m.group(1))
    return 0.0 if returncode == 0 else 100.0


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
    except (OSError, subprocess.TimeoutExpired):
        return PoolStatus(tool, False, 100.0, "gate unavailable")
    # Prefer the real spend %; fall back to `check`'s output if the gate is too old
    # to have `spend` mode — degrade gracefully, never crash.
    if sp.stdout.strip().startswith("spend:"):
        pct = parse_pct(sp.stdout, 0)
    else:
        pct = parse_pct(ck.stdout, ck.returncode)
    raw = ck.stdout.strip()
    reason = raw[len("blocked: "):] if raw.startswith("blocked: ") else raw
    return PoolStatus(tool, ck.returncode == 0, pct, reason)


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
    builder, reviewer = choose_pair(cs, xs, {"claude": claude, "codex": codex})
    if builder is not None:
        return builder, reviewer, ""
    blocked = [s for s in (cs, xs) if not s.clear]
    block_msg = ", ".join(
        f"{s.tool}: {s.reason}" if s.reason else s.tool
        for s in blocked
    ) or "both at capacity"
    return None, None, block_msg
