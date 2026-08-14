from dataclasses import fields, replace
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow.capability_contracts import (
    CapabilityContractFact,
    CapabilityReadyFact,
    ContractRequirement,
    preflight,
    validate_capability_ready_fact,
)


def test_ready_preflight_returns_a_self_validating_fact_for_checked_closure(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/codex")
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status",
        lambda *_args: ("ok", "provider discovery contract intact"),
    )
    requirement = ContractRequirement(
        "codebase-design",
        "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666",
        dependencies=(ContractRequirement("domain-modeling", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"),),
    )

    result = preflight(tmp_path, "build", "codex", (requirement,))

    assert result.ready
    assert result.ready_fact is not None
    assert result.contracts == (requirement,)
    assert result.ready_fact.stage == "build"
    assert result.ready_fact.provider == "codex"
    assert [contract.contract_id for contract in result.ready_fact.contracts] == [
        "codebase-design", "domain-modeling"
    ]
    assert validate_capability_ready_fact(result.ready_fact)


def test_ready_identity_changes_for_stage_provider_and_exact_manifest_bytes(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/provider")
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status", lambda *_args: ("ok", "ok")
    )
    requirement = (ContractRequirement("tdd", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"),)
    original = preflight(tmp_path, "build", "codex", requirement).ready_fact
    changed_stage = preflight(tmp_path, "review", "codex", requirement).ready_fact
    changed_provider = preflight(tmp_path, "build", "claude", requirement).ready_fact
    manifest_bytes = files("agentflow").joinpath("capabilities.toml").read_bytes() + b"\n"
    monkeypatch.setattr(
        "agentflow.capability_contracts.files",
        lambda _package: SimpleNamespace(
            joinpath=lambda _name: SimpleNamespace(read_bytes=lambda: manifest_bytes)
        ),
    )
    changed_manifest = preflight(tmp_path, "build", "codex", requirement).ready_fact

    assert all(validate_capability_ready_fact(item) for item in (
        original, changed_stage, changed_provider, changed_manifest,
    ))
    assert original is not None
    assert changed_stage is not None and changed_stage.capability_id != original.capability_id
    assert changed_provider is not None and changed_provider.capability_id != original.capability_id
    assert changed_manifest is not None and changed_manifest.capability_id != original.capability_id


def test_ready_fact_pins_the_public_digest_vector_and_content_free_shape(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/codex")
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status", lambda *_args: ("ok", "ok")
    )
    result = preflight(
        tmp_path, "build", "codex", (
            ContractRequirement(
                "codebase-design", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666",
                dependencies=(ContractRequirement(
                    "domain-modeling", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"
                ),),
            ),
        )
    )

    assert result.ready_fact is not None
    assert result.ready_fact.manifest_digest == "cba84e63be53884e6ed566a534883912f7d22156aad7e4a5590515140d18fcad"
    assert result.ready_fact.capability_digest == "92200c70593d97ebddecb03362b740ef229ce5bb62f76e08de5355a3f137c3bf"
    assert result.ready_fact.capability_id == "capability-ready-v1:92200c70593d97ebddecb03362b740ef229ce5bb62f76e08de5355a3f137c3bf"
    assert [field.name for field in fields(CapabilityReadyFact)] == [
        "schema", "status", "stage", "provider", "manifest_digest", "contracts",
        "capability_digest", "capability_id",
    ]
    assert [field.name for field in fields(CapabilityContractFact)] == [
        "contract_id", "contract_version", "runtime",
    ]


@pytest.mark.parametrize(
    "change",
    (
        lambda fact: replace(fact, schema="capability-ready-v2"),
        lambda fact: replace(fact, status="held"),
        lambda fact: replace(fact, stage="review"),
        lambda fact: replace(fact, provider="claude"),
        lambda fact: replace(fact, manifest_digest="0" * 64),
        lambda fact: replace(fact, contracts=tuple(reversed(fact.contracts))),
        lambda fact: replace(fact, contracts=fact.contracts + (fact.contracts[0],)),
        lambda fact: replace(fact, capability_digest="0" * 64),
        lambda fact: replace(fact, capability_id="capability-ready-v1:" + "0" * 64),
    ),
)
def test_validator_rejects_each_changed_or_noncanonical_fact_field(change):
    fact = CapabilityReadyFact(
        "capability-ready-v1", "ready", "build", "codex",
        "cba84e63be53884e6ed566a534883912f7d22156aad7e4a5590515140d18fcad",
        (CapabilityContractFact("codebase-design", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666", False),
         CapabilityContractFact("domain-modeling", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666", False)),
        "92200c70593d97ebddecb03362b740ef229ce5bb62f76e08de5355a3f137c3bf",
        "capability-ready-v1:92200c70593d97ebddecb03362b740ef229ce5bb62f76e08de5355a3f137c3bf",
    )

    assert validate_capability_ready_fact(fact)
    assert not validate_capability_ready_fact(change(fact))


@pytest.mark.parametrize(
    "fact",
    (
        object(),
        CapabilityReadyFact("capability-ready-v1", "ready", "bad?", "codex", "a" * 64, (), "0" * 64, "capability-ready-v1:" + "0" * 64),
        CapabilityReadyFact("capability-ready-v1", "ready", "build", "other", "A" * 64, (), "0" * 64, "capability-ready-v1:" + "0" * 64),
        CapabilityReadyFact("capability-ready-v1", "ready", "build", "codex", "a" * 63, (), "0" * 64, "capability-ready-v1:" + "0" * 64),
        CapabilityReadyFact("capability-ready-v1", "ready", "build", "codex", "a" * 64, (CapabilityContractFact("tdd", "v0.3.0", 0),), "0" * 64, "capability-ready-v1:" + "0" * 64),
    ),
)
def test_validator_rejects_malformed_types_literals_and_digests(fact):
    assert not validate_capability_ready_fact(fact)


@pytest.mark.parametrize("status", ("missing", "drifted", "incompatible", "malformed"))
def test_nonready_provider_discovery_results_never_carry_a_ready_fact(tmp_path, monkeypatch, status):
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: "/bin/codex")
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status", lambda *_args: (status, "failed")
    )

    result = preflight(
        tmp_path, "build", "codex",
        (ContractRequirement("tdd", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"),),
    )

    assert not result.ready
    assert result.ready_fact is None


def test_unavailable_provider_never_carries_a_ready_fact(tmp_path, monkeypatch):
    monkeypatch.setattr("agentflow.capability_contracts.shutil.which", lambda _name: None)
    monkeypatch.setattr(
        "agentflow.capability_contracts.provider_skill_status", lambda *_args: ("ok", "ok")
    )

    result = preflight(
        tmp_path, "build", "codex",
        (ContractRequirement("tdd", "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"),),
    )

    assert result.state == "incompatible"
    assert result.ready_fact is None
