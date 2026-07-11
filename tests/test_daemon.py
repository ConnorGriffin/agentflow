"""The daemon's cycle isolation — one bad repo must not stop the others."""

import sys
from unittest import mock

from agentflow.daemon import cycle, main
from agentflow.loop import RepoConfig

A = RepoConfig("owner/a", "/tmp/a")
B = RepoConfig("owner/b", "/tmp/b")


def test_cycle_runs_every_repo_and_isolates_errors():
    seen, logs = [], []

    def run(cfg):
        seen.append(cfg.repo)
        if cfg.repo == "owner/a":
            raise RuntimeError("boom")
        return "ok"

    cycle([A, B], run=run, _log=logs.append)
    assert seen == ["owner/a", "owner/b"]           # B still ran after A raised
    assert any("cycle error" in m and "owner/a" in m for m in logs)
    assert any("owner/b: ok" in m for m in logs)


def test_cycle_logs_result_per_repo():
    logs = []
    cycle([B], run=lambda cfg: "no ready-for-agent issues", _log=logs.append)
    assert logs == ["owner/b: no ready-for-agent issues"]


def test_main_once_runs_one_cycle_and_exits(tmp_path):
    """--once runs exactly one cycle without entering the poll loop."""
    cycle_calls = []

    with (
        mock.patch("agentflow.daemon.STATE_DIR", tmp_path),
        mock.patch("agentflow.daemon.LOCK", tmp_path / "daemon.lock"),
        mock.patch("agentflow.daemon.REPOS", [A, B]),
        mock.patch("agentflow.daemon.cycle", side_effect=lambda repos: cycle_calls.append(list(repos))),
        mock.patch("agentflow.daemon.log"),
        mock.patch.object(sys, "argv", ["daemon", "--once"]),
    ):
        main()

    assert cycle_calls == [[A, B]]
    assert not (tmp_path / "daemon.lock").exists()  # lock released on exit
