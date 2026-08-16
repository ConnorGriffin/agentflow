"""Dry-run-first cleanup of proven AgentFlow-owned disposable residue."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from agentflow.worktree_ownership import worktree_ownership


def _git(workdir: str, *args: str) -> subprocess.CompletedProcess:
    env = os.environ | {"GIT_OPTIONAL_LOCKS": "0"}
    return subprocess.run(
        ["git", "-C", workdir, *args], text=True, capture_output=True, env=env
    )


def _registered_worktrees(workdir: str) -> list[str] | None:
    result = _git(workdir, "worktree", "list", "--porcelain", "-z")
    if result.returncode != 0:
        return None
    return [
        field.removeprefix("worktree ")
        for field in result.stdout.split("\0")
        if field.startswith("worktree ")
    ]


def _clean(path: str) -> bool:
    result = _git(path, "status", "--porcelain", "--untracked-files=all")
    return result.returncode == 0 and not result.stdout.strip()


def _known_probe_directories(path: str) -> list[Path]:
    from agentflow.provider_skills import NATIVE_DISCOVERY_SKILL, _NATIVE_DISCOVERY_FIXTURE

    found = []
    for location in (".agents", ".claude"):
        probe = Path(path) / location / "skills" / NATIVE_DISCOVERY_SKILL
        skill = probe / "SKILL.md"
        try:
            exact = (
                not probe.is_symlink()
                and probe.is_dir()
                and not skill.is_symlink()
                and skill.is_file()
                and skill.read_text() == _NATIVE_DISCOVERY_FIXTURE
                and {item.name for item in probe.iterdir()} == {"SKILL.md"}
            )
        except OSError:
            exact = False
        if exact:
            found.append(probe)
    return found


def _classify_worktree(
    workdir: str, path: str, live_sources: set[str], held_sources: set[str],
    state_available: bool = True,
) -> dict:
    from agentflow.runner import _worktree_is_active, _worktree_is_locked

    record = {"action": "remove-worktree", "path": path, "eligible": False}
    real = os.path.realpath(path)
    if real == os.path.realpath(workdir):
        return record | {"reason": "retained", "applied": False}
    ownership = worktree_ownership(path)
    if ownership is None:
        return record | {"reason": "unknown-owned", "applied": False}
    if real in live_sources or _worktree_is_active(Path(path)):
        return record | {"reason": "live", "applied": False}
    if real in held_sources:
        return record | {"reason": "held", "applied": False}
    if not state_available:
        return record | {"reason": "unreachable", "applied": False}
    if not ownership["disposable"] or _worktree_is_locked(Path(path)):
        return record | {"reason": "retained", "applied": False}
    if not _clean(path):
        return record | {"reason": "dirty", "applied": False}
    return record | {"eligible": True, "reason": "inactive-clean-owned", "applied": False}


def maintain(
    repositories,
    *,
    apply: bool = False,
    live_sources=(),
    held_sources=(),
    index=None,
    state_available: bool = True,
) -> list[dict]:
    """Inventory the configured fleet completely, then apply still-eligible actions."""
    live = {os.path.realpath(path) for path in live_sources}
    held = {os.path.realpath(path) for path in held_sources}
    records: list[dict] = []
    for repository in repositories:
        paths = _registered_worktrees(repository.workdir)
        if paths is None:
            records.append({
                "action": "inventory-worktrees", "path": repository.workdir,
                "eligible": False, "reason": "unreachable", "applied": False,
            })
            continue
        for path in paths:
            if not Path(path).exists():
                records.append({
                    "action": "prune-registration", "path": path, "eligible": True,
                    "reason": "missing", "applied": False,
                })
                continue
            worktree = _classify_worktree(
                repository.workdir, path, live, held, state_available
            )
            records.append(worktree)
            if _known_probe_directories(path):
                records.append({
                    "action": "remove-probe", "path": path,
                    "eligible": worktree["eligible"],
                    "reason": "obsolete-probe" if worktree["eligible"] else worktree["reason"],
                    "applied": False,
                })

    removable = {
        os.path.realpath(record["path"])
        for record in records
        if record["action"] == "remove-worktree" and record["eligible"]
    }
    if index is not None:
        try:
            projects = index.list_projects()
        except Exception:
            projects = None
        if projects is None:
            records.append({
                "action": "delete-index", "project": "", "eligible": False,
                "reason": "unreachable", "applied": False,
            })
        else:
            for project in projects:
                name = project.get("name") if isinstance(project, dict) else None
                root = project.get("root_path") if isinstance(project, dict) else None
                record = {
                    "action": "delete-index", "project": name or "",
                    "eligible": False, "reason": "unreachable", "applied": False,
                }
                if isinstance(name, str) and name and isinstance(root, str) and root:
                    record["root"] = root
                    if not Path(root).exists():
                        record |= {"eligible": True, "reason": "missing"}
                    elif os.path.realpath(root) in removable:
                        record |= {"eligible": True, "reason": "worktree-pruned"}
                    else:
                        record["reason"] = "reachable"
                records.append(record)

    if not apply:
        return records

    workdirs = {os.path.realpath(repo.workdir): repo.workdir for repo in repositories}
    for record in records:
        if not record["eligible"]:
            continue
        if record["action"] == "delete-index":
            root = record["root"]
            if record["reason"] == "missing":
                still_eligible = not Path(root).exists()
            else:
                still_eligible = not Path(root).exists() and any(
                    item["action"] == "remove-worktree"
                    and os.path.realpath(item.get("path", "")) == os.path.realpath(root)
                    and item["applied"]
                    for item in records
                )
            if still_eligible:
                try:
                    record["applied"] = index.delete_project(record["project"]) is True
                except Exception:
                    pass
            continue
        if record["action"] == "prune-registration":
            path = record["path"]
            if Path(path).exists():
                continue
            owner = next(
                (workdir for workdir in workdirs.values()
                 if path in (_registered_worktrees(workdir) or ())),
                None,
            )
            if owner is None:
                continue
            _git(owner, "worktree", "remove", "--force", path)
            record["applied"] = path not in (_registered_worktrees(owner) or ())
            continue
        if record["action"] == "remove-probe":
            path = record["path"]
            if not Path(path).exists():
                record["applied"] = any(
                    item["action"] == "remove-worktree"
                    and item.get("path") == path and item["applied"]
                    for item in records
                )
                continue
            owner = next(
                (workdir for root, workdir in workdirs.items() if root != os.path.realpath(path)
                 and path in (_registered_worktrees(workdir) or ())),
                None,
            )
            if owner is None or not _classify_worktree(
                owner, path, live, held, state_available
            )["eligible"]:
                continue
            probes = _known_probe_directories(path)
            for probe in probes:
                shutil.rmtree(probe)
            record["applied"] = bool(probes) and not _known_probe_directories(path)
            continue
        if record["action"] != "remove-worktree":
            continue
        path = record["path"]
        owner = next(
            (workdir for root, workdir in workdirs.items() if root != os.path.realpath(path)
             and path in (_registered_worktrees(workdir) or ())),
            None,
        )
        if owner is None:
            continue
        current = _classify_worktree(owner, path, live, held, state_available)
        if not current["eligible"]:
            continue
        removed = _git(owner, "worktree", "remove", path)
        record["applied"] = removed.returncode == 0
    return records


class CodebaseMemoryIndex:
    """The installed Codebase Memory single-tool CLI as a narrow maintenance adapter."""

    def __init__(
        self, executable: str | None = None, cache_directory: str | Path | None = None
    ) -> None:
        self.executable = executable or shutil.which("codebase-memory-mcp")
        self.cache_directory = Path(
            cache_directory
            or Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            / "codebase-memory-mcp"
        )

    def _call(self, tool: str, payload: dict) -> dict | None:
        if not self.executable:
            return None
        result = subprocess.run(
            [self.executable, "cli", tool, json.dumps(payload, separators=(",", ":"))],
            text=True, capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) and "error" not in value else None

    def list_projects(self):
        result = self._call("list_projects", {})
        projects = result.get("projects") if result else None
        if not isinstance(projects, list):
            return None
        enriched = []
        for project in projects:
            if not isinstance(project, dict):
                enriched.append(project)
                continue
            item = dict(project)
            if not item.get("root_path"):
                item["root_path"] = self._cached_root(item.get("name")) or ""
            enriched.append(item)
        return enriched

    def _cached_root(self, project: object) -> str | None:
        if not isinstance(project, str) or not project or Path(project).name != project:
            return None
        database = self.cache_directory / f"{project}.db"
        if database.is_symlink() or not database.is_file():
            return None
        try:
            with sqlite3.connect(
                f"file:{database}?mode=ro&immutable=1", uri=True
            ) as connection:
                rows = connection.execute(
                    "SELECT root_path FROM projects WHERE name = ?", (project,)
                ).fetchall()
        except sqlite3.Error:
            return None
        if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
            return None
        return rows[0][0]

    def delete_project(self, project: str) -> bool:
        return self._call("delete_project", {"project": project}) is not None


def maintenance_sources(repositories) -> tuple[set[str], set[str], bool]:
    """Read live and held worktree ownership from the one durable coordinator store."""
    from agentflow.coordinator import tracer
    from agentflow.coordinator.record import HELD
    from agentflow.coordinator.store import default_store_path

    store = Path(default_store_path())
    if not store.exists():
        return set(), set(), True
    try:
        records = tracer.load_records(store)
    except Exception:
        return set(), set(), False
    repository_names = {repository.repo for repository in repositories}
    live = {
        os.path.realpath(record.source)
        for record in records
        if record.repo in repository_names and record.source and not record.retired
        and record.state != HELD
    }
    held = {
        os.path.realpath(record.source)
        for record in records
        if record.repo in repository_names and record.source and not record.retired
        and record.state == HELD
    }
    return live, held, True
