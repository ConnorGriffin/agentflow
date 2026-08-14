from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import importlib.util
from itertools import combinations
import json
import os
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
    "53359f35de57047441defa76a477564580b956f968ab6425356cca3a1c5a8409",
    "1c477cc45b49cc66e4b7751d4961617d6674c809179f2f370691058ba5d53915",
    "185f41a5e4549cc1ccbc4615af5846c3ed0f95285790d193e1b2f43aa3dc8554",
)
SUCCESS = b'{"checked":3,"format":"evaluation-contract-candidate-check-v1","status":"ok"}\n'
PUBLIC_ERROR_CODES = (
    "E_ROOT", "E_SOURCE_DRIFT", "E_SOURCE_LOCATOR", "E_REQUIREMENT_DUPLICATE",
    "E_REQUIREMENT_MISSING", "E_IO", "E_LIMIT", "E_UTF8", "E_JSON",
    "E_DUPLICATE_KEY", "E_CANONICAL", "E_SCHEMA", "E_REF", "E_REF_CYCLE",
    "E_REF_UNUSED", "E_PATH", "E_DIGEST", "E_ID", "E_CROSS_REFERENCE",
    "E_LINEAGE", "E_ORACLE", "E_GENERATOR_TEMPLATE", "E_GENERATOR_TARGET",
    "E_GENERATOR_PRECONDITION", "E_GENERATOR_COLLISION", "E_GENERATOR_LIMIT",
    "E_SEMANTIC", "E_INTERNAL",
)
ERROR_PAIRS = tuple(combinations(PUBLIC_ERROR_CODES, 2))


def _load_checker(script):
    spec = importlib.util.spec_from_file_location("evaluation_candidate_checker", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker(SCRIPT)


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


def _refresh_fixture_locks(root: Path, stale_paths=()) -> None:
    script = root / "scripts/check-evaluation-contract-candidate.py"
    text = script.read_text()
    for relative, original in zip(STAGE_A, STAGE_A_DIGESTS):
        if relative in stale_paths:
            continue
        current = sha256((root / relative).read_bytes()).hexdigest()
        text = text.replace(original, current)
    script.write_text(text)


def _error(checker, code: str, path: str) -> bytes:
    return checker._line({"code": code, "format": checker.FORMAT, "path": path, "status": "error"})


def _redigest(checker, value):
    updated = deepcopy(value)
    updated["digest"] = "0" * 64
    updated["digest"] = sha256(checker._canonical({key: item for key, item in updated.items() if key != "digest"}).encode("ascii")).hexdigest()
    return updated


def _canonical_bytes(checker, value) -> bytes:
    return (checker._canonical(value) + "\n").encode("ascii")


def _load_module(checker, source):
    return checker._audit_and_load_module(source, checker._decode_module_source(source))


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


def _mutate_locked_byte(root, relative):
    path = root / relative
    data = path.read_bytes()
    index = data.index(b"evaluation")
    path.write_bytes(data[:index] + b"f" + data[index + 1:])


@pytest.mark.parametrize("relative", STAGE_A)
def test_each_whole_file_lock_rejects_one_byte_mutation(tmp_path, checker, relative):
    root = _copy_bundle(tmp_path)
    _mutate_locked_byte(root, relative)

    result = _run(root)

    assert result.returncode == 1
    assert result.stderr == _error(checker, "E_DIGEST", relative)
    assert result.stdout == b""


@pytest.mark.parametrize(
    ("mutated", "expected_path"),
    [
        ((STAGE_A[2],), STAGE_A[2]),
        ((STAGE_A[0], STAGE_A[2]), STAGE_A[0]),
        ((STAGE_A[1], STAGE_A[2]), STAGE_A[1]),
    ],
    ids=("module-over-report-derivative", "candidate-direct-order", "report-direct-order"),
)
def test_simultaneous_digest_failures_prefer_direct_mutated_artifact(
        tmp_path, checker, mutated, expected_path):
    root = _copy_bundle(tmp_path)
    for relative in mutated:
        _mutate_locked_byte(root, relative)

    actual_digests = {
        relative: sha256((root / relative).read_bytes()).hexdigest()
        for relative in STAGE_A
    }
    direct = tuple(
        relative for relative, locked in zip(STAGE_A, STAGE_A_DIGESTS)
        if actual_digests[relative] != locked
    )
    candidate = json.loads((root / STAGE_A[0]).read_text())
    report = json.loads((root / STAGE_A[1]).read_text())
    derivative = []
    if candidate["semantic_module"]["source_sha256"] != actual_digests[STAGE_A[2]]:
        derivative.append(STAGE_A[2])
    if report["artifact_bindings"]["candidate_sha256"] != actual_digests[STAGE_A[0]]:
        derivative.append(STAGE_A[1])
    if report["artifact_bindings"]["module_sha256"] != actual_digests[STAGE_A[2]]:
        derivative.append(STAGE_A[1])
    if report["source_binding_sha256"] != actual_digests[STAGE_A[2]]:
        derivative.append(STAGE_A[1])

    assert direct == mutated
    assert derivative
    assert len(direct) + len(derivative) >= 2
    result = _run(root)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == _error(checker, "E_DIGEST", expected_path)


def test_candidate_and_report_are_canonical_ascii_json(checker):
    for relative in STAGE_A[:2]:
        data = (ROOT / relative).read_bytes()
        assert all(byte < 128 for byte in data)
        assert checker._decode_json(data, relative) is not None


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
    evaluate = _load_module(checker, source)

    checker._validate_report(
        report, candidate, STAGE_A_DIGESTS[0], STAGE_A_DIGESTS[2], evaluate,
    )
    assert len(report["requirement_coverage"]) == 66
    assert not report["unresolved"]


def test_duplicate_and_wrong_requirement_ownership_fail(checker, bundle):
    candidate, report, source = bundle
    evaluate = _load_module(checker, source)
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
    evaluate = _load_module(checker, source)
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


def test_score_gates_uses_contract_present_state(checker, bundle):
    candidate, report, source = bundle
    evaluate = _load_module(checker, source)
    case = next(case for case in report["semantic_cases"] if case["operation_id"] == "op-v1-score-gates")
    contract = deepcopy(candidate)
    contract["missingness_policy"]["present_state"] = "current"
    input_value = deepcopy(case["input_value"])

    def replace_present(value):
        if isinstance(value, dict):
            return {key: replace_present(item) for key, item in value.items()}
        if isinstance(value, list):
            return [replace_present(item) for item in value]
        return "current" if value == "present" else value

    input_value = replace_present(input_value)
    input_value["attempt_pages"][0]["attempts"][0]["candidate"]["critical_miss"]["value"] = True
    result = evaluate(contract, case["operation_id"], input_value)

    scorecard = result["value"]["scorecard"]
    assert scorecard["gates"]["hard"] is False
    assert scorecard["metrics"]["new_critical_miss_count"] == 1
    assert scorecard["metrics"]["token_saving_median"]["state"] == "present"
    assert scorecard["metrics"]["round_saving_median"]["state"] == "present"


def test_input_and_expectation_cannot_be_rebound_around_whole_file_lock(tmp_path, checker, bundle):
    candidate, report, source = bundle
    evaluate = _load_module(checker, source)
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
    evaluate = _load_module(checker, source)

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
        b"from fractions import Fraction\nfrom hashlib import sha256\n\ndef evaluate_v1(contract, operation_id, input_value):\n    return {}\n",
        b"def helper():\n    return open('x')\n\ndef evaluate_v1(contract, operation_id, input_value):\n    return {}\n",
        b"def public():\n    return 1\n\ndef evaluate_v1(contract, operation_id, input_value):\n    return {}\n",
        b"def evaluate_v1(contract, operation_id):\n    return {}\n",
    ],
)
def test_module_audit_rejects_import_capability_surface_and_interface(checker, source):
    with pytest.raises(checker.CheckFailure, match="E_SEMANTIC"):
        checker._audit_and_load_module(source, checker._decode_module_source(source))


def test_imported_public_callable_reaches_public_surface_check(checker):
    source = (
        b"from fractions import Fraction\nfrom hashlib import sha256\n\n"
        b"def evaluate_v1(contract, operation_id, input_value):\n    return {}\n"
    )

    with pytest.raises(checker.CheckFailure) as caught:
        checker._audit_and_load_module(source, checker._decode_module_source(source))

    assert caught.value.code == "E_SEMANTIC"


def test_module_source_exact_limit_passes_and_plus_one_fails(checker):
    prefix = (
        b'"""x"""\n\nfrom fractions import Fraction as _Fraction\nfrom hashlib import sha256 as _sha256\n\n'
        b"def evaluate_v1(contract, operation_id, input_value):\n    return input_value\n"
    )
    exact = prefix + b"#" + b"x" * (checker.LIMITS["module_source_bytes"] - len(prefix) - 2) + b"\n"
    checker._audit_and_load_module(exact, checker._decode_module_source(exact))

    with pytest.raises(checker.CheckFailure, match="E_LIMIT"):
        oversized = exact[:-1] + b"x\n"
        checker._audit_and_load_module(oversized, checker._decode_module_source(oversized))


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


def _fault_name(code):
    return "error-" + code.lower().replace("_", "-")


def _new_fault_bundle(tmp_path, checker, bundle):
    candidate, report, source = bundle
    return {
        "root": _copy_bundle(tmp_path),
        "checker": checker,
        "candidate": deepcopy(candidate),
        "report": deepcopy(report),
        "module": source,
        "codes": [],
        "raw": [],
        "stale": set(),
        "root_fault": False,
        "internal_fault": False,
        "io_fault": False,
        "collision": False,
        "paths": {},
    }


def _candidate_fault(state, code, mutation):
    mutation(state["candidate"])
    state["paths"][code] = STAGE_A[0]


def _report_fault(state, code, mutation):
    mutation(state["report"])
    state["paths"][code] = STAGE_A[1]


def _inject_root(state):
    state["root_fault"] = True
    state["paths"]["E_ROOT"] = "scripts/check-evaluation-contract-candidate.py"


def _inject_source_drift(state):
    _candidate_fault(state, "E_SOURCE_DRIFT", lambda value: value["source_bindings"][0].update(sha256="0" * 64))


def _inject_source_locator(state):
    _candidate_fault(state, "E_SOURCE_LOCATOR", lambda value: value["requirements"][0].update(source_locator="issue/583/acceptance/a15"))


def _inject_requirement_duplicate(state):
    _candidate_fault(state, "E_REQUIREMENT_DUPLICATE", lambda value: value["requirements"].append(deepcopy(value["requirements"][1])))


def _inject_requirement_missing(state):
    _candidate_fault(state, "E_REQUIREMENT_MISSING", lambda value: value["requirements"].pop(-2))


def _inject_io(state):
    state["io_fault"] = True


def _inject_limit(state):
    state["module"] += b"#" * 65_536
    state["paths"]["E_LIMIT"] = STAGE_A[2]


def _inject_raw(state, code):
    state["raw"].append(code)


def _inject_schema(state):
    _candidate_fault(state, "E_SCHEMA", lambda value: value.update(unexpected=None))


def _inject_ref(state):
    def mutation(value):
        value["schemas"]["authority-blinding-error-v1"]["root"]["properties"]["code"] = {
            "type": "ref", "ref": "#/definitions/missing",
        }
    _candidate_fault(state, "E_REF", mutation)


def _inject_ref_cycle(state):
    def mutation(value):
        schema = value["schemas"]["bootstrap-lower-bound-error-v1"]
        schema["definitions"]["cycle"] = {"type": "ref", "ref": "#/definitions/cycle"}
        schema["root"]["properties"]["code"] = {"type": "ref", "ref": "#/definitions/cycle"}
    _candidate_fault(state, "E_REF_CYCLE", mutation)


def _inject_ref_unused(state):
    def mutation(value):
        value["schemas"]["schedule-success-v1"]["definitions"]["unused"] = {"type": "null"}
    _candidate_fault(state, "E_REF_UNUSED", mutation)


def _inject_path(state):
    _candidate_fault(state, "E_PATH", lambda value: value["artifact_registry"][0].update(path="../contract.json"))


def _inject_digest(state):
    state["module"] += b"# stale whole-file lock\n"
    state["stale"].add(STAGE_A[2])
    state["paths"]["E_DIGEST"] = STAGE_A[2]


def _inject_id(state):
    _candidate_fault(state, "E_ID", lambda value: value["rules"][0].update(rule_id="INVALID"))


def _inject_cross_reference(state):
    _candidate_fault(state, "E_CROSS_REFERENCE", lambda value: value["operation_contracts"][0].update(input_schema="missing-schema"))


def _inject_lineage(state):
    _report_fault(state, "E_LINEAGE", lambda value: value["requirement_coverage"][0].update(owner="checker"))


def _inject_oracle(state):
    def mutation(value):
        case = next(
            row for row in value["semantic_cases"]
            if "EVAL_V1_OPERATION" in row["coverage"]
        )
        case["coverage"] = ["not-a-semantic-code"]
    _report_fault(state, "E_ORACLE", mutation)


def _append_generator_template(state, template):
    state["candidate"]["generation"]["templates"].append(template)
    state["candidate"]["generation"]["templates"].sort(key=lambda row: row["id"])


def _inject_generator_template(state):
    _candidate_fault(state, "E_GENERATOR_TEMPLATE", lambda value: value["generation"].update(generator="wrong-generator"))


def _inject_generator_target(state):
    _append_generator_template(state, {
        "base_case_id": "base-canonical", "id": "a-target", "operand": 0,
        "operation": "json_replace", "target": {"json_pointer": "not-a-pointer"},
    })
    state["paths"]["E_GENERATOR_TARGET"] = STAGE_A[0]


def _inject_generator_precondition(state):
    _append_generator_template(state, {
        "base_case_id": "base-canonical", "id": "b-precondition", "operand": 0,
        "operation": "json_replace", "target": {"json_pointer": "/missing"},
    })
    state["paths"]["E_GENERATOR_PRECONDITION"] = STAGE_A[0]


def _inject_generator_collision(state):
    _append_generator_template(state, {
        "base_case_id": "base-canonical", "id": "c-collision", "operand": 0,
        "operation": "json_replace", "target": {"json_pointer": "/a"},
    })
    state["collision"] = True
    state["paths"]["E_GENERATOR_COLLISION"] = STAGE_A[0]


def _inject_generator_limit(state):
    _append_generator_template(state, {
        "base_case_id": "base-canonical", "id": "z-limit", "operand": "x" * 65_534,
        "operation": "json_replace", "target": {"json_pointer": ""},
    })
    state["paths"]["E_GENERATOR_LIMIT"] = STAGE_A[0]


def _inject_semantic(state):
    state["module"] = state["module"].replace(
        b"from hashlib import sha256 as _sha256", b"import os                              ", 1,
    )
    state["paths"]["E_SEMANTIC"] = STAGE_A[2]


def _inject_internal(state):
    state["internal_fault"] = True
    state["paths"]["E_INTERNAL"] = "scripts/check-evaluation-contract-candidate.py"


FAULT_INJECTORS = {
    "E_ROOT": _inject_root,
    "E_SOURCE_DRIFT": _inject_source_drift,
    "E_SOURCE_LOCATOR": _inject_source_locator,
    "E_REQUIREMENT_DUPLICATE": _inject_requirement_duplicate,
    "E_REQUIREMENT_MISSING": _inject_requirement_missing,
    "E_IO": _inject_io,
    "E_LIMIT": _inject_limit,
    "E_UTF8": lambda state: _inject_raw(state, "E_UTF8"),
    "E_JSON": lambda state: _inject_raw(state, "E_JSON"),
    "E_DUPLICATE_KEY": lambda state: _inject_raw(state, "E_DUPLICATE_KEY"),
    "E_CANONICAL": lambda state: _inject_raw(state, "E_CANONICAL"),
    "E_SCHEMA": _inject_schema,
    "E_REF": _inject_ref,
    "E_REF_CYCLE": _inject_ref_cycle,
    "E_REF_UNUSED": _inject_ref_unused,
    "E_PATH": _inject_path,
    "E_DIGEST": _inject_digest,
    "E_ID": _inject_id,
    "E_CROSS_REFERENCE": _inject_cross_reference,
    "E_LINEAGE": _inject_lineage,
    "E_ORACLE": _inject_oracle,
    "E_GENERATOR_TEMPLATE": _inject_generator_template,
    "E_GENERATOR_TARGET": _inject_generator_target,
    "E_GENERATOR_PRECONDITION": _inject_generator_precondition,
    "E_GENERATOR_COLLISION": _inject_generator_collision,
    "E_GENERATOR_LIMIT": _inject_generator_limit,
    "E_SEMANTIC": _inject_semantic,
    "E_INTERNAL": _inject_internal,
}


def _inject_fault(state, code):
    assert code not in state["codes"]
    FAULT_INJECTORS[code](state)
    state["codes"].append(code)


def _bind_fault_bundle(state):
    checker = state["checker"]
    candidate = state["candidate"]
    report = state["report"]
    module = state["module"]
    if state["collision"]:
        templates = candidate["generation"]["templates"]
        template = next(row for row in templates if row["id"] == "c-collision")
        ordinal = templates.index(template)
        preimage = {
            "generator": candidate["generation"]["generator"],
            "seed": candidate["generation"]["seed"],
            "ordinal": ordinal,
            "template": template["id"],
        }
        collision_id = "g-" + sha256(checker._canonical(preimage).encode("ascii")).hexdigest()[:24]
        candidate["generation_base_cases"].append({"base_case_id": collision_id, "input": {"a": 1}})
    candidate["semantic_module"]["source_sha256"] = sha256(module).hexdigest()
    candidate = _redigest(checker, candidate)
    candidate_bytes = _canonical_bytes(checker, candidate)
    report["artifact_bindings"]["candidate_sha256"] = sha256(candidate_bytes).hexdigest()
    report["artifact_bindings"]["module_sha256"] = sha256(module).hexdigest()
    report["contract_digest"] = candidate["digest"]
    report["source_binding_sha256"] = sha256(module).hexdigest()
    report = _redigest(checker, report)
    root = state["root"]
    (root / STAGE_A[0]).write_bytes(candidate_bytes)
    (root / STAGE_A[1]).write_bytes(_canonical_bytes(checker, report))
    (root / STAGE_A[2]).write_bytes(module)

    raw = state["raw"]
    if raw:
        candidate_fault = set(state["codes"]) & {
            "E_SOURCE_DRIFT", "E_SOURCE_LOCATOR", "E_REQUIREMENT_DUPLICATE",
            "E_REQUIREMENT_MISSING", "E_SCHEMA", "E_REF", "E_REF_CYCLE",
            "E_REF_UNUSED", "E_PATH", "E_ID", "E_CROSS_REFERENCE",
            "E_GENERATOR_TEMPLATE", "E_GENERATOR_TARGET",
            "E_GENERATOR_PRECONDITION", "E_GENERATOR_COLLISION",
            "E_GENERATOR_LIMIT",
        }
        if len(raw) == 1:
            assignments = [(raw[0], STAGE_A[1] if candidate_fault else STAGE_A[0])]
        else:
            assignments = [(raw[0], STAGE_A[0]), (raw[1], STAGE_A[1])]
        for code, relative in assignments:
            path = root / relative
            data = path.read_bytes()
            if code == "E_UTF8":
                data = b"\xff" + data[1:]
            elif code == "E_JSON":
                data = b"{\n"
            elif code == "E_DUPLICATE_KEY":
                data = b'{"digest":"' + b"0" * 64 + b'",' + data[1:]
            else:
                data = data[:-1] + b" \n"
            path.write_bytes(data)
            state["paths"][code] = relative

    _refresh_fixture_locks(root, state["stale"])

    if state["io_fault"]:
        others = set(state["codes"]) - {"E_IO"}
        if not others:
            relative = STAGE_A[0]
        elif others & {"E_LINEAGE", "E_ORACLE"} or state["raw"]:
            relative = STAGE_A[2]
        else:
            relative = STAGE_A[1]
        (root / relative).unlink()
        state["paths"]["E_IO"] = relative
    script = root / "scripts/check-evaluation-contract-candidate.py"
    if state["root_fault"]:
        renamed = script.with_name("not-the-checker.py")
        script.rename(renamed)
        script = renamed
    state["script"] = script


def _run_fault_bundle(state):
    _bind_fault_bundle(state)
    if not state["internal_fault"]:
        return subprocess.run(
            [sys.executable, str(state["script"])], cwd=state["root"],
            capture_output=True, check=False,
        )
    launcher = (
        "import importlib.util;"
        f"p={str(state['script'])!r};"
        "s=importlib.util.spec_from_file_location('fault_checker',p);"
        "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
        "raise SystemExit(m.main(force_internal=True))"
    )
    return subprocess.run(
        [sys.executable, "-c", launcher], cwd=state["root"],
        capture_output=True, check=False,
    )


def _assert_public_failure(state, result, expected_code):
    checker = state["checker"]
    expected_exit = 2 if expected_code == "E_INTERNAL" else 1
    assert result.returncode == expected_exit
    assert result.stdout == b""
    assert result.stderr == _error(checker, expected_code, state["paths"][expected_code])


@pytest.mark.parametrize("code", PUBLIC_ERROR_CODES, ids=_fault_name)
def test_isolated_public_checker_fixture(tmp_path, checker, bundle, code):
    assert tuple(checker.ERROR_PRECEDENCE) == PUBLIC_ERROR_CODES
    state = _new_fault_bundle(tmp_path, checker, bundle)
    _inject_fault(state, code)

    result = _run_fault_bundle(state)

    assert state["codes"] == [code]
    _assert_public_failure(state, result, code)


def test_isolated_fixture_owner_names_are_closed_and_hyphenated():
    names = tuple(_fault_name(code) for code in PUBLIC_ERROR_CODES)
    assert len(names) == len(set(names)) == 28
    assert all(name.startswith("error-e-") and "_" not in name for name in names)


PAIR_REACHABILITY = {
    pair: {"reachable": True, "reason": None}
    for pair in ERROR_PAIRS
}


@pytest.mark.parametrize(
    "pair", ERROR_PAIRS,
    ids=lambda pair: _fault_name(pair[0]) + "--" + _fault_name(pair[1]),
)
def test_public_checker_unordered_pair_priority(tmp_path, checker, bundle, pair):
    row = PAIR_REACHABILITY[pair]
    if not row["reachable"]:
        assert row["reason"] and "unreachable" in row["reason"]
        return
    state = _new_fault_bundle(tmp_path, checker, bundle)
    for code in pair:
        _inject_fault(state, code)

    result = _run_fault_bundle(state)

    assert state["codes"] == list(pair)
    _assert_public_failure(state, result, pair[0])


def test_pair_reachability_table_is_exhaustive_and_executes_every_reachable_pair():
    assert tuple(PAIR_REACHABILITY) == ERROR_PAIRS
    assert len(PAIR_REACHABILITY) == 378
    assert sum(row["reachable"] for row in PAIR_REACHABILITY.values()) == 378
    assert sum(not row["reachable"] for row in PAIR_REACHABILITY.values()) == 0
    assert all(row["reason"] is None for row in PAIR_REACHABILITY.values())


def _assert_module_execution_blocked(monkeypatch, root, expected_path):
    copied_checker = _load_checker(root / "scripts/check-evaluation-contract-candidate.py")
    calls = []

    def forbidden_audit(*_args):
        calls.append("module audit")

        def forbidden_evaluate(*_evaluate_args):
            calls.append("vector execution")

        return forbidden_evaluate

    monkeypatch.setattr(
        copied_checker, "_audit_and_load_module", forbidden_audit,
    )
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(copied_checker.CheckFailure) as caught:
            copied_checker._check_bundle(root_fd)
    finally:
        os.close(root_fd)
    assert (caught.value.code, caught.value.path) == ("E_DIGEST", expected_path)
    assert calls == []


@pytest.mark.parametrize("relative", STAGE_A, ids=("candidate", "report", "module"))
def test_stale_whole_file_lock_blocks_module_execution(
        tmp_path, checker, bundle, monkeypatch, relative):
    state = _new_fault_bundle(tmp_path, checker, bundle)
    if relative == STAGE_A[0]:
        state["candidate"]["authority_policy"]["scope_prefix"] += "fixture/"
    elif relative == STAGE_A[1]:
        state["report"]["requirement_coverage"][0]["reason"] += " Fixture."
    else:
        state["module"] += b"# stale whole-file lock\n"
    state["stale"].add(relative)
    _bind_fault_bundle(state)

    _assert_module_execution_blocked(monkeypatch, state["root"], relative)


def test_wrong_module_binding_blocks_ast_load(tmp_path, checker, bundle, monkeypatch):
    state = _new_fault_bundle(tmp_path, checker, bundle)
    _bind_fault_bundle(state)
    root = state["root"]
    candidate = json.loads((root / STAGE_A[0]).read_text())
    candidate["semantic_module"]["source_sha256"] = "0" * 64
    candidate = _redigest(checker, candidate)
    candidate_bytes = _canonical_bytes(checker, candidate)
    (root / STAGE_A[0]).write_bytes(candidate_bytes)
    report = json.loads((root / STAGE_A[1]).read_text())
    report["artifact_bindings"]["candidate_sha256"] = sha256(candidate_bytes).hexdigest()
    report["contract_digest"] = candidate["digest"]
    (root / STAGE_A[1]).write_bytes(_canonical_bytes(checker, _redigest(checker, report)))
    shutil.copyfile(SCRIPT, root / "scripts/check-evaluation-contract-candidate.py")
    _refresh_fixture_locks(root)

    _assert_module_execution_blocked(monkeypatch, root, STAGE_A[2])


@pytest.mark.parametrize("relative", STAGE_A, ids=("candidate", "report", "module"))
def test_utf8_precedes_stale_digest_for_every_artifact(tmp_path, checker, relative):
    root = _copy_bundle(tmp_path)
    path = root / relative
    path.write_bytes(b"\xff" + path.read_bytes()[1:])

    result = _run(root)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == _error(checker, "E_UTF8", relative)


@pytest.mark.parametrize(
    ("relative", "data"),
    ((STAGE_A[0], b"[]\n"), (STAGE_A[1], b"null\n")),
    ids=("candidate-array", "report-null"),
)
def test_non_object_schema_precedes_stale_digest(tmp_path, checker, relative, data):
    root = _copy_bundle(tmp_path)
    (root / relative).write_bytes(data)

    result = _run(root)

    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr == _error(checker, "E_SCHEMA", relative)


def _bind_generation_stream(checker, candidate):
    stream = bytearray()
    bases = {row["base_case_id"]: row["input"] for row in candidate["generation_base_cases"]}
    for ordinal, item in enumerate(candidate["generation"]["templates"]):
        preimage = {
            "generator": candidate["generation"]["generator"],
            "seed": candidate["generation"]["seed"],
            "ordinal": ordinal,
            "template": item["id"],
        }
        generated_id = "g-" + sha256(checker._canonical(preimage).encode("ascii")).hexdigest()[:24]
        payload = checker._generate_payload(bases[item["base_case_id"]], item)
        stream.extend((checker._canonical({"id": generated_id, "input_bytes_hex": payload.hex()}) + "\n").encode("ascii"))
    candidate["generated_stream_sha256"] = sha256(stream).hexdigest()
    return len(stream)


def test_generated_case_byte_limit_routes_through_generation_validation(checker, bundle):
    candidate, _, _ = bundle
    exact = deepcopy(candidate)
    exact["generation"]["templates"] = [{
        "base_case_id": "base-canonical", "id": "exact-generated-case-bytes",
        "operand": "x" * 65_533, "operation": "json_replace",
        "target": {"json_pointer": ""},
    }]
    _bind_generation_stream(checker, exact)
    checker._validate_generation(exact)

    plus_one = deepcopy(exact)
    plus_one["generation"]["templates"][0]["operand"] += "x"
    with pytest.raises(checker.CheckFailure) as caught:
        checker._validate_generation(plus_one)
    assert caught.value.code == "E_GENERATOR_LIMIT"


def _candidate_with_generated_corpus_size(checker, candidate, target, template_count):
    sized = deepcopy(candidate)
    payload_total, parity = divmod(target - 57 * template_count, 2)
    assert parity == 0
    payload_size, larger_count = divmod(payload_total, template_count)
    assert 3 <= payload_size <= checker.LIMITS["generated_case_bytes"]
    sized["generation"]["templates"] = [
        {
            "base_case_id": "base-canonical", "id": f"corpus-{index:03d}",
            "operand": "x" * (payload_size + (index < larger_count) - 3),
            "operation": "json_replace", "target": {"json_pointer": ""},
        }
        for index in range(template_count)
    ]
    assert _bind_generation_stream(checker, sized) == target
    return sized


def test_generated_corpus_byte_limit_routes_through_generation_validation(checker, bundle):
    candidate, _, _ = bundle
    exact = _candidate_with_generated_corpus_size(
        checker, candidate, checker.LIMITS["generated_corpus_bytes"], 256,
    )
    checker._validate_generation(exact)

    plus_one = _candidate_with_generated_corpus_size(
        checker, candidate, checker.LIMITS["generated_corpus_bytes"] + 1, 255,
    )
    with pytest.raises(checker.CheckFailure) as caught:
        checker._validate_generation(plus_one)
    assert caught.value.code == "E_GENERATOR_LIMIT"


def test_generator_template_bound_accepts_256_and_rejects_257(checker, bundle):
    candidate, _, _ = bundle
    exact = deepcopy(candidate)
    template = exact["generation"]["templates"][0]
    exact["generation"]["templates"] = [
        {**template, "id": f"replace-{index:03d}", "operand": index}
        for index in range(checker.LIMITS["generated_cases"])
    ]
    _bind_generation_stream(checker, exact)
    checker._validate_generation(exact)

    too_many = deepcopy(exact)
    too_many["generation"]["templates"].append(
        {**too_many["generation"]["templates"][-1], "id": "replace-256", "operand": 256}
    )
    with pytest.raises(checker.CheckFailure) as caught:
        checker._validate_generation(too_many)
    assert caught.value.code == "E_GENERATOR_LIMIT"
