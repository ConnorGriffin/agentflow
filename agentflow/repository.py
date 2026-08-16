"""Shared repository configuration value used by config loading and dispatch."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoConfig:
    repo: str        # "owner/name" on GitHub
    workdir: str     # local main checkout
    declared_workdir: str | None = None  # non-resolved config path; recovery rejects symlinks
