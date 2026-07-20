"""Spend accounting for the Intake replay, denominated per the spend-per-success contract
(ADR 0040, #227).

Two spend units with fixed roles, exactly as the contract mandates:

- **Headroom-weighted tokens (the target).** Per-pool gate formulas — Claude:
  ``input + 1.25 × cache_creation + 5 × output``; Codex:
  ``uncached_input + 0.25 × cached_input + 5 × (output + reasoning_output)``. The two pools'
  values are different currencies against different prepaid plans and are NEVER compared to
  each other.
- **Normalized dollar equivalent (the cross-tool comparison signal only).** Claude's
  harness-computed ``cost_usd``; never the optimization target, never a billing claim.

Cache *reads* are free in the Claude headroom formula and near-free in dollars; cache
*creation* is weighted 1.25×. Raw token counts are diagnostics only.
"""

from __future__ import annotations

from statistics import median
from typing import Iterable

from agentflow.coordinator.telemetry import AttemptUsage

CLAUDE = "claude"
CODEX = "codex"


def headroom_weight(usage: AttemptUsage, pool: str) -> float:
    """The pool's headroom-weighted token count for one attempt (ADR 0040). A missing token
    field counts as zero *for the weight only* — callers track missing-usage separately so a
    genuinely unmeasured attempt is never silently treated as free elsewhere."""
    inp = usage.input_tokens or 0
    cache_create = usage.cache_creation_tokens or 0
    cached = usage.cached_input_tokens or 0
    out = usage.output_tokens or 0
    reasoning = usage.reasoning_output_tokens or 0
    if pool == CLAUDE:
        return inp + 1.25 * cache_create + 5 * out
    if pool == CODEX:
        return inp + 0.25 * cached + 5 * (out + reasoning)
    raise ValueError(f"unknown pool {pool!r}")


def percentile(values: list[float], pct: float) -> float:
    """A simple nearest-rank percentile over a sorted copy. Medians and P75/P90, not means —
    the Intake spend tail is heavy (one chain cost $48 against a $2.60 median)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if pct <= 0:
        return ordered[0]
    if pct >= 100:
        return ordered[-1]
    rank = max(0, min(len(ordered) - 1, round(pct / 100 * len(ordered) + 0.5) - 1))
    return ordered[rank]


def spend_summary(per_issue_headroom: Iterable[float],
                  per_issue_cost: Iterable[float | None]) -> dict:
    """Median / P75 / P90 headroom and median normalized dollar over a set of per-issue totals.
    Costs of ``None`` (no provider dollar, e.g. Codex) are excluded from the dollar median and
    counted, never coerced to zero."""
    headroom = [float(h) for h in per_issue_headroom]
    costs = [c for c in per_issue_cost if c is not None]
    return {
        "issues": len(headroom),
        "median_headroom": median(headroom) if headroom else 0.0,
        "p75_headroom": percentile(headroom, 75),
        "p90_headroom": percentile(headroom, 90),
        "total_headroom": sum(headroom),
        "median_cost_usd": median(costs) if costs else None,
        "total_cost_usd": sum(costs) if costs else None,
        "cost_missing": sum(1 for c in per_issue_cost if c is None),
    }
