"""Durable per-pool operator pause controls (ADR 0011, issue #545)."""

from __future__ import annotations

from agentflow.state import state_path


POOLS = ("claude", "codex")


def _pause_flag(pool: str):
    if pool not in POOLS:
        raise ValueError(f"unknown pool: {pool}")
    return state_path("pools", f"{pool}.paused")


def pool_paused(pool: str) -> bool:
    """Whether the operator has durably refused new work on ``pool``."""
    return _pause_flag(pool).exists()


def pause_pool(pool: str) -> None:
    """Refuse new work on one pool without stopping daemon reconciliation."""
    flag = _pause_flag(pool)
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()


def resume_pool(pool: str) -> None:
    """Allow the ordinary capacity policy to consider one pool again."""
    _pause_flag(pool).unlink(missing_ok=True)
