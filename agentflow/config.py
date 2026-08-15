"""Public runtime configuration for the enrolled repository fleet."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agentflow.loop import RepoConfig


class ConfigurationError(ValueError):
    """The runtime configuration cannot safely drive AgentFlow."""


@dataclass(frozen=True)
class RuntimeConfig:
    repositories: tuple[RepoConfig, ...]
    workspace_repositories: tuple[RepoConfig, ...]
    path: Path


def default_config_path() -> Path:
    configured = os.environ.get("AGENTFLOW_CONFIG")
    if configured:
        return Path(configured).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser()
    return config_home / "agentflow" / "config.toml"


def load_config(path: str | Path | None = None) -> RuntimeConfig:
    source = Path(path).expanduser() if path is not None else default_config_path()
    try:
        document = tomllib.loads(source.read_text())
    except FileNotFoundError as exc:
        raise ConfigurationError(
            f"configuration not found: {source} "
            "(pass --config or set AGENTFLOW_CONFIG)"
        ) from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(f"cannot read configuration {source}: {exc}") from exc

    entries = document.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise ConfigurationError(
            f"{source}: define at least one [[repositories]] entry"
        )

    repositories: list[RepoConfig] = []
    workspace_repositories: list[RepoConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, start=1):
        label = f"{source}: repositories entry {index}"
        if not isinstance(entry, dict):
            raise ConfigurationError(f"{label} must be a TOML table")
        unknown = set(entry) - {"repo", "workdir", "workspace"}
        if unknown:
            raise ConfigurationError(
                f"{label} has unknown fields: {', '.join(sorted(unknown))}"
            )
        repo = entry.get("repo")
        workdir_value = entry.get("workdir")
        workspace = entry.get("workspace", False)
        if (
            not isinstance(repo, str)
            or len(repo.split("/")) != 2
            or not all(repo.split("/"))
        ):
            raise ConfigurationError(f"{label}.repo must be an owner/name string")
        if repo in seen:
            raise ConfigurationError(f"{label}.repo duplicates {repo}")
        if not isinstance(workdir_value, str) or not workdir_value:
            raise ConfigurationError(f"{label}.workdir must be a path")
        if not isinstance(workspace, bool):
            raise ConfigurationError(f"{label}.workspace must be true or false")

        declared_workdir = Path(workdir_value).expanduser()
        if not declared_workdir.is_absolute():
            declared_workdir = source.parent / declared_workdir
        declared_workdir = declared_workdir.absolute()
        workdir = declared_workdir.resolve()
        if not workdir.is_dir():
            raise ConfigurationError(f"{label}.workdir is not a directory: {workdir}")

        configured_repo = RepoConfig(repo, str(workdir), str(declared_workdir))
        repositories.append(configured_repo)
        if workspace:
            workspace_repositories.append(configured_repo)
        seen.add(repo)

    return RuntimeConfig(
        tuple(repositories), tuple(workspace_repositories), source.resolve()
    )
