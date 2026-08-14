"""Public promotion journeys and adversarial authority refusals for #584."""
from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import inspect
from pathlib import Path
import subprocess

import pytest

from agentflow.evidence import (AuthorityPointer, EvidenceError, EvidenceStore, FakeAuthorityVerifier,
                                LessonCandidate, Observation, SubjectRevision)
from agentflow.promotion import (GitHubAuthorityFacts, GitHubAuthorityVerifier,
                                 PromotionAuthorityError, PromotionScopeRegistry,
                                 parse_promotion_scope)


REGISTRY = b'{"fleet_control_repository":"ConnorGriffin/agentflow","overlay_ownership":"same-repository","schema_version":"agentflow-promotion-scopes-v1"}'


def _registry(tmp_path, monkeypatch, raw=REGISTRY):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "docs/evidence/promotion-scope-registry-v1.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "registry"], check=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True,
                              stdout=subprocess.PIPE).stdout.strip()
    return PromotionScopeRegistry.load(Path("docs/evidence/promotion-scope-registry-v1.json"),
                                       revision, sha256(raw).hexdigest())


def _pointer(*, repository="ConnorGriffin/agentflow", scope="fleet-policy/0-to-1",
             digest="b" * 64):
    return AuthorityPointer("github", repository, "pulls/42/files/docs/policy.json", "a" * 40,
                            "sha256", digest, scope)


def _facts(pointer):
    return GitHubAuthorityFacts(pointer.repository, 42, True, pointer.revision, "d" * 40,
                                "c" * 40, "docs/policy.json", pointer.revision,
                                pointer.content_hash, True, True, "maintainer", "maintain")


class Source:
    def __init__(self, facts):
        self.facts = facts
        self.calls = []

    def promotion_facts(self, repository, pull_number, artifact_path, revision):
        self.calls.append((repository, pull_number, artifact_path, revision))
        return self.facts


class UnreadableSource:
    def promotion_facts(self, repository, pull_number, artifact_path, revision):
        raise RuntimeError("untrusted source details must not escape")


def _candidate(store, pointer, *, candidate_id="candidate-1", version=1, digest=None):
    event = store.observe(Observation("observation-1", SubjectRevision("review", "pr/42", "d" * 40),
        "original_defect", "observed", "e" * 64, "v1",
        AuthorityPointer("github", "octo/repo", "issues/42", "d" * 40, "sha256", "f" * 64, "issue"), 1))
    return store.nominate(LessonCandidate(candidate_id, (event.event_id,), digest or pointer.content_hash,
                                          version, 1))


def test_registry_is_canonical_revision_bound_and_rejects_path_or_symlink(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    assert registry.fleet_control_repository == "ConnorGriffin/agentflow"
    revision = registry.revision
    digest = registry.sha256
    with pytest.raises(PromotionAuthorityError):
        PromotionScopeRegistry.load(Path("docs/evidence/../evidence/promotion-scope-registry-v1.json"), revision, digest)
    with pytest.raises(PromotionAuthorityError):
        PromotionScopeRegistry.load(Path("docs/evidence/promotion-scope-registry-v1.json"), "a" * 40, digest)
    path = Path("docs/evidence/promotion-scope-registry-v1.json")
    path.write_bytes(REGISTRY + b"\n")
    with pytest.raises(PromotionAuthorityError, match="registry revision rejected"):
        PromotionScopeRegistry.load(path, revision, sha256(REGISTRY + b"\n").hexdigest())
    path.unlink(); path.symlink_to("../../docs/evidence/promotion-scope-registry-v1.json")
    with pytest.raises(PromotionAuthorityError):
        PromotionScopeRegistry.load(path, revision, digest)


def test_registry_rejects_a_symlinked_parent_directory(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    Path("docs/evidence").rename("real-evidence")
    Path("docs/evidence").symlink_to("../real-evidence", target_is_directory=True)
    with pytest.raises(PromotionAuthorityError, match="registry unavailable"):
        PromotionScopeRegistry.load(
            Path("docs/evidence/promotion-scope-registry-v1.json"),
            registry.revision,
            registry.sha256,
        )


def test_registry_revision_must_be_a_commit_with_a_regular_file(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    tree = subprocess.run(["git", "rev-parse", "HEAD^{tree}"], check=True, text=True,
                          stdout=subprocess.PIPE).stdout.strip()
    with pytest.raises(PromotionAuthorityError, match="registry revision rejected"):
        PromotionScopeRegistry.load(
            Path("docs/evidence/promotion-scope-registry-v1.json"), tree, registry.sha256)


def test_registry_revision_rejects_a_committed_symlink(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path("docs/evidence/promotion-scope-registry-v1.json")
    path.parent.mkdir(parents=True)
    path.symlink_to(REGISTRY.decode())
    subprocess.run(["git", "init", "-q"], check=True)
    subprocess.run(["git", "add", "."], check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid",
                    "commit", "-qm", "registry"], check=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], check=True, text=True,
                              stdout=subprocess.PIPE).stdout.strip()
    path.unlink()
    path.write_bytes(REGISTRY)
    with pytest.raises(PromotionAuthorityError, match="registry revision rejected"):
        PromotionScopeRegistry.load(path, revision, sha256(REGISTRY).hexdigest())


@pytest.mark.parametrize("raw", [
    REGISTRY + b"\n",
    REGISTRY.replace(b'"overlay_ownership":"same-repository",', b""),
    REGISTRY[:-1] + b',"unknown":"field"}',
    REGISTRY.replace(b'"schema_version":', b'"schema_version":"duplicate","schema_version":'),
])
def test_registry_rejects_noncanonical_missing_unknown_or_duplicate_fields(
        tmp_path, monkeypatch, raw):
    with pytest.raises(PromotionAuthorityError, match="registry schema rejected"):
        _registry(tmp_path, monkeypatch, raw)


def test_checked_in_registry_has_the_exact_canonical_bytes():
    assert Path("docs/evidence/promotion-scope-registry-v1.json").read_bytes() == REGISTRY


def test_verifier_accepts_exact_fleet_authority_and_generates_stable_approval(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    pointer = _pointer()
    source = Source(_facts(pointer))
    verifier = GitHubAuthorityVerifier(source, registry)
    approval = verifier.verify(pointer)
    assert approval == verifier.verify(pointer)
    assert source.calls == 2 * [(pointer.repository, 42, "docs/policy.json", pointer.revision)]
    changed_tree = replace(_facts(pointer), tree="e" * 40)
    changed = GitHubAuthorityVerifier(Source(changed_tree), registry).verify(pointer)
    assert changed.approval_id != approval.approval_id
    assert list(inspect.signature(GitHubAuthorityVerifier.verify).parameters) == ["self", "authority"]
    assert not {"verify", "promotion_facts", "load"} & set(vars(EvidenceStore))


def test_verifier_accepts_same_repository_overlay_and_admin_permission(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    pointer = _pointer(repository="octo/repo", scope="repository-policy/octo/repo/0-to-1")
    facts = replace(_facts(pointer), merged_by_permission="admin")
    assert GitHubAuthorityVerifier(Source(facts), registry).verify(pointer) is not None


@pytest.mark.parametrize("change", [
    {"repository": "other/repo"}, {"pull_number": 43}, {"merged": False},
    {"merge_commit": "b" * 40}, {"head_commit": "not-a-sha"}, {"tree": "not-a-sha"},
    {"artifact_path": "docs/other.json"}, {"artifact_revision": "b" * 40},
    {"artifact_sha256": "c" * 64}, {"linked_issue_closed": False},
    {"linked_issue_completed": False}, {"merged_by": ""}, {"merged_by_permission": "write"},
])
def test_verifier_fails_closed_for_missing_or_mismatched_authority_facts(tmp_path, monkeypatch, change):
    registry = _registry(tmp_path, monkeypatch)
    pointer = _pointer()
    assert GitHubAuthorityVerifier(Source(replace(_facts(pointer), **change)), registry).verify(pointer) is None


def test_verifier_fails_closed_for_deleted_or_unreadable_authority(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    pointer = _pointer()
    assert GitHubAuthorityVerifier(Source(None), registry).verify(pointer) is None
    assert GitHubAuthorityVerifier(UnreadableSource(), registry).verify(pointer) is None


def test_verifier_rejects_forged_fleet_and_cross_repository_overlay(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    forged = _pointer(repository="octo/repo")
    assert GitHubAuthorityVerifier(Source(_facts(forged)), registry).verify(forged) is None
    overlay = _pointer(repository="octo/repo", scope="repository-policy/other/repo/0-to-1")
    assert GitHubAuthorityVerifier(Source(_facts(overlay)), registry).verify(overlay) is None
    unknown = _pointer(scope="unknown-policy/0-to-1")
    assert GitHubAuthorityVerifier(Source(_facts(unknown)), registry).verify(unknown) is None


def test_verifier_reloads_registry_and_rejects_directly_constructed_ownership(
        tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    forged_registry = replace(registry, fleet_control_repository="octo/repo")
    pointer = _pointer(repository="octo/repo")
    assert GitHubAuthorityVerifier(Source(_facts(pointer)), forged_registry).verify(pointer) is None


@pytest.mark.parametrize("scope", ["fleet-policy/1-to-1", "fleet-policy/00-to-1",
                                    "fleet-policy/x-to-1",
                                    "repository-policy/octo/repo/0-to-1/extra"])
def test_exact_scope_and_locator_parsing_reject_ambiguous_values(scope, tmp_path, monkeypatch):
    with pytest.raises(PromotionAuthorityError): parse_promotion_scope(scope)
    pointer = replace(_pointer(), locator="pulls/42/files/../policy.json")
    assert GitHubAuthorityVerifier(Source(_facts(pointer)), _registry(tmp_path, monkeypatch)).verify(pointer) is None


def test_promotion_binds_digest_version_prior_and_is_idempotent(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    pointer = _pointer()
    store = EvidenceStore(path=tmp_path / "evidence.db",
                          verifier=GitHubAuthorityVerifier(Source(_facts(pointer)), registry))
    _candidate(store, pointer)
    first = store.promote("candidate-1", pointer, promoted_at=2)
    assert store.promote("candidate-1", pointer, promoted_at=99) == first
    for candidate_id, version, candidate_digest, pointer_digest, scope in (
        ("digest", 2, "b" * 64, "c" * 64, "fleet-policy/1-to-2"),
        ("version", 1, pointer.content_hash, pointer.content_hash, "fleet-policy/1-to-2"),
        ("prior", 2, pointer.content_hash, pointer.content_hash, "fleet-policy/0-to-2"),
    ):
        candidate_pointer = _pointer(scope=scope, digest=pointer_digest)
        _candidate(store, candidate_pointer, candidate_id=candidate_id, version=version,
                   digest=candidate_digest)
        verifier = GitHubAuthorityVerifier(Source(_facts(candidate_pointer)), registry)
        store.verifier = verifier
        with pytest.raises(EvidenceError):
            store.promote(candidate_id, candidate_pointer, promoted_at=3)
    successor = _pointer(scope="fleet-policy/1-to-2", digest="d" * 64)
    _candidate(store, successor, candidate_id="successor", version=2)
    store.verifier = GitHubAuthorityVerifier(Source(_facts(successor)), registry)
    assert store.promote("successor", successor, promoted_at=4).policy_version == 2


def test_candidate_policy_version_rejects_boolean(tmp_path):
    store = EvidenceStore(path=tmp_path / "evidence.db")
    pointer = _pointer()
    event = store.observe(Observation(
        "observation-1", SubjectRevision("review", "pr/42", "d" * 40),
        "original_defect", "observed", "e" * 64, "v1",
        AuthorityPointer("github", "octo/repo", "issues/42", "d" * 40,
                         "sha256", "f" * 64, "issue"), 1))
    with pytest.raises(EvidenceError, match="policy_version"):
        LessonCandidate("candidate-1", (event.event_id,), pointer.content_hash, True, 1)


def test_authority_failure_leaves_candidate_promotable(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    pointer = _pointer()
    store = EvidenceStore(path=tmp_path / "evidence.db",
                          verifier=GitHubAuthorityVerifier(UnreadableSource(), registry))
    _candidate(store, pointer)
    with pytest.raises(EvidenceError, match="authority was not verified") as refusal:
        store.promote("candidate-1", pointer, promoted_at=2)
    assert "untrusted source details" not in str(refusal.value)
    store.verifier = GitHubAuthorityVerifier(Source(_facts(pointer)), registry)
    assert store.promote("candidate-1", pointer, promoted_at=3).policy_version == 1


def test_exact_replay_survives_later_source_unavailability(tmp_path, monkeypatch):
    registry = _registry(tmp_path, monkeypatch)
    pointer = _pointer()
    store = EvidenceStore(path=tmp_path / "evidence.db",
                          verifier=GitHubAuthorityVerifier(Source(_facts(pointer)), registry))
    _candidate(store, pointer)
    receipt = store.promote("candidate-1", pointer, promoted_at=2)
    store.verifier = GitHubAuthorityVerifier(UnreadableSource(), registry)
    assert store.promote("candidate-1", pointer, promoted_at=3) == receipt


def test_promotion_rejects_boolean_timestamp_without_calling_authority(tmp_path):
    source = UnreadableSource()
    store = EvidenceStore(path=tmp_path / "evidence.db", verifier=source)
    with pytest.raises(EvidenceError, match="invalid promoted_at"):
        store.promote("candidate-1", _pointer(), promoted_at=True)
