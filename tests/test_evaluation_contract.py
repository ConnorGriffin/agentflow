from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import shutil

import pytest

import agentflow.evaluation_contract as evaluation_contract
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
    cases = [json.loads(path.read_text()) for path in _case_files()]
    results = [contract.evaluate(case["operation_id"], case["input_value"]) for case in cases]

    assert len(cases) == 33
    assert [_thaw(result) for result in results] == [case["expected_result"] for case in cases]
    with pytest.raises(TypeError):
        results[0]["status"] = "changed"


def test_evaluate_dispatches_exact_module_before_local_input_rejection(contract):
    result = contract.evaluate(
        contract.operation_ids["schedule"],
        {"case_id_pages": [], "partition": 1.5, "seed": 0},
    )

    assert _thaw(result) == {
        "code": "EVAL_V1_PAIRING",
        "operation_id": contract.operation_ids["schedule"],
        "path": "/partition",
        "status": "error",
    }


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
    with pytest.raises(AttributeError):
        contract.contract_version = "changed"
    with pytest.raises(TypeError):
        replace(contract)
    assert not hasattr(contract, "_contract")
    assert not hasattr(contract, "_evaluate_v1")
    assert not hasattr(contract, "_EvaluationContractV1__evaluate")
    assert contract._EvaluationContractV1__contract_bytes == CONTRACT.read_bytes()
    assert contract._EvaluationContractV1__module_bytes == MODULE.read_bytes()
    immutable = contract._EvaluationContractV1__contract_value
    assert not isinstance(immutable, dict)
    with pytest.raises(TypeError):
        immutable["contract_version"] = "changed"


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
    assert artifact.permitted_root == PurePosixPath("docs/evaluation")
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


def test_json_byte_limit_accepts_exact_limit_and_rejects_plus_one(contract):
    maximum = contract.limits["json_artifact_bytes"]
    exact = b'"' + b"x" * (maximum - 3) + b'"\n'
    assert len(exact) == maximum
    assert evaluation_contract._decode_json(exact, "exact.json", contract.limits) == "x" * (maximum - 3)

    oversized = b'"' + b"x" * (maximum - 2) + b'"\n'
    with pytest.raises(EvaluationContractError) as caught:
        evaluation_contract._decode_json(oversized, "plus-one.json", contract.limits)
    assert caught.value.code == "E_LIMIT"


def test_numeric_expansion_and_parser_recursion_are_bounded(contract):
    with pytest.raises(EvaluationContractError) as exponent:
        evaluation_contract._decode_json(b"1e999999999\n", "number.json", contract.limits)
    assert exponent.value.code == "E_LIMIT"

    deeply_nested = b"[" * 2_000 + b"0" + b"]" * 2_000 + b"\n"
    with pytest.raises(EvaluationContractError) as recursion:
        evaluation_contract._decode_json(deeply_nested, "nested.json", contract.limits)
    assert recursion.value.code == "E_LIMIT"


def test_nesting_and_collection_entry_exact_limits_and_plus_one(contract):
    nesting = contract.limits["json_nesting"]
    exact_nested = b"[" * nesting + b"0" + b"]" * nesting + b"\n"
    assert evaluation_contract._decode_json(exact_nested, "exact.json", contract.limits) is not None
    with pytest.raises(EvaluationContractError) as nested_plus_one:
        evaluation_contract._decode_json(
            b"[" * (nesting + 1) + b"0" + b"]" * (nesting + 1) + b"\n",
            "plus-one.json",
            contract.limits,
        )
    assert nested_plus_one.value.code == "E_LIMIT"

    entries = contract.limits["object_or_array_entries"]
    exact_collection = ("[" + ",".join("0" for _ in range(entries)) + "]\n").encode()
    assert len(evaluation_contract._decode_json(exact_collection, "exact.json", contract.limits)) == entries
    plus_collection = ("[" + ",".join("0" for _ in range(entries + 1)) + "]\n").encode()
    with pytest.raises(EvaluationContractError) as entries_plus_one:
        evaluation_contract._decode_json(plus_collection, "plus-one.json", contract.limits)
    assert entries_plus_one.value.code == "E_LIMIT"


def _schema(definitions: dict[str, object], root: object) -> dict[str, object]:
    return {
        "definitions": definitions,
        "root": root,
        "schema_version": "evaluation-schema-v1",
    }


def _reference(name: str) -> dict[str, str]:
    return {"ref": f"#/definitions/{name}", "type": "ref"}


def test_definition_and_reference_exact_limits_and_plus_one(contract):
    definition_limit = contract.limits["definitions"]
    definitions: dict[str, object] = {
        f"d{index}": _reference(f"d{index + 1}")
        for index in range(definition_limit - 1)
    }
    definitions[f"d{definition_limit - 1}"] = {
        "max_length": 1,
        "min_length": 0,
        "pattern": "^x?$",
        "type": "string",
    }
    evaluation_contract._validate_schema(
        _schema(definitions, _reference("d0")), contract.limits, "exact.json",
    )
    definitions_plus_one = dict(definitions)
    definitions_plus_one[f"d{definition_limit}"] = definitions[f"d{definition_limit - 1}"]
    with pytest.raises(EvaluationContractError) as too_many_definitions:
        evaluation_contract._validate_schema(
            _schema(definitions_plus_one, _reference("d0")), contract.limits, "plus-one.json",
        )
    assert too_many_definitions.value.code == "E_LIMIT"

    reference_limit = contract.limits["references_per_schema"]
    leaf = definitions[f"d{definition_limit - 1}"]
    properties = {f"p{index}": _reference("leaf") for index in range(reference_limit)}
    root = {
        "additional_properties": False,
        "properties": properties,
        "required": sorted(properties),
        "type": "object",
    }
    evaluation_contract._validate_schema(
        _schema({"leaf": leaf}, root), contract.limits, "exact.json",
    )
    properties_plus_one = dict(properties)
    properties_plus_one[f"p{reference_limit}"] = _reference("leaf")
    root_plus_one = {**root, "properties": properties_plus_one, "required": sorted(properties_plus_one)}
    with pytest.raises(EvaluationContractError) as too_many_references:
        evaluation_contract._validate_schema(
            _schema({"leaf": leaf}, root_plus_one), contract.limits, "plus-one.json",
        )
    assert too_many_references.value.code == "E_LIMIT"


def test_path_byte_and_depth_exact_limits_and_plus_one(contract):
    byte_limit = contract.limits["path_bytes"]
    assert evaluation_contract._safe_relative("a" * byte_limit, contract.limits, "exact.json")
    with pytest.raises(EvaluationContractError) as bytes_plus_one:
        evaluation_contract._safe_relative("a" * (byte_limit + 1), contract.limits, "plus-one.json")
    assert bytes_plus_one.value.code == "E_PATH"

    depth_limit = contract.limits["path_depth"]
    assert len(evaluation_contract._safe_relative("/".join("a" for _ in range(depth_limit)), contract.limits, "exact.json").parts) == depth_limit
    with pytest.raises(EvaluationContractError) as depth_plus_one:
        evaluation_contract._safe_relative("/".join("a" for _ in range(depth_limit + 1)), contract.limits, "plus-one.json")
    assert depth_plus_one.value.code == "E_PATH"


def test_module_source_exact_limit_and_plus_one(contract):
    maximum = contract.limits["module_source_bytes"]
    source = MODULE.read_bytes()
    padding = maximum - len(source)
    exact = source + b"#" + b"x" * (padding - 2) + b"\n"
    assert len(exact) == maximum
    assert callable(evaluation_contract._audit_module(exact))
    with pytest.raises(EvaluationContractError) as plus_one:
        evaluation_contract._audit_module(exact + b" ")
    assert plus_one.value.code == "E_LIMIT"


def test_scoped_counts_accept_each_corpus_exactly_and_reject_plus_one(contract):
    row = next(
        item for item in contract._EvaluationContractV1__contract_value["artifact_registry"]
        if item["kind"] == "case-manifest"
    )
    counts: dict[tuple[str, str], int] = {}
    for corpus_id in ("corpus-a", "corpus-b"):
        group_id, context = evaluation_contract._scope_group(
            row, {"corpus-id": corpus_id, "case-id": "case-a"}, {}, "case-a.json",
        )
        assert context == {"corpus-id": corpus_id}
        for _unused in range(row["max_instances"]):
            evaluation_contract._increment_scoped_count(counts, row, group_id)
    assert counts[("case-manifest", "corpus-a")] == row["max_instances"]
    assert counts[("case-manifest", "corpus-b")] == row["max_instances"]
    with pytest.raises(EvaluationContractError) as plus_one:
        evaluation_contract._increment_scoped_count(counts, row, "corpus-a")
    assert plus_one.value.code == "E_LIMIT"


def test_long_acyclic_bundle_graph_is_iterative(contract, tmp_path):
    child = None
    total = 1_100
    for index in reversed(range(total)):
        artifact_id = f"index-{index}"
        relative = f"evaluation/v1/indexes/{artifact_id}.json"
        entries = [] if child is None else [child]
        path, digest = _write_artifact(
            tmp_path,
            relative,
            {
                "children": [],
                "entries": entries,
                "entry_count": 1,
                "group_id": "group-a",
                "group_kind": "run",
                "level": 0,
                "ordinal": index,
            },
        )
        child = {
            "digest": digest,
            "id": artifact_id,
            "kind": "artifact-index",
            "path": path.as_posix(),
        }

    bundle = load_evaluation_bundle(
        contract, tmp_path, PurePosixPath("evaluation/v1/indexes/index-0.json"),
    )
    assert len(bundle.artifacts) == total


def test_role_specific_identity_rejects_a_case_ref_using_corpus_capture(contract, tmp_path):
    case_path = "docs/evaluation/v1/corpora/corpus-a/cases/case-b.json"
    entry, _digest = _write_artifact(
        tmp_path,
        "evaluation/v1/indexes/index-a.json",
        {
            "children": [],
            "entries": [{
                "digest": "0" * 64,
                "id": "corpus-a",
                "kind": "case-manifest",
                "path": case_path,
            }],
            "entry_count": 1,
            "group_id": "group-a",
            "group_kind": "run",
            "level": 0,
            "ordinal": 0,
        },
    )
    with pytest.raises(EvaluationContractError) as identity:
        load_evaluation_bundle(contract, tmp_path, entry)
    assert (identity.value.code, identity.value.basename) == ("E_ID", "case-b.json")


def test_nul_surrogate_wrong_terminal_and_internal_boundaries_are_sanitized(
    contract, tmp_path, monkeypatch,
):
    with pytest.raises(EvaluationContractError) as nul_root:
        load_evaluation_bundle(
            contract, Path("\0"), PurePosixPath("docs/evaluation/v1/sources/source-a.json"),
        )
    assert (nul_root.value.code, nul_root.value.basename) == ("E_ROOT", "<bundle>")

    with pytest.raises(EvaluationContractError) as surrogate:
        load_evaluation_bundle(contract, tmp_path, PurePosixPath("\ud800.json"))
    assert (surrogate.value.code, surrogate.value.basename) == ("E_PATH", "<bundle>")

    terminal_path = PurePosixPath("docs/evaluation/v1/corpora/corpus-a/answers/case-a.json")
    loaded = {
        terminal_path: (
            {"kind": "answer-key"}, {}, {}, "0" * 64, "case-a", {"corpus-id": "corpus-a"},
        ),
    }
    with pytest.raises(EvaluationContractError) as terminal:
        evaluation_contract._validate_result_missingness(
            contract,
            PurePosixPath("result.json"),
            {"terminal_edge": {"path": terminal_path.as_posix()}},
            loaded,
        )
    assert (terminal.value.code, terminal.value.basename) == ("E_LINEAGE", "result.json")

    with pytest.raises(EvaluationContractError) as semantic_key:
        contract.evaluate(contract.operation_ids["schedule"], {})
    assert (semantic_key.value.code, semantic_key.value.basename) == ("E_SEMANTIC", "<module>")

    monkeypatch.setattr(evaluation_contract, "_registry_match", lambda *_args: {}["missing"])
    with pytest.raises(EvaluationContractError) as internal_key:
        load_evaluation_bundle(
            contract, tmp_path, PurePosixPath("docs/evaluation/v1/sources/source-a.json"),
        )
    assert (internal_key.value.code, internal_key.value.basename) == ("E_INTERNAL", "<bundle>")


@pytest.mark.parametrize("code", evaluation_contract._DECLARED_REJECTION_CODES)
def test_every_declared_rejection_code_is_closed_and_sanitized(code):
    with pytest.raises(EvaluationContractError) as caught:
        evaluation_contract._error(code, "bad\0\ud800/name")
    assert caught.value.code == code
    assert caught.value.basename == "<contract>"
    assert "bad" not in str(caught.value)
