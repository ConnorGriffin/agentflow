"""Read-only, fail-closed effective policy briefing delivery (#628).

The resolver folds one reviewed fleet policy, an injected same-repository overlay,
and the requested stage.  It owns no storage and deliberately has no adapter for
GitHub, Evidence writes, coordinator state, or provider content.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import re
import subprocess
from types import MappingProxyType
from pathlib import Path
import unicodedata
from typing import Any, Mapping, Protocol

from agentflow.evidence import PromotionReceiptReader


DEPENDENCY_PINS: Mapping[str, object] = MappingProxyType({
    "evidence_schema": 4,
    "promotion_contract": "github-merged-pr-v1",
    "issue_584_merge": "ef08dd3d2f691aa154ddaa193e6161b559099396",
    "issue_585_merge": "bd818fa1d65c92def671192464207e6bc3904a34",
    "promotion_reader_blob": "02e7d525a4cba5c4cdd95e26143673ea186e5519",
    "issue_617_merge": "121bc28b9dc65bbddf537396dae479bb259e7f52",
    "evaluation_candidate_sha256":
        "53359f35de57047441defa76a477564580b956f968ab6425356cca3a1c5a8409",
    "evaluation_module_sha256":
        "185f41a5e4549cc1ccbc4615af5846c3ed0f95285790d193e1b2f43aa3dc8554",
})

STAGES = (
    "intake", "attack", "research", "build", "review", "revise", "mockup", "respond",
)
HOLD_CODES = (
    "briefing_overflow", "incompatible_policy", "invalid_briefing", "invalid_overlay",
    "invalid_receipt", "missing_policy", "missing_receipt",
)
_MAX_INTEGER = 9_223_372_036_854_775_807
_MAX_OVERLAY_BYTES = 8192
_MAX_BRIEFING_BYTES = 16384
_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_REPOSITORY_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_LOCATOR = re.compile(r"^pulls/([1-9][0-9]*)/files/([A-Za-z0-9][A-Za-z0-9._:/-]*)$")
_COMMIT_REVISION = re.compile(r"^[a-f0-9]{40,64}$")
_CONTENT_REVISION = re.compile(r"^sha256:([a-f0-9]{64})$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_SUBJECT_REVISION = re.compile(r"^[a-f0-9]{40}$")
_BOUND_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9._-]{0,63}$")
_SCOPE_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]+$")


class PolicyValidationError(ValueError):
    """Untrusted policy or briefing input was rejected without retaining it."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PolicyValidationError("duplicate object member")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise PolicyValidationError("non-finite number")


def _nfc(value: object, name: str) -> str:
    if not isinstance(value, str) or unicodedata.normalize("NFC", value) != value:
        raise PolicyValidationError(f"invalid {name}")
    return value


def _token(value: object, name: str = "token") -> str:
    text = _nfc(value, name)
    if (not _TOKEN.fullmatch(text) or len(text.encode("utf-8")) > 128
            or "?" in text or "#" in text or text != text.strip()
            or any(ord(character) < 32 or ord(character) == 127 for character in text)):
        raise PolicyValidationError(f"invalid {name}")
    return text


def _repository(value: object) -> str:
    text = _nfc(value, "repository")
    pieces = text.split("/")
    if (len(pieces) != 2 or not all(_REPOSITORY_SEGMENT.fullmatch(piece) for piece in pieces)
            or not 3 <= len(text.encode("utf-8")) <= 200):
        raise PolicyValidationError("invalid repository")
    return text


def _locator(value: object) -> str:
    text = _nfc(value, "locator")
    match = _LOCATOR.fullmatch(text)
    if not 15 <= len(text.encode("utf-8")) <= 128 or match is None:
        raise PolicyValidationError("invalid locator")
    tail = match.group(2)
    if (tail.startswith("/") or "\\" in tail or "?" in tail or "#" in tail
            or "\0" in tail or any(piece in {"", ".", ".."} for piece in tail.split("/"))):
        raise PolicyValidationError("invalid locator")
    return text


def _scope(value: object) -> tuple[str, str, int, int]:
    text = _nfc(value, "scope")
    if len(text.encode("utf-8")) > 128:
        raise PolicyValidationError("invalid scope")
    pieces = text.split("/")
    repository = ""
    if len(pieces) == 2 and pieces[0] == "fleet-policy":
        transition = pieces[1]
        kind = "fleet"
    elif len(pieces) == 4 and pieces[0] == "repository-policy":
        owner, name, transition = pieces[1:]
        if (not _SCOPE_SEGMENT.fullmatch(owner) or not _SCOPE_SEGMENT.fullmatch(name)
                or owner in {".", ".."} or name in {".", ".."}):
            raise PolicyValidationError("invalid scope")
        repository = f"{owner}/{name}"
        kind = "repository"
    else:
        raise PolicyValidationError("invalid scope")
    match = re.fullmatch(r"(0|[1-9][0-9]*)-to-([1-9][0-9]*)", transition)
    if match is None:
        raise PolicyValidationError("invalid scope")
    prior, new = (int(item) for item in match.groups())
    if new <= prior:
        raise PolicyValidationError("invalid scope")
    return kind, repository, prior, new


def _revision(value: object, authority_kind: str, content_hash: str) -> str:
    text = _nfc(value, "revision")
    content = _CONTENT_REVISION.fullmatch(text)
    if authority_kind == "github":
        accepted = _COMMIT_REVISION.fullmatch(text) is not None or (
            content is not None and content.group(1) == content_hash)
    else:
        accepted = content is not None and content.group(1) == content_hash
    if not accepted:
        raise PolicyValidationError("invalid revision")
    return text


def _digest(value: object, name: str = "digest") -> str:
    text = _nfc(value, name)
    if not _DIGEST.fullmatch(text):
        raise PolicyValidationError(f"invalid {name}")
    return text


def _bound_name(value: object) -> str:
    text = _nfc(value, "bound_name")
    if not _BOUND_NAME.fullmatch(text) or len(text.encode("utf-8")) > 64:
        raise PolicyValidationError("invalid bound_name")
    return text


def _integer(value: object, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= _MAX_INTEGER:
        raise PolicyValidationError(f"invalid {name}")
    return value


def _json_value(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        _nfc(value, "string")
        return value
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            _nfc(key, "object member")
            result[key] = _json_value(item)
        return result
    raise PolicyValidationError("unsupported canonical value")


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as error:
        raise PolicyValidationError("canonicalization failed") from error


def _sorted_unique(values: tuple[object, ...], name: str, maximum: int) -> None:
    if len(values) > maximum:
        raise PolicyValidationError(f"too many {name}")
    encoded = tuple(_canonical_bytes(value) for value in values)
    if encoded != tuple(sorted(encoded)) or len(set(encoded)) != len(encoded):
        raise PolicyValidationError(f"invalid {name} ordering")


@dataclass(frozen=True, slots=True)
class Bound:
    name: str
    maximum: int

    def __post_init__(self) -> None:
        _bound_name(self.name)
        _integer(self.maximum, "maximum")

    def value(self) -> dict[str, object]:
        return {"maximum": self.maximum, "name": self.name}


@dataclass(frozen=True, slots=True)
class CapabilityRequirement:
    contract_id: str
    contract_version: str
    contract_digest: str
    required: bool = True
    bounds: tuple[Bound, ...] = ()

    def __post_init__(self) -> None:
        _token(self.contract_id, "contract_id")
        _token(self.contract_version, "contract_version")
        _digest(self.contract_digest, "contract_digest")
        if self.required is not True or not isinstance(self.bounds, tuple):
            raise PolicyValidationError("invalid capability")
        values = tuple(bound.value() for bound in self.bounds)
        _sorted_unique(values, "bounds", 32)

    def value(self) -> dict[str, object]:
        return {
            "bounds": [bound.value() for bound in self.bounds],
            "contract_digest": self.contract_digest,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "required": True,
        }


@dataclass(frozen=True, slots=True)
class BriefingAuthority:
    authority_kind: str
    repository: str
    locator: str
    revision: str
    content_hash_algorithm: str
    content_hash: str
    scope: str
    approval_id: str
    approved_revision: str
    approved_hash: str
    approved_scope: str
    verifier_id: str
    verifier_version: str
    outcome: str

    def __post_init__(self) -> None:
        if self.authority_kind not in {"github", "repository"}:
            raise PolicyValidationError("invalid authority_kind")
        _repository(self.repository)
        _locator(self.locator)
        _digest(self.content_hash, "content_hash")
        if self.content_hash_algorithm != "sha256":
            raise PolicyValidationError("invalid content_hash_algorithm")
        _revision(self.revision, self.authority_kind, self.content_hash)
        _scope(self.scope)
        _token(self.approval_id, "approval_id")
        if (self.approved_revision != self.revision or self.approved_hash != self.content_hash
                or self.approved_scope != self.scope):
            raise PolicyValidationError("approval does not bind authority")
        if (self.verifier_id != "github-authority" or self.verifier_version != "v1"
                or self.outcome != "verified"):
            raise PolicyValidationError("invalid approval verifier")

    def value(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BriefingReceipt:
    receipt_id: str
    candidate_id: str
    approval_id: str
    policy_version: int
    authoritative: bool
    authority: BriefingAuthority

    def __post_init__(self) -> None:
        _token(self.receipt_id, "receipt_id")
        _token(self.candidate_id, "candidate_id")
        _token(self.approval_id, "approval_id")
        _integer(self.policy_version, "policy_version", 1)
        if self.authoritative is not True or self.approval_id != self.authority.approval_id:
            raise PolicyValidationError("invalid authoritative receipt")

    def value(self) -> dict[str, object]:
        return {
            "approval_id": self.approval_id,
            "authority": self.authority.value(),
            "authoritative": True,
            "candidate_id": self.candidate_id,
            "policy_version": self.policy_version,
            "receipt_id": self.receipt_id,
        }


@dataclass(frozen=True, slots=True)
class FleetPolicyV1:
    policy_version: int
    receipts: tuple[BriefingReceipt, ...]
    capabilities: tuple[CapabilityRequirement, ...]
    applicable_stages: tuple[str, ...] = tuple(sorted(STAGES))

    def __post_init__(self) -> None:
        _integer(self.policy_version, "policy_version", 1)
        if (not isinstance(self.receipts, tuple) or not isinstance(self.capabilities, tuple)
                or not isinstance(self.applicable_stages, tuple)):
            raise PolicyValidationError("fleet policy collections must be tuples")
        _sorted_unique(tuple(item.value() for item in self.receipts), "receipts", 64)
        _sorted_unique(tuple(item.value() for item in self.capabilities), "capabilities", 64)
        _sorted_unique(self.applicable_stages, "stages", 64)
        if any(stage not in STAGES for stage in self.applicable_stages):
            raise PolicyValidationError("invalid stage")
        if any(receipt.policy_version != self.policy_version for receipt in self.receipts):
            raise PolicyValidationError("incompatible receipt policy version")


@dataclass(frozen=True, slots=True)
class NarrowBound:
    contract_id: str
    bound_name: str
    maximum: int

    def __post_init__(self) -> None:
        _token(self.contract_id, "contract_id")
        _bound_name(self.bound_name)
        _integer(self.maximum, "maximum")

    def value(self) -> dict[str, object]:
        return {"bound_name": self.bound_name, "contract_id": self.contract_id,
                "maximum": self.maximum}


@dataclass(frozen=True, slots=True)
class OverlayV1:
    schema: str
    overlay_digest: str
    repository: str
    policy_version: int
    remove_receipt_ids: tuple[str, ...]
    remove_capability_contract_ids: tuple[str, ...]
    narrow_bounds: tuple[NarrowBound, ...]
    holds: tuple[str, ...]
    not_applicable_stages: tuple[str, ...]
    canonical_bytes: bytes

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if self.schema != "briefing-overlay-v1" or not isinstance(self.canonical_bytes, bytes):
            raise PolicyValidationError("invalid overlay schema")
        _digest(self.overlay_digest, "overlay_digest")
        _repository(self.repository)
        _integer(self.policy_version, "policy_version", 1)
        collections = (
            self.remove_receipt_ids, self.remove_capability_contract_ids, self.narrow_bounds,
            self.holds, self.not_applicable_stages,
        )
        if any(not isinstance(value, tuple) for value in collections):
            raise PolicyValidationError("overlay collections must be tuples")
        for item in self.remove_receipt_ids:
            _token(item, "receipt_id")
        for item in self.remove_capability_contract_ids:
            _token(item, "contract_id")
        if any(not isinstance(item, NarrowBound) for item in self.narrow_bounds):
            raise PolicyValidationError("invalid narrow bound")
        if any(item not in HOLD_CODES for item in self.holds):
            raise PolicyValidationError("invalid hold")
        if any(item not in STAGES for item in self.not_applicable_stages):
            raise PolicyValidationError("invalid stage")
        array_values = (
            (self.remove_receipt_ids, "remove_receipt_ids"),
            (self.remove_capability_contract_ids, "remove_capability_contract_ids"),
            (tuple(item.value() for item in self.narrow_bounds), "narrow_bounds"),
            (self.holds, "holds"),
            (self.not_applicable_stages, "not_applicable_stages"),
        )
        for values, name in array_values:
            _sorted_unique(values, name, 64)
        preimage = self._value(include_digest=False)
        if sha256(_canonical_bytes(preimage)).hexdigest() != self.overlay_digest:
            raise PolicyValidationError("invalid overlay digest")
        complete = self._value(include_digest=True)
        if (len(self.canonical_bytes) > _MAX_OVERLAY_BYTES
                or self.canonical_bytes != _canonical_bytes(complete)):
            raise PolicyValidationError("invalid overlay canonical bytes")

    def _value(self, *, include_digest: bool) -> dict[str, object]:
        value: dict[str, object] = {
            "holds": list(self.holds),
            "narrow_bounds": [item.value() for item in self.narrow_bounds],
            "not_applicable_stages": list(self.not_applicable_stages),
            "policy_version": self.policy_version,
            "remove_capability_contract_ids": list(self.remove_capability_contract_ids),
            "remove_receipt_ids": list(self.remove_receipt_ids),
            "repository": self.repository,
            "schema": self.schema,
        }
        if include_digest:
            value["overlay_digest"] = self.overlay_digest
        return value

    @classmethod
    def parse(cls, raw: bytes | str) -> "OverlayV1":
        if isinstance(raw, str):
            try:
                encoded = raw.encode("utf-8")
            except UnicodeEncodeError as error:
                raise PolicyValidationError("invalid overlay encoding") from error
        elif isinstance(raw, bytes):
            encoded = raw
        else:
            raise PolicyValidationError("invalid overlay input")
        if len(encoded) > _MAX_OVERLAY_BYTES:
            raise PolicyValidationError("overlay overflow")
        try:
            value = json.loads(encoded.decode("utf-8"), object_pairs_hook=_unique_object,
                               parse_constant=_reject_constant)
        except (UnicodeDecodeError, json.JSONDecodeError, PolicyValidationError) as error:
            raise PolicyValidationError("invalid overlay JSON") from error
        if not isinstance(value, dict) or set(value) != {
            "schema", "overlay_digest", "repository", "policy_version", "remove_receipt_ids",
            "remove_capability_contract_ids", "narrow_bounds", "holds",
            "not_applicable_stages",
        }:
            raise PolicyValidationError("invalid overlay schema")
        if _canonical_bytes(value) != encoded:
            raise PolicyValidationError("overlay is not canonical")
        if value["schema"] != "briefing-overlay-v1":
            raise PolicyValidationError("invalid overlay schema")
        supplied_digest = _digest(value["overlay_digest"], "overlay_digest")
        preimage = dict(value)
        del preimage["overlay_digest"]
        if sha256(_canonical_bytes(preimage)).hexdigest() != supplied_digest:
            raise PolicyValidationError("invalid overlay digest")
        lists = ("remove_receipt_ids", "remove_capability_contract_ids", "narrow_bounds",
                 "holds", "not_applicable_stages")
        if any(not isinstance(value[name], list) for name in lists):
            raise PolicyValidationError("invalid overlay arrays")
        for name in lists:
            _sorted_unique(tuple(value[name]), name, 64)
        receipt_ids = tuple(_token(item, "receipt_id") for item in value["remove_receipt_ids"])
        contract_ids = tuple(
            _token(item, "contract_id") for item in value["remove_capability_contract_ids"])
        narrow: list[NarrowBound] = []
        for item in value["narrow_bounds"]:
            if not isinstance(item, dict) or set(item) != {"contract_id", "bound_name", "maximum"}:
                raise PolicyValidationError("invalid narrow bound")
            narrow.append(NarrowBound(item["contract_id"], item["bound_name"], item["maximum"]))
        holds = tuple(value["holds"])
        stages = tuple(value["not_applicable_stages"])
        if any(item not in HOLD_CODES for item in holds) or any(item not in STAGES for item in stages):
            raise PolicyValidationError("invalid closed overlay value")
        return cls("briefing-overlay-v1", supplied_digest, _repository(value["repository"]),
                   _integer(value["policy_version"], "policy_version", 1), receipt_ids,
                   contract_ids, tuple(narrow), holds, stages, encoded)


class RepositoryOverlaySource(Protocol):
    """Injected read-only same-repository configuration authority."""

    def read(self, repository: str, subject_revision: str) -> OverlayV1 | None: ...


class ExactRevisionRepositoryOverlaySource:
    """Read the sole production overlay object without observing a mutable checkout.

    ``git show <revision>:<path>`` addresses the object directly in the enrolled local
    repository.  It neither changes HEAD nor needs a network; the resolver deliberately
    translates every malformed/unavailable result to its existing ``invalid_overlay`` hold.
    """

    _PATH = ".agentflow/briefing-overlay-v1.json"

    def __init__(self, repositories: Mapping[str, str | Path]) -> None:
        self._repositories = {name: Path(path) for name, path in repositories.items()}

    def read(self, repository: str, subject_revision: str) -> OverlayV1 | None:
        root = self._repositories.get(repository)
        if root is None or not _SUBJECT_REVISION.fullmatch(subject_revision):
            raise PolicyValidationError("overlay repository or revision is unavailable")
        try:
            result = subprocess.run(
                ["git", "show", f"{subject_revision}:{self._PATH}"], cwd=root,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=False, timeout=5)
        except (OSError, subprocess.SubprocessError) as error:
            raise PolicyValidationError("overlay object is unreadable") from error
        if result.returncode:
            # Missing is the only absence; an invalid revision is rejected above and a
            # repository whose object database cannot serve a known revision also fails closed.
            probe = subprocess.run(
                ["git", "cat-file", "-e", f"{subject_revision}^{{commit}}"], cwd=root,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                check=False, timeout=5)
            if probe.returncode == 0:
                return None
            raise PolicyValidationError("overlay revision is unavailable")
        if len(result.stdout) > _MAX_OVERLAY_BYTES:
            raise PolicyValidationError("overlay exceeds byte limit")
        return OverlayV1.parse(result.stdout)


@dataclass(frozen=True, slots=True)
class ApplicabilityFacts:
    repository_scope: str
    stage: str
    subject_revision: str

    def __post_init__(self) -> None:
        _scope(self.repository_scope)
        if self.stage not in STAGES or not _SUBJECT_REVISION.fullmatch(self.subject_revision):
            raise PolicyValidationError("invalid applicability")

    def value(self) -> dict[str, object]:
        return asdict(self)


class BriefingV1:
    schema = "briefing-v1"
    status: str
    repository: str
    stage: str
    subject_revision: str
    briefing_digest: str
    briefing_id: str

    def value(self) -> dict[str, object]:
        raise NotImplementedError

    def canonical_bytes(self) -> bytes:
        return _canonical_bytes(self.value())


@dataclass(frozen=True, slots=True)
class ReadyBriefing(BriefingV1):
    repository: str
    stage: str
    subject_revision: str
    briefing_digest: str
    briefing_id: str
    policy_version: int
    receipts: tuple[BriefingReceipt, ...]
    capabilities: tuple[CapabilityRequirement, ...]
    applicability: ApplicabilityFacts
    schema: str = "briefing-v1"
    status: str = "ready"

    def __post_init__(self) -> None:
        _briefing_identity(self, "ready")
        _integer(self.policy_version, "policy_version", 1)
        if not isinstance(self.receipts, tuple) or not isinstance(self.capabilities, tuple):
            raise PolicyValidationError("briefing collections must be tuples")
        _sorted_unique(tuple(item.value() for item in self.receipts), "receipts", 64)
        _sorted_unique(tuple(item.value() for item in self.capabilities), "capabilities", 64)
        if any(item.policy_version != self.policy_version for item in self.receipts):
            raise PolicyValidationError("receipt policy version mismatch")
        if (not isinstance(self.applicability, ApplicabilityFacts)
                or self.applicability.stage != self.stage
                or self.applicability.subject_revision != self.subject_revision):
            raise PolicyValidationError("applicability does not bind briefing")
        _validate_finished(self.value())

    def value(self) -> dict[str, object]:
        return {
            "applicability": self.applicability.value(),
            "briefing_digest": self.briefing_digest,
            "briefing_id": self.briefing_id,
            "capabilities": [item.value() for item in self.capabilities],
            "policy_version": self.policy_version,
            "receipts": [item.value() for item in self.receipts],
            "repository": self.repository,
            "schema": self.schema,
            "stage": self.stage,
            "status": self.status,
            "subject_revision": self.subject_revision,
        }


@dataclass(frozen=True, slots=True)
class NotApplicableBriefing(BriefingV1):
    repository: str
    stage: str
    subject_revision: str
    briefing_digest: str
    briefing_id: str
    reason: str = "stage_not_applicable"
    schema: str = "briefing-v1"
    status: str = "not_applicable"

    def __post_init__(self) -> None:
        _briefing_identity(self, "not_applicable")
        if self.reason != "stage_not_applicable":
            raise PolicyValidationError("invalid not-applicable reason")
        _validate_finished(self.value())

    def value(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HoldBriefing(BriefingV1):
    repository: str
    stage: str
    subject_revision: str
    briefing_digest: str
    briefing_id: str
    hold_code: str
    references: tuple[str, ...] = ()
    schema: str = "briefing-v1"
    status: str = "hold"

    def __post_init__(self) -> None:
        _briefing_identity(self, "hold")
        if self.hold_code not in HOLD_CODES or not isinstance(self.references, tuple):
            raise PolicyValidationError("invalid hold")
        if len(self.references) > 64:
            raise PolicyValidationError("too many references")
        for reference in self.references:
            try:
                _token(reference, "reference")
            except PolicyValidationError:
                _digest(reference, "reference")
        _sorted_unique(self.references, "references", 64)
        _validate_finished(self.value())

    def value(self) -> dict[str, object]:
        return {
            "briefing_digest": self.briefing_digest,
            "briefing_id": self.briefing_id,
            "hold_code": self.hold_code,
            "references": list(self.references),
            "repository": self.repository,
            "schema": self.schema,
            "stage": self.stage,
            "status": self.status,
            "subject_revision": self.subject_revision,
        }


def validate_briefing(value: object) -> bool:
    """Revalidate one closed briefing after it crosses a process/owner boundary."""
    if type(value) not in {ReadyBriefing, NotApplicableBriefing, HoldBriefing}:
        return False
    try:
        _briefing_identity(value, value.status)
        _validate_finished(value.value())
    except Exception:
        return False
    return True


def _finish(value: dict[str, object]) -> tuple[str, str, bytes]:
    preimage = dict(value)
    preimage.pop("briefing_digest", None)
    preimage.pop("briefing_id", None)
    digest = sha256(_canonical_bytes(preimage)).hexdigest()
    identity = f"briefing-v1:{digest}"
    value["briefing_digest"] = digest
    value["briefing_id"] = identity
    encoded = _canonical_bytes(value)
    return digest, identity, encoded


def _briefing_identity(result: BriefingV1, status: str) -> None:
    if result.schema != "briefing-v1" or result.status != status:
        raise PolicyValidationError("invalid briefing literal")
    _repository(result.repository)
    if result.stage not in STAGES:
        raise PolicyValidationError("invalid briefing stage")
    if not _SUBJECT_REVISION.fullmatch(result.subject_revision):
        raise PolicyValidationError("invalid briefing subject_revision")
    _digest(result.briefing_digest, "briefing_digest")
    if result.briefing_id != f"briefing-v1:{result.briefing_digest}":
        raise PolicyValidationError("invalid briefing_id")


def _validate_finished(value: dict[str, object]) -> None:
    supplied_digest = value["briefing_digest"]
    supplied_id = value["briefing_id"]
    digest, identity, encoded = _finish(value)
    if supplied_digest != digest or supplied_id != identity:
        raise PolicyValidationError("invalid briefing self-digest")
    if len(encoded) > _MAX_BRIEFING_BYTES:
        raise PolicyValidationError("briefing overflow")


def _hold(repository: str, stage: str, subject_revision: str, hold_code: str,
          references: tuple[str, ...] = ()) -> HoldBriefing:
    if hold_code not in HOLD_CODES:
        hold_code = "invalid_briefing"
    try:
        if len(references) > 64:
            raise PolicyValidationError("too many references")
        for reference in references:
            try:
                _token(reference, "reference")
            except PolicyValidationError:
                _digest(reference, "reference")
        _sorted_unique(references, "references", 64)
    except PolicyValidationError:
        hold_code, references = "invalid_briefing", ()
    value: dict[str, object] = {
        "briefing_digest": "", "briefing_id": "", "hold_code": hold_code,
        "references": list(references), "repository": repository, "schema": "briefing-v1",
        "stage": stage, "status": "hold", "subject_revision": subject_revision,
    }
    digest, identity, encoded = _finish(value)
    if len(encoded) > _MAX_BRIEFING_BYTES and hold_code != "briefing_overflow":
        return _hold(repository, stage, subject_revision, "briefing_overflow")
    return HoldBriefing(repository, stage, subject_revision, digest, identity,
                        hold_code, references)


_PINNED_AUTHORITY = BriefingAuthority(
    authority_kind="github",
    repository="ConnorGriffin/agentflow",
    locator="pulls/639/files/docs/evaluation/design/contract-v1.candidate.json",
    revision=DEPENDENCY_PINS["issue_617_merge"],
    content_hash_algorithm="sha256",
    content_hash=DEPENDENCY_PINS["evaluation_candidate_sha256"],
    scope="fleet-policy/0-to-1",
    approval_id="approval-a13219d0ab285fc314f64e66a4b1a9e6",
    approved_revision=DEPENDENCY_PINS["issue_617_merge"],
    approved_hash=DEPENDENCY_PINS["evaluation_candidate_sha256"],
    approved_scope="fleet-policy/0-to-1",
    verifier_id="github-authority",
    verifier_version="v1",
    outcome="verified",
)
_PINNED_EVALUATION_POLICY = FleetPolicyV1(
    policy_version=1,
    receipts=(BriefingReceipt(
        receipt_id="receipt-evaluation-contract-v1",
        candidate_id="evaluation-contract-v1",
        approval_id=_PINNED_AUTHORITY.approval_id,
        policy_version=1,
        authoritative=True,
        authority=_PINNED_AUTHORITY,
    ),),
    capabilities=(CapabilityRequirement(
        contract_id="evaluation-semantics-v1",
        contract_version="evaluation-contract-v1",
        contract_digest=DEPENDENCY_PINS["evaluation_module_sha256"],
    ),),
)
# Public inspection may read this recursively immutable contract.  Both module names are aliases;
# resolver authority captures the same object below and never resolves either name at call time.
PINNED_EVALUATION_POLICY = _PINNED_EVALUATION_POLICY

EFFECTIVE_POLICY_CONTRACT: Mapping[str, object] = MappingProxyType({
    "canonical_encoder": MappingProxyType({
        "allow_nan": False, "ensure_ascii": False, "normalization": "NFC",
        "separators": (",", ":"), "sort_keys": True,
    }),
    "dependency_pins": DEPENDENCY_PINS,
    "fold_order": ("fleet", "repository_overlay", "stage"),
    "hold_codes": HOLD_CODES,
    "overlay_schema": "briefing-overlay-v1",
    "result_schema": "briefing-v1",
    "schema": "effective-policy-contract-v1",
    "stages": STAGES,
})
EFFECTIVE_POLICY_CONTRACT_DIGEST = sha256(
    _canonical_bytes(EFFECTIVE_POLICY_CONTRACT)).hexdigest()


def _bind_authoritative_policy(implementation: Any, policy: FleetPolicyV1) -> Any:
    def brief_for(self: EffectivePolicyResolver, repo: str, stage: str,
                  subject_revision: str) -> BriefingV1:
        return implementation(self, repo, stage, subject_revision, policy)

    return brief_for


class EffectivePolicyResolver:
    """Resolve one immutable stage briefing without persistence or authority writes."""

    __slots__ = ("_promotion_receipts", "_overlay_source")

    def __init__(self, *, promotion_receipts: PromotionReceiptReader,
                 overlay_source: RepositoryOverlaySource) -> None:
        self._promotion_receipts = promotion_receipts
        self._overlay_source = overlay_source

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("EffectivePolicyResolver is sealed")

    def _brief_for(self, repo: str, stage: str, subject_revision: str,
                   policy: FleetPolicyV1) -> BriefingV1:
        repository, safe_stage, safe_revision, valid = self._validated_request(
            repo, stage, subject_revision)
        if not valid:
            return _hold(repository, safe_stage, safe_revision, "invalid_briefing")

        try:
            overlay = self._overlay_source.read(repository, safe_revision)
        except Exception:
            return _hold(repository, safe_stage, safe_revision, "invalid_overlay")
        if overlay is not None:
            try:
                if not isinstance(overlay, OverlayV1):
                    raise PolicyValidationError("invalid overlay type")
                overlay.validate()
                if (overlay.repository != repository
                        or overlay.policy_version != policy.policy_version):
                    raise PolicyValidationError("overlay authority mismatch")
                folded = self._apply_overlay(policy, overlay)
                if folded is None:
                    raise PolicyValidationError("invalid overlay restriction")
            except Exception:
                return _hold(repository, safe_stage, safe_revision, "invalid_overlay")
            receipts, capabilities = folded
            not_applicable = safe_stage in overlay.not_applicable_stages
        else:
            receipts, capabilities = policy.receipts, policy.capabilities
            not_applicable = False

        resolved_by_id: dict[str, BriefingReceipt] = {}
        for expected in policy.receipts:
            try:
                actual = self._promotion_receipts.read(expected.receipt_id)
            except Exception:
                return _hold(repository, safe_stage, safe_revision, "missing_receipt",
                             (expected.receipt_id,))
            try:
                candidate = self._receipt_value(actual)
            except Exception:
                return _hold(repository, safe_stage, safe_revision, "invalid_receipt",
                             (expected.receipt_id,))
            if candidate != expected:
                return _hold(repository, safe_stage, safe_revision, "invalid_receipt",
                             (expected.receipt_id,))
            scope_kind, scope_repository, _, new = _scope(candidate.authority.scope)
            if (new != policy.policy_version or (scope_kind == "repository"
                    and scope_repository != repository)):
                return _hold(repository, safe_stage, safe_revision, "invalid_receipt",
                             (expected.receipt_id,))
            resolved_by_id[candidate.receipt_id] = candidate

        if overlay is not None:
            if overlay.holds:
                return _hold(repository, safe_stage, safe_revision, overlay.holds[0],
                             (overlay.overlay_digest,))

        if not_applicable or safe_stage not in policy.applicable_stages:
            value: dict[str, object] = {
                "briefing_digest": "", "briefing_id": "", "reason": "stage_not_applicable",
                "repository": repository, "schema": "briefing-v1", "stage": safe_stage,
                "status": "not_applicable", "subject_revision": safe_revision,
            }
            try:
                digest, identity, encoded = _finish(value)
                if len(encoded) > _MAX_BRIEFING_BYTES:
                    return _hold(repository, safe_stage, safe_revision, "briefing_overflow")
                return NotApplicableBriefing(
                    repository, safe_stage, safe_revision, digest, identity)
            except Exception:
                return _hold(repository, safe_stage, safe_revision, "invalid_briefing")

        scope = receipts[0].authority.scope if receipts else f"fleet-policy/0-to-{policy.policy_version}"
        applicability = ApplicabilityFacts(scope, safe_stage, safe_revision)
        resolved = tuple(resolved_by_id[item.receipt_id] for item in receipts)
        value = {
            "applicability": applicability.value(), "briefing_digest": "", "briefing_id": "",
            "capabilities": [item.value() for item in capabilities],
            "policy_version": policy.policy_version,
            "receipts": [item.value() for item in resolved], "repository": repository,
            "schema": "briefing-v1", "stage": safe_stage, "status": "ready",
            "subject_revision": safe_revision,
        }
        try:
            _sorted_unique(tuple(value["receipts"]), "receipts", 64)
            _sorted_unique(tuple(value["capabilities"]), "capabilities", 64)
            digest, identity, encoded = _finish(value)
            if len(encoded) > _MAX_BRIEFING_BYTES:
                return _hold(repository, safe_stage, safe_revision, "briefing_overflow")
            result = ReadyBriefing(repository, safe_stage, safe_revision, digest, identity,
                                   policy.policy_version, resolved, capabilities, applicability)
            if result.canonical_bytes() != encoded:
                raise PolicyValidationError("briefing reconstruction changed bytes")
            return result
        except Exception:
            return _hold(repository, safe_stage, safe_revision, "invalid_briefing")

    @staticmethod
    def _validated_request(repo: object, stage: object,
                           subject_revision: object) -> tuple[str, str, str, bool]:
        repository = "invalid/repository"
        repository_valid = False
        try:
            repository = str(_repository(repo))
            repository_valid = True
        except Exception:
            pass
        stage_valid = type(stage) is str and stage in STAGES
        revision_valid = (type(subject_revision) is str
                          and _SUBJECT_REVISION.fullmatch(subject_revision) is not None)
        safe_stage = stage if stage_valid else "respond"
        safe_revision = subject_revision if revision_valid else "0" * 40
        return repository, safe_stage, safe_revision, (
            repository_valid and stage_valid and revision_valid)

    @staticmethod
    def _apply_overlay(policy: FleetPolicyV1, overlay: OverlayV1
                       ) -> tuple[tuple[BriefingReceipt, ...],
                                  tuple[CapabilityRequirement, ...]] | None:
        receipt_ids = {item.receipt_id for item in policy.receipts}
        contract_ids = {item.contract_id for item in policy.capabilities}
        if (not set(overlay.remove_receipt_ids) <= receipt_ids
                or not set(overlay.remove_capability_contract_ids) <= contract_ids
                or (overlay.holds and overlay.not_applicable_stages)):
            return None
        capabilities = [item for item in policy.capabilities
                        if item.contract_id not in overlay.remove_capability_contract_ids]
        narrowed: dict[tuple[str, str], int] = {}
        for item in overlay.narrow_bounds:
            key = (item.contract_id, item.bound_name)
            if key in narrowed:
                return None
            capability = next((candidate for candidate in capabilities
                               if candidate.contract_id == item.contract_id), None)
            bound = None if capability is None else next(
                (candidate for candidate in capability.bounds if candidate.name == item.bound_name), None)
            if bound is None or item.maximum > bound.maximum:
                return None
            narrowed[key] = item.maximum
        folded_capabilities = []
        for item in capabilities:
            bounds = tuple(sorted(
                (Bound(bound.name, narrowed.get((item.contract_id, bound.name), bound.maximum))
                 for bound in item.bounds), key=lambda bound: _canonical_bytes(bound.value())))
            folded_capabilities.append(CapabilityRequirement(
                item.contract_id, item.contract_version, item.contract_digest, True, bounds))
        folded_capabilities.sort(key=lambda item: _canonical_bytes(item.value()))
        receipts = tuple(item for item in policy.receipts
                         if item.receipt_id not in overlay.remove_receipt_ids)
        return receipts, tuple(folded_capabilities)

    @staticmethod
    def _receipt_value(receipt: Any) -> BriefingReceipt:
        authority = receipt.authority
        pointer = authority.pointer
        approved = BriefingAuthority(
            pointer.authority_kind, pointer.repository, pointer.locator, pointer.revision,
            pointer.content_hash_algorithm, pointer.content_hash, pointer.scope,
            authority.approval_id, authority.approved_revision, authority.approved_hash,
            authority.approved_scope, authority.verifier_id, authority.verifier_version,
            authority.outcome,
        )
        return BriefingReceipt(receipt.receipt_id, receipt.candidate_id, receipt.approval_id,
                               receipt.policy_version, receipt.authoritative, approved)

    brief_for = _bind_authoritative_policy(_brief_for, _PINNED_EVALUATION_POLICY)
    del _brief_for


del _bind_authoritative_policy
