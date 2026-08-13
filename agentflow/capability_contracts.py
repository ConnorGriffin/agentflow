"""Pinned stage methodology contracts and their fail-closed inspection.

The small public interface is ``requirements_for`` and ``preflight``.  Prompt rendering
declares direct invocations; this module expands their transitive contract edges once, so a
doctor report and dispatch admission cannot drift into separate registries.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
import tomllib
from pathlib import Path
from typing import Iterable
import shutil

from agentflow.provider_skills import provider_skill_status
from agentflow.runtime_contracts import playwright_runtime_status


@dataclass(frozen=True)
class ContractRequirement:
    id: str
    version: str
    runtime: bool = False
    dependencies: tuple["ContractRequirement", ...] = ()


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
    """Expand direct, conditional, and transitive prompt requirements deterministically."""
    seen: dict[str, ContractRequirement] = {}
    visiting: set[str] = set()

    def add(requirement: ContractRequirement) -> None:
        if requirement.id in visiting:
            raise ValueError(f"cyclic capability dependency at {requirement.id}")
        prior = seen.get(requirement.id)
        if prior is not None:
            if (prior.version, prior.runtime) != (requirement.version, requirement.runtime):
                raise ValueError(f"conflicting capability requirement for {requirement.id}")
            return
        visiting.add(requirement.id)
        seen[requirement.id] = requirement
        for dependency in requirement.dependencies:
            add(dependency)
        visiting.remove(requirement.id)

    for invocation in invocations:
        condition = getattr(invocation, "condition", None)
        if condition is not None and not context.get(condition, False):
            continue
        add(getattr(invocation, "requirement"))
    return tuple(seen.values())


def preflight(root: str | Path, stage: str, provider: str, requirements: tuple[ContractRequirement, ...]) -> CapabilityPreflightResult:
    """Inspect only pinned project-local destinations; ambient skills never count as evidence."""
    root = Path(root)
    manifest = tomllib.loads(files("agentflow").joinpath("capabilities.toml").read_text())
    specs = {item["id"]: item for item in manifest["capabilities"]}
    evidence: list[str] = []
    states: list[str] = []
    native_failure = False
    other_failure = False
    if provider not in {"claude", "codex"} or shutil.which(provider) is None:
        states.append("incompatible")
        other_failure = True
        evidence.append(f"{provider}: selected provider runtime is unavailable")
    for requirement in requirements:
        if requirement.runtime:
            runtime = manifest.get("playwright", {})
            pinned_version = runtime.get("version")
            if requirement.id != "playwright" or requirement.version != pinned_version:
                states.append("incompatible")
                other_failure = True
                evidence.append(
                    f"{requirement.id}@{requirement.version}: manifest pins "
                    f"playwright@{pinned_version}"
                )
                continue
            status, detail = playwright_runtime_status(
                root,
                version=pinned_version,
                node_minimum=runtime["node_minimum"],
                manifest=manifest,
                provider=provider,
            )
            if status != "ok":
                states.append(status)
                other_failure = True
                evidence.append(f"{requirement.id}@{requirement.version}: {detail}")
            continue
        spec = specs.get(requirement.id)
        if spec is None:
            states.append("incompatible")
            other_failure = True
            evidence.append(f"{requirement.id}@{requirement.version}: no manifest contract")
            continue
        if spec.get("version") != requirement.version:
            states.append("incompatible")
            other_failure = True
            evidence.append(
                f"{requirement.id}@{requirement.version}: manifest pins "
                f"{spec.get('version', 'no version')}"
            )
            continue
        declared_dependencies = tuple(spec.get("dependencies", ()))
        required_dependencies = tuple(item.id for item in requirement.dependencies)
        if declared_dependencies != required_dependencies:
            states.append("incompatible")
            other_failure = True
            evidence.append(
                f"{requirement.id}@{requirement.version}: dependency contract is incompatible"
            )
            continue
        status, detail = provider_skill_status(root, provider, spec)
        if status != "ok":
            states.append(status)
            if "native-discovery receipt" in detail:
                native_failure = True
            else:
                other_failure = True
        evidence.append(f"{requirement.id}@{requirement.version}: {detail}")
    state = next((item for item in ("missing", "drifted", "incompatible") if item in states), "ready")
    return CapabilityPreflightResult(
        stage=stage, provider=provider, contracts=requirements, state=state,
        evidence=tuple(evidence) or ("pinned project-local contracts present",),
        repair_command=(
            f"agentflow capability-probe --repo {root} --provider {provider}"
            if native_failure and not other_failure
            else f"agentflow enroll {root} --apply"
        ),
    )
