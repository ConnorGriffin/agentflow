"""Fail-closed GitHub authority verification for Evidence promotion.

This module owns the whole external-authority boundary.  It accepts immutable,
content-free facts from one read-only source seam and returns the approval type
already understood by :mod:`agentflow.evidence`; it cannot write GitHub or the
working tree.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256 as _sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from typing import Callable, Protocol, TYPE_CHECKING

from agentflow.promotion_contract import (PromotionAuthorityError, PromotionScope,
                                          parse_promotion_scope)

if TYPE_CHECKING:
    from agentflow.evidence import ApprovedAuthority, AuthorityPointer
    from agentflow.github import PromotionAuthorityRead


_REGISTRY = Path("docs/evidence/promotion-scope-registry-v1.json")
_MAX_REGISTRY_BYTES = 4096
_SHA = re.compile(r"^[a-f0-9]{40,64}$")
_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_LOCATOR = re.compile(r"^pulls/([1-9][0-9]*)/files/(.+)$")


def _read_registry() -> bytes:
    """Read the fixed path without following a symlink in any component."""
    descriptors: list[int] = []
    try:
        directory_flags = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                           | getattr(os, "O_NOFOLLOW", 0))
        descriptors.append(os.open(".", directory_flags))
        for component in _REGISTRY.parts[:-1]:
            descriptors.append(os.open(component, directory_flags, dir_fd=descriptors[-1]))
        descriptor = os.open(
            _REGISTRY.name, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptors[-1],
        )
        descriptors.append(descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PromotionAuthorityError("registry file rejected")
        chunks: list[bytes] = []
        remaining = _MAX_REGISTRY_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_REGISTRY_BYTES:
            raise PromotionAuthorityError("registry file rejected")
        return raw
    except PromotionAuthorityError:
        raise
    except OSError as error:
        raise PromotionAuthorityError("registry unavailable") from error
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in pairs:
        if key in parsed:
            raise PromotionAuthorityError("registry schema rejected")
        parsed[key] = value
    return parsed


@dataclass(frozen=True)
class PromotionScopeRegistry:
    fleet_control_repository: str
    overlay_ownership: str
    schema_version: str
    revision: str
    sha256: str

    @classmethod
    def load(cls, path: Path, revision: str, sha256: str) -> "PromotionScopeRegistry":
        """Load only the canonical checked-in registry at its declared revision."""
        if not isinstance(revision, str) or not _SHA.fullmatch(revision):
            raise PromotionAuthorityError("registry revision rejected")
        if not isinstance(sha256, str) or not _DIGEST.fullmatch(sha256):
            raise PromotionAuthorityError("registry digest rejected")
        try:
            candidate = Path(path)
        except TypeError as error:
            raise PromotionAuthorityError("registry path rejected") from error
        if candidate.as_posix() != _REGISTRY.as_posix() or candidate.is_absolute():
            raise PromotionAuthorityError("registry path rejected")
        raw = _read_registry()
        if _sha256(raw).hexdigest() != sha256:
            raise PromotionAuthorityError("registry digest rejected")
        try:
            object_type = subprocess.check_output(
                ["git", "cat-file", "-t", revision], stdin=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL)
            entry = subprocess.check_output(
                ["git", "ls-tree", revision, "--", _REGISTRY.as_posix()],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pinned = subprocess.check_output(
                ["git", "show", f"{revision}:{_REGISTRY.as_posix()}"],
                stdin=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.CalledProcessError) as error:
            raise PromotionAuthorityError("registry revision rejected") from error
        entry_pattern = (rb"100(?:644|755) blob [a-f0-9]{40,64}\t"
                         + re.escape(_REGISTRY.as_posix().encode()) + rb"\n")
        if (object_type != b"commit\n" or re.fullmatch(entry_pattern, entry) is None
                or len(pinned) > _MAX_REGISTRY_BYTES or pinned != raw):
            raise PromotionAuthorityError("registry revision rejected")
        try:
            parsed = json.loads(raw, object_pairs_hook=_unique_object)
        except (PromotionAuthorityError, TypeError, UnicodeDecodeError,
                json.JSONDecodeError) as error:
            raise PromotionAuthorityError("registry schema rejected") from error
        expected = {"fleet_control_repository", "overlay_ownership", "schema_version"}
        if not isinstance(parsed, dict) or set(parsed) != expected:
            raise PromotionAuthorityError("registry schema rejected")
        canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()
        if raw != canonical:
            raise PromotionAuthorityError("registry schema rejected")
        if (parsed["fleet_control_repository"] != "ConnorGriffin/agentflow"
                or parsed["overlay_ownership"] != "same-repository"
                or parsed["schema_version"] != "agentflow-promotion-scopes-v1"):
            raise PromotionAuthorityError("registry schema rejected")
        return cls(parsed["fleet_control_repository"], parsed["overlay_ownership"],
                   parsed["schema_version"], revision, sha256)


def _parse_locator(value: str) -> tuple[int, str]:
    match = _LOCATOR.fullmatch(value)
    if match is None:
        raise PromotionAuthorityError("promotion locator rejected")
    path = match.group(2)
    pieces = path.split("/")
    if "\x00" in path or "\\" in path or any(piece in {"", ".", ".."} for piece in pieces):
        raise PromotionAuthorityError("promotion locator rejected")
    return int(match.group(1)), path


@dataclass(frozen=True)
class GitHubAuthorityFacts:
    """One PR's immutable, content-free facts from one read-only GitHub lookup.

    The source binds every field to ``repository`` and ``pull_number``; ``tree``
    is the tree of ``merge_commit`` and the artifact facts are read from it.
    """
    repository: str
    pull_number: int
    merged: bool
    merge_commit: str
    head_commit: str
    tree: str
    artifact_path: str
    artifact_revision: str
    artifact_sha256: str
    linked_issue_closed: bool
    linked_issue_completed: bool
    merged_by: str
    merged_by_permission: str


class GitHubAuthoritySource(Protocol):
    """One read-only seam; implementations may use GitHub's REST or GraphQL API."""
    def promotion_facts(self, repository: str, pull_number: int,
                        artifact_path: str, revision: str) -> GitHubAuthorityFacts | None: ...


class GitHubAuthoritySourceAdapter:
    """Production read-only adapter from typed GitHub reads to verifier facts."""
    def __init__(self, reader: Callable[
            [str, int, str, str], "PromotionAuthorityRead | None"] | None = None) -> None:
        self._reader = reader

    def promotion_facts(self, repository: str, pull_number: int,
                        artifact_path: str, revision: str) -> GitHubAuthorityFacts | None:
        try:
            reader = self._reader
            if reader is None:
                from agentflow.github import promotion_authority_read
                reader = promotion_authority_read
            result = reader(repository, pull_number, artifact_path, revision)
            if result is None:
                return None
            return GitHubAuthorityFacts(
                repository=result.repository,
                pull_number=result.pull_number,
                merged=result.merged,
                merge_commit=result.merge_commit,
                head_commit=result.head_commit,
                tree=result.tree,
                artifact_path=result.artifact_path,
                artifact_revision=result.artifact_revision,
                artifact_sha256=_sha256(result.artifact_bytes).hexdigest(),
                linked_issue_closed=result.linked_issue_closed,
                linked_issue_completed=result.linked_issue_completed,
                merged_by=result.merged_by,
                merged_by_permission=result.merged_by_permission,
            )
        except Exception:
            return None


class GitHubAuthorityVerifier:
    """Verify the one GitHub PR authority accepted by promotion."""
    verifier_id = "github-authority"
    verifier_version = "v1"

    def __init__(self, source: GitHubAuthoritySource, registry: PromotionScopeRegistry) -> None:
        self._source = source
        self._registry = registry

    def verify(self, authority: "AuthorityPointer") -> "ApprovedAuthority | None":
        # Import here: Evidence owns the public value types and imports no adapter.
        from agentflow.evidence import ApprovedAuthority

        try:
            registry = PromotionScopeRegistry.load(
                _REGISTRY, self._registry.revision, self._registry.sha256)
            if registry != self._registry:
                return None
            if (authority.authority_kind != "github"
                    or authority.content_hash_algorithm != "sha256"
                    or not _SHA.fullmatch(authority.revision)
                    or not _DIGEST.fullmatch(authority.content_hash)
                    or not _REPOSITORY.fullmatch(authority.repository)
                    or any(part in {".", ".."} for part in authority.repository.split("/"))):
                return None
            pull_number, artifact_path = _parse_locator(authority.locator)
            scope = parse_promotion_scope(authority.scope)
            if scope.kind == "fleet":
                if authority.repository != registry.fleet_control_repository:
                    return None
            elif authority.repository != scope.repository:
                return None
            facts = self._source.promotion_facts(authority.repository, pull_number,
                                                  artifact_path, authority.revision)
            if facts is None or not self._matches(authority, pull_number, artifact_path, facts):
                return None
            approval_id = "approval-" + _sha256("\0".join((
                self.verifier_version, authority.repository, str(pull_number), authority.revision,
                facts.head_commit, facts.tree, authority.locator, authority.content_hash,
                authority.scope,
            )).encode()).hexdigest()[:32]
            return ApprovedAuthority(authority, approval_id, authority.revision, authority.content_hash,
                                     authority.scope, self.verifier_id, self.verifier_version, "verified")
        except Exception:
            return None

    @staticmethod
    def _matches(authority: "AuthorityPointer", pull_number: int, artifact_path: str,
                 facts: GitHubAuthorityFacts) -> bool:
        return (
            facts.repository == authority.repository
            and facts.pull_number == pull_number
            and facts.merged is True
            and facts.merge_commit == authority.revision
            and bool(_SHA.fullmatch(facts.head_commit))
            and bool(_SHA.fullmatch(facts.tree))
            and facts.artifact_path == artifact_path
            and facts.artifact_revision == authority.revision
            and facts.artifact_sha256 == authority.content_hash
            and facts.linked_issue_closed is True and facts.linked_issue_completed is True
            and bool(facts.merged_by)
            and facts.merged_by_permission in {"maintain", "admin"}
        )
