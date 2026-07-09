"""The daemon's cycle isolation — one bad repo must not stop the others."""

from agentflow.daemon import cycle
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
