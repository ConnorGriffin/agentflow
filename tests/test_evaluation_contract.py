from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import shutil

import pytest

from agentflow.evaluation_contract import (
    EvaluationContractError,
    load_evaluation_bundle,
    load_evaluation_contract,
)


ROOT = Path(__file__).parents[1]
CONTRACT = ROOT / "docs/evaluation/contract-v1.json"
CANDIDATE = ROOT / "docs/evaluation/design/contract-v1.candidate.json"
MODULE = ROOT / "agentflow/evaluation_semantics_v1.py"
REPORT = ROOT / "docs/evaluation/design/contract-v1.conformance.json"
FIXTURES = ROOT / "tests/fixtures/evaluation/contract-v1"
CONTRACT_SHA256 = "53359f35de57047441defa76a477564580b956f968ab6425356cca3a1c5a8409"
MODULE_SHA256 = "185f41a5e4549cc1ccbc4615af5846c3ed0f95285790d193e1b2f43aa3dc8554"


@pytest.fixture(scope="module")
def contract():
    return load_evaluation_contract(CONTRACT)


def _case_files() -> list[Path]:
    return sorted(FIXTURES.glob("*/*.json"))


def _copy_contract_root(tmp_path: Path) -> Path:
    target_contract = tmp_path / "docs/evaluation/contract-v1.json"
    target_module = tmp_path / "agentflow/evaluation_semantics_v1.py"
    target_contract.parent.mkdir(parents=True)
    target_module.parent.mkdir(parents=True)
    shutil.copyfile(CONTRACT, target_contract)
    shutil.copyfile(MODULE, target_module)
    return target_contract


def _canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")


def _thaw(value: object) -> object:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _write_artifact(root: Path, relative: str, value: object) -> tuple[PurePosixPath, str]:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _canonical(value)
    path.write_bytes(data)
    return PurePosixPath(relative), sha256(data).hexdigest()


def test_pinned_bytes_and_closed_fixture_directory_are_exact():
    report = json.loads(REPORT.read_text())
    declared = {
        f'{case["classification"]}/{case["case_id"]}.json'
        for case in report["semantic_cases"]
    }
    actual = {path.relative_to(FIXTURES).as_posix() for path in _case_files()}

    assert CONTRACT.read_bytes() == CANDIDATE.read_bytes()
    assert sha256(CONTRACT.read_bytes()).hexdigest() == CONTRACT_SHA256
    assert sha256(MODULE.read_bytes()).hexdigest() == MODULE_SHA256
    assert actual == declared
    assert sum(name.startswith("positive/") for name in actual) == 18
    assert sum(name.startswith("negative/") for name in actual) == 15


def test_every_declared_fixture_calls_exact_evaluate_v1_and_returns_immutable_result(contract):
    calls: list[tuple[str, object]] = []
    exact = contract._evaluate_v1

    def recording_evaluate(candidate, operation_id, input_value):
        calls.append((operation_id, input_value))
        return exact(candidate, operation_id, input_value)

    instrumented = replace(contract, _evaluate_v1=recording_evaluate)
    cases = [json.loads(path.read_text()) for path in _case_files()]
    results = [instrumented.evaluate(case["operation_id"], case["input_value"]) for case in cases]

    assert len(calls) == len(cases) == 33
    assert [operation for operation, _value in calls] == [case["operation_id"] for case in cases]
    assert [_thaw(result) for result in results] == [case["expected_result"] for case in cases]
    with pytest.raises(TypeError):
        results[0]["status"] = "changed"


def test_negative_fixture_mutations_cover_each_declared_rejection_code(contract):
    candidate = json.loads(CONTRACT.read_text())
    expected_codes = set(candidate["semantic_errors"].values())
    observed_codes: set[str] = set()
    for path in sorted((FIXTURES / "negative").glob("*.json")):
        case = json.loads(path.read_text())
        result = contract.evaluate(case["operation_id"], case["input_value"])
        assert result["status"] == "error"
        assert result["code"] in case["coverage"]
        observed_codes.add(result["code"])
    assert observed_codes == expected_codes


def test_adr_606_missingness_and_canonical_lineage_branches(contract):
    cases = {path.stem: json.loads(path.read_text()) for path in _case_files()}
    unavailable = contract.evaluate(cases["unavailable-arm"]["operation_id"], cases["unavailable-arm"]["input_value"])
    reported = contract.evaluate(cases["reported-optional-null"]["operation_id"], cases["reported-optional-null"]["input_value"])
    forged = contract.evaluate(cases["forged-lineage"]["operation_id"], cases["forged-lineage"]["input_value"])

    assert _thaw(unavailable) == cases["unavailable-arm"]["expected_result"]
    assert _thaw(reported) == cases["reported-optional-null"]["expected_result"]
    assert forged["code"] == "EVAL_V1_LINEAGE"


def test_contract_values_are_immutable(contract):
    assert contract.contract_version == "evaluation-contract-v1"
    assert contract.semantic_module_path == PurePosixPath("agentflow/evaluation_semantics_v1.py")
    assert contract.semantic_module_sha256 == MODULE_SHA256
    with pytest.raises(TypeError):
        contract.operation_ids["schedule"] = "changed"
    with pytest.raises(FrozenInstanceError):
        contract.contract_version = "changed"


@pytest.mark.parametrize(
    ("mutation", "code"),
    (
        (lambda data: data[:-1] + b" \n", "E_CANONICAL"),
        (lambda data: b'{"artifact_registry":[],' + data[1:], "E_DUPLICATE_KEY"),
        (lambda data: data[:-2] + b',"unknown":null}\n', "E_DIGEST"),
    ),
    ids=("noncanonical", "duplicate-key", "unknown-field"),
)
def test_contract_mutations_fail_closed_before_module_load(tmp_path, mutation, code):
    copied = _copy_contract_root(tmp_path)
    copied.write_bytes(mutation(copied.read_bytes()))
    with pytest.raises(EvaluationContractError) as caught:
        load_evaluation_contract(copied)
    assert caught.value.code == code
    assert caught.value.basename == "contract-v1.json"


def test_module_mutation_substitution_wrong_root_and_symlink_fail_closed(tmp_path):
    copied = _copy_contract_root(tmp_path)
    module = tmp_path / "agentflow/evaluation_semantics_v1.py"
    module.write_bytes(module.read_bytes()[:-1] + b" ")
    with pytest.raises(EvaluationContractError) as mutated:
        load_evaluation_contract(copied)
    assert (mutated.value.code, mutated.value.basename) == ("E_DIGEST", "evaluation_semantics_v1.py")

    module.write_text("def evaluate_v1(contract, operation_id, input_value):\n    return {}\n")
    with pytest.raises(EvaluationContractError) as substituted:
        load_evaluation_contract(copied)
    assert substituted.value.code == "E_DIGEST"

    with pytest.raises(EvaluationContractError) as wrong_root:
        load_evaluation_contract(tmp_path / "contract-v1.json")
    assert wrong_root.value.code == "E_ROOT"

    copied.unlink()
    copied.symlink_to(CONTRACT)
    with pytest.raises(EvaluationContractError) as symlink:
        load_evaluation_contract(copied)
    assert symlink.value.code == "E_IO"
    assert str(tmp_path) not in str(symlink.value)


def test_bundle_derives_role_visibility_root_and_returns_no_raw_payload(contract, tmp_path):
    entry, _digest = _write_artifact(
        tmp_path,
        "docs/evaluation/v1/sources/source-a.json",
        {"entries": [{"digest": "0" * 64, "path": "source.txt"}], "root_digest": "1" * 64},
    )
    bundle = load_evaluation_bundle(contract, tmp_path, entry)

    assert bundle.entrypoint == entry
    assert len(bundle.artifacts) == 1
    artifact = bundle.artifacts[0]
    assert (artifact.kind, artifact.role_family, artifact.visibility) == ("source-bundle", "public-source", "public")
    assert artifact.artifact_root == PurePosixPath("docs/evaluation")
    with pytest.raises(TypeError):
        artifact.value["root_digest"] = "2" * 64


def test_bundle_rejects_cycles_digest_kind_path_symlink_nonregular_and_root(contract, tmp_path):
    index_path = "evaluation/v1/indexes/index-a.json"
    self_ref = {"digest": "0" * 64, "id": "index-a", "kind": "artifact-index", "path": index_path}
    entry, _digest = _write_artifact(
        tmp_path,
        index_path,
        {"children": [self_ref], "entries": [], "entry_count": 1, "group_id": "group-a", "group_kind": "run", "level": 0, "ordinal": 0},
    )
    with pytest.raises(EvaluationContractError) as cycle:
        load_evaluation_bundle(contract, tmp_path, entry)
    assert cycle.value.code == "E_REF_CYCLE"

    _write_artifact(
        tmp_path,
        "evaluation/v1/indexes/index-bool.json",
        {"children": [], "entries": [], "entry_count": 1, "group_id": "group-a", "group_kind": "run", "level": True, "ordinal": 0},
    )
    with pytest.raises(EvaluationContractError) as boolean_integer:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath("evaluation/v1/indexes/index-bool.json"))
    assert boolean_integer.value.code == "E_SCHEMA"

    bad_ref = {**self_ref, "kind": "source-bundle", "path": "evaluation/v1/indexes/index-c.json"}
    _write_artifact(
        tmp_path,
        "evaluation/v1/indexes/index-b.json",
        {"children": [], "entries": [bad_ref], "entry_count": 1, "group_id": "group-a", "group_kind": "run", "level": 0, "ordinal": 0},
    )
    with pytest.raises(EvaluationContractError) as kind:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath("evaluation/v1/indexes/index-b.json"))
    assert kind.value.code == "E_CROSS_REFERENCE"

    source_path = tmp_path / "docs/evaluation/v1/sources/source-a.json"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.symlink_to(CONTRACT)
    with pytest.raises(EvaluationContractError) as symlink:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath("docs/evaluation/v1/sources/source-a.json"))
    assert symlink.value.code == "E_IO"
    source_path.unlink()
    source_path.mkdir()
    with pytest.raises(EvaluationContractError) as nonregular:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath("docs/evaluation/v1/sources/source-a.json"))
    assert nonregular.value.code == "E_IO"

    linked_root = tmp_path.parent / f"{tmp_path.name}-link"
    linked_root.symlink_to(tmp_path, target_is_directory=True)
    try:
        with pytest.raises(EvaluationContractError) as root:
            load_evaluation_bundle(contract, linked_root, entry)
        assert root.value.code == "E_ROOT"
    finally:
        linked_root.unlink()

    with pytest.raises(EvaluationContractError) as unsafe:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath("../escape.json"))
    assert unsafe.value.code == "E_PATH"


def test_bundle_json_byte_limit_accepts_exact_limit_and_rejects_plus_one(contract, tmp_path):
    relative = "docs/evaluation/v1/sources/source-a.json"
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    maximum = contract.limits["json_artifact_bytes"]
    target.write_bytes(b'"' + b"x" * (maximum - 3) + b'"\n')
    with pytest.raises(EvaluationContractError) as exact:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath(relative))
    assert exact.value.code == "E_SCHEMA"

    target.write_bytes(b'"' + b"x" * (maximum - 2) + b'"\n')
    with pytest.raises(EvaluationContractError) as plus_one:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath(relative))
    assert plus_one.value.code == "E_LIMIT"
