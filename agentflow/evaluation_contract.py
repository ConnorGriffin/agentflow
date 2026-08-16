"""Load and validate the immutable Evaluation v1 semantic bundle."""
from __future__ import annotations

import ast
from dataclasses import dataclass
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
_COMPONENT_GUARD_PREFIX = "/__agentflow_evaluation_component__/"
_SENTINELS = frozenset({"<contract>", "<bundle>", "<module>"})
_SAFE_BASENAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,159}$")
_SAFE_ID = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_SAFE_PATH = re.compile(r"^[a-z0-9][a-z0-9._/-]*$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_DECLARED_REJECTION_CODES = (
    "E_ROOT", "E_SOURCE_DRIFT", "E_SOURCE_LOCATOR", "E_REQUIREMENT_DUPLICATE",
    "E_REQUIREMENT_MISSING", "E_IO", "E_LIMIT", "E_UTF8", "E_JSON",
    "E_DUPLICATE_KEY", "E_CANONICAL", "E_SCHEMA", "E_REF", "E_REF_CYCLE",
    "E_REF_UNUSED", "E_PATH", "E_DIGEST", "E_ID", "E_CROSS_REFERENCE",
    "E_LINEAGE", "E_ORACLE", "E_GENERATOR_TEMPLATE", "E_GENERATOR_TARGET",
    "E_GENERATOR_PRECONDITION", "E_GENERATOR_COLLISION", "E_GENERATOR_LIMIT",
    "E_SEMANTIC", "E_INTERNAL",
)
_DECLARED_REJECTION_SET = frozenset(_DECLARED_REJECTION_CODES)
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
        self.code = code if isinstance(code, str) and code in _DECLARED_REJECTION_SET else "E_INTERNAL"
        self.basename = basename if isinstance(basename, str) and (
            basename in _SENTINELS or _SAFE_BASENAME.fullmatch(basename)
        ) else "<contract>"
        super().__init__(f"{self.code}: {self.basename}")


class _DuplicateKey(ValueError):
    pass


class _NumberLimit(ValueError):
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


_JSON_NUMBER = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.([0-9]+))?(?:[eE]([+-]?)([0-9]+))?\Z"
)


def _bounded_decimal(token: str, maximum: int) -> Decimal:
    """Parse a JSON decimal only when its fixed-point form is already bounded."""
    match = _JSON_NUMBER.fullmatch(token)
    if match is None or len(token.encode("ascii")) > maximum:
        raise _NumberLimit
    exponent_digits = match.group(3)
    exponent = 0
    if exponent_digits is not None:
        if len(exponent_digits) > len(str(maximum)) or int(exponent_digits) > maximum:
            raise _NumberLimit
        exponent = int(exponent_digits)
        if match.group(2) == "-":
            exponent = -exponent
    coefficient_digits = len(token.split("e", 1)[0].split("E", 1)[0].replace("-", "").replace(".", ""))
    fractional_digits = len(match.group(1) or "")
    digits_before_point = coefficient_digits - fractional_digits + exponent
    sign_bytes = int(token.startswith("-"))
    if digits_before_point <= 0:
        fixed_bytes = sign_bytes + 2 - digits_before_point + coefficient_digits
    elif digits_before_point >= coefficient_digits:
        fixed_bytes = sign_bytes + digits_before_point
    else:
        fixed_bytes = sign_bytes + coefficient_digits + 1
    if fixed_bytes > maximum:
        raise _NumberLimit
    return Decimal(token)


def _bounded_integer(token: str, maximum: int) -> int:
    if len(token.encode("ascii")) > maximum:
        raise _NumberLimit
    try:
        return int(token)
    except ValueError as exc:
        raise _NumberLimit from exc


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


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _depth_and_entries(value: Any, limits: Mapping[str, int], basename: str = "<contract>") -> int:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, parent_depth = stack.pop()
        nodes += 1
        if nodes > _MAX_JSON_BYTES + 1:
            _error("E_LIMIT", basename)
        if not isinstance(current, (dict, list)):
            continue
        depth = parent_depth + 1
        if depth > limits["json_nesting"] or len(current) > limits["object_or_array_entries"]:
            _error("E_LIMIT", basename)
        children = current.values() if isinstance(current, dict) else current
        stack.extend((child, depth) for child in children)
    return nodes


def _decode_json(data: bytes, basename: str, limits: Mapping[str, int] | None = None) -> Any:
    if len(data) > _MAX_JSON_BYTES:
        _error("E_LIMIT", basename)
    if data.startswith(b"\xef\xbb\xbf") or b"\r" in data:
        _error("E_UTF8", basename)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        _error("E_UTF8", basename)
    active_limits = limits or {"json_nesting": 32, "object_or_array_entries": 256}
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs,
            parse_float=lambda token: _bounded_decimal(token, len(data)),
            parse_int=lambda token: _bounded_integer(token, len(data)),
            parse_constant=_reject_constant,
        )
    except _DuplicateKey:
        _error("E_DUPLICATE_KEY", basename)
    except (_NumberLimit, RecursionError):
        _error("E_LIMIT", basename)
    except (ValueError, json.JSONDecodeError):
        _error("E_JSON", basename)
    _depth_and_entries(value, active_limits, basename)
    try:
        canonical = (_canonical(value) + "\n").encode("ascii")
    except (UnicodeError, ValueError):
        _error("E_CANONICAL", basename)
    if canonical != data:
        _error("E_CANONICAL", basename)
    return value


def _open_directory(path: Path, sentinel: str) -> int:
    try:
        raw = os.fspath(path)
    except Exception:
        _error("E_ROOT", sentinel)
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
        except Exception:
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
    except Exception:
        _error("E_ROOT", sentinel)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_at(root_fd: int, relative: PurePosixPath, maximum: int, basename: str) -> bytes:
    try:
        if type(relative) is not PurePosixPath or relative.is_absolute() or not relative.parts:
            _error("E_PATH", basename)
        text = relative.as_posix()
        normalized = os.path.normpath(os.path.join(_COMPONENT_GUARD_PREFIX, text))
        components: list[str] = []
        if normalized.startswith(_COMPONENT_GUARD_PREFIX):
            authorized = normalized[len(_COMPONENT_GUARD_PREFIX):]
            parts = authorized.split("/")
            if (
                authorized == text
                and _SAFE_PATH.fullmatch(authorized)
                and all(part not in {"", ".", ".."} for part in parts)
            ):
                components = parts
        if not components:
            _error("E_PATH", basename)
    except EvaluationContractError:
        raise
    except Exception:
        _error("E_PATH", basename)
    descriptor = os.dup(root_fd)
    opened: int | None = descriptor
    try:
        for component in components[:-1]:
            child = os.open(
                component,
                os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=opened,
            )
            os.close(opened)
            opened = child
        file_fd = os.open(
            components[-1],
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
    except Exception:
        _error("E_IO", basename)
    finally:
        if opened is not None:
            os.close(opened)


def _safe_relative(value: object, limits: Mapping[str, int], basename: str) -> PurePosixPath:
    if not isinstance(value, (str, PurePosixPath)):
        _error("E_PATH", basename)
    try:
        text = value.as_posix() if isinstance(value, PurePosixPath) else value
        path = PurePosixPath(text)
        unsafe = (
            path.is_absolute() or not path.parts or text != path.as_posix()
            or len(text.encode("utf-8")) > limits["path_bytes"]
            or len(path.parts) > limits["path_depth"]
            or not _SAFE_PATH.fullmatch(text)
            or any(part in {"", ".", ".."} for part in path.parts)
        )
    except Exception:
        _error("E_PATH", basename)
    if unsafe:
        _error("E_PATH", basename)
    return path


def _schema_references(node: Any) -> list[str]:
    references: list[str] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if current.get("type") == "ref" and isinstance(current.get("ref"), str):
                references.append(current["ref"].removeprefix("#/definitions/"))
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return references


def _validate_schema_node(node: Any, definitions: Mapping[str, Any], references: list[str], basename: str) -> None:
    stack = [node]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict) or current.get("type") not in _SCHEMA_KEYS:
            _error("E_SCHEMA", basename)
        kind = current["type"]
        if set(current) != _SCHEMA_KEYS[kind]:
            _error("E_SCHEMA", basename)
        if kind == "object":
            if current["additional_properties"] is not False or not isinstance(current["required"], list) or not isinstance(current["properties"], dict):
                _error("E_SCHEMA", basename)
            if current["required"] != sorted(set(current["required"])) or not set(current["required"]) <= set(current["properties"]):
                _error("E_SCHEMA", basename)
            stack.extend(current["properties"].values())
        elif kind == "array":
            bounds = (current["min_items"], current["max_items"])
            if any(isinstance(item, bool) or not isinstance(item, int) for item in bounds) or not 0 <= bounds[0] <= bounds[1]:
                _error("E_SCHEMA", basename)
            stack.append(current["items"])
        elif kind == "string":
            bounds = (current["min_length"], current["max_length"])
            if not isinstance(current["pattern"], str) or any(isinstance(item, bool) or not isinstance(item, int) for item in bounds) or not 0 <= bounds[0] <= bounds[1]:
                _error("E_SCHEMA", basename)
            try:
                re.compile(current["pattern"])
            except re.error:
                _error("E_SCHEMA", basename)
        elif kind == "integer":
            bounds = (current["minimum"], current["maximum"])
            if any(isinstance(item, bool) or not isinstance(item, int) for item in bounds) or bounds[0] > bounds[1]:
                _error("E_SCHEMA", basename)
        elif kind == "ref":
            match = re.fullmatch(r"#/definitions/([a-z][a-z0-9-]{0,47})", current["ref"] if isinstance(current["ref"], str) else "")
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
    graph = {name: tuple(_schema_references(node)) for name, node in definitions.items()}
    roots = tuple(_schema_references(schema["root"]))
    if len(graph) > limits["definitions"] or (
        not catalog and sum(map(len, graph.values())) + len(roots) > limits["references_per_schema"]
    ):
        _error("E_LIMIT", basename)
    visited: set[str] = set()
    active: set[str] = set()
    for root in roots:
        if root in visited:
            continue
        stack: list[tuple[str, bool]] = [(root, False)]
        while stack:
            name, leaving = stack.pop()
            if leaving:
                active.remove(name)
                visited.add(name)
                continue
            if name in active:
                _error("E_REF_CYCLE", basename)
            if name in visited:
                continue
            active.add(name)
            stack.append((name, True))
            stack.extend((target, False) for target in reversed(graph[name]))
    if set(definitions) - visited:
        _error("E_REF_UNUSED", basename)


def _value_matches(value: Any, node: Mapping[str, Any], definitions: Mapping[str, Any]) -> bool:
    stack: list[tuple[Any, Mapping[str, Any]]] = [(value, node)]
    while stack:
        current, schema = stack.pop()
        kind = schema["type"]
        if kind == "ref":
            stack.append((current, definitions[schema["ref"].rsplit("/", 1)[-1]]))
        elif kind == "object":
            if not isinstance(current, dict) or not set(schema["required"]) <= set(current) <= set(schema["properties"]):
                return False
            stack.extend((item, schema["properties"][key]) for key, item in current.items())
        elif kind == "array":
            if not isinstance(current, list) or not schema["min_items"] <= len(current) <= schema["max_items"]:
                return False
            stack.extend((item, schema["items"]) for item in current)
        elif kind == "string":
            if not isinstance(current, str) or not schema["min_length"] <= len(current) <= schema["max_length"] or re.fullmatch(schema["pattern"], current) is None:
                return False
        elif kind == "integer":
            if not isinstance(current, int) or isinstance(current, bool) or not schema["minimum"] <= current <= schema["maximum"]:
                return False
        elif kind == "boolean":
            if not isinstance(current, bool):
                return False
        elif current is not None:
            return False
    return True


def _audit_module(
    source: bytes,
    _fraction: type[Fraction] = Fraction,
    _hash: Callable[[bytes], Any] = sha256,
    _maximum: int = _MAX_MODULE_BYTES,
    _path: PurePosixPath = _MODULE_PATH,
    _signature: Callable[[Any], inspect.Signature] = inspect.signature,
) -> Callable[[dict[str, Any], str, Any], Any]:
    if len(source) > _maximum:
        _error("E_LIMIT", "<module>")
    if source.startswith(b"\xef\xbb\xbf") or b"\r" in source:
        _error("E_UTF8", "<module>")
    try:
        text = source.decode("utf-8")
        tree = ast.parse(text, filename=_path.as_posix())
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
            return type("Fractions", (), {"Fraction": _fraction})
        if level == 0 and name == "hashlib" and tuple(fromlist) == ("sha256",):
            return type("Hashlib", (), {"sha256": _hash})
        raise ImportError(name)

    allowed_builtins["__import__"] = restricted_import
    namespace: dict[str, Any] = {"__builtins__": allowed_builtins, "__name__": "_evaluation_semantics_v1"}
    try:
        exec(compile(tree, _path.as_posix(), "exec", dont_inherit=True, optimize=0), namespace)
    except Exception:
        _error("E_SEMANTIC", "<module>")
    evaluate = namespace.get("evaluate_v1")
    callables = sorted(name for name, value in namespace.items() if not name.startswith("_") and callable(value))
    if callables != ["evaluate_v1"] or not callable(evaluate) or len(_signature(evaluate).parameters) != 3:
        _error("E_SEMANTIC", "<module>")
    return evaluate


@dataclass(frozen=True)
class ValidatedEvaluationArtifactV1:
    artifact_id: str
    kind: str
    path: PurePosixPath
    sha256: str
    role_family: str
    visibility: str
    _declared_root: PurePosixPath
    value: Any

    @property
    def permitted_root(self) -> PurePosixPath:
        return self._declared_root


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
    if candidate.get("checker_boundary", {}).get("error_precedence") != list(_DECLARED_REJECTION_CODES):
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
        expected_group = {"contract": "contract-id", "corpus": "corpus-id", "run": "run-id"}
        if (
            row["count_scope"] not in expected_group
            or row["group_key"] != expected_group[row["count_scope"]]
            or isinstance(row["max_instances"], bool)
            or not isinstance(row["max_instances"], int)
            or row["max_instances"] <= 0
        ):
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


def _immutable_state_matches(frozen: Any, plain: Any) -> bool:
    stack = [(frozen, plain)]
    while stack:
        immutable, value = stack.pop()
        if isinstance(value, dict):
            if not isinstance(immutable, MappingProxyType) or set(immutable) != set(value):
                return False
            stack.extend((immutable[key], item) for key, item in value.items())
        elif isinstance(value, list):
            if not isinstance(immutable, tuple) or len(immutable) != len(value):
                return False
            stack.extend(zip(immutable, value))
        elif type(immutable) is not type(value) or immutable != value:
            return False
    return True


def _validate_authority_state(
    contract_bytes: Any,
    module_bytes: Any,
    contract_value: Any,
    contract_path: Any,
    *,
    _audit: Callable[[bytes], Callable[[dict[str, Any], str, Any], Any]] = _audit_module,
    _contract_path: PurePosixPath = PurePosixPath(*_CONTRACT_SUFFIX),
    _contract_digest: str = _CONTRACT_SHA256,
    _contract_validator: Callable[[Any, str], None] = _validate_contract,
    _decoder: Callable[[bytes, str, Mapping[str, int] | None], Any] = _decode_json,
    _immutable_match: Callable[[Any, Any], bool] = _immutable_state_matches,
    _module_digest: str = _MODULE_SHA256,
    _module_interface: str = _MODULE_INTERFACE,
    _module_path: PurePosixPath = _MODULE_PATH,
    _max_contract_bytes: int = _MAX_JSON_BYTES,
    _max_module_bytes: int = _MAX_MODULE_BYTES,
    _sha256: Callable[[bytes], Any] = sha256,
    _signature: Callable[[Any], inspect.Signature] = inspect.signature,
) -> tuple[dict[str, Any], Callable[[dict[str, Any], str, Any], Any]]:
    """Re-establish byte, schema, state, and executable authority at every use."""
    try:
        if type(contract_path) is not PurePosixPath:
            _error("E_ROOT", "<contract>")
        if contract_path.as_posix() != _contract_path.as_posix():
            _error("E_ROOT", "<contract>")
        if type(contract_bytes) is not bytes or type(module_bytes) is not bytes:
            _error("E_DIGEST", "<contract>")
        if len(contract_bytes) > _max_contract_bytes or len(module_bytes) > _max_module_bytes:
            _error("E_LIMIT", "<contract>" if len(contract_bytes) > _max_contract_bytes else "<module>")
        if _sha256(contract_bytes).hexdigest() != _contract_digest:
            _error("E_DIGEST", "contract-v1.json")
        if _sha256(module_bytes).hexdigest() != _module_digest:
            _error("E_DIGEST", _module_path.name)
        candidate = _decoder(contract_bytes, "contract-v1.json", None)
        _contract_validator(candidate, _module_digest)
        binding = candidate["semantic_module"]
        if binding != {
            "interface_version": _module_interface,
            "path": _module_path.as_posix(),
            "source_sha256": _module_digest,
        }:
            _error("E_SEMANTIC", "<module>")
        if not _immutable_match(contract_value, candidate):
            _error("E_DIGEST", "contract-v1.json")
        evaluate = _audit(module_bytes)
        code = getattr(evaluate, "__code__", None)
        if (
            not callable(evaluate)
            or getattr(evaluate, "__name__", None) != "evaluate_v1"
            or getattr(evaluate, "__module__", None) != "_evaluation_semantics_v1"
            or code is None
            or code.co_name != "evaluate_v1"
            or code.co_filename != _module_path.as_posix()
            or tuple(_signature(evaluate).parameters) != (
                "contract", "operation_id", "input_value",
            )
        ):
            _error("E_SEMANTIC", "<module>")
        return candidate, evaluate
    except EvaluationContractError:
        raise
    except (AttributeError, KeyError, OSError, RecursionError, TypeError, UnicodeError, ValueError):
        _error("E_DIGEST", "<contract>")


class EvaluationContractV1:
    """Self-validating authority for the byte-pinned Evaluation v1 bundle."""

    __slots__ = ("__contract_bytes", "__contract_path", "__contract_value", "__module_bytes")

    def __init__(
        self,
        contract_bytes: bytes,
        module_bytes: bytes,
        _decoder: Callable[[bytes, str, Mapping[str, int] | None], Any] = _decode_json,
        _freezer: Callable[[Any], Any] = _freeze,
        _contract_path: PurePosixPath = PurePosixPath(*_CONTRACT_SUFFIX),
    ) -> None:
        try:
            if type(contract_bytes) is not bytes or type(module_bytes) is not bytes:
                _error("E_DIGEST", "<contract>")
            value = _decoder(contract_bytes, "contract-v1.json", None)
            object.__setattr__(self, "_EvaluationContractV1__contract_bytes", contract_bytes)
            object.__setattr__(
                self, "_EvaluationContractV1__contract_path", _contract_path,
            )
            object.__setattr__(self, "_EvaluationContractV1__contract_value", _freezer(value))
            object.__setattr__(self, "_EvaluationContractV1__module_bytes", module_bytes)
            self.__validated()
        except EvaluationContractError:
            raise
        except (AttributeError, KeyError, OSError, RecursionError, TypeError, UnicodeError, ValueError):
            _error("E_DIGEST", "<contract>")

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise AttributeError("EvaluationContractV1 is immutable")

    def __validated(
        self,
        _validator: Callable[
            [Any, Any, Any, Any],
            tuple[dict[str, Any], Callable[[dict[str, Any], str, Any], Any]],
        ] = _validate_authority_state,
    ) -> tuple[dict[str, Any], Callable[[dict[str, Any], str, Any], Any]]:
        try:
            return _validator(
                object.__getattribute__(self, "_EvaluationContractV1__contract_bytes"),
                object.__getattribute__(self, "_EvaluationContractV1__module_bytes"),
                object.__getattribute__(self, "_EvaluationContractV1__contract_value"),
                object.__getattribute__(self, "_EvaluationContractV1__contract_path"),
            )
        except EvaluationContractError:
            raise
        except (AttributeError, KeyError, OSError, RecursionError, TypeError, UnicodeError, ValueError):
            _error("E_DIGEST", "<contract>")

    @property
    def contract_version(self) -> str:
        candidate, _evaluate = self.__validated()
        return candidate["contract_version"]

    @property
    def operation_ids(self) -> Mapping[str, str]:
        candidate, _evaluate = self.__validated()
        return _freeze(candidate["operation_ids"])

    @property
    def limits(self) -> Mapping[str, int]:
        candidate, _evaluate = self.__validated()
        return _freeze(candidate["limits"])

    @property
    def semantic_module_path(self) -> PurePosixPath:
        candidate, _evaluate = self.__validated()
        return PurePosixPath(candidate["semantic_module"]["path"])

    @property
    def semantic_module_sha256(self) -> str:
        candidate, _evaluate = self.__validated()
        return candidate["semantic_module"]["source_sha256"]

    def evaluate(self, operation_id: str, input_value: Any) -> Any:
        """Dispatch original caller input through the revalidated exact ``evaluate_v1``."""
        candidate, evaluate = self.__validated()
        try:
            contracts = {row["operation_id"]: row for row in candidate["operation_contracts"]}
            operation = contracts.get(operation_id)
            if operation is not None:
                schema = candidate["schemas"][operation["input_schema"]]
                if not _value_matches(input_value, schema["root"], schema["definitions"]):
                    _error("E_SEMANTIC", "<module>")
        except EvaluationContractError:
            raise
        except (KeyError, OSError, RecursionError, TypeError, UnicodeError, ValueError):
            _error("E_SEMANTIC", "<module>")
        try:
            result = evaluate(candidate, operation_id, input_value)
        except Exception:
            _error("E_SEMANTIC", "<module>")
        try:
            contracts = {row["operation_id"]: row for row in candidate["operation_contracts"]}
            operation = contracts.get(operation_id)
            if operation is None:
                schema_name = next(iter(contracts.values()))["error_schema"]
            else:
                schema_name = operation["success_schema"] if isinstance(result, dict) and result.get("status") == "ok" else operation["error_schema"]
            schema = candidate["schemas"][schema_name]
            if not _value_matches(result, schema["root"], schema["definitions"]):
                _error("E_SEMANTIC", "<module>")
            return _freeze(result)
        except EvaluationContractError:
            raise
        except (KeyError, OSError, RecursionError, TypeError, UnicodeError, ValueError):
            _error("E_SEMANTIC", "<module>")


def load_evaluation_contract(path: Path) -> EvaluationContractV1:
    """Load the one fixed production contract and its repository-relative module binding."""
    try:
        supplied = Path(path)
    except Exception:
        _error("E_ROOT", "<contract>")
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
        _audit_module(module_data)
        return EvaluationContractV1(contract_data, module_data)
    except EvaluationContractError:
        raise
    except Exception:
        _error("E_INTERNAL", "<contract>")
    finally:
        os.close(root_fd)


def _registry_match(authority: Mapping[str, Any], path: PurePosixPath) -> tuple[dict[str, Any], dict[str, str]]:
    matches: list[tuple[dict[str, Any], dict[str, str]]] = []
    for immutable_row in authority["artifact_registry"]:
        row = _thaw(immutable_row)
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
    result: list[dict[str, Any]] = []
    stack: list[tuple[Any, Mapping[str, Any]]] = [(value, node)]
    while stack:
        current, schema = stack.pop()
        if schema["type"] == "ref":
            name = schema["ref"].rsplit("/", 1)[-1]
            if name == "typed-ref-v1":
                result.append(current)
            else:
                stack.append((current, definitions[name]))
        elif schema["type"] == "object":
            stack.extend((item, schema["properties"][key]) for key, item in current.items())
        elif schema["type"] == "array":
            stack.extend((item, schema["items"]) for item in current)
    return result


def _validate_result_missingness(
    authority: Mapping[str, Any],
    path: PurePosixPath,
    value: dict[str, Any],
    loaded: Mapping[
        PurePosixPath,
        tuple[dict[str, Any], dict[str, str], dict[str, Any], str, str, dict[str, str]],
    ],
) -> None:
    policy = authority["missingness_policy"]
    terminal_path = PurePosixPath(value["terminal_edge"]["path"])
    terminal_record = loaded.get(terminal_path)
    if terminal_record is None or terminal_record[0]["kind"] != "lifecycle-edge":
        _error("E_LINEAGE", path.name)
    terminal = terminal_record[2]
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


def _scope_group(
    row: Mapping[str, Any], captures: Mapping[str, str], inherited: Mapping[str, str], basename: str,
) -> tuple[str, dict[str, str]]:
    context = dict(inherited)
    for key in ("corpus-id", "run-id"):
        if key in captures:
            prior = context.get(key)
            if prior is not None and prior != captures[key]:
                _error("E_CROSS_REFERENCE", basename)
            context[key] = captures[key]
    group_key = row["group_key"]
    if row["count_scope"] == "contract":
        if group_key != "contract-id":
            _error("E_SCHEMA", basename)
        return "contract-id", context
    group_id = context.get(group_key, "")
    if not group_id:
        _error("E_CROSS_REFERENCE", basename)
    return group_id, context


def _increment_scoped_count(
    counts: dict[tuple[str, str], int], row: Mapping[str, Any], group_id: str,
) -> None:
    counter = (row["kind"], group_id)
    counts[counter] = counts.get(counter, 0) + 1
    if counts[counter] > row["max_instances"]:
        _error("E_LIMIT", "<bundle>")


def load_evaluation_bundle(
    contract: EvaluationContractV1,
    root: Path,
    entrypoint: PurePosixPath,
) -> ValidatedEvaluationBundleV1:
    """Validate one manifest-rooted artifact closure without caller-supplied authority facts."""
    root_fd: int | None = None
    try:
        if type(contract) is not EvaluationContractV1:
            _error("E_SCHEMA", "<contract>")
        authority, _evaluate = contract._EvaluationContractV1__validated()
        limits = authority["limits"]
        entry = _safe_relative(entrypoint, limits, "<bundle>")
        try:
            root_path = Path(root)
        except Exception:
            _error("E_ROOT", "<bundle>")
        root_fd = _open_directory(root_path, "<bundle>")
        definitions = authority["schema_catalog"]["definitions"]
        registry = authority["artifact_registry"]
        group_maxima = {
            "contract": 1,
            "corpus": next(row["max_instances"] for row in registry if row["kind"] == "corpus-manifest"),
            "run": next(row["max_instances"] for row in registry if row["kind"] == "run-manifest"),
        }
        graph_limit = sum(row["max_instances"] * group_maxima[row["count_scope"]] for row in registry)
        loaded: dict[PurePosixPath, tuple[dict[str, Any], dict[str, str], dict[str, Any], str, str, dict[str, str]]] = {}
        identities: dict[PurePosixPath, tuple[str, str, str]] = {}
        edges: dict[PurePosixPath, tuple[PurePosixPath, ...]] = {}
        counts: dict[tuple[str, str], int] = {}
        queued: set[PurePosixPath] = {entry}
        pending: list[tuple[PurePosixPath, dict[str, Any], dict[str, str]]] = []
        work: list[tuple[PurePosixPath, dict[str, Any] | None, dict[str, str]]] = [(entry, None, {})]

        def path_identity(captures: Mapping[str, str], fallback: str) -> str:
            for key in ("artifact-id", "case-id", "corpus-id", "run-id", "partition"):
                if key in captures:
                    return captures[key]
            return fallback

        while work:
            path, expected, inherited = work.pop()
            row, captures = _registry_match(authority, path)
            if "payload_schema" not in row:
                _error("E_SCHEMA", path.name)
            expected_id = path_identity(captures, path.stem)
            if expected is not None:
                if expected["kind"] != row["kind"] or expected["path"] != path.as_posix():
                    _error("E_CROSS_REFERENCE", path.name)
                if expected["id"] != expected_id:
                    _error("E_ID", path.name)
                identity = (expected["id"], expected["kind"], expected["digest"])
                previous = identities.get(path)
                if previous is not None and previous != identity:
                    _error("E_CROSS_REFERENCE", path.name)
                identities[path] = identity
                pending.append((path, expected, captures))
            group_id, scope_context = _scope_group(row, captures, inherited, path.name)
            if path in loaded:
                if loaded[path][5] != scope_context:
                    _error("E_CROSS_REFERENCE", path.name)
                continue
            _increment_scoped_count(counts, row, group_id)
            if len(loaded) >= graph_limit:
                _error("E_LIMIT", "<bundle>")
            data = _read_at(root_fd, path, limits["json_artifact_bytes"], path.name)
            digest = sha256(data).hexdigest()
            value = _decode_json(data, path.name, limits)
            schema = definitions[row["payload_schema"]]
            if not _value_matches(value, schema, definitions):
                _error("E_SCHEMA", path.name)
            loaded[path] = (row, captures, value, digest, expected_id, scope_context)
            references = _typed_refs(value, schema, definitions)
            child_paths: list[PurePosixPath] = []
            for reference in references:
                reference_path = _safe_relative(reference["path"], contract.limits, path.name)
                child_paths.append(reference_path)
                if reference_path not in queued:
                    if len(queued) >= graph_limit:
                        _error("E_LIMIT", "<bundle>")
                    queued.add(reference_path)
                work.append((reference_path, reference, scope_context))
            edges[path] = tuple(child_paths)

        visited: set[PurePosixPath] = set()
        active: set[PurePosixPath] = set()
        for root_path in loaded:
            if root_path in visited:
                continue
            traversal: list[tuple[PurePosixPath, bool]] = [(root_path, False)]
            while traversal:
                node, leaving = traversal.pop()
                if leaving:
                    active.remove(node)
                    visited.add(node)
                    continue
                if node in active:
                    _error("E_REF_CYCLE", node.name)
                if node in visited:
                    continue
                active.add(node)
                traversal.append((node, True))
                traversal.extend((child, False) for child in reversed(edges.get(node, ())))

        for path, expected, _captures in pending:
            if loaded[path][3] != expected["digest"]:
                _error("E_DIGEST", path.name)

        for path, (row, _captures, value, _digest_value, _artifact_id, _scope) in loaded.items():
            if row["kind"] == "pre-adjudication-result":
                _validate_result_missingness(authority, path, value, loaded)
            if row["kind"] != "adjudication-receipt":
                continue
            case_ref = value["case"]
            case_path = PurePosixPath(case_ref["path"])
            case_record = loaded.get(case_path)
            if case_record is None:
                _error("E_LINEAGE", path.name)
            case_row, _case_captures, case_value, case_digest, _case_id, _case_scope = case_record
            answer_ref = case_value["answer_key"]
            answer_path = PurePosixPath(answer_ref["path"])
            answer_record = loaded.get(answer_path)
            if answer_record is None:
                _error("E_LINEAGE", path.name)
            answer_row, _answer_captures, _answer_value, answer_digest, _answer_id, _answer_scope = answer_record
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
                _declared_root=PurePosixPath(row["permitted_root"]),
                value=_freeze(value),
            )
            for path, (row, _captures, value, digest, artifact_id, _scope) in sorted(loaded.items(), key=lambda item: item[0].as_posix().encode("ascii"))
        )
        return ValidatedEvaluationBundleV1(entrypoint=entry, artifacts=artifacts)
    except EvaluationContractError:
        raise
    except Exception:
        _error("E_INTERNAL", "<bundle>")
    finally:
        if root_fd is not None:
            os.close(root_fd)
