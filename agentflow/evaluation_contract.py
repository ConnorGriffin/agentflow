"""Load and validate the immutable Evaluation v1 semantic bundle."""
from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from types import MappingProxyType
from typing import Any, Callable, Mapping


_CONTRACT_SUFFIX = ("docs", "evaluation", "contract-v1.json")
_CONTRACT_SHA256 = "53359f35de57047441defa76a477564580b956f968ab6425356cca3a1c5a8409"
_MODULE_PATH = PurePosixPath("agentflow/evaluation_semantics_v1.py")
_MODULE_INTERFACE = "evaluation-semantics-v1"
_MODULE_SHA256 = "185f41a5e4549cc1ccbc4615af5846c3ed0f95285790d193e1b2f43aa3dc8554"
_MAX_JSON_BYTES = 1_048_576
_MAX_MODULE_BYTES = 65_536
_SENTINELS = frozenset({"<contract>", "<bundle>", "<module>"})
_SAFE_BASENAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_SAFE_PATH = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SCHEMA_KEYS = {
    "object": frozenset({"type", "required", "properties", "additional_properties"}),
    "array": frozenset({"type", "items", "min_items", "max_items"}),
    "string": frozenset({"type", "pattern", "min_length", "max_length"}),
    "integer": frozenset({"type", "minimum", "maximum"}),
    "boolean": frozenset({"type"}),
    "null": frozenset({"type"}),
    "ref": frozenset({"type", "ref"}),
}


class EvaluationContractError(ValueError):
    """A bounded contract failure containing no rejected content or absolute path."""

    def __init__(self, code: str, basename: str) -> None:
        self.code = code
        self.basename = basename if basename in _SENTINELS or _SAFE_BASENAME.fullmatch(basename) else "<contract>"
        super().__init__(f"{self.code}: {self.basename}")


class _DuplicateKey(ValueError):
    pass


def _error(code: str, basename: str) -> None:
    raise EvaluationContractError(code, basename)


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise _DuplicateKey
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _canonical_string(value: str) -> str:
    pieces = ['"']
    for character in value:
        code = ord(character)
        if character == '"':
            pieces.append('\\"')
        elif character == "\\":
            pieces.append("\\\\")
        elif code < 0x20:
            pieces.append(f"\\u{code:04x}")
        elif code < 0x80:
            pieces.append(character)
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError
        elif code <= 0xFFFF:
            pieces.append(f"\\u{code:04x}")
        else:
            scalar = code - 0x10000
            pieces.append(f"\\u{0xD800 + (scalar >> 10):04x}\\u{0xDC00 + (scalar & 0x3FF):04x}")
    pieces.append('"')
    return "".join(pieces)


def _canonical(value: Any) -> str:
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
            raise ValueError
        return rendered
    if isinstance(value, str):
        return _canonical_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_canonical(item) for item in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ",".join(
            _canonical_string(key) + ":" + _canonical(value[key]) for key in sorted(value)
        ) + "}"
    raise ValueError


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _depth_and_entries(value: Any, limits: Mapping[str, int], depth: int = 0) -> None:
    if isinstance(value, (dict, list)):
        depth += 1
        if depth > limits["json_nesting"] or len(value) > limits["object_or_array_entries"]:
            _error("E_LIMIT", "<contract>")
        children = value.values() if isinstance(value, dict) else value
        for child in children:
            _depth_and_entries(child, limits, depth)


def _decode_json(data: bytes, basename: str, limits: Mapping[str, int] | None = None) -> Any:
    if len(data) > _MAX_JSON_BYTES:
        _error("E_LIMIT", basename)
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        _error("E_UTF8", basename)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _error("E_UTF8", basename)
    try:
        value = json.loads(
            text, object_pairs_hook=_pairs, parse_float=Decimal,
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        _error("E_DUPLICATE_KEY", basename)
    except (ValueError, json.JSONDecodeError):
        _error("E_JSON", basename)
    active_limits = limits or {"json_nesting": 32, "object_or_array_entries": 256}
    _depth_and_entries(value, active_limits)
    try:
        canonical = (_canonical(value) + "\n").encode("ascii")
    except (UnicodeError, ValueError):
        _error("E_CANONICAL", basename)
    if canonical != data:
        _error("E_CANONICAL", basename)
    return value


def _open_directory(path: Path, sentinel: str) -> int:
    raw = os.fspath(path)
    if not isinstance(raw, str):
        _error("E_ROOT", sentinel)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW
    if raw in {".", "/"}:
        try:
            descriptor = os.open(raw, flags)
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise OSError
            return descriptor
        except OSError:
            _error("E_ROOT", sentinel)
    absolute = raw.startswith(os.sep)
    components = (raw[1:] if absolute else raw).split(os.sep)
    if not components or any(component in {"", ".", ".."} for component in components):
        _error("E_ROOT", sentinel)
    descriptor: int | None = None
    try:
        descriptor = os.open("/" if absolute else ".", flags)
        for component in components:
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
                raise OSError
            child = os.open(component, flags, dir_fd=descriptor)
            after = os.fstat(child)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                os.close(child)
                raise OSError
            os.close(descriptor)
            descriptor = child
        result = descriptor
        descriptor = None
        return result
    except OSError:
        _error("E_ROOT", sentinel)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_at(root_fd: int, relative: PurePosixPath, maximum: int, basename: str) -> bytes:
    descriptor = os.dup(root_fd)
    opened: int | None = descriptor
    try:
        for component in relative.parts[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=opened,
            )
            os.close(opened)
            opened = child
        file_fd = os.open(
            relative.name,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=opened,
        )
        try:
            details = os.fstat(file_fd)
            if not stat.S_ISREG(details.st_mode) or details.st_size > maximum:
                _error("E_LIMIT" if details.st_size > maximum else "E_IO", basename)
            chunks: list[bytes] = []
            remaining = maximum + 1
            while remaining:
                chunk = os.read(file_fd, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > maximum:
                _error("E_LIMIT", basename)
            return data
        finally:
            os.close(file_fd)
    except EvaluationContractError:
        raise
    except OSError:
        _error("E_IO", basename)
    finally:
        if opened is not None:
            os.close(opened)


def _safe_relative(value: object, limits: Mapping[str, int], basename: str) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        _error("E_PATH", basename)
    text = value.as_posix() if isinstance(value, PurePosixPath) else value
    path = PurePosixPath(text)
    if (
        path.is_absolute() or not path.parts or text != path.as_posix()
        or len(text.encode("utf-8")) > limits["path_bytes"]
        or len(path.parts) > limits["path_depth"]
        or not _SAFE_PATH.fullmatch(text)
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        _error("E_PATH", basename)
    return path


def _schema_references(node: Any) -> list[str]:
    references: list[str] = []
    if isinstance(node, dict):
        if node.get("type") == "ref" and isinstance(node.get("ref"), str):
            references.append(node["ref"].removeprefix("#/definitions/"))
        for child in node.values():
            references.extend(_schema_references(child))
    elif isinstance(node, list):
        for child in node:
            references.extend(_schema_references(child))
    return references


def _validate_schema_node(node: Any, definitions: Mapping[str, Any], references: list[str], basename: str) -> None:
    if not isinstance(node, dict) or node.get("type") not in _SCHEMA_KEYS:
        _error("E_SCHEMA", basename)
    kind = node["type"]
    if set(node) != _SCHEMA_KEYS[kind]:
        _error("E_SCHEMA", basename)
    if kind == "object":
        if node["additional_properties"] is not False or not isinstance(node["required"], list) or not isinstance(node["properties"], dict):
            _error("E_SCHEMA", basename)
        if node["required"] != sorted(set(node["required"])) or not set(node["required"]) <= set(node["properties"]):
            _error("E_SCHEMA", basename)
        for child in node["properties"].values():
            _validate_schema_node(child, definitions, references, basename)
    elif kind == "array":
        bounds = (node["min_items"], node["max_items"])
        if any(isinstance(item, bool) or not isinstance(item, int) for item in bounds) or not 0 <= bounds[0] <= bounds[1]:
            _error("E_SCHEMA", basename)
        _validate_schema_node(node["items"], definitions, references, basename)
    elif kind == "string":
        bounds = (node["min_length"], node["max_length"])
        if not isinstance(node["pattern"], str) or any(isinstance(item, bool) or not isinstance(item, int) for item in bounds) or not 0 <= bounds[0] <= bounds[1]:
            _error("E_SCHEMA", basename)
        try:
            re.compile(node["pattern"])
        except re.error:
            _error("E_SCHEMA", basename)
    elif kind == "integer":
        bounds = (node["minimum"], node["maximum"])
        if any(isinstance(item, bool) or not isinstance(item, int) for item in bounds) or bounds[0] > bounds[1]:
            _error("E_SCHEMA", basename)
    elif kind == "ref":
        match = re.fullmatch(r"#/definitions/([a-z][a-z0-9-]{0,47})", node["ref"] if isinstance(node["ref"], str) else "")
        if match is None or match.group(1) not in definitions:
            _error("E_REF", basename)
        references.append(match.group(1))


def _validate_schema(schema: Any, limits: Mapping[str, int], basename: str, *, catalog: bool = False) -> None:
    if not isinstance(schema, dict) or set(schema) != {"schema_version", "root", "definitions"}:
        _error("E_SCHEMA", basename)
    definitions = schema["definitions"]
    if schema["schema_version"] != "evaluation-schema-v1" or not isinstance(definitions, dict):
        _error("E_SCHEMA", basename)
    if len(definitions) > limits["definitions"] or not all(_SAFE_ID.fullmatch(name) for name in definitions):
        _error("E_LIMIT" if len(definitions) > limits["definitions"] else "E_ID", basename)
    references: list[str] = []
    _validate_schema_node(schema["root"], definitions, references, basename)
    for node in definitions.values():
        _validate_schema_node(node, definitions, references, basename)
    if not catalog and len(references) > limits["references_per_schema"]:
        _error("E_LIMIT", basename)
    graph = {name: _schema_references(node) for name, node in definitions.items()}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visiting:
            _error("E_REF_CYCLE", basename)
        if name not in visited:
            visiting.add(name)
            for target in graph[name]:
                visit(target)
            visiting.remove(name)
            visited.add(name)

    for target in _schema_references(schema["root"]):
        visit(target)
    if set(definitions) - visited:
        _error("E_REF_UNUSED", basename)


def _value_matches(value: Any, node: Mapping[str, Any], definitions: Mapping[str, Any]) -> bool:
    kind = node["type"]
    if kind == "ref":
        return _value_matches(value, definitions[node["ref"].rsplit("/", 1)[-1]], definitions)
    if kind == "object":
        return (
            isinstance(value, dict) and set(node["required"]) <= set(value) <= set(node["properties"])
            and all(_value_matches(value[key], node["properties"][key], definitions) for key in value)
        )
    if kind == "array":
        return isinstance(value, list) and node["min_items"] <= len(value) <= node["max_items"] and all(
            _value_matches(item, node["items"], definitions) for item in value
        )
    if kind == "string":
        return isinstance(value, str) and node["min_length"] <= len(value) <= node["max_length"] and re.fullmatch(node["pattern"], value) is not None
    if kind == "integer":
        return isinstance(value, int) and not isinstance(value, bool) and node["minimum"] <= value <= node["maximum"]
    if kind == "boolean":
        return isinstance(value, bool)
    return value is None


def _audit_module(source: bytes) -> Callable[[dict[str, Any], str, Any], Any]:
    if len(source) > _MAX_MODULE_BYTES:
        _error("E_LIMIT", "<module>")
    if source.startswith(b"\xef\xbb\xbf") or b"\r" in source:
        _error("E_UTF8", "<module>")
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text, filename=_MODULE_PATH.as_posix())
    except (UnicodeDecodeError, SyntaxError):
        _error("E_SEMANTIC", "<module>")
    allowed_imports = {("fractions", "Fraction"), ("hashlib", "sha256")}
    seen_imports: set[tuple[str, str]] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
            continue
        if isinstance(statement, ast.ImportFrom):
            if statement.level or len(statement.names) != 1:
                _error("E_SEMANTIC", "<module>")
            binding = (statement.module or "", statement.names[0].name)
            if binding not in allowed_imports or (statement.names[0].asname is not None and not statement.names[0].asname.startswith("_")):
                _error("E_SEMANTIC", "<module>")
            seen_imports.add(binding)
            continue
        if isinstance(statement, ast.FunctionDef):
            args = statement.args
            if statement.decorator_list or statement.returns is not None or args.defaults or args.kw_defaults or args.vararg or args.kwarg or args.posonlyargs or args.kwonlyargs or any(arg.annotation is not None for arg in args.args):
                _error("E_SEMANTIC", "<module>")
            continue
        _error("E_SEMANTIC", "<module>")
    if seen_imports != allowed_imports:
        _error("E_SEMANTIC", "<module>")
    forbidden = frozenset({
        "eval", "exec", "compile", "open", "__import__", "getattr", "setattr", "delattr",
        "hasattr", "globals", "locals", "vars", "dir", "type", "object", "os", "sys",
        "pathlib", "socket", "subprocess", "random", "secrets", "time", "datetime",
        "importlib", "pkgutil", "builtins", "ctypes", "marshal", "pickle",
    })
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.AsyncFunctionDef, ast.ClassDef, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Await, ast.Yield, ast.YieldFrom)):
            _error("E_SEMANTIC", "<module>")
        if isinstance(node, ast.Name) and node.id in forbidden:
            _error("E_SEMANTIC", "<module>")
        if isinstance(node, ast.Attribute) and (node.attr.startswith("__") or node.attr.endswith("__")):
            _error("E_SEMANTIC", "<module>")
    definitions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    public = [node for node in definitions if not node.name.startswith("_")]
    if len(public) != 1 or public[0].name != "evaluate_v1" or [arg.arg for arg in public[0].args.args] != ["contract", "operation_id", "input_value"]:
        _error("E_SEMANTIC", "<module>")
    builtin_names = (
        "abs", "all", "any", "bool", "bytes", "dict", "enumerate", "int", "isinstance",
        "len", "list", "max", "min", "range", "reversed", "set", "sorted", "str", "sum",
        "tuple", "zip", "KeyError", "TypeError", "ValueError",
    )
    builtin_source = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    allowed_builtins = {name: builtin_source[name] for name in builtin_names}

    def restricted_import(name: str, globals: Any = None, locals: Any = None, fromlist: tuple[str, ...] = (), level: int = 0) -> Any:
        if level == 0 and name == "fractions" and tuple(fromlist) == ("Fraction",):
            return type("Fractions", (), {"Fraction": Fraction})
        if level == 0 and name == "hashlib" and tuple(fromlist) == ("sha256",):
            return type("Hashlib", (), {"sha256": sha256})
        raise ImportError(name)

    allowed_builtins["__import__"] = restricted_import
    namespace: dict[str, Any] = {"__builtins__": allowed_builtins, "__name__": "_evaluation_semantics_v1"}
    try:
        exec(compile(tree, _MODULE_PATH.as_posix(), "exec", dont_inherit=True, optimize=0), namespace)
    except Exception:
        _error("E_SEMANTIC", "<module>")
    evaluate = namespace.get("evaluate_v1")
    callables = sorted(name for name, value in namespace.items() if not name.startswith("_") and callable(value))
    if callables != ["evaluate_v1"] or not callable(evaluate) or len(inspect.signature(evaluate).parameters) != 3:
        _error("E_SEMANTIC", "<module>")
    return evaluate


@dataclass(frozen=True)
class EvaluationContractV1:
    contract_version: str
    operation_ids: Mapping[str, str]
    limits: Mapping[str, int]
    semantic_module_path: PurePosixPath
    semantic_module_sha256: str
    _contract: dict[str, Any] = field(repr=False, compare=False)
    _evaluate_v1: Callable[[dict[str, Any], str, Any], Any] = field(repr=False, compare=False)

    def evaluate(self, operation_id: str, input_value: Any) -> Any:
        """Call the exact bound ``evaluate_v1``, validate its result, and freeze it."""
        contracts = {row["operation_id"]: row for row in self._contract["operation_contracts"]}
        operation = contracts.get(operation_id)
        supplied = deepcopy(input_value)
        before = _canonical(supplied)
        try:
            result = self._evaluate_v1(self._contract, operation_id, supplied)
        except Exception:
            _error("E_SEMANTIC", "<module>")
        if _canonical(supplied) != before:
            _error("E_SEMANTIC", "<module>")
        if operation is None:
            error_schema_name = next(iter(contracts.values()))["error_schema"]
            schema = self._contract["schemas"][error_schema_name]
        else:
            schema_name = operation["success_schema"] if isinstance(result, dict) and result.get("status") == "ok" else operation["error_schema"]
            schema = self._contract["schemas"][schema_name]
        if not _value_matches(result, schema["root"], schema["definitions"]):
            _error("E_SEMANTIC", "<module>")
        return _freeze(result)


@dataclass(frozen=True)
class ValidatedEvaluationArtifactV1:
    artifact_id: str
    kind: str
    path: PurePosixPath
    sha256: str
    role_family: str
    visibility: str
    artifact_root: PurePosixPath
    value: Any


@dataclass(frozen=True)
class ValidatedEvaluationBundleV1:
    entrypoint: PurePosixPath
    artifacts: tuple[ValidatedEvaluationArtifactV1, ...]


def _validate_contract(candidate: Any, module_digest: str) -> None:
    basename = "contract-v1.json"
    expected_keys = {
        "artifact_registry", "authority_policy", "bootstrap_policy", "canonical_json",
        "checker_boundary", "contract_version", "corpus_policy", "digest", "digest_policy",
        "evidence_policy", "failure_classes", "gate_policy", "generated_stream_sha256",
        "generation", "generation_base_cases", "implementation_boundary", "lifecycle_policy",
        "limits", "manifest_policy", "metric_policy", "missingness_policy", "module_constraints",
        "operation_contracts", "operation_ids", "path_policy", "requirements", "rules",
        "schedule_policy", "schema_catalog", "schemas", "semantic_errors", "semantic_module",
        "source_bindings", "status_truth_table",
    }
    if not isinstance(candidate, dict) or set(candidate) != expected_keys:
        _error("E_SCHEMA", basename)
    limits = candidate["limits"]
    required_limits = {
        "definitions", "generated_case_bytes", "generated_cases", "generated_corpus_bytes",
        "json_artifact_bytes", "json_nesting", "module_source_bytes", "object_or_array_entries",
        "path_bytes", "path_depth", "references_per_schema", "stdout_or_stderr_bytes",
    }
    if candidate["contract_version"] != "evaluation-contract-v1" or not isinstance(limits, dict) or set(limits) != required_limits or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in limits.values()):
        _error("E_SCHEMA", basename)
    if limits["json_artifact_bytes"] != _MAX_JSON_BYTES or limits["module_source_bytes"] != _MAX_MODULE_BYTES:
        _error("E_LIMIT", basename)
    _depth_and_entries(candidate, limits)
    binding = candidate["semantic_module"]
    if binding != {"interface_version": _MODULE_INTERFACE, "path": _MODULE_PATH.as_posix(), "source_sha256": module_digest}:
        _error("E_DIGEST" if isinstance(binding, dict) and binding.get("source_sha256") != module_digest else "E_SCHEMA", "<module>")
    schemas = candidate["schemas"]
    if not isinstance(schemas, dict) or not schemas or not all(_SAFE_ID.fullmatch(name) for name in schemas):
        _error("E_SCHEMA", basename)
    _validate_schema(candidate["schema_catalog"], limits, basename, catalog=True)
    for schema in schemas.values():
        _validate_schema(schema, limits, basename)
    operation_ids = candidate["operation_ids"]
    if not isinstance(operation_ids, dict) or len(set(operation_ids.values())) != len(operation_ids) or not all(isinstance(value, str) and _SAFE_ID.fullmatch(value) for value in operation_ids.values()):
        _error("E_ID", basename)
    contracts = candidate["operation_contracts"]
    if not isinstance(contracts, list) or {row.get("operation_id") for row in contracts if isinstance(row, dict)} != set(operation_ids.values()):
        _error("E_CROSS_REFERENCE", basename)
    for row in contracts:
        if set(row) != {"operation_id", "input_schema", "success_schema", "error_schema"} or not {row["input_schema"], row["success_schema"], row["error_schema"]} <= set(schemas):
            _error("E_CROSS_REFERENCE", basename)
    definitions = candidate["schema_catalog"]["definitions"]
    registry = candidate["artifact_registry"]
    kinds: set[str] = set()
    if not isinstance(registry, list):
        _error("E_SCHEMA", basename)
    for row in registry:
        required = {"count_scope", "group_key", "kind", "max_instances", "path", "permitted_root", "role_family", "visibility"}
        if not isinstance(row, dict) or set(row) not in (required, required | {"payload_schema"}):
            _error("E_SCHEMA", basename)
        if row["kind"] in kinds or not _SAFE_ID.fullmatch(row["kind"]):
            _error("E_ID", basename)
        kinds.add(row["kind"])
        _safe_relative(re.sub(r"\{[a-z][a-z0-9-]*\}", "x", row["path"]), limits, basename)
        root = _safe_relative(row["permitted_root"], limits, basename)
        concrete = PurePosixPath(re.sub(r"\{[a-z][a-z0-9-]*\}", "x", row["path"]))
        if concrete.parts[:len(root.parts)] != root.parts:
            _error("E_PATH", basename)
        if "payload_schema" in row and row["payload_schema"] not in definitions:
            _error("E_CROSS_REFERENCE", basename)
    digest = candidate.get("digest")
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        _error("E_DIGEST", basename)
    preimage = {key: value for key, value in candidate.items() if key != candidate["digest_policy"]["self_member"]}
    if sha256(_canonical(preimage).encode("ascii")).hexdigest() != digest:
        _error("E_DIGEST", basename)


def load_evaluation_contract(path: Path) -> EvaluationContractV1:
    """Load the one fixed production contract and its repository-relative module binding."""
    supplied = Path(path)
    if tuple(supplied.parts[-3:]) != _CONTRACT_SUFFIX:
        _error("E_ROOT", supplied.name or "<contract>")
    root_path = supplied
    for _unused in _CONTRACT_SUFFIX:
        root_path = root_path.parent
    root_fd = _open_directory(root_path, "<contract>")
    try:
        contract_data = _read_at(root_fd, PurePosixPath(*_CONTRACT_SUFFIX), _MAX_JSON_BYTES, "contract-v1.json")
        candidate = _decode_json(contract_data, "contract-v1.json")
        if sha256(contract_data).hexdigest() != _CONTRACT_SHA256:
            _error("E_DIGEST", "contract-v1.json")
        module_data = _read_at(root_fd, _MODULE_PATH, _MAX_MODULE_BYTES, _MODULE_PATH.name)
        module_digest = sha256(module_data).hexdigest()
        if module_digest != _MODULE_SHA256:
            _error("E_DIGEST", _MODULE_PATH.name)
        _validate_contract(candidate, module_digest)
        evaluate_v1 = _audit_module(module_data)
        return EvaluationContractV1(
            contract_version=candidate["contract_version"],
            operation_ids=_freeze(candidate["operation_ids"]),
            limits=_freeze(candidate["limits"]),
            semantic_module_path=_MODULE_PATH,
            semantic_module_sha256=module_digest,
            _contract=candidate,
            _evaluate_v1=evaluate_v1,
        )
    finally:
        os.close(root_fd)


def _registry_match(contract: EvaluationContractV1, path: PurePosixPath) -> tuple[dict[str, Any], dict[str, str]]:
    matches: list[tuple[dict[str, Any], dict[str, str]]] = []
    for row in contract._contract["artifact_registry"]:
        pattern = re.escape(row["path"])
        names = re.findall(r"\{([a-z][a-z0-9-]*)\}", row["path"])
        for name in names:
            pattern = pattern.replace(re.escape("{" + name + "}"), f"(?P<{name.replace('-', '_')}>[a-z][a-z0-9-]{{0,47}})", 1)
        match = re.fullmatch(pattern, path.as_posix())
        if match is not None:
            matches.append((row, {name.replace("_", "-"): value for name, value in match.groupdict().items()}))
    if len(matches) != 1:
        _error("E_PATH", path.name)
    return matches[0]


def _typed_refs(value: Any, node: Mapping[str, Any], definitions: Mapping[str, Any]) -> list[dict[str, Any]]:
    if node["type"] == "ref":
        name = node["ref"].rsplit("/", 1)[-1]
        if name == "typed-ref-v1":
            return [value]
        return _typed_refs(value, definitions[name], definitions)
    if node["type"] == "object":
        result: list[dict[str, Any]] = []
        for key, item in value.items():
            result.extend(_typed_refs(item, node["properties"][key], definitions))
        return result
    if node["type"] == "array":
        result = []
        for item in value:
            result.extend(_typed_refs(item, node["items"], definitions))
        return result
    return []


def _validate_result_missingness(
    contract: EvaluationContractV1,
    path: PurePosixPath,
    value: dict[str, Any],
    loaded: Mapping[PurePosixPath, tuple[dict[str, Any], dict[str, str], dict[str, Any], str, str]],
) -> None:
    policy = contract._contract["missingness_policy"]
    terminal_path = PurePosixPath(value["terminal_edge"]["path"])
    terminal = loaded[terminal_path][2]
    metrics = value["metrics"]
    if terminal["state"] != policy["reported_status"]:
        expected = policy["unavailable_metric_names"]
        stateful = ("duration_ms", "provider_dollars_micros", "review_rounds", "tokens")
        if (
            metrics["missing_metric_names"] != expected
            or any(metrics[name] != {"state": policy["missing_state"], "value": policy["missing_integer_sentinel"]} for name in stateful)
            or metrics["quality"] != {"state": policy["missing_state"], "value": policy["missing_integer_sentinel"]}
            or metrics["fix_introduced_defect_count"] != policy["missing_integer_sentinel"]
            or metrics["grounded_false_positive_count"] != policy["missing_integer_sentinel"]
        ):
            _error("E_LINEAGE", path.name)
        return
    if metrics["duration_ms"]["state"] != policy["present_state"]:
        _error("E_LINEAGE", path.name)
    storage_names = {
        "provider_dollars_micros": "provider_dollars_micros",
        "quality_micros": "quality",
        "review_rounds": "review_rounds",
        "tokens": "tokens",
    }
    missing: list[str] = []
    for semantic_name in policy["optional_reported_metrics"]:
        metric = metrics[storage_names[semantic_name]]
        if metric["state"] == policy["missing_state"]:
            if metric["value"] != policy["missing_integer_sentinel"]:
                _error("E_LINEAGE", path.name)
            missing.append(semantic_name)
        elif metric["state"] != policy["present_state"]:
            _error("E_LINEAGE", path.name)
    if metrics["missing_metric_names"] != sorted(missing):
        _error("E_LINEAGE", path.name)


def load_evaluation_bundle(
    contract: EvaluationContractV1,
    root: Path,
    entrypoint: PurePosixPath,
) -> ValidatedEvaluationBundleV1:
    """Validate one manifest-rooted artifact closure without caller-supplied authority facts."""
    if not isinstance(contract, EvaluationContractV1):
        _error("E_SCHEMA", "<contract>")
    entry = _safe_relative(entrypoint, contract.limits, "<bundle>")
    root_fd = _open_directory(Path(root), "<bundle>")
    definitions = contract._contract["schema_catalog"]["definitions"]
    loaded: dict[PurePosixPath, tuple[dict[str, Any], dict[str, str], dict[str, Any], str, str]] = {}
    active: set[PurePosixPath] = set()
    identities: dict[PurePosixPath, tuple[str, str, str]] = {}

    def visit(path: PurePosixPath, expected: dict[str, Any] | None = None) -> None:
        if path in active:
            _error("E_REF_CYCLE", path.name)
        row, captures = _registry_match(contract, path)
        if "payload_schema" not in row:
            _error("E_SCHEMA", path.name)
        if expected is not None:
            if expected["kind"] != row["kind"] or expected["path"] != path.as_posix():
                _error("E_CROSS_REFERENCE", path.name)
            identity = (expected["id"], expected["kind"], expected["digest"])
            previous = identities.get(path)
            if previous is not None and previous != identity:
                _error("E_CROSS_REFERENCE", path.name)
            identities[path] = identity
            if captures and expected["id"] not in captures.values():
                _error("E_ID", path.name)
        if path in loaded:
            return
        active.add(path)
        try:
            data = _read_at(root_fd, path, contract.limits["json_artifact_bytes"], path.name)
            digest = sha256(data).hexdigest()
            if expected is not None and digest != expected["digest"]:
                _error("E_DIGEST", path.name)
            value = _decode_json(data, path.name, contract.limits)
            schema = definitions[row["payload_schema"]]
            if not _value_matches(value, schema, definitions):
                _error("E_SCHEMA", path.name)
            loaded[path] = (row, captures, value, digest, expected["id"] if expected else next(iter(captures.values()), path.stem))
            for reference in _typed_refs(value, schema, definitions):
                reference_path = _safe_relative(reference["path"], contract.limits, path.name)
                visit(reference_path, reference)
        finally:
            active.remove(path)

    try:
        visit(entry)
        counts: dict[str, int] = {}
        maxima = {row["kind"]: row["max_instances"] for row in contract._contract["artifact_registry"]}
        for row, _captures, _value, _digest_value, _artifact_id in loaded.values():
            counts[row["kind"]] = counts.get(row["kind"], 0) + 1
            if counts[row["kind"]] > maxima[row["kind"]]:
                _error("E_LIMIT", "<bundle>")
        for path, (row, _captures, value, _digest_value, _artifact_id) in loaded.items():
            if row["kind"] == "pre-adjudication-result":
                _validate_result_missingness(contract, path, value, loaded)
            if row["kind"] != "adjudication-receipt":
                continue
            case_ref = value["case"]
            case_path = PurePosixPath(case_ref["path"])
            case_row, _case_captures, case_value, case_digest, _case_id = loaded[case_path]
            answer_ref = case_value["answer_key"]
            answer_path = PurePosixPath(answer_ref["path"])
            answer_row, _answer_captures, _answer_value, answer_digest, _answer_id = loaded[answer_path]
            if (
                case_row["kind"] != "case-manifest" or answer_row["kind"] != "answer-key"
                or value["case_manifest_digest"] != case_digest
                or value["answer_key"] != answer_ref
                or value["answer_key"]["digest"] != answer_digest
            ):
                _error("E_LINEAGE", path.name)
        artifacts = tuple(
            ValidatedEvaluationArtifactV1(
                artifact_id=artifact_id,
                kind=row["kind"],
                path=path,
                sha256=digest,
                role_family=row["role_family"],
                visibility=row["visibility"],
                artifact_root=PurePosixPath(row["permitted_root"]),
                value=_freeze(value),
            )
            for path, (row, _captures, value, digest, artifact_id) in sorted(loaded.items(), key=lambda item: item[0].as_posix().encode("ascii"))
        )
        return ValidatedEvaluationBundleV1(entrypoint=entry, artifacts=artifacts)
    finally:
        os.close(root_fd)
