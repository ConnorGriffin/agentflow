#!/usr/bin/env python3
"""Validate the locked Evaluation v1 semantic authority bundle."""

from __future__ import annotations

import ast
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import re
import stat
import sys


FORMAT = "evaluation-contract-candidate-check-v1"
CANDIDATE_PATH = "docs/evaluation/design/contract-v1.candidate.json"
REPORT_PATH = "docs/evaluation/design/contract-v1.conformance.json"
MODULE_PATH = "agentflow/evaluation_semantics_v1.py"
SCRIPT_PATH = "scripts/check-evaluation-contract-candidate.py"
ARTIFACT_PATHS = (CANDIDATE_PATH, REPORT_PATH, MODULE_PATH)
WHOLE_FILE_SHA256 = {
    CANDIDATE_PATH: "53359f35de57047441defa76a477564580b956f968ab6425356cca3a1c5a8409",
    REPORT_PATH: "1c477cc45b49cc66e4b7751d4961617d6674c809179f2f370691058ba5d53915",
    MODULE_PATH: "185f41a5e4549cc1ccbc4615af5846c3ed0f95285790d193e1b2f43aa3dc8554",
}
ERROR_PRECEDENCE = (
    "E_ROOT", "E_SOURCE_DRIFT", "E_SOURCE_LOCATOR", "E_REQUIREMENT_DUPLICATE",
    "E_REQUIREMENT_MISSING", "E_IO", "E_LIMIT", "E_UTF8", "E_JSON",
    "E_DUPLICATE_KEY", "E_CANONICAL", "E_SCHEMA", "E_REF", "E_REF_CYCLE",
    "E_REF_UNUSED", "E_PATH", "E_DIGEST", "E_ID", "E_CROSS_REFERENCE",
    "E_LINEAGE", "E_ORACLE", "E_GENERATOR_TEMPLATE", "E_GENERATOR_TARGET",
    "E_GENERATOR_PRECONDITION", "E_GENERATOR_COLLISION", "E_GENERATOR_LIMIT",
    "E_SEMANTIC", "E_INTERNAL",
)
LIMITS = {
    "definitions": 64,
    "generated_case_bytes": 65_536,
    "generated_cases": 256,
    "generated_corpus_bytes": 8_388_608,
    "json_artifact_bytes": 1_048_576,
    "json_nesting": 32,
    "module_source_bytes": 65_536,
    "object_or_array_entries": 256,
    "path_bytes": 160,
    "path_depth": 12,
    "references_per_schema": 64,
    "stdout_or_stderr_bytes": 4_096,
}
SOURCE_BINDINGS = (
    ("issue-583", "issue/583/", "body-lf", "cdbaa62e34b3943fbbd2f3f63edf0b0cf17b00e3632983f8ab31506b89238c9d", None),
    ("adr-605", "adr/605/", "f5580b55cf373a7e9de47d99e617b08256b7647d", "6977d6e1ce0bf5ebcaaff4fb2f47112dd59208705fd739ab394aa26bc589e70f", "docs/adr/adr-605-canonical-evaluation-rulebook.md"),
    ("adr-606", "adr/606/", "f5580b55cf373a7e9de47d99e617b08256b7647d", "4bde5dd87bcf4002de60c5a7a07f366fdea274e628dd24604ce5fd2495e4967b", "docs/adr/adr-606-explicit-missing-metrics-and-adjudication-lineage.md"),
    ("adr-620", "adr/620/", "3cd31b7d5528a6bb5bb322334a32a25ac13991b5", "7aed248b63d8035364114a28eb184c0aa839b55c627f5de3d9d17e1af1b1cb9a", "docs/adr/adr-620-evaluation-failure-classes.md"),
    ("adr-626", "adr/626/", "c13cc6b77bac94ab71b4c689aeef7f2eaa242be3", "6bccfb0848fd1ad985c1442e1cb8dbb633ded8bd0f10878e80b0876c9e17a13e", "docs/adr/adr-626-manifest-rooted-evaluation-semantic-bundle.md"),
)
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
PATH_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")


class CheckFailure(Exception):
    def __init__(self, code: str, path: str):
        super().__init__(code)
        self.code = code
        self.path = path


class _DuplicateKey(ValueError):
    pass


def _fail(code: str, path: str) -> None:
    raise CheckFailure(code, path)


def _canonical_string(value: str) -> str:
    parts = ['"']
    index = 0
    while index < len(value):
        code = ord(value[index])
        if code == 0x22:
            parts.append('\\"')
        elif code == 0x5C:
            parts.append("\\\\")
        elif code < 0x20:
            parts.append(f"\\u{code:04x}")
        elif code < 0x80:
            parts.append(chr(code))
        elif 0xD800 <= code <= 0xDBFF:
            if index + 1 >= len(value) or not 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
                raise ValueError("surrogate")
            parts.append(f"\\u{code:04x}\\u{ord(value[index + 1]):04x}")
            index += 1
        elif 0xDC00 <= code <= 0xDFFF:
            raise ValueError("surrogate")
        elif code <= 0xFFFF:
            parts.append(f"\\u{code:04x}")
        else:
            scalar = code - 0x10000
            parts.append(f"\\u{0xD800 + (scalar >> 10):04x}\\u{0xDC00 + (scalar & 0x3FF):04x}")
        index += 1
    parts.append('"')
    return "".join(parts)


def _canonical(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        rendered = format(value, "f")
        if "." not in rendered or rendered.endswith("0"):
            raise ValueError("decimal")
        return rendered
    if isinstance(value, str):
        return _canonical_string(value)
    if isinstance(value, list):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("key")
        return "{" + ",".join(
            _canonical_string(key) + ":" + _canonical(value[key]) for key in sorted(value)
        ) + "}"
    raise ValueError("value")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(key)
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("constant")


def _json_depth_and_entries(value, depth=0):
    if isinstance(value, (dict, list)):
        depth += 1
        if depth > LIMITS["json_nesting"]:
            raise CheckFailure("E_LIMIT", "")
        if len(value) > LIMITS["object_or_array_entries"]:
            raise CheckFailure("E_LIMIT", "")
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            _json_depth_and_entries(child, depth)


def _decode_json(data: bytes, path: str):
    if len(data) > LIMITS["json_artifact_bytes"]:
        _fail("E_LIMIT", path)
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        _fail("E_UTF8", path)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _fail("E_UTF8", path)
    try:
        value = json.loads(
            text, object_pairs_hook=_object, parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        _fail("E_DUPLICATE_KEY", path)
    except (ValueError, json.JSONDecodeError):
        _fail("E_JSON", path)
    try:
        _json_depth_and_entries(value)
    except CheckFailure as error:
        error.path = path
        raise
    try:
        canonical = (_canonical(value) + "\n").encode("ascii")
    except (UnicodeError, ValueError):
        _fail("E_CANONICAL", path)
    if canonical != data:
        _fail("E_CANONICAL", path)
    return value


def _digest_record(record) -> str:
    if not isinstance(record, dict) or not DIGEST_RE.fullmatch(str(record.get("digest", ""))):
        raise ValueError("digest")
    preimage = {key: value for key, value in record.items() if key != "digest"}
    return sha256(_canonical(preimage).encode("ascii")).hexdigest()


def _require_keys(value, keys, path, code="E_SCHEMA"):
    if not isinstance(value, dict) or set(value) != set(keys):
        _fail(code, path)


def _valid_id(value) -> bool:
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def _validate_path(value, path):
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > LIMITS["path_bytes"]:
        _fail("E_PATH", path)
    concrete = re.sub(r"\{[a-z][a-z0-9-]*\}", "x", value)
    pieces = concrete.split("/")
    if (not PATH_RE.fullmatch(concrete) or len(pieces) > LIMITS["path_depth"]
            or any(piece in ("", ".", "..") for piece in pieces)):
        _fail("E_PATH", path)


def _requirement_locators():
    locators = ["issue/583/outcome/p1", "issue/583/scope/p1", "issue/583/scope/p2"]
    locators += [f"issue/583/versioned-contract/b{n}" for n in range(1, 7)]
    locators += [f"issue/583/eligibility-gates/n{n}" for n in range(1, 6)]
    locators += [f"issue/583/acceptance/a{n}" for n in range(1, 15)]
    locators += [f"issue/583/out-of-scope/o{n}" for n in range(1, 6)]
    locators += [f"adr/605/decision/p{n}" for n in range(1, 4)]
    locators += [f"adr/606/decision/p{n}" for n in range(1, 6)]
    locators += ["adr/620/decision/intro/p1"]
    locators += [f"adr/620/decision/class/r{n}" for n in range(1, 7)]
    locators += ["adr/620/decision/orthogonality/p1", "adr/620/decision/governance/p1"]
    locators += [
        "adr/626/decision/bundle/p1", "adr/626/decision/binding/intro-p1",
        "adr/626/decision/binding/code1", "adr/626/decision/interface/intro-p1",
        "adr/626/decision/interface/code1", "adr/626/decision/operation/p1",
        "adr/626/decision/purity/p1", "adr/626/decision/open-set/intro-p1",
        "adr/626/decision/open-set/n1", "adr/626/decision/open-set/n2",
        "adr/626/decision/open-set/n3", "adr/626/decision/locks/p1",
        "adr/626/decision/loading/p1", "adr/626/decision/checker/p1",
        "adr/626/decision/independence/p1", "adr/626/decision/supersession/p1",
    ]
    return locators


LOCATORS = tuple(_requirement_locators())
REQUIREMENT_IDS = tuple("eval-v1:" + locator for locator in LOCATORS)


def _expected_source_id(locator):
    if locator.startswith("issue/583/"):
        return "issue-583"
    return "adr-" + locator.split("/")[1]


def _expected_owner(locator):
    if locator == "issue/583/scope/p2" or locator == "issue/583/out-of-scope/o1":
        return "runner"
    if locator in {"issue/583/out-of-scope/o2", "issue/583/out-of-scope/o3", "issue/583/out-of-scope/o5"}:
        return "product-owner"
    if locator in {"issue/583/acceptance/a2", "issue/583/acceptance/a3", "issue/583/acceptance/a9", "issue/583/acceptance/a11", "issue/583/out-of-scope/o4"}:
        return "fixture-author"
    if locator in {"issue/583/acceptance/a13", "issue/583/acceptance/a14", "adr/605/decision/p2"}:
        return "implementation-boundary"
    if locator in {
        "adr/605/decision/p1", "adr/626/decision/bundle/p1",
        "adr/626/decision/operation/p1", "adr/626/decision/supersession/p1",
    }:
        return "semantic-bundle"
    if locator == "issue/583/acceptance/a12" or "/open-set/" in locator or locator in {
        "adr/626/decision/locks/p1", "adr/626/decision/loading/p1",
        "adr/626/decision/checker/p1", "adr/626/decision/independence/p1",
    }:
        return "checker"
    module_locators = {
        "issue/583/versioned-contract/b2", "issue/583/versioned-contract/b3",
        "issue/583/versioned-contract/b4", "issue/583/versioned-contract/b6",
        "issue/583/eligibility-gates/n1", "issue/583/eligibility-gates/n2",
        "issue/583/eligibility-gates/n3", "issue/583/eligibility-gates/n4",
        "issue/583/eligibility-gates/n5", "issue/583/acceptance/a4",
        "issue/583/acceptance/a5", "issue/583/acceptance/a6",
        "issue/583/acceptance/a7", "issue/583/acceptance/a8",
        "issue/583/acceptance/a10", "adr/605/decision/p3", "adr/606/decision/p4",
        "adr/606/decision/p5", "adr/626/decision/interface/intro-p1",
        "adr/626/decision/interface/code1", "adr/626/decision/purity/p1",
    }
    return "semantic-module" if locator in module_locators else "canonical-contract"


def _validate_source_and_requirements(candidate):
    bindings = candidate.get("source_bindings")
    if not isinstance(bindings, list) or len(bindings) != len(SOURCE_BINDINGS):
        _fail("E_SOURCE_DRIFT", CANDIDATE_PATH)
    for actual, expected in zip(bindings, SOURCE_BINDINGS):
        source_id, prefix, revision, digest, path = expected
        wanted = {
            "kind": "github-issue-body" if path is None else "repository-file",
            "locator_prefix": prefix, "revision": revision, "sha256": digest,
            "source_id": source_id,
        }
        if path is not None:
            wanted["path"] = path
        if actual != wanted:
            _fail("E_SOURCE_DRIFT", CANDIDATE_PATH)
    requirements = candidate.get("requirements")
    if not isinstance(requirements, list):
        _fail("E_REQUIREMENT_MISSING", CANDIDATE_PATH)
    ids = [row.get("requirement_id") for row in requirements if isinstance(row, dict)]
    locators = [row.get("source_locator") for row in requirements if isinstance(row, dict)]
    if len(ids) != len(set(ids)) or len(locators) != len(set(locators)):
        _fail("E_REQUIREMENT_DUPLICATE", CANDIDATE_PATH)
    unknown = [locator for locator in locators if locator not in LOCATORS]
    if unknown:
        _fail("E_SOURCE_LOCATOR", CANDIDATE_PATH)
    if tuple(ids) != REQUIREMENT_IDS or tuple(locators) != LOCATORS:
        _fail("E_REQUIREMENT_MISSING", CANDIDATE_PATH)
    for row, locator in zip(requirements, LOCATORS):
        if row != {
            "requirement_id": "eval-v1:" + locator,
            "source_id": _expected_source_id(locator),
            "source_locator": locator,
        }:
            _fail("E_SOURCE_LOCATOR", CANDIDATE_PATH)


_NODE_KEYS = {
    "object": {"type", "required", "properties", "additional_properties"},
    "array": {"type", "items", "min_items", "max_items"},
    "string": {"type", "pattern", "min_length", "max_length"},
    "integer": {"type", "minimum", "maximum"},
    "boolean": {"type"}, "null": {"type"}, "ref": {"type", "ref"},
}


def _validate_schema_node(node, definitions, references, path):
    if not isinstance(node, dict) or node.get("type") not in _NODE_KEYS:
        _fail("E_SCHEMA", path)
    kind = node["type"]
    if set(node) != _NODE_KEYS[kind]:
        _fail("E_SCHEMA", path)
    if kind == "object":
        if node["additional_properties"] is not False or not isinstance(node["required"], list) or not isinstance(node["properties"], dict):
            _fail("E_SCHEMA", path)
        if node["required"] != sorted(set(node["required"])) or not set(node["required"]) <= set(node["properties"]):
            _fail("E_SCHEMA", path)
        for key in sorted(node["properties"]):
            _validate_schema_node(node["properties"][key], definitions, references, path)
    elif kind == "array":
        if (not isinstance(node["min_items"], int) or isinstance(node["min_items"], bool)
                or not isinstance(node["max_items"], int) or isinstance(node["max_items"], bool)
                or node["min_items"] < 0
                or node["min_items"] > node["max_items"]):
            _fail("E_SCHEMA", path)
        _validate_schema_node(node["items"], definitions, references, path)
    elif kind == "string":
        if (not isinstance(node["pattern"], str) or not isinstance(node["min_length"], int)
                or isinstance(node["min_length"], bool) or not isinstance(node["max_length"], int)
                or isinstance(node["max_length"], bool) or node["min_length"] < 0
                or node["min_length"] > node["max_length"]):
            _fail("E_SCHEMA", path)
        try:
            re.compile(node["pattern"])
        except re.error:
            _fail("E_SCHEMA", path)
    elif kind == "integer":
        if (not isinstance(node["minimum"], int) or isinstance(node["minimum"], bool)
                or not isinstance(node["maximum"], int) or isinstance(node["maximum"], bool)
                or node["minimum"] > node["maximum"]):
            _fail("E_SCHEMA", path)
    elif kind == "ref":
        match = re.fullmatch(r"#/definitions/([a-z][a-z0-9-]{0,47})", node["ref"] if isinstance(node["ref"], str) else "")
        if match is None or match.group(1) not in definitions:
            _fail("E_REF", path)
        references.append(match.group(1))


def _schema_refs(node):
    result = []
    if isinstance(node, dict):
        if node.get("type") == "ref":
            result.append(node["ref"].removeprefix("#/definitions/"))
        for value in node.values():
            result.extend(_schema_refs(value))
    elif isinstance(node, list):
        for value in node:
            result.extend(_schema_refs(value))
    return result


def _validate_schema(schema, path, *, enforce_reference_limit=True):
    _require_keys(schema, {"schema_version", "root", "definitions"}, path)
    if schema["schema_version"] != "evaluation-schema-v1" or not isinstance(schema["definitions"], dict):
        _fail("E_SCHEMA", path)
    definitions = schema["definitions"]
    if len(definitions) > LIMITS["definitions"] or not all(_valid_id(key) for key in definitions):
        _fail("E_LIMIT" if len(definitions) > LIMITS["definitions"] else "E_ID", path)
    references = []
    _validate_schema_node(schema["root"], definitions, references, path)
    for name in sorted(definitions):
        _validate_schema_node(definitions[name], definitions, references, path)
    if enforce_reference_limit and len(references) > LIMITS["references_per_schema"]:
        _fail("E_LIMIT", path)
    graph = {name: _schema_refs(definitions[name]) for name in definitions}
    visiting, visited = set(), set()
    def visit(name):
        if name in visiting:
            _fail("E_REF_CYCLE", path)
        if name not in visited:
            visiting.add(name)
            for target in graph[name]:
                visit(target)
            visiting.remove(name)
            visited.add(name)
    for root_ref in _schema_refs(schema["root"]):
        visit(root_ref)
    if set(definitions) - visited:
        _fail("E_REF_UNUSED", path)


def _validate_value(value, node, definitions):
    kind = node["type"]
    if kind == "ref":
        return _validate_value(value, definitions[node["ref"].split("/")[-1]], definitions)
    if kind == "object":
        if (not isinstance(value, dict) or not set(node["required"]) <= set(value)
                or not set(value) <= set(node["properties"])):
            return False
        return all(_validate_value(value[key], node["properties"][key], definitions) for key in value)
    if kind == "array":
        return (isinstance(value, list) and node["min_items"] <= len(value) <= node["max_items"]
                and all(_validate_value(item, node["items"], definitions) for item in value))
    if kind == "string":
        return (isinstance(value, str) and node["min_length"] <= len(value) <= node["max_length"]
                and re.fullmatch(node["pattern"], value) is not None)
    if kind == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)
                and node["minimum"] <= value <= node["maximum"])
    if kind == "boolean":
        return isinstance(value, bool)
    return value is None


def _validate_candidate(candidate, module_digest):
    expected_keys = {
        "artifact_registry", "authority_policy", "bootstrap_policy", "canonical_json",
        "checker_boundary", "contract_version", "corpus_policy", "digest", "digest_policy",
        "evidence_policy", "failure_classes", "gate_policy", "generated_stream_sha256",
        "generation", "generation_base_cases", "implementation_boundary", "lifecycle_policy",
        "limits", "manifest_policy", "metric_policy", "missingness_policy",
        "module_constraints", "operation_contracts", "operation_ids", "path_policy",
        "requirements", "rules", "schedule_policy", "schema_catalog", "schemas",
        "semantic_errors", "semantic_module", "source_bindings", "status_truth_table",
    }
    _require_keys(candidate, expected_keys, CANDIDATE_PATH)
    _validate_source_and_requirements(candidate)
    if candidate["contract_version"] != "evaluation-contract-v1" or candidate["limits"] != LIMITS:
        _fail("E_SCHEMA", CANDIDATE_PATH)
    if candidate["checker_boundary"].get("artifact_order") != list(ARTIFACT_PATHS) or candidate["checker_boundary"].get("error_precedence") != list(ERROR_PRECEDENCE):
        _fail("E_SCHEMA", CANDIDATE_PATH)
    binding = candidate["semantic_module"]
    _require_keys(binding, {"interface_version", "path", "source_sha256"}, CANDIDATE_PATH)
    _validate_path(binding["path"], CANDIDATE_PATH)
    if binding["interface_version"] != "evaluation-semantics-v1" or binding["path"] != MODULE_PATH:
        _fail("E_SCHEMA", CANDIDATE_PATH)
    if binding["source_sha256"] != module_digest:
        _fail("E_DIGEST", CANDIDATE_PATH)
    schemas = candidate["schemas"]
    if not isinstance(schemas, dict) or not schemas or not all(_valid_id(name) for name in schemas):
        _fail("E_SCHEMA", CANDIDATE_PATH)
    _validate_schema(candidate["schema_catalog"], CANDIDATE_PATH, enforce_reference_limit=False)
    for name in sorted(schemas):
        _validate_schema(schemas[name], CANDIDATE_PATH)
    rules = candidate["rules"]
    rule_ids = [row.get("rule_id") for row in rules if isinstance(row, dict)]
    if len(rule_ids) != len(rules) or len(rule_ids) != len(set(rule_ids)) or not all(_valid_id(item) for item in rule_ids):
        _fail("E_ID", CANDIDATE_PATH)
    operation_ids = list(candidate["operation_ids"].values())
    if len(operation_ids) != len(set(operation_ids)) or not all(_valid_id(item) for item in operation_ids):
        _fail("E_ID", CANDIDATE_PATH)
    contracts = candidate["operation_contracts"]
    if {row.get("operation_id") for row in contracts} != set(operation_ids):
        _fail("E_CROSS_REFERENCE", CANDIDATE_PATH)
    for row in contracts:
        _require_keys(row, {"operation_id", "input_schema", "success_schema", "error_schema"}, CANDIDATE_PATH)
        if not {row["input_schema"], row["success_schema"], row["error_schema"]} <= set(schemas):
            _fail("E_CROSS_REFERENCE", CANDIDATE_PATH)
    for row in candidate["artifact_registry"]:
        if not isinstance(row, dict) or "path" not in row or "permitted_root" not in row:
            _fail("E_SCHEMA", CANDIDATE_PATH)
        _validate_path(row["path"], CANDIDATE_PATH)
        _validate_path(row["permitted_root"], CANDIDATE_PATH)
        if "payload_schema" in row and row["payload_schema"] not in candidate["schema_catalog"]["definitions"]:
            _fail("E_CROSS_REFERENCE", CANDIDATE_PATH)
    for row in candidate["source_bindings"]:
        if "path" in row:
            _validate_path(row["path"], CANDIDATE_PATH)
    if candidate["digest"] != _digest_record(candidate):
        _fail("E_DIGEST", CANDIDATE_PATH)
    _validate_generation(candidate)


def _pointer_parent(value, pointer):
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise CheckFailure("E_GENERATOR_TARGET", CANDIDATE_PATH)
    tokens = [] if pointer == "" else pointer[1:].split("/")
    decoded = []
    for token in tokens:
        if re.search(r"~(?![01])", token):
            raise CheckFailure("E_GENERATOR_TARGET", CANDIDATE_PATH)
        decoded.append(token.replace("~1", "/").replace("~0", "~"))
    if not decoded:
        return None, None, value
    parent = value
    for token in decoded[:-1]:
        if isinstance(parent, dict) and token in parent:
            parent = parent[token]
        elif isinstance(parent, list) and token.isdigit() and int(token) < len(parent):
            parent = parent[int(token)]
        else:
            raise CheckFailure("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
    token = decoded[-1]
    if isinstance(parent, list) and token.isdigit():
        token = int(token)
    return parent, token, value


def _raw_duplicate(data, offset, operand):
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset < len(data) or data[offset:offset + 1] != b"}":
        raise CheckFailure("E_GENERATOR_TARGET", CANDIDATE_PATH)
    try:
        prefix = json.loads(data[:offset + 1].decode("ascii"), object_pairs_hook=_object)
    except (ValueError, json.JSONDecodeError):
        raise CheckFailure("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
    if not isinstance(prefix, dict) or operand["key"] not in prefix:
        raise CheckFailure("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
    insertion = ("," + _canonical(operand["key"]) + ":" + _canonical(operand["value"])).encode("ascii")
    return data[:offset] + insertion + data[offset:]


def _generate_payload(base, template):
    operation = template["operation"]
    operand = template["operand"]
    target = template["target"]
    copied = deepcopy(base)
    base_bytes = _canonical(base).encode("ascii")
    if operation.startswith("json_"):
        if set(target) != {"json_pointer"}:
            _fail("E_GENERATOR_TARGET", CANDIDATE_PATH)
        parent, token, _ = _pointer_parent(copied, target["json_pointer"])
        if operation == "json_replace":
            if parent is None:
                copied = deepcopy(operand)
            elif isinstance(parent, dict) and token in parent:
                parent[token] = deepcopy(operand)
            elif isinstance(parent, list) and isinstance(token, int) and token < len(parent):
                parent[token] = deepcopy(operand)
            else:
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
        elif operation == "json_remove":
            if operand is not None or parent is None:
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
            if isinstance(parent, dict) and token in parent:
                del parent[token]
            elif isinstance(parent, list) and isinstance(token, int) and token < len(parent):
                del parent[token]
            else:
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
        elif operation == "json_object_insert":
            if parent is None:
                target_value = copied
            elif isinstance(parent, dict) and token in parent:
                target_value = parent[token]
            elif isinstance(parent, list) and isinstance(token, int) and token < len(parent):
                target_value = parent[token]
            else:
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
            if not isinstance(operand, dict) or set(operand) != {"key", "value"} or not isinstance(target_value, dict) or operand["key"] in target_value:
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
            target_value[operand["key"]] = deepcopy(operand["value"])
        elif operation == "json_array_insert":
            if parent is None:
                target_value = copied
            elif isinstance(parent, dict) and token in parent:
                target_value = parent[token]
            elif isinstance(parent, list) and isinstance(token, int) and token < len(parent):
                target_value = parent[token]
            else:
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
            if not isinstance(operand, dict) or set(operand) != {"index", "value"} or not isinstance(target_value, list):
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
            index = operand["index"]
            if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index <= len(target_value):
                _fail("E_GENERATOR_PRECONDITION", CANDIDATE_PATH)
            target_value.insert(index, deepcopy(operand["value"]))
        else:
            _fail("E_GENERATOR_TEMPLATE", CANDIDATE_PATH)
        return (_canonical(copied) + "\n").encode("ascii")
    if set(target) != {"raw_byte_offset"}:
        _fail("E_GENERATOR_TARGET", CANDIDATE_PATH)
    offset = target["raw_byte_offset"]
    if not isinstance(offset, int) or isinstance(offset, bool) or not 0 <= offset < len(base_bytes):
        _fail("E_GENERATOR_TARGET", CANDIDATE_PATH)
    if operation == "raw_truncate" and operand is None:
        return base_bytes[:offset]
    if operation == "raw_byte_replace" and isinstance(operand, int) and not isinstance(operand, bool) and 0 <= operand <= 255:
        return base_bytes[:offset] + bytes([operand]) + base_bytes[offset + 1:]
    if operation == "raw_duplicate_key_inject" and isinstance(operand, dict) and set(operand) == {"key", "value"} and isinstance(operand["key"], str):
        return _raw_duplicate(base_bytes, offset, operand)
    _fail("E_GENERATOR_TEMPLATE", CANDIDATE_PATH)


def _validate_generation(candidate):
    generation = candidate["generation"]
    _require_keys(generation, {"generator", "seed", "templates"}, CANDIDATE_PATH, "E_GENERATOR_TEMPLATE")
    if generation["generator"] != "evaluation-v1-casegen" or not isinstance(generation["seed"], int) or isinstance(generation["seed"], bool) or not 0 <= generation["seed"] <= 18_446_744_073_709_551_615:
        _fail("E_GENERATOR_TEMPLATE", CANDIDATE_PATH)
    templates = generation["templates"]
    if not isinstance(templates, list) or not templates or len(templates) > LIMITS["generated_cases"]:
        _fail("E_GENERATOR_LIMIT", CANDIDATE_PATH)
    ids = [row.get("id") for row in templates if isinstance(row, dict)]
    if ids != sorted(ids) or len(ids) != len(set(ids)) or not all(_valid_id(item) for item in ids):
        _fail("E_GENERATOR_TEMPLATE", CANDIDATE_PATH)
    bases = candidate["generation_base_cases"]
    if not isinstance(bases, list):
        _fail("E_GENERATOR_TEMPLATE", CANDIDATE_PATH)
    base_map = {row.get("base_case_id"): row.get("input") for row in bases if isinstance(row, dict) and set(row) == {"base_case_id", "input"}}
    if len(base_map) != len(bases) or not all(_valid_id(item) for item in base_map):
        _fail("E_GENERATOR_TEMPLATE", CANDIDATE_PATH)
    generated_ids = set()
    stream = bytearray()
    for ordinal, template in enumerate(templates):
        _require_keys(template, {"base_case_id", "id", "operation", "operand", "target"}, CANDIDATE_PATH, "E_GENERATOR_TEMPLATE")
        if template["base_case_id"] not in base_map:
            _fail("E_GENERATOR_TEMPLATE", CANDIDATE_PATH)
        preimage = {"generator": "evaluation-v1-casegen", "seed": generation["seed"], "ordinal": ordinal, "template": template["id"]}
        generated_id = "g-" + sha256(_canonical(preimage).encode("ascii")).hexdigest()[:24]
        if generated_id in generated_ids or generated_id in base_map:
            _fail("E_GENERATOR_COLLISION", CANDIDATE_PATH)
        generated_ids.add(generated_id)
        payload = _generate_payload(base_map[template["base_case_id"]], template)
        if len(payload) > LIMITS["generated_case_bytes"]:
            _fail("E_GENERATOR_LIMIT", CANDIDATE_PATH)
        stream.extend((_canonical({"id": generated_id, "input_bytes_hex": payload.hex()}) + "\n").encode("ascii"))
        if len(stream) > LIMITS["generated_corpus_bytes"]:
            _fail("E_GENERATOR_LIMIT", CANDIDATE_PATH)
    if sha256(stream).hexdigest() != candidate["generated_stream_sha256"]:
        _fail("E_DIGEST", CANDIDATE_PATH)


def _validate_report(report, candidate, candidate_digest, module_digest, evaluate):
    _require_keys(report, {"artifact_bindings", "contract_digest", "declarative_cases", "digest", "format", "requirement_coverage", "semantic_cases", "source_binding_sha256", "unresolved"}, REPORT_PATH)
    if report["format"] != "evaluation-contract-conformance-v1" or report["unresolved"] != []:
        _fail("E_SCHEMA", REPORT_PATH)
    if report["artifact_bindings"] != {
        "candidate_path": CANDIDATE_PATH, "candidate_sha256": candidate_digest,
        "module_path": MODULE_PATH, "module_sha256": module_digest,
    } or report["contract_digest"] != candidate["digest"] or report["source_binding_sha256"] != module_digest:
        _fail("E_DIGEST", REPORT_PATH)
    declarative = report["declarative_cases"]
    semantic = report["semantic_cases"]
    if not isinstance(declarative, list) or not isinstance(semantic, list) or not semantic:
        _fail("E_SCHEMA", REPORT_PATH)
    case_ids = [row.get("case_id") for row in declarative + semantic if isinstance(row, dict)]
    if len(case_ids) != len(declarative) + len(semantic) or len(case_ids) != len(set(case_ids)) or not all(_valid_id(item) for item in case_ids):
        _fail("E_ID", REPORT_PATH)
    rules = {row["rule_id"]: row for row in candidate["rules"]}
    for row in declarative:
        _require_keys(row, {"candidate_path", "case_id", "rule_id"}, REPORT_PATH)
        if row["rule_id"] not in rules or row["candidate_path"] != rules[row["rule_id"]]["candidate_path"]:
            _fail("E_CROSS_REFERENCE", REPORT_PATH)
    coverage = report["requirement_coverage"]
    coverage_ids = [row.get("requirement_id") for row in coverage if isinstance(row, dict)]
    if len(coverage_ids) != len(set(coverage_ids)):
        _fail("E_REQUIREMENT_DUPLICATE", REPORT_PATH)
    if tuple(coverage_ids) != REQUIREMENT_IDS:
        _fail("E_REQUIREMENT_MISSING", REPORT_PATH)
    all_cases = set(case_ids)
    operations = set(candidate["operation_ids"].values())
    for row, locator in zip(coverage, LOCATORS):
        _require_keys(row, {"authority_id", "binding_kind", "case_ids", "disposition", "owner", "reason", "requirement_id"}, REPORT_PATH)
        if row["owner"] != _expected_owner(locator) or row["disposition"] not in {"covered", "not-applicable"} or not isinstance(row["reason"], str) or not row["reason"]:
            _fail("E_LINEAGE", REPORT_PATH)
        if not isinstance(row["case_ids"], list) or not row["case_ids"] or not set(row["case_ids"]) <= all_cases:
            _fail("E_CROSS_REFERENCE", REPORT_PATH)
        if row["binding_kind"] == "candidate-rule":
            valid_authority = row["authority_id"] in rules
        elif row["binding_kind"] == "module-operation":
            valid_authority = row["authority_id"] in operations
        elif row["binding_kind"] == "preserved-owner":
            valid_authority = row["authority_id"] == row["owner"]
        else:
            valid_authority = False
        if not valid_authority:
            _fail("E_CROSS_REFERENCE", REPORT_PATH)
    contracts = {row["operation_id"]: row for row in candidate["operation_contracts"]}
    rejection_codes = set(candidate["semantic_errors"].values())
    isolated_negative_codes = set()
    for case in semantic:
        _require_keys(case, {"case_id", "classification", "coverage", "expected_result", "input_value", "operation_id"}, REPORT_PATH)
        if case["classification"] not in {"positive", "negative"} or not isinstance(case["coverage"], list) or not case["coverage"]:
            _fail("E_ORACLE", REPORT_PATH)
        if case["classification"] == "negative":
            codes = set(case["coverage"]) & rejection_codes
            if len(codes) == 1:
                isolated_negative_codes.update(codes)
        operation_id = case["operation_id"]
        contract = contracts.get(operation_id)
        if case["classification"] == "positive" and operation_id == candidate["operation_ids"]["index_lineage"]:
            _validate_positive_lineage(case["input_value"])
        before = _canonical(case["input_value"])
        try:
            actual = evaluate(candidate, operation_id, case["input_value"])
        except Exception:
            _fail("E_SEMANTIC", MODULE_PATH)
        if _canonical(case["input_value"]) != before:
            _fail("E_SEMANTIC", MODULE_PATH)
        expected = case["expected_result"]
        if contract is None:
            schema_name = next(iter(contracts.values()))["error_schema"]
        else:
            schema_name = contract["success_schema"] if isinstance(actual, dict) and actual.get("status") == "ok" else contract["error_schema"]
        schema = candidate["schemas"][schema_name]
        if not _validate_value(actual, schema["root"], schema["definitions"]):
            _fail("E_SEMANTIC", MODULE_PATH)
        if _canonical(actual) != _canonical(expected):
            _fail("E_SEMANTIC", MODULE_PATH)
    if isolated_negative_codes != rejection_codes:
        _fail("E_ORACLE", REPORT_PATH)
    if report["digest"] != _digest_record(report):
        _fail("E_DIGEST", REPORT_PATH)


def _validate_positive_lineage(input_value):
    records = [
        record
        for group in input_value["record_groups"]
        for page in group["pages"]
        for record in page["records"]
    ]
    manifests = {
        record["case_id"]: record
        for record in records
        if record["kind"] == "case-manifest"
    }
    receipts = [
        receipt
        for page in input_value["adjudication_receipt_pages"]
        for receipt in page["receipts"]
    ]
    for receipt in receipts:
        manifest = manifests.get(receipt["case_id"])
        if (manifest is None or receipt["case_manifest_digest"] != manifest["digest"]
                or receipt["answer_key_digest"] != manifest["answer_key_digest"]
                or receipt["digest"] != _digest_record(receipt)):
            _fail("E_LINEAGE", REPORT_PATH)


def _audit_and_load_module(source: bytes):
    if len(source) > LIMITS["module_source_bytes"]:
        _fail("E_LIMIT", MODULE_PATH)
    if source.startswith(b"\xef\xbb\xbf") or b"\r" in source:
        _fail("E_UTF8", MODULE_PATH)
    try:
        text = source.decode("utf-8")
    except UnicodeDecodeError:
        _fail("E_UTF8", MODULE_PATH)
    if not source.endswith(b"\n") or source.endswith(b"\n\n"):
        _fail("E_SEMANTIC", MODULE_PATH)
    try:
        tree = ast.parse(text, filename=MODULE_PATH)
    except SyntaxError:
        _fail("E_SEMANTIC", MODULE_PATH)
    allowed_imports = {("fractions", "Fraction"), ("hashlib", "sha256")}
    seen_imports = set()
    for statement in tree.body:
        if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)):
            continue
        if isinstance(statement, ast.ImportFrom):
            if statement.level != 0 or len(statement.names) != 1:
                _fail("E_SEMANTIC", MODULE_PATH)
            binding = (statement.module, statement.names[0].name)
            if binding not in allowed_imports:
                _fail("E_SEMANTIC", MODULE_PATH)
            if statement.names[0].asname is not None and not statement.names[0].asname.startswith("_"):
                _fail("E_SEMANTIC", MODULE_PATH)
            seen_imports.add(binding)
            continue
        if isinstance(statement, ast.FunctionDef):
            if statement.decorator_list or statement.returns is not None or statement.type_comment is not None:
                _fail("E_SEMANTIC", MODULE_PATH)
            args = statement.args
            if args.defaults or args.kw_defaults or args.vararg or args.kwarg or args.posonlyargs or args.kwonlyargs or any(arg.annotation is not None for arg in args.args):
                _fail("E_SEMANTIC", MODULE_PATH)
            continue
        _fail("E_SEMANTIC", MODULE_PATH)
    if seen_imports != allowed_imports:
        _fail("E_SEMANTIC", MODULE_PATH)
    forbidden = {
        "eval", "exec", "compile", "open", "__import__", "getattr", "setattr",
        "delattr", "hasattr", "globals", "locals", "vars", "dir", "type", "object",
        "os", "sys", "pathlib", "socket", "subprocess", "random", "secrets", "time",
        "datetime", "importlib", "pkgutil", "builtins", "ctypes", "marshal", "pickle",
    }
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.AsyncFunctionDef, ast.ClassDef, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Await, ast.Yield, ast.YieldFrom)):
            _fail("E_SEMANTIC", MODULE_PATH)
        if isinstance(node, ast.Name) and node.id in forbidden:
            _fail("E_SEMANTIC", MODULE_PATH)
        if isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr.endswith("__")):
            _fail("E_SEMANTIC", MODULE_PATH)
    evaluate_defs = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "evaluate_v1"]
    public_defs = [node.name for node in tree.body if isinstance(node, ast.FunctionDef) and not node.name.startswith("_")]
    if len(evaluate_defs) != 1 or public_defs != ["evaluate_v1"] or [arg.arg for arg in evaluate_defs[0].args.args] != ["contract", "operation_id", "input_value"]:
        _fail("E_SEMANTIC", MODULE_PATH)
    allowed_builtins = {
        name: getattr(__builtins__, name) if not isinstance(__builtins__, dict) else __builtins__[name]
        for name in (
            "abs", "all", "any", "bool", "bytes", "dict", "enumerate", "int",
            "isinstance", "len", "list", "max", "min", "range", "reversed", "set",
            "sorted", "str", "sum", "tuple", "zip", "KeyError", "TypeError", "ValueError",
        )
    }
    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level != 0 or tuple(fromlist) not in {("Fraction",), ("sha256",)} or name not in {"fractions", "hashlib"}:
            raise ImportError(name)
        if name == "fractions" and tuple(fromlist) == ("Fraction",):
            return type("Fractions", (), {"Fraction": Fraction})
        if name == "hashlib" and tuple(fromlist) == ("sha256",):
            return type("Hashlib", (), {"sha256": sha256})
        raise ImportError(name)
    allowed_builtins["__import__"] = restricted_import
    namespace = {"__builtins__": allowed_builtins, "__name__": "_evaluation_semantics_v1"}
    try:
        exec(compile(tree, MODULE_PATH, "exec", dont_inherit=True, optimize=0), namespace)
    except Exception:
        _fail("E_SEMANTIC", MODULE_PATH)
    public_callables = sorted(name for name, value in namespace.items() if not name.startswith("_") and callable(value))
    evaluate = namespace.get("evaluate_v1")
    if public_callables != ["evaluate_v1"] or not callable(evaluate):
        _fail("E_SEMANTIC", MODULE_PATH)
    if len(inspect.signature(evaluate).parameters) != 3:
        _fail("E_SEMANTIC", MODULE_PATH)
    return evaluate


def _read_file_at(root_fd, path, maximum):
    parts = path.split("/")
    current = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            following = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = following
        descriptor = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=current)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("not regular")
            chunks = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)
        finally:
            os.close(descriptor)
    finally:
        os.close(current)


def _repository_root():
    script = Path(__file__)
    if script.name != "check-evaluation-contract-candidate.py" or script.parent.name != "scripts":
        _fail("E_ROOT", SCRIPT_PATH)
    root = script.parent.parent
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        _fail("E_ROOT", SCRIPT_PATH)
    return root_fd


def _check_bundle(root_fd):
    contents = {}
    maxima = (LIMITS["json_artifact_bytes"], LIMITS["json_artifact_bytes"], LIMITS["module_source_bytes"])
    for path, maximum in zip(ARTIFACT_PATHS, maxima):
        try:
            data = _read_file_at(root_fd, path, maximum)
        except OSError:
            _fail("E_IO", path)
        if len(data) > maximum:
            _fail("E_LIMIT", path)
        contents[path] = data
    # Observe encoding and syntax before whole-file locks.  The public priority
    # puts E_UTF8/E_JSON ahead of E_DIGEST, so a malformed artifact must not be
    # hidden by a stale lock.  The module audit has the same limit/encoding order.
    candidate = _decode_json(contents[CANDIDATE_PATH], CANDIDATE_PATH)
    report = _decode_json(contents[REPORT_PATH], REPORT_PATH)
    evaluate = _audit_and_load_module(contents[MODULE_PATH])
    digests = {path: sha256(data).hexdigest() for path, data in contents.items()}
    for path in ARTIFACT_PATHS:
        if digests[path] != WHOLE_FILE_SHA256[path]:
            _fail("E_DIGEST", path)
    _validate_candidate(candidate, digests[MODULE_PATH])
    _validate_report(report, candidate, digests[CANDIDATE_PATH], digests[MODULE_PATH], evaluate)


def _line(value):
    return (_canonical(value) + "\n").encode("ascii")


def main(*, force_internal=False):
    root_fd = None
    try:
        root_fd = _repository_root()
        _check_bundle(root_fd)
        if force_internal:
            raise RuntimeError("controlled internal fault")
    except CheckFailure as error:
        sys.stdout.flush()
        sys.stderr.buffer.write(_line({"code": error.code, "format": FORMAT, "path": error.path, "status": "error"}))
        return 1
    except Exception:
        sys.stdout.flush()
        sys.stderr.buffer.write(_line({"code": "E_INTERNAL", "format": FORMAT, "path": SCRIPT_PATH, "status": "error"}))
        return 2
    finally:
        if root_fd is not None:
            os.close(root_fd)
    sys.stdout.buffer.write(_line({"checked": 3, "format": FORMAT, "status": "ok"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
