"""Balancer decision + gate-output parsing — the pure surfaces."""

import pytest

from agentflow.balancer import PoolStatus, choose_pair, parse_pct

RUNNERS = {"claude": "CLAUDE", "codex": "CODEX"}


def test_more_headroom_builds_other_reviews():
    claude = PoolStatus("claude", True, 20.0)   # more headroom
    codex = PoolStatus("codex", True, 60.0)
    assert choose_pair(claude, codex, RUNNERS) == ("CLAUDE", "CODEX")


def test_flips_when_codex_has_more_headroom():
    claude = PoolStatus("claude", True, 70.0)
    codex = PoolStatus("codex", True, 30.0)     # more headroom
    assert choose_pair(claude, codex, RUNNERS) == ("CODEX", "CLAUDE")


def test_single_pool_clear_is_single_tool_no_reviewer():
    claude = PoolStatus("claude", True, 50.0)
    codex = PoolStatus("codex", False, 100.0)
    assert choose_pair(claude, codex, RUNNERS) == ("CLAUDE", None)


def test_neither_clear_is_no_capacity():
    claude = PoolStatus("claude", False, 100.0)
    codex = PoolStatus("codex", False, 100.0)
    assert choose_pair(claude, codex, RUNNERS) == (None, None)


@pytest.mark.parametrize("stdout,rc,expected", [
    ("clear: trailing-5h spend at 23% of peak", 0, 23.0),
    ("clear: trailing-5h spend at 7.5% of limit", 0, 7.5),
    ("blocked: trailing-5h spend at 88% of peak (threshold 40%)", 1, 88.0),
    ("blocked: interactive session on a tty", 1, 100.0),   # unparsed + blocked -> no headroom
    ("clear: something unparsed", 0, 0.0),                 # unparsed + clear -> full headroom
])
def test_parse_pct(stdout, rc, expected):
    assert parse_pct(stdout, rc) == expected
