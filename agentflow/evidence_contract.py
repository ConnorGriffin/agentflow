"""Validate checked-in Evidence producer fixtures without exposing rejected content."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any

from agentflow.evidence import (ALL_VALIDATION_STATES, FAILURE_CLASSES, LINEAGE_RELATIONS,
                                PRODUCER_KINDS, REVIEW_ACTIONS, AuthorityPointer, EvidenceError,
                                Observation, SubjectRevision, _DIGEST, _ID, _LINEAGE_MATRIX, _SHA)

_FIXTURE = re.compile(r"^(positive|negative)-([a-z0-9]+(?:-[a-z0-9]+)*)-v(1|2)\.json$")
_FORBIDDEN = frozenset({"prompt", "prompts", "transcript", "transcripts", "source_body",
                        "source_bodies", "secret", "secrets", "finding", "summary",
                        "summaries", "grounding", "payload", "payloads", "excerpt", "body",
                        "text", "raw", "metadata", "reason"})
_REASON_CODES = frozenset({"duplicate-key", "shape", "type", "vocabulary", "redaction",
                           "suffix", "manifest", "json", "io"})
_MANIFESTS = frozenset({"contract-v1.json", "contract-v2.json"})
_MAX_FIXTURE_BYTES = 1_048_576
_V1_FIELDS = {"observation_id", "subject", "failure_class", "validation_state",
              "signature_digest", "normalizer_version", "source", "observed_at",
              "reviewed_parent_revision", "fixer_revision"}
_V1_MANIFEST = {"version": 1, "envelope": "observation",
                "allowed_fields": sorted(_V1_FIELDS),
                "failure_classes": sorted(FAILURE_CLASSES),
                "validation_states": sorted(ALL_VALIDATION_STATES)}
_V2_MANIFEST = {
    "version": 2,
    "envelope": "tagged_failure_or_producer",
    "failure_envelope_fields": ["envelope_kind", "failure", "observation_id", "observed_at", "source", "subject"],
    "producer_envelope_fields": ["envelope_kind", "links", "observation_id", "observed_at", "producer", "source", "subject"],
    "failure_fact_fields": ["failure_class", "fixer_revision", "normalizer_version", "reviewed_parent_revision", "signature_digest", "validation_state"],
    "producer_fact_fields": ["fact_digest", "normalizer_version", "producer_kind", "review_action", "validation_state"],
    "review_subject_fields": ["revision", "subject", "subject_kind"],
    "content_subject_fields": ["content_digest", "locator", "revision", "subject", "subject_kind"],
    "source_fields": ["authority_kind", "content_hash", "content_hash_algorithm", "locator", "repository", "revision", "scope"],
    "link_fields": ["ordinal", "relation", "target_event_id"],
    "failure_classes": ["fix_introduced_defect", "original_defect", "plan_gap", "reviewer_false_claim", "slice_scope_error", "speculative_preference"],
    "validation_states": ["human_validated", "model_judged", "observed", "refuted", "reproduced", "unvalidated"],
    "producer_kinds": ["claim", "criterion", "decision", "decline", "delegation", "disposition", "finding", "fix", "objection", "review_action", "revision", "settlement", "slice", "verification", "verdict"],
    "review_actions": ["ask_maintainer", "discard_preference", "fix_before_completion", "necessary_follow_up"],
    "lineage_relations": ["addresses", "delegates", "derives_from", "governs", "implements", "refutes", "revises", "settles", "verifies"],
    "max_links": 32,
}


class _ContractFailure(EvidenceError):
    def __init__(self, basename: str, code: str) -> None:
        self.basename = basename
        self.code = code
        super().__init__(f"{basename}: {code}")


class _DuplicateKey(ValueError):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise _DuplicateKey
        value[key] = item
    return value


def _read(directory_fd: int, basename: str) -> str:
    path = Path(basename)
    if (path.is_absolute() or path.name != basename
            or (basename not in _MANIFESTS and _FIXTURE.fullmatch(basename) is None)):
        raise _ContractFailure("<corpus>", "io")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            basename,
            os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode) or details.st_size > _MAX_FIXTURE_BYTES:
            raise OSError
        body = bytearray()
        while True:
            remaining = _MAX_FIXTURE_BYTES - len(body)
            chunk = os.read(descriptor, min(65_536, remaining) if remaining else 1)
            if not chunk:
                break
            if len(chunk) > remaining:
                raise OSError
            body.extend(chunk)
        return body.decode("utf-8")
    except (OSError, UnicodeError) as exc:
        raise _ContractFailure(basename, "io") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _decode(directory_fd: int, basename: str) -> Any:
    body = _read(directory_fd, basename)
    try:
        return json.loads(body, object_pairs_hook=_pairs)
    except json.JSONDecodeError as exc:
        raise _ContractFailure(basename, "json") from exc
    except _DuplicateKey as exc:
        raise _ContractFailure(basename, "duplicate-key") from exc


def _has_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(set(value) & _FORBIDDEN) or any(_has_forbidden(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_forbidden(item) for item in value)
    return False


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _v1_reason(value: Any) -> str | None:
    if _has_forbidden(value):
        return "redaction"
    if not isinstance(value, dict):
        return "type"
    if set(value) - _V1_FIELDS:
        return "shape"
    required = _V1_FIELDS - {"reviewed_parent_revision", "fixer_revision"}
    if not required <= set(value):
        return "shape"
    if not all(_nonempty_string(value.get(name)) for name in (
            "observation_id", "failure_class", "validation_state", "signature_digest",
            "normalizer_version")):
        return "type"
    if not isinstance(value.get("subject"), dict) or not isinstance(value.get("source"), dict):
        return "type"
    if value["failure_class"] not in FAILURE_CLASSES or value["validation_state"] not in ALL_VALIDATION_STATES:
        return "vocabulary"
    try:
        Observation(value["observation_id"], SubjectRevision(**value["subject"]),
                    value["failure_class"], value["validation_state"],
                    value["signature_digest"], value["normalizer_version"],
                    AuthorityPointer(**value["source"]), value["observed_at"],
                    value.get("reviewed_parent_revision", ""), value.get("fixer_revision", ""))
    except (EvidenceError, TypeError):
        return "vocabulary"
    return None


def _shape(value: Any, keys: set[str], errors: set[str]) -> bool:
    if not isinstance(value, dict):
        errors.add("type")
        return False
    if set(value) != keys:
        errors.add("shape")
    return True


def _strings(value: dict[str, Any], names: set[str], errors: set[str]) -> None:
    for name in names & set(value):
        if not isinstance(value[name], str):
            errors.add("type")
        elif not value[name]:
            errors.add("vocabulary")


def _v2_reason(value: Any) -> str | None:
    errors: set[str] = set()
    if _has_forbidden(value):
        errors.add("redaction")
    if not isinstance(value, dict):
        errors.add("type")
        return _first(errors)
    kind = value.get("envelope_kind")
    kind_is_string = isinstance(kind, str)
    if "envelope_kind" not in value:
        errors.add("shape")
    elif not kind_is_string:
        errors.add("type")
    elif kind not in {"failure_observation", "producer_fact"}:
        errors.add("vocabulary")
    expected = ({"envelope_kind", "failure", "observation_id", "observed_at", "source", "subject"}
                if kind == "failure_observation" else
                {"envelope_kind", "links", "observation_id", "observed_at", "producer", "source", "subject"}
                if kind == "producer_fact" else set(value))
    if kind_is_string and kind in {"failure_observation", "producer_fact"} and set(value) != expected:
        errors.add("shape")
    _strings(value, {"envelope_kind", "observation_id"}, errors)
    observed_at = value.get("observed_at")
    if "observed_at" in value and (isinstance(observed_at, bool) or not isinstance(observed_at, int)):
        errors.add("type")
    elif isinstance(observed_at, int) and observed_at < 0:
        errors.add("vocabulary")

    subject = value.get("subject")
    if isinstance(subject, dict):
        subject_kind = subject.get("subject_kind")
        subject_kind_is_string = isinstance(subject_kind, str)
        if "subject_kind" not in subject:
            errors.add("shape")
        subject_keys = ({"subject_kind", "subject", "revision"} if subject_kind == "review" else
                        {"subject_kind", "subject", "revision", "locator", "content_digest"}
                        if subject_kind_is_string and subject_kind in {"issue", "document"}
                        else set(subject))
        _shape(subject, subject_keys, errors)
        _strings(subject, subject_keys, errors)
        if subject_kind_is_string and subject_kind not in {"review", "issue", "document"}:
            errors.add("vocabulary")
        if subject_kind == "review" and isinstance(subject.get("revision"), str) and not _SHA.fullmatch(subject["revision"]):
            errors.add("vocabulary")
        if subject_kind_is_string and subject_kind in {"issue", "document"}:
            if isinstance(subject.get("content_digest"), str) and not _DIGEST.fullmatch(subject["content_digest"]):
                errors.add("vocabulary")
            for name in ("subject", "revision", "locator"):
                if isinstance(subject.get(name), str) and not _ID.fullmatch(subject[name]):
                    errors.add("vocabulary")
    elif "subject" in value:
        errors.add("type")

    source = value.get("source")
    source_keys = {"authority_kind", "repository", "locator", "revision",
                   "content_hash_algorithm", "content_hash", "scope"}
    if isinstance(source, dict):
        _shape(source, source_keys, errors)
        _strings(source, source_keys, errors)
        for name in ("authority_kind", "repository", "locator", "content_hash_algorithm", "scope"):
            if isinstance(source.get(name), str) and not _ID.fullmatch(source[name]):
                errors.add("vocabulary")
        if isinstance(source.get("content_hash"), str) and not _DIGEST.fullmatch(source["content_hash"]):
            errors.add("vocabulary")
        authority_kind = source.get("authority_kind")
        revision = source.get("revision")
        if authority_kind == "github" and isinstance(revision, str) and not _SHA.fullmatch(revision):
            errors.add("vocabulary")
        elif authority_kind == "repository" and isinstance(revision, str):
            expected_revision = "sha256:" + str(source.get("content_hash", ""))
            if revision != expected_revision:
                errors.add("vocabulary")
        elif isinstance(authority_kind, str) and authority_kind not in {"github", "repository"}:
            errors.add("vocabulary")
    elif "source" in value:
        errors.add("type")

    if kind == "failure_observation":
        _failure(value.get("failure"), errors)
    elif kind == "producer_fact":
        producer_kind = _producer(value.get("producer"), errors)
        links = value.get("links")
        if not isinstance(links, list):
            if "links" in value:
                errors.add("type")
        else:
            if len(links) > 32:
                errors.add("vocabulary")
            pairs: set[tuple[Any, Any]] = set()
            relations: list[str] = []
            for position, link in enumerate(links):
                if not _shape(link, {"ordinal", "relation", "target_event_id"}, errors):
                    continue
                _strings(link, {"relation", "target_event_id"}, errors)
                ordinal = link.get("ordinal")
                if isinstance(ordinal, bool) or not isinstance(ordinal, int):
                    errors.add("type")
                elif ordinal != position or not 0 <= ordinal <= 31:
                    errors.add("vocabulary")
                relation = link.get("relation")
                target = link.get("target_event_id")
                relation_is_string = isinstance(relation, str)
                target_is_string = isinstance(target, str)
                if relation_is_string:
                    relations.append(relation)
                    if relation not in LINEAGE_RELATIONS:
                        errors.add("vocabulary")
                    elif producer_kind in PRODUCER_KINDS and producer_kind not in _LINEAGE_MATRIX[relation][0]:
                        errors.add("vocabulary")
                if target_is_string and not _ID.fullmatch(target):
                    errors.add("vocabulary")
                if relation_is_string and target_is_string:
                    pair = (relation, target)
                    if pair in pairs:
                        errors.add("vocabulary")
                    pairs.add(pair)
            required = {"fix": "addresses", "settlement": "settles", "delegation": "delegates",
                        "slice": "derives_from"}.get(producer_kind)
            if required is not None and required not in relations:
                errors.add("vocabulary")
    return _first(errors)


def _failure(value: Any, errors: set[str]) -> None:
    if not isinstance(value, dict):
        errors.add("type")
        return
    failure_class = value.get("failure_class")
    base = {"failure_class", "validation_state", "signature_digest", "normalizer_version"}
    keys = base | ({"reviewed_parent_revision", "fixer_revision"}
                   if failure_class == "fix_introduced_defect" else set())
    _shape(value, keys, errors)
    _strings(value, keys, errors)
    if isinstance(failure_class, str) and failure_class not in FAILURE_CLASSES:
        errors.add("vocabulary")
    state = value.get("validation_state")
    if isinstance(state, str) and state not in ALL_VALIDATION_STATES:
        errors.add("vocabulary")
    digest = value.get("signature_digest")
    if isinstance(digest, str) and not _DIGEST.fullmatch(digest):
        errors.add("vocabulary")
    for name in ("normalizer_version",):
        if isinstance(value.get(name), str) and not _ID.fullmatch(value[name]):
            errors.add("vocabulary")
    for name in ("reviewed_parent_revision", "fixer_revision"):
        if name in value and isinstance(value[name], str) and not _SHA.fullmatch(value[name]):
            errors.add("vocabulary")


def _producer(value: Any, errors: set[str]) -> str | None:
    if not isinstance(value, dict):
        errors.add("type")
        return None
    producer_kind = value.get("producer_kind")
    base = {"producer_kind", "fact_digest", "normalizer_version", "validation_state"}
    keys = base | ({"review_action"} if producer_kind == "review_action" else set())
    _shape(value, keys, errors)
    _strings(value, keys, errors)
    if isinstance(producer_kind, str) and producer_kind not in PRODUCER_KINDS:
        errors.add("vocabulary")
    state = value.get("validation_state")
    if isinstance(state, str) and state not in ALL_VALIDATION_STATES:
        errors.add("vocabulary")
    digest = value.get("fact_digest")
    if isinstance(digest, str) and not _DIGEST.fullmatch(digest):
        errors.add("vocabulary")
    normalizer = value.get("normalizer_version")
    if isinstance(normalizer, str) and not _ID.fullmatch(normalizer):
        errors.add("vocabulary")
    action = value.get("review_action")
    if isinstance(action, str) and action not in REVIEW_ACTIONS:
        errors.add("vocabulary")
    return producer_kind if isinstance(producer_kind, str) else None


def _first(errors: set[str]) -> str | None:
    for code in ("redaction", "shape", "type", "vocabulary"):
        if code in errors:
            return code
    return None


def _negative_reason(slug: str) -> str | None:
    return next((code for code in sorted(_REASON_CODES, key=len, reverse=True)
                 if slug.startswith(code + "-")), None)


def _manifest(directory_fd: int, version: int, expected: dict[str, Any]) -> None:
    basename = f"contract-v{version}.json"
    try:
        actual = json.loads(_read(directory_fd, basename), object_pairs_hook=_pairs)
    except (json.JSONDecodeError, _DuplicateKey) as exc:
        raise _ContractFailure(basename, "manifest") from exc
    if actual != expected:
        raise _ContractFailure(basename, "manifest")


def _open_authorized_directory(directory: Path) -> int:
    supplied = directory.lstat()
    if stat.S_ISLNK(supplied.st_mode) or not stat.S_ISDIR(supplied.st_mode):
        raise OSError
    resolved = directory.resolve(strict=True)
    expected = resolved.stat(follow_symlinks=False)
    if (not stat.S_ISDIR(expected.st_mode)
            or (supplied.st_dev, supplied.st_ino) != (expected.st_dev, expected.st_ino)):
        raise OSError
    descriptor: int | None = None
    try:
        descriptor = os.open(
            resolved,
            os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        opened = os.fstat(descriptor)
        if (not stat.S_ISDIR(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino)):
            raise OSError
        return descriptor
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise


def validate_fixtures(directory: Path) -> None:
    directory_fd: int | None = None
    try:
        directory_fd = _open_authorized_directory(directory)
        basenames = sorted(os.listdir(directory_fd))
        names = set(basenames)
        routed: list[tuple[str, str, str, int]] = []
        seen: set[tuple[int, str]] = set()
        for basename in basenames:
            if basename in _MANIFESTS | {"README.md"}:
                continue
            match = _FIXTURE.fullmatch(basename)
            if match is None:
                raise _ContractFailure(basename, "suffix")
            polarity, slug, version_text = match.groups()
            version = int(version_text)
            identity = (version, slug)
            if identity in seen:
                raise _ContractFailure(basename, "suffix")
            seen.add(identity)
            routed.append((basename, polarity, slug, version))
        for version in (1, 2):
            if f"contract-v{version}.json" not in names:
                raise _ContractFailure(f"contract-v{version}.json", "manifest")
        _manifest(directory_fd, 1, _V1_MANIFEST)
        _manifest(directory_fd, 2, _V2_MANIFEST)
        for basename, polarity, slug, version in routed:
            try:
                value = _decode(directory_fd, basename)
            except _ContractFailure as failure:
                if (polarity == "negative" and version == 2
                        and _negative_reason(slug) == failure.code):
                    continue
                raise
            reason = _v1_reason(value) if version == 1 else _v2_reason(value)
            if polarity == "positive":
                if reason is not None:
                    raise _ContractFailure(basename, reason)
            elif version == 2:
                expected_reason = _negative_reason(slug)
                if expected_reason is None or reason != expected_reason:
                    raise _ContractFailure(basename, reason or "shape")
            elif reason is None:
                raise _ContractFailure(basename, "shape")
    except _ContractFailure:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise _ContractFailure("<corpus>", "io") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)


def main() -> int:
    try:
        directory = Path(sys.argv[1]) if len(sys.argv) == 2 else Path("docs/evidence")
        validate_fixtures(directory)
    except _ContractFailure as exc:
        print(f"evidence contract invalid: {exc.basename}: {exc.code}", file=sys.stderr)
        return 1
    except (EvidenceError, OSError, json.JSONDecodeError):
        print("evidence contract invalid: <corpus>: io", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
