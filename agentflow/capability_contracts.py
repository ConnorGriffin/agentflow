"""Pinned stage methodology contracts and their fail-closed inspection.

The small public interface is ``requirements_for`` and ``preflight``.  Prompt rendering
declares direct invocations; this module expands their transitive contract edges once, so a
doctor report and dispatch admission cannot drift into separate registries.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from importlib.resources import files
import tomllib
from pathlib import Path
from typing import Iterable
import shutil


@dataclass(frozen=True)
class ContractRequirement:
    id: str
    version: str
    runtime: bool = False


@dataclass(frozen=True)
class CapabilityPreflightResult:
    stage: str
    provider: str
    contracts: tuple[ContractRequirement, ...]
    state: str
    evidence: tuple[str, ...]
    repair_command: str

    @property
    def ready(self) -> bool:
        return self.state == "ready"


def requirements_for(invocations: Iterable[object], context: dict[str, object]) -> tuple[ContractRequirement, ...]:
    """Expand direct prompt declarations, conditionals, and transitive edges deterministically."""
    seen: dict[str, ContractRequirement] = {}
    for invocation in invocations:
        condition = getattr(invocation, "condition", None)
        if condition is not None and not context.get(condition, False):
            continue
        for requirement in getattr(invocation, "requirements", ()):
            seen.setdefault(requirement.id, requirement)
    return tuple(seen.values())


def preflight(root: str | Path, stage: str, provider: str, requirements: tuple[ContractRequirement, ...]) -> CapabilityPreflightResult:
    """Inspect only pinned project-local destinations; ambient skills never count as evidence."""
    root = Path(root)
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    specs = {item["id"]: item for item in manifest["capabilities"]}
    evidence: list[str] = []
    states: list[str] = []
    if provider not in {"claude", "codex"} or shutil.which(provider) is None:
        states.append("incompatible")
        evidence.append(f"{provider}: selected provider runtime is unavailable")
    for requirement in requirements:
        if requirement.runtime:
            runtime = manifest.get("playwright", {})
            pinned_version = runtime.get("version")
            if requirement.id != "playwright" or requirement.version != pinned_version:
                states.append("incompatible")
                evidence.append(
                    f"{requirement.id}@{requirement.version}: manifest pins "
                    f"playwright@{pinned_version}"
                )
                continue
            from agentflow.enroll import playwright_runtime_status

            status, detail = playwright_runtime_status(
                root,
                version=pinned_version,
                node_minimum=runtime["node_minimum"],
                manifest=manifest,
            )
            if status != "ok":
                states.append(status)
                evidence.append(f"{requirement.id}@{requirement.version}: {detail}")
            continue
        destinations = [root / ".agents" / "skills" / requirement.id,
                        root / ".claude" / "skills" / requirement.id]
        if not all(destination.is_dir() for destination in destinations):
            states.append("missing")
            evidence.append(f"{requirement.id}@{requirement.version}: project-local destination missing")
            continue
        spec = specs.get(requirement.id, {})
        expected = {item["path"]: item["sha256"] for item in spec.get("files", [])}
        for destination in destinations:
            actual = {path.relative_to(destination).as_posix() for path in destination.rglob("*")
                      if path.is_file()}
            if actual != set(expected) or any(
                hashlib.sha256((destination / path).read_bytes()).hexdigest() != digest
                for path, digest in expected.items()
            ):
                states.append("drifted")
                evidence.append(f"{requirement.id}@{requirement.version}: pinned files drifted")
                break
    state = next((item for item in ("missing", "drifted", "incompatible") if item in states), "ready")
    return CapabilityPreflightResult(
        stage=stage, provider=provider, contracts=requirements, state=state,
        evidence=tuple(evidence) or ("pinned project-local contracts present",),
        repair_command=f"agentflow enroll {root} --apply",
    )
