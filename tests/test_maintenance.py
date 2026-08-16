from __future__ import annotations

from pathlib import Path
import json
import shutil
import sqlite3
import subprocess
from types import SimpleNamespace


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "agentflow@example.com")
    _git(repo, "config", "user.name", "AgentFlow Test")
    (repo / "README.md").write_text("start\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "start")
    _git(repo, "branch", "-M", "main")
    _git(repo, "push", "-u", "origin", "main")
    _git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    return repo


class _Index:
    def __init__(self, projects=()):
        self.projects = list(projects)
        self.deleted = []

    def list_projects(self):
        return list(self.projects)

    def delete_project(self, project):
        self.deleted.append(project)
        self.projects = [item for item in self.projects if item["name"] != project]
        return True


def test_maintenance_removes_only_marked_disposable_worktrees(tmp_path):
    from agentflow.maintenance import maintain
    from agentflow.worktree_ownership import mark_worktree_owned

    repo = _repo(tmp_path)
    owned = repo / ".agentflow" / "worktrees" / "codex" / "issue-1-owned"
    unknown = repo / ".agentflow" / "worktrees" / "codex" / "issue-2-unknown"
    for worktree in (owned, unknown):
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "--detach", str(worktree), "origin/main")
    mark_worktree_owned(owned, disposable=True)

    records = maintain(
        [SimpleNamespace(repo="owner/repo", workdir=str(repo))],
        apply=True,
        live_sources=set(),
        held_sources=set(),
        index=_Index(),
    )

    assert not owned.exists()
    assert unknown.exists()
    by_path = {record["path"]: record for record in records if record.get("path")}
    assert by_path[str(owned)] == {
        "action": "remove-worktree",
        "path": str(owned),
        "eligible": True,
        "reason": "inactive-clean-owned",
        "applied": True,
    }
    assert by_path[str(unknown)]["eligible"] is False
    assert by_path[str(unknown)]["reason"] == "unknown-owned"


def test_maintenance_refuses_dirty_live_held_and_retained_worktrees(tmp_path):
    from agentflow.maintenance import maintain
    from agentflow.runner import worktree_session
    from agentflow.worktree_ownership import mark_worktree_owned

    repo = _repo(tmp_path)
    roots = {}
    for reason in ("dirty", "live", "held", "retained"):
        worktree = repo / ".agentflow" / "worktrees" / "codex" / f"issue-{reason}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "--detach", str(worktree), "origin/main")
        mark_worktree_owned(worktree, disposable=reason != "retained")
        roots[reason] = worktree
    (roots["dirty"] / "operator-notes.md").write_text("keep me\n")

    with worktree_session(roots["live"]):
        records = maintain(
            [SimpleNamespace(repo="owner/repo", workdir=str(repo))],
            apply=True,
            live_sources=set(),
            held_sources={str(roots["held"])},
            index=_Index(),
        )

    by_path = {record["path"]: record for record in records if record.get("path")}
    for reason, worktree in roots.items():
        assert worktree.exists()
        assert by_path[str(worktree)]["eligible"] is False
        assert by_path[str(worktree)]["reason"] == reason


def test_maintenance_cleans_known_probes_and_vanished_registrations_safely(tmp_path):
    from agentflow.maintenance import maintain
    from agentflow.provider_skills import NATIVE_DISCOVERY_SKILL, _NATIVE_DISCOVERY_FIXTURE
    from agentflow.worktree_ownership import mark_worktree_owned

    repo = _repo(tmp_path)
    for location in (".agents", ".claude"):
        probe = repo / location / "skills" / NATIVE_DISCOVERY_SKILL / "SKILL.md"
        probe.parent.mkdir(parents=True)
        probe.write_text(_NATIVE_DISCOVERY_FIXTURE)
    _git(repo, "add", ".agents", ".claude")
    _git(repo, "commit", "-m", "historical probe")

    worktrees = {}
    for kind in ("owned", "dirty", "unknown", "vanished"):
        worktree = repo / ".agentflow" / "worktrees" / "codex" / f"issue-{kind}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        worktrees[kind] = worktree
    mark_worktree_owned(worktrees["owned"], disposable=True)
    mark_worktree_owned(worktrees["dirty"], disposable=True)
    (worktrees["dirty"] / "operator-notes.md").write_text("keep\n")
    shutil.rmtree(worktrees["vanished"])

    records = maintain(
        [SimpleNamespace(repo="owner/repo", workdir=str(repo))],
        apply=True,
        live_sources=set(),
        held_sources=set(),
        index=_Index(),
    )

    assert not worktrees["owned"].exists()
    for kind in ("dirty", "unknown"):
        assert worktrees[kind].exists()
        assert (
            worktrees[kind] / ".agents" / "skills" / NATIVE_DISCOVERY_SKILL
        ).exists()
    registered = _git(repo, "worktree", "list", "--porcelain")
    assert str(worktrees["vanished"]) not in registered

    probes = {
        record["path"]: record
        for record in records
        if record["action"] == "remove-probe"
    }
    assert probes[str(worktrees["owned"])]["eligible"] is True
    assert probes[str(worktrees["owned"])]["applied"] is True
    assert probes[str(worktrees["dirty"])]["reason"] == "dirty"
    assert probes[str(worktrees["unknown"])]["reason"] == "unknown-owned"
    registration = next(
        record for record in records
        if record["action"] == "prune-registration"
        and record["path"] == str(worktrees["vanished"])
    )
    assert registration["eligible"] is True
    assert registration["reason"] == "missing"
    assert registration["applied"] is True


def test_maintenance_indexes_are_coupled_to_inventory_and_replay_safe(tmp_path):
    from agentflow.maintenance import maintain
    from agentflow.worktree_ownership import mark_worktree_owned

    repo = _repo(tmp_path)
    worktree = repo / ".agentflow" / "worktrees" / "codex" / "issue-indexed"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "--detach", str(worktree), "origin/main")
    mark_worktree_owned(worktree, disposable=True)
    missing = tmp_path / "gone-project"
    index = _Index([
        {"name": "worktree-project", "root_path": str(worktree)},
        {"name": "missing-project", "root_path": str(missing)},
        {"name": "reachable-project", "root_path": str(repo)},
    ])
    repository = SimpleNamespace(repo="owner/repo", workdir=str(repo))
    index_path = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    before_index = index_path.read_bytes()
    before_mtime = index_path.stat().st_mtime_ns
    before_registry = _git(repo, "worktree", "list", "--porcelain")

    dry_run = maintain(
        [repository], apply=False, live_sources=set(), held_sources=set(), index=index
    )

    assert worktree.exists()
    assert index.deleted == []
    assert _git(repo, "worktree", "list", "--porcelain") == before_registry
    assert index_path.read_bytes() == before_index
    assert index_path.stat().st_mtime_ns == before_mtime
    projects = {
        record["project"]: record
        for record in dry_run
        if record["action"] == "delete-index"
    }
    assert projects["worktree-project"]["eligible"] is True
    assert projects["worktree-project"]["reason"] == "worktree-pruned"
    assert projects["missing-project"]["eligible"] is True
    assert projects["missing-project"]["reason"] == "missing"
    assert projects["reachable-project"]["eligible"] is False
    assert projects["reachable-project"]["reason"] == "reachable"

    first = maintain(
        [repository], apply=True, live_sources=set(), held_sources=set(), index=index
    )
    assert not worktree.exists()
    assert index.deleted == ["worktree-project", "missing-project"]
    assert sum(record["applied"] for record in first) == 3

    deleted = list(index.deleted)
    second = maintain(
        [repository], apply=True, live_sources=set(), held_sources=set(), index=index
    )
    assert index.deleted == deleted
    assert not any(record["applied"] for record in second)


def test_public_maintenance_cli_is_jsonl_and_dry_run_by_default(monkeypatch, capsys):
    from agentflow.cli import main

    config = SimpleNamespace(repositories=[SimpleNamespace(repo="o/r", workdir="/repo")])
    calls = []
    monkeypatch.setattr("agentflow.config.load_config", lambda *_args: config)
    monkeypatch.setattr(
        "agentflow.maintenance.maintenance_sources", lambda _repos: (set(), set(), True)
    )
    monkeypatch.setattr(
        "agentflow.maintenance.maintain",
        lambda repos, **kwargs: calls.append((repos, kwargs)) or [{
            "action": "remove-worktree", "path": "/repo/wt", "eligible": False,
            "reason": "unknown-owned", "applied": False,
        }],
    )

    assert main(["maintenance"]) == 0
    dry_record = json.loads(capsys.readouterr().out)
    assert dry_record["reason"] == "unknown-owned"
    assert calls[-1][1]["apply"] is False

    assert main(["maintenance", "--apply"]) == 0
    json.loads(capsys.readouterr().out)
    assert calls[-1][1]["apply"] is True


def test_codebase_memory_adapter_reads_authoritative_root_from_project_database(
    tmp_path, monkeypatch
):
    from agentflow.maintenance import CodebaseMemoryIndex

    project = "indexed-project"
    database = tmp_path / f"{project}.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE projects (name TEXT PRIMARY KEY, indexed_at TEXT, root_path TEXT)"
        )
        connection.execute(
            "INSERT INTO projects VALUES (?, ?, ?)", (project, "now", "/exact/root")
        )
    adapter = CodebaseMemoryIndex(executable="codebase-memory-mcp", cache_directory=tmp_path)
    monkeypatch.setattr(
        adapter,
        "_call",
        lambda tool, payload: {"projects": [{"name": project, "root_path": ""}]},
    )

    assert adapter.list_projects() == [{"name": project, "root_path": "/exact/root"}]
