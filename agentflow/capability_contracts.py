"""Pinned stage methodology contracts and their fail-closed inspection.

The small public interface is ``requirements_for`` and ``preflight``.  Prompt rendering
declares direct invocations; this module expands their transitive contract edges once, so a
doctor report and dispatch admission cannot drift into separate registries.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json
import re
import tomllib
from pathlib import Path
from typing import Iterable, Literal
import shutil

from agentflow.provider_skills import provider_skill_status
from agentflow.runtime_contracts import playwright_runtime_status


@dataclass(frozen=True)
class ContractRequirement:
    id: str
    version: str
    runtime: bool = False
    dependencies: tuple["ContractRequirement", ...] = ()


@dataclass(frozen=True, slots=True)
class CapabilityContractFact:
    contract_id: str
    contract_version: str
    runtime: bool


@dataclass(frozen=True, slots=True)
class CapabilityReadyFact:
    schema: Literal["capability-ready-v1"]
    status: Literal["ready"]
    stage: str
    provider: Literal["claude", "codex"]
    manifest_digest: str
    contracts: tuple[CapabilityContractFact, ...]
    capability_digest: str
    capability_id: str


@dataclass(frozen=True)
class CapabilityPreflightResult:
    stage: str
    provider: str
    contracts: tuple[ContractRequirement, ...]
    state: str
    evidence: tuple[str, ...]
    repair_command: str
    ready_fact: CapabilityReadyFact | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"


_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_FAILURE_STATES = frozenset({"missing", "drifted", "incompatible"})


def _canonical_bytes(value: object) -> bytes:
    """Encode the closed readiness payload without broad JSON coercion."""
    def valid(item: object) -> bool:
        if type(item) in {str, bool} or item is None:
            return True
        if isinstance(item, list):
            return all(valid(child) for child in item)
        if isinstance(item, dict):
            return all(type(key) is str and valid(child) for key, child in item.items())
        return False

    if not valid(value):
        raise TypeError("capability readiness payload contains an unsupported value")
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")


def _contract_value(contract: CapabilityContractFact) -> dict[str, object]:
    return {
        "contract_id": contract.contract_id,
        "contract_version": contract.contract_version,
        "runtime": contract.runtime,
    }


def _ready_payload(
    *, stage: str, provider: str, manifest_digest: str,
    contracts: tuple[CapabilityContractFact, ...],
) -> dict[str, object]:
    return {
        "schema": "capability-ready-v1",
        "status": "ready",
        "stage": stage,
        "provider": provider,
        "manifest_digest": manifest_digest,
        "contracts": [_contract_value(contract) for contract in contracts],
    }


def _checked_requirements(requirements: tuple[ContractRequirement, ...]) -> tuple[ContractRequirement, ...]:
    """Return the unique direct and transitive requirements preflight must inspect."""
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

    for requirement in requirements:
        add(requirement)
    return tuple(seen.values())


def _ready_fact(
    stage: str, provider: str, manifest_bytes: bytes,
    requirements: tuple[ContractRequirement, ...],
) -> CapabilityReadyFact:
    contracts = tuple(sorted(
        (CapabilityContractFact(item.id, item.version, item.runtime) for item in requirements),
        key=lambda item: _canonical_bytes(_contract_value(item)),
    ))
    manifest_digest = sha256(manifest_bytes).hexdigest()
    payload = _ready_payload(
        stage=stage, provider=provider, manifest_digest=manifest_digest, contracts=contracts,
    )
    capability_digest = sha256(_canonical_bytes(payload)).hexdigest()
    return CapabilityReadyFact(
        "capability-ready-v1", "ready", stage, provider, manifest_digest, contracts,
        capability_digest, f"capability-ready-v1:{capability_digest}",
    )


def validate_capability_ready_fact(value: object) -> bool:
    """Check a readiness fact's closed representation and self-authenticated identity."""
    if type(value) is not CapabilityReadyFact:
        return False
    if (type(value.schema) is not str or value.schema != "capability-ready-v1"
            or type(value.status) is not str or value.status != "ready"
            or type(value.provider) is not str or value.provider not in {"claude", "codex"}
            or type(value.stage) is not str or not _TOKEN.fullmatch(value.stage)
            or type(value.manifest_digest) is not str or not _DIGEST.fullmatch(value.manifest_digest)
            or type(value.capability_digest) is not str or not _DIGEST.fullmatch(value.capability_digest)
            or type(value.capability_id) is not str
            or value.capability_id != f"capability-ready-v1:{value.capability_digest}"
            or type(value.contracts) is not tuple):
        return False
    if any(
        type(item) is not CapabilityContractFact
        or type(item.contract_id) is not str or not _TOKEN.fullmatch(item.contract_id)
        or type(item.contract_version) is not str or not _TOKEN.fullmatch(item.contract_version)
        or type(item.runtime) is not bool
        for item in value.contracts
    ):
        return False
    contract_bytes = tuple(_canonical_bytes(_contract_value(item)) for item in value.contracts)
    if len(set(contract_bytes)) != len(contract_bytes) or contract_bytes != tuple(sorted(contract_bytes)):
        return False
    payload = _ready_payload(
        stage=value.stage, provider=value.provider, manifest_digest=value.manifest_digest,
        contracts=value.contracts,
    )
    return sha256(_canonical_bytes(payload)).hexdigest() == value.capability_digest


def requirements_for(invocations: Iterable[object], context: dict[str, object]) -> tuple[ContractRequirement, ...]:
    """Expand direct, conditional, and transitive prompt requirements deterministically."""
    requirements: list[ContractRequirement] = []
    for invocation in invocations:
        condition = getattr(invocation, "condition", None)
        if condition is not None and not context.get(condition, False):
            continue
        requirements.append(getattr(invocation, "requirement"))
    return _checked_requirements(tuple(requirements))


def preflight(root: str | Path, stage: str, provider: str, requirements: tuple[ContractRequirement, ...]) -> CapabilityPreflightResult:
    """Inspect only pinned project-local destinations; ambient skills never count as evidence."""
    root = Path(root)
    manifest_bytes = files("agentflow").joinpath("capabilities.toml").read_bytes()
    manifest = tomllib.loads(manifest_bytes.decode("utf-8"))
    checked_requirements = _checked_requirements(requirements)
    specs = {item["id"]: item for item in manifest["capabilities"]}
    evidence: list[str] = []
    states: list[str] = []
    native_failure = False
    other_failure = False
    if provider not in {"claude", "codex"} or shutil.which(provider) is None:
        states.append("incompatible")
        other_failure = True
        evidence.append(f"{provider}: selected provider runtime is unavailable")
    for requirement in checked_requirements:
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
                states.append(status if status in _FAILURE_STATES else "incompatible")
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
            states.append(status if status in _FAILURE_STATES else "incompatible")
            if "native-discovery receipt" in detail:
                native_failure = True
            else:
                other_failure = True
        evidence.append(f"{requirement.id}@{requirement.version}: {detail}")
    state = next((item for item in ("missing", "drifted", "incompatible") if item in states), "ready")
    ready_fact = (
        _ready_fact(stage, provider, manifest_bytes, checked_requirements)
        if state == "ready" else None
    )
    return CapabilityPreflightResult(
        stage=stage, provider=provider, contracts=requirements, state=state,
        evidence=tuple(evidence) or ("pinned project-local contracts present",),
        repair_command=(
            f"agentflow capability-probe --repo {root} --provider {provider}"
            if native_failure and not other_failure
            else f"agentflow enroll {root} --apply"
        ),
        ready_fact=ready_fact,
    )
