from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check-evaluation-contract-candidate.py"
STAGE_A = (
    "docs/evaluation/design/contract-v1.candidate.json",
    "docs/evaluation/design/contract-v1.conformance.json",
    "agentflow/evaluation_semantics_v1.py",
)
STAGE_A_DIGESTS = (
    "162a4fe0d0a5cf7d9d23eede686b956eb6c30b3a7638e63398fa88dc33f5cb1f",
    "3f94d7f0479d646eba9c40ffe9ee9cf9bbcdb81aa29da69f3d30bbee254d7455",
    "63749bf9a5fedf0b36b3271f6dff35e4962fb4d68c2882bf679f17e838a7c38c",
)
SUCCESS = b'{"checked":3,"format":"evaluation-contract-candidate-check-v1","status":"ok"}\n'


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("evaluation_candidate_checker", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def bundle():
    candidate = json.loads((ROOT / STAGE_A[0]).read_text())
    report = json.loads((ROOT / STAGE_A[1]).read_text())
    source = (ROOT / STAGE_A[2]).read_bytes()
    return candidate, report, source


def _copy_bundle(tmp_path: Path) -> Path:
    for relative in (*STAGE_A, "scripts/check-evaluation-contract-candidate.py"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def _run(root: Path, cwd: Path | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(root / "scripts/check-evaluation-contract-candidate.py")],
        cwd=cwd or root, capture_output=True, check=False,
    )


def _refresh_fixture_locks(root: Path) -> None:
    script = root / "scripts/check-evaluation-contract-candidate.py"
    text = script.read_text()
    for relative, original in zip(STAGE_A, STAGE_A_DIGESTS):
        current = sha256((root / relative).read_bytes()).hexdigest()
        text = text.replace(original, current)
    script.write_text(text)


def _write_fixture_json(checker, path: Path, value) -> None:
    path.write_bytes(_canonical_bytes(checker, value))


def _error(checker, code: str, path: str) -> bytes:
    return checker._line({"code": code, "format": checker.FORMAT, "path": path, "status": "error"})


def _redigest(checker, value):
    updated = deepcopy(value)
    updated["digest"] = "0" * 64
    updated["digest"] = sha256(checker._canonical({key: item for key, item in updated.items() if key != "digest"}).encode("ascii")).hexdigest()
    return updated


def _canonical_bytes(checker, value) -> bytes:
    return (checker._canonical(value) + "\n").encode("ascii")


def _minimal_schema(definition_count=0, reference_count=0):
    definitions = {
        f"d-{index:02d}": {"type": "null"} for index in range(definition_count)
    }
    properties = {
        f"p-{index:02d}": {"type": "ref", "ref": "#/definitions/d-00"}
        for index in range(reference_count)
    }
    if definition_count and not properties:
        properties = {
            f"p-{index:02d}": {"type": "ref", "ref": f"#/definitions/d-{index:02d}"}
            for index in range(definition_count)
        }
    elif definition_count > 1:
        for index in range(1, definition_count):
            properties[f"used-{index:02d}"] = {
                "type": "ref", "ref": f"#/definitions/d-{index:02d}",
            }
    root = {
        "type": "object", "required": sorted(properties), "properties": properties,
        "additional_properties": False,
    }
    return {"schema_version": "evaluation-schema-v1", "root": root, "definitions": definitions}


def test_stage_a_bytes_are_the_reviewed_bundle():
    assert tuple(sha256((ROOT / path).read_bytes()).hexdigest() for path in STAGE_A) == STAGE_A_DIGESTS


def test_zero_argument_checker_is_exact_and_cwd_independent(tmp_path):
    result = _run(ROOT, cwd=tmp_path)

    assert result.returncode == 0
    assert result.stdout == SUCCESS
    assert result.stderr == b""


@pytest.mark.parametrize("relative", STAGE_A)
def test_each_whole_file_lock_rejects_one_byte_mutation(tmp_path, checker, relative):
    root = _copy_bundle(tmp_path)
    path = root / relative
    data = path.read_bytes()
    index = data.index(b"evaluation")
    path.write_bytes(data[:index] + b"f" + data[index + 1:])

    result = _run(root)

    assert result.returncode == 1
    assert result.stderr == _error(checker, "E_DIGEST", relative)
    assert result.stdout == b""


def test_candidate_and_report_are_canonical_ascii_json(checker):
    for relative in STAGE_A[:2]:
        data = (ROOT / relative).read_bytes()
        assert all(byte < 128 for byte in data)
        assert checker._decode_json(data, relative) is not None


@pytest.mark.parametrize(
    ("fault", "code", "path"),
    [
        ("canonical", "E_CANONICAL", STAGE_A[0]),
        ("duplicate", "E_DUPLICATE_KEY", STAGE_A[0]),
        ("source", "E_SOURCE_DRIFT", STAGE_A[0]),
        ("schema", "E_SCHEMA", STAGE_A[0]),
        ("ref", "E_REF_UNUSED", STAGE_A[0]),
        ("path", "E_PATH", STAGE_A[0]),
        ("ownership", "E_LINEAGE", STAGE_A[1]),
        ("module-ast", "E_SEMANTIC", STAGE_A[2]),
        ("module-limit", "E_LIMIT", STAGE_A[2]),
    ],
)
def test_public_checker_reaches_closed_rejection_classes(
        tmp_path, checker, bundle, fault, code, path):
    root = _copy_bundle(tmp_path)
    candidate_path = root / STAGE_A[0]
    report_path = root / STAGE_A[1]
    module_path = root / STAGE_A[2]
    candidate, report, _ = bundle
    if fault == "canonical":
        candidate_path.write_bytes(candidate_path.read_bytes()[:-1] + b" \n")
    elif fault == "duplicate":
        candidate_path.write_bytes(b'{"digest":"' + b"0" * 64 + b'",' + candidate_path.read_bytes()[1:])
    elif fault == "source":
        changed = deepcopy(candidate)
        changed["source_bindings"][0]["sha256"] = "0" * 64
        _write_fixture_json(checker, candidate_path, changed)
    elif fault == "schema":
        changed = deepcopy(candidate)
        changed["unexpected"] = None
        _write_fixture_json(checker, candidate_path, changed)
    elif fault == "ref":
        changed = deepcopy(candidate)
        changed["schemas"]["authority-blinding-error-v1"]["definitions"]["unused"] = {"type": "null"}
        _write_fixture_json(checker, candidate_path, changed)
    elif fault == "path":
        changed = deepcopy(candidate)
        changed["semantic_module"]["path"] = "../agentflow/evaluation_semantics_v1.py"
        _write_fixture_json(checker, candidate_path, changed)
    elif fault == "ownership":
        changed = deepcopy(report)
        changed["requirement_coverage"][0]["owner"] = "checker"
        _write_fixture_json(checker, report_path, changed)
    elif fault == "module-ast":
        module_path.write_bytes(module_path.read_bytes().replace(
            b"from hashlib import sha256", b"import os                    ", 1,
        ))
    elif fault == "module-limit":
        module_path.write_bytes(module_path.read_bytes() + b"#" * 65_536)
    _refresh_fixture_locks(root)

    result = _run(root)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == _error(checker, code, path)


def test_public_checker_reports_fixed_root_and_io_paths(tmp_path, checker):
    root = _copy_bundle(tmp_path)
    renamed = root / "scripts/not-the-checker.py"
    (root / "scripts/check-evaluation-contract-candidate.py").rename(renamed)
    root_result = subprocess.run([sys.executable, str(renamed)], capture_output=True, check=False)
    assert root_result.returncode == 1
    assert root_result.stderr == _error(checker, "E_ROOT", checker.SCRIPT_PATH)

    root = _copy_bundle(tmp_path / "io")
    (root / STAGE_A[0]).unlink()
    io_result = _run(root)
    assert io_result.returncode == 1
    assert io_result.stderr == _error(checker, "E_IO", checker.CANDIDATE_PATH)


def test_controlled_internal_fault_has_exact_public_framing(checker, capfdbinary):
    result = checker.main(force_internal=True)
    captured = capfdbinary.readouterr()

    assert result == 2
    assert captured.out == b""
    assert captured.err == _error(checker, "E_INTERNAL", checker.SCRIPT_PATH)


@pytest.mark.parametrize(
    ("data", "code"),
    [
        (b'{"a":1,"a":2}\n', "E_DUPLICATE_KEY"),
        (b'{"a":"\\n"}\n', "E_CANONICAL"),
        (b'\xef\xbb\xbf{}\n', "E_UTF8"),
        (b'{}\r\n', "E_UTF8"),
        (b'{"a":NaN}\n', "E_JSON"),
    ],
)
def test_canonical_parser_rejects_alternate_encodings(checker, data, code):
    with pytest.raises(checker.CheckFailure) as caught:
        checker._decode_json(data, checker.CANDIDATE_PATH)

    assert caught.value.code == code


def test_schema_catalog_and_every_operation_schema_are_closed(checker, bundle):
    candidate, _, _ = bundle

    checker._validate_schema(candidate["schema_catalog"], checker.CANDIDATE_PATH, enforce_reference_limit=False)
    for schema in candidate["schemas"].values():
        checker._validate_schema(schema, checker.CANDIDATE_PATH)


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda s: s["root"].update({"title": "open"}), "E_SCHEMA"),
        (lambda s: s["root"]["properties"]["p-00"].update({"ref": "https://example.invalid/schema"}), "E_REF"),
        (lambda s: s["definitions"].update({"unused": {"type": "null"}}), "E_REF_UNUSED"),
        (lambda s: s["definitions"].update({"d-00": {"type": "ref", "ref": "#/definitions/d-00"}}), "E_REF_CYCLE"),
    ],
)
def test_schema_grammar_refs_cycles_and_unused_definitions_fail_closed(checker, mutation, code):
    schema = _minimal_schema(1, 1)
    mutation(schema)

    with pytest.raises(checker.CheckFailure) as caught:
        checker._validate_schema(schema, checker.CANDIDATE_PATH)

    assert caught.value.code == code


@pytest.mark.parametrize(
    "node",
    [
        {"type": "array", "items": {"type": "null"}, "min_items": 0, "max_items": True},
        {"type": "string", "pattern": "^x$", "min_length": False, "max_length": 1},
    ],
)
def test_boolean_schema_bounds_are_not_integers(checker, node):
    schema = {"schema_version": "evaluation-schema-v1", "root": node, "definitions": {}}
    with pytest.raises(checker.CheckFailure, match="E_SCHEMA"):
        checker._validate_schema(schema, checker.CANDIDATE_PATH)


def test_source_bindings_and_closed_locator_universe_are_independent(checker, bundle):
    candidate, _, source = bundle
    drifted = deepcopy(candidate)
    drifted["source_bindings"][0]["sha256"] = "0" * 64
    with pytest.raises(checker.CheckFailure, match="E_SOURCE_DRIFT"):
        checker._validate_candidate(drifted, sha256(source).hexdigest())

    unknown = deepcopy(candidate)
    unknown["requirements"][0]["source_locator"] = "issue/583/acceptance/a15"
    with pytest.raises(checker.CheckFailure, match="E_SOURCE_LOCATOR"):
        checker._validate_candidate(unknown, sha256(source).hexdigest())


def test_requirement_coverage_has_one_preflight_owner_and_valid_cases(checker, bundle):
    candidate, report, source = bundle
    evaluate = checker._audit_and_load_module(source)

    checker._validate_report(
        report, candidate, STAGE_A_DIGESTS[0], STAGE_A_DIGESTS[2], evaluate,
    )
    assert len(report["requirement_coverage"]) == 66
    assert not report["unresolved"]


def test_duplicate_and_wrong_requirement_ownership_fail(checker, bundle):
    candidate, report, source = bundle
    evaluate = checker._audit_and_load_module(source)
    duplicate = deepcopy(report)
    duplicate["requirement_coverage"][1]["requirement_id"] = duplicate["requirement_coverage"][0]["requirement_id"]
    with pytest.raises(checker.CheckFailure, match="E_REQUIREMENT_DUPLICATE"):
        checker._validate_report(duplicate, candidate, STAGE_A_DIGESTS[0], STAGE_A_DIGESTS[2], evaluate)

    wrong_owner = deepcopy(report)
    wrong_owner["requirement_coverage"][0]["owner"] = "checker"
    with pytest.raises(checker.CheckFailure, match="E_LINEAGE"):
        checker._validate_report(wrong_owner, candidate, STAGE_A_DIGESTS[0], STAGE_A_DIGESTS[2], evaluate)


def test_every_semantic_vector_dispatches_through_exact_interface(checker, bundle):
    candidate, report, source = bundle
    evaluate = checker._audit_and_load_module(source)
    calls = []

    def recording(contract, operation_id, input_value):
        assert contract is candidate
        assert not any("expected" in key for key in input_value if isinstance(key, str))
        calls.append(operation_id)
        return evaluate(contract, operation_id, input_value)

    checker._validate_report(
        report, candidate, STAGE_A_DIGESTS[0], STAGE_A_DIGESTS[2], recording,
    )

    assert len(calls) == len(report["semantic_cases"]) == 33
    assert set(candidate["operation_ids"].values()) <= set(calls)


def test_input_and_expectation_cannot_be_rebound_around_whole_file_lock(tmp_path, checker, bundle):
    candidate, report, source = bundle
    evaluate = checker._audit_and_load_module(source)
    changed = deepcopy(report)
    case = changed["semantic_cases"][0]
    case["input_value"]["seed"] += 1
    case["expected_result"] = evaluate(candidate, case["operation_id"], deepcopy(case["input_value"]))
    changed = _redigest(checker, changed)
    root = _copy_bundle(tmp_path)
    (root / STAGE_A[1]).write_bytes(_canonical_bytes(checker, changed))

    result = _run(root)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == _error(checker, "E_DIGEST", checker.REPORT_PATH)


def test_module_interface_ast_import_and_capability_audit(checker, bundle):
    _, _, source = bundle
    evaluate = checker._audit_and_load_module(source)

    assert list(__import__("inspect").signature(evaluate).parameters) == [
        "contract", "operation_id", "input_value",
    ]
    tree = __import__("ast").parse(source)
    imports = {
        (node.module, node.names[0].name) for node in tree.body
        if isinstance(node, __import__("ast").ImportFrom)
    }
    assert imports == {("fractions", "Fraction"), ("hashlib", "sha256")}


@pytest.mark.parametrize(
    "source",
    [
        b"import os\n\ndef evaluate_v1(contract, operation_id, input_value):\n    return {}\n",
        b"def helper():\n    return open('x')\n\ndef evaluate_v1(contract, operation_id, input_value):\n    return {}\n",
        b"def public():\n    return 1\n\ndef evaluate_v1(contract, operation_id, input_value):\n    return {}\n",
        b"def evaluate_v1(contract, operation_id):\n    return {}\n",
    ],
)
def test_module_audit_rejects_import_capability_surface_and_interface(checker, source):
    with pytest.raises(checker.CheckFailure, match="E_SEMANTIC"):
        checker._audit_and_load_module(source)


def test_module_source_exact_limit_passes_and_plus_one_fails(checker):
    prefix = (
        b'"""x"""\n\nfrom fractions import Fraction\nfrom hashlib import sha256\n\n'
        b"def evaluate_v1(contract, operation_id, input_value):\n    return input_value\n"
    )
    exact = prefix + b"#" + b"x" * (checker.LIMITS["module_source_bytes"] - len(prefix) - 2) + b"\n"
    checker._audit_and_load_module(exact)

    with pytest.raises(checker.CheckFailure, match="E_LIMIT"):
        checker._audit_and_load_module(exact[:-1] + b"x\n")


def test_json_artifact_byte_limit_exact_and_plus_one(checker):
    overhead = len(b'{"a":""}\n')
    exact = b'{"a":"' + b"x" * (checker.LIMITS["json_artifact_bytes"] - overhead) + b'"}\n'
    checker._decode_json(exact, checker.CANDIDATE_PATH)

    too_large = b'{"a":"' + b"x" * (checker.LIMITS["json_artifact_bytes"] - overhead + 1) + b'"}\n'
    with pytest.raises(checker.CheckFailure, match="E_LIMIT"):
        checker._decode_json(too_large, checker.CANDIDATE_PATH)


@pytest.mark.parametrize("collection", ["array", "object"])
def test_collection_entry_limit_exact_and_plus_one(checker, collection):
    if collection == "array":
        exact = list(range(checker.LIMITS["object_or_array_entries"]))
        too_many = exact + [999]
    else:
        exact = {str(index): index for index in range(checker.LIMITS["object_or_array_entries"])}
        too_many = {**exact, "extra": 999}
    checker._json_depth_and_entries(exact)
    with pytest.raises(checker.CheckFailure, match="E_LIMIT"):
        checker._json_depth_and_entries(too_many)


def test_json_nesting_limit_exact_and_plus_one(checker):
    exact = None
    for _ in range(checker.LIMITS["json_nesting"]):
        exact = [exact]
    checker._json_depth_and_entries(exact)

    with pytest.raises(checker.CheckFailure, match="E_LIMIT"):
        checker._json_depth_and_entries([exact])


def test_definition_limit_exact_and_plus_one(checker):
    checker._validate_schema(_minimal_schema(checker.LIMITS["definitions"]), checker.CANDIDATE_PATH)

    with pytest.raises(checker.CheckFailure, match="E_LIMIT"):
        checker._validate_schema(_minimal_schema(checker.LIMITS["definitions"] + 1), checker.CANDIDATE_PATH)


def test_reference_limit_exact_and_plus_one(checker):
    checker._validate_schema(_minimal_schema(1, checker.LIMITS["references_per_schema"]), checker.CANDIDATE_PATH)

    with pytest.raises(checker.CheckFailure, match="E_LIMIT"):
        checker._validate_schema(_minimal_schema(1, checker.LIMITS["references_per_schema"] + 1), checker.CANDIDATE_PATH)


def test_path_byte_and_depth_limits_exact_and_plus_one(checker):
    exact_bytes = "a" * checker.LIMITS["path_bytes"]
    checker._validate_path(exact_bytes, checker.CANDIDATE_PATH)
    with pytest.raises(checker.CheckFailure, match="E_PATH"):
        checker._validate_path(exact_bytes + "a", checker.CANDIDATE_PATH)

    exact_depth = "/".join("a" for _ in range(checker.LIMITS["path_depth"]))
    checker._validate_path(exact_depth, checker.CANDIDATE_PATH)
    with pytest.raises(checker.CheckFailure, match="E_PATH"):
        checker._validate_path(exact_depth + "/a", checker.CANDIDATE_PATH)


def test_generated_case_byte_limit_exact_and_plus_one(checker):
    template = {
        "operation": "json_replace", "operand": "x" * (checker.LIMITS["generated_case_bytes"] - 3),
        "target": {"json_pointer": ""},
    }
    assert len(checker._generate_payload({}, template)) == checker.LIMITS["generated_case_bytes"]
    template["operand"] += "x"
    assert len(checker._generate_payload({}, template)) == checker.LIMITS["generated_case_bytes"] + 1


def test_generated_template_count_limit_and_replay_are_deterministic(checker, bundle):
    candidate, _, _ = bundle
    checker._validate_generation(candidate)
    checker._validate_generation(deepcopy(candidate))

    too_many = deepcopy(candidate)
    template = too_many["generation"]["templates"][0]
    too_many["generation"]["templates"] = [
        {**template, "id": f"t-{index:03d}"}
        for index in range(checker.LIMITS["generated_cases"] + 1)
    ]
    with pytest.raises(checker.CheckFailure, match="E_GENERATOR_LIMIT"):
        checker._validate_generation(too_many)


def test_all_generation_operations_have_closed_behavior(checker):
    cases = [
        ({"a": 1}, {"operation": "json_replace", "operand": 2, "target": {"json_pointer": "/a"}}),
        ({"a": 1}, {"operation": "json_remove", "operand": None, "target": {"json_pointer": "/a"}}),
        ({"a": {}}, {"operation": "json_object_insert", "operand": {"key": "b", "value": 2}, "target": {"json_pointer": "/a"}}),
        ({"a": []}, {"operation": "json_array_insert", "operand": {"index": 0, "value": 2}, "target": {"json_pointer": "/a"}}),
        ({"a": 1}, {"operation": "raw_truncate", "operand": None, "target": {"raw_byte_offset": 1}}),
        ({"a": 1}, {"operation": "raw_byte_replace", "operand": 91, "target": {"raw_byte_offset": 0}}),
        ({"a": 1}, {"operation": "raw_duplicate_key_inject", "operand": {"key": "a", "value": 2}, "target": {"raw_byte_offset": 6}}),
    ]
    payloads = [checker._generate_payload(base, template) for base, template in cases]

    assert payloads[:4] == [b'{"a":2}\n', b'{}\n', b'{"a":{"b":2}}\n', b'{"a":[2]}\n']
    assert payloads[4:] == [b'{', b'["a":1}', b'{"a":1,"a":2}']


def test_error_registry_and_output_invariants_are_closed(checker, bundle):
    candidate, _, _ = bundle

    assert tuple(candidate["checker_boundary"]["error_precedence"]) == checker.ERROR_PRECEDENCE
    assert len(checker.ERROR_PRECEDENCE) == 28
    assert len(SUCCESS) <= checker.LIMITS["stdout_or_stderr_bytes"]
    for code in checker.ERROR_PRECEDENCE:
        assert len(_error(checker, code, checker.CANDIDATE_PATH)) <= checker.LIMITS["stdout_or_stderr_bytes"]


def test_public_error_precedence_uses_earlier_joint_fault(checker, bundle):
    candidate, _, source = bundle
    changed = deepcopy(candidate)
    changed["source_bindings"][0]["sha256"] = "0" * 64
    changed["limits"]["definitions"] = 63

    with pytest.raises(checker.CheckFailure) as caught:
        checker._validate_candidate(changed, sha256(source).hexdigest())

    assert caught.value.code == "E_SOURCE_DRIFT"
    assert checker.ERROR_PRECEDENCE.index("E_SOURCE_DRIFT") < checker.ERROR_PRECEDENCE.index("E_SCHEMA")
