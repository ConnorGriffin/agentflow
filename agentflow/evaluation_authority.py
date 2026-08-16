"""Publish and inspect the one shipped Evaluation promotion authority."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import json
import os
from pathlib import Path
import tempfile

from agentflow.effective_policy import PINNED_EVALUATION_POLICY
from agentflow.evidence import (ApprovedAuthority, AuthorityPointer, EvidenceEnvelopeV2,
                                EvidenceError, EvidenceStore, Evaluation, LessonCandidate,
                                ProducerFacts, PromotionReceipt,
                                PromotionReceiptReader, SubjectRevision)
from agentflow.state import state_path


ARTIFACT_DIGEST = "a0e90b5b41c87ff67f257315cc6578b0b181249037f1ced2bac827cd3670d1ec"
_EVENT_ID = "event-4225ac80985e788e14c279f143d399c2"
_RECEIPT_ID = "receipt-evaluation-contract-v1"
_RECEIPT_DIGEST = "f39ec2e8a6eeff7718ad3db5a58a1bc762aec46f7e59c9cddd6f4b0121707562"
_MERGED_AT = 1786694020


@dataclass(frozen=True)
class AuthorityResult:
    status: str
    receipt_id: str
    artifact_digest: str
    path: str

    def value(self) -> dict[str, str]:
        return {"status": self.status, "receipt_id": self.receipt_id,
                "artifact_digest": self.artifact_digest, "path": self.path}


class _ManifestVerifier:
    """The deployment's one-use authority verifier, sealed to the locked manifest."""

    __slots__ = ("_approval", "_pointer", "_sealed")

    def __init__(self, pointer: AuthorityPointer, approval: ApprovedAuthority) -> None:
        if approval.pointer != pointer:
            raise EvidenceError("evaluation authority approval was not accepted")
        object.__setattr__(self, "_pointer", pointer)
        object.__setattr__(self, "_approval", approval)
        object.__setattr__(self, "_sealed", True)

    def __init_subclass__(cls, **kwargs: object) -> None:
        raise TypeError("_ManifestVerifier is sealed")

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("_ManifestVerifier is sealed")
        object.__setattr__(self, name, value)

    def verify(self, authority: AuthorityPointer) -> ApprovedAuthority | None:
        return self._approval if authority == self._pointer else None


def _target(path: Path | None) -> Path:
    return (path or state_path("evidence", "evidence.db")).resolve()


def _manifest() -> dict[str, object]:
    raw = files("agentflow").joinpath("_data/evaluation-authority-bootstrap-v1.json").read_bytes()
    if (len(raw) != 2628 or sha256(raw).hexdigest() != ARTIFACT_DIGEST
            or not raw.endswith(b"\n")):
        raise EvidenceError("evaluation authority artifact was not accepted")
    try:
        manifest = json.loads(raw)
        receipt = manifest["receipt"]
        receipt_bytes = json.dumps(receipt, ensure_ascii=False, allow_nan=False, sort_keys=True,
                                  separators=(",", ":")).encode()
        timestamps = (manifest["observation"]["observed_at"],
                      manifest["evaluation"]["evaluated_at"],
                      manifest["candidate"]["nominated_at"])
        if (sha256(receipt_bytes).hexdigest() != _RECEIPT_DIGEST
                or manifest["candidate"]["event_ids"] != [_EVENT_ID]
                or manifest["evaluation"]["event_id"] != _EVENT_ID
                or timestamps != (_MERGED_AT, _MERGED_AT, _MERGED_AT)):
            raise EvidenceError("evaluation authority artifact was not accepted")
    except (KeyError, TypeError, ValueError) as error:
        raise EvidenceError("evaluation authority artifact was not accepted") from error
    return manifest


def _pointer(value: dict[str, object]) -> AuthorityPointer:
    return AuthorityPointer(value["authority_kind"], value["repository"], value["locator"],
                            value["revision"], value["content_hash_algorithm"],
                            value["content_hash"], value["scope"])


def _receipt_value(receipt: PromotionReceipt) -> dict[str, object]:
    if not receipt.authoritative or receipt.authority is None:
        raise EvidenceError("promotion receipt was not authoritative")
    authority = receipt.authority
    pointer = authority.pointer
    return {"receipt_id": receipt.receipt_id, "candidate_id": receipt.candidate_id,
            "approval_id": receipt.approval_id, "policy_version": receipt.policy_version,
            "authoritative": receipt.authoritative, "authority": {
                "authority_kind": pointer.authority_kind, "repository": pointer.repository,
                "locator": pointer.locator, "revision": pointer.revision,
                "content_hash_algorithm": pointer.content_hash_algorithm,
                "content_hash": pointer.content_hash, "scope": pointer.scope,
                "approval_id": authority.approval_id, "approved_revision": authority.approved_revision,
                "approved_hash": authority.approved_hash, "approved_scope": authority.approved_scope,
                "verifier_id": authority.verifier_id, "verifier_version": authority.verifier_version,
                "outcome": authority.outcome}}


def _expected(manifest: dict[str, object]) -> dict[str, object]:
    expected = manifest["receipt"]
    if expected != PINNED_EVALUATION_POLICY.receipts[0].value():
        raise EvidenceError("evaluation authority receipt was not pinned")
    return expected


def _valid(path: Path, manifest: dict[str, object]) -> bool:
    try:
        return _receipt_value(PromotionReceiptReader(path=path).read(_RECEIPT_ID)) == _expected(manifest)
    except Exception:
        return False


def _build(path: Path, manifest: dict[str, object]) -> None:
    source = _pointer(manifest["source"])
    subject_value = manifest["subject"]
    subject = SubjectRevision(subject_value["subject_kind"], subject_value["subject"],
                              subject_value["revision"], subject_value["locator"],
                              subject_value["content_digest"])
    producer_value = manifest["producer"]
    observed = manifest["observation"]
    event = EvidenceEnvelopeV2(
        "producer_fact", observed["observation_id"], subject, source, observed["observed_at"], (),
        producer=ProducerFacts(producer_value["producer_kind"], producer_value["fact_digest"],
                               producer_value["normalizer_version"], producer_value["validation_state"],
                               producer_value["review_action"]))
    approval_value = manifest["approval"]
    approval = ApprovedAuthority(source, approval_value["approval_id"],
                                 approval_value["approved_revision"], approval_value["approved_hash"],
                                 approval_value["approved_scope"], approval_value["verifier_id"],
                                 approval_value["verifier_version"], approval_value["outcome"])
    store = EvidenceStore(path=path, verifier=_ManifestVerifier(source, approval))
    derived = store.observe(event)
    if derived.event_id != _EVENT_ID:
        raise EvidenceError("evaluation authority event was not derived exactly")
    evaluation = manifest["evaluation"]
    store.evaluate(Evaluation(evaluation["evaluation_id"], evaluation["event_id"],
                              evaluation["validation_state"], evaluation["evaluated_at"]))
    candidate = manifest["candidate"]
    store.nominate(LessonCandidate(candidate["candidate_id"], tuple(candidate["event_ids"]),
                                   candidate["proposal_digest"], candidate["policy_version"],
                                   candidate["nominated_at"]))
    receipt = store.promote(candidate["candidate_id"], source,
                            promoted_at=manifest["evaluation"]["evaluated_at"])
    if _receipt_value(receipt) != _expected(manifest):
        raise EvidenceError("evaluation authority receipt was not constructed exactly")


def _fsync(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _result(status: str, path: Path) -> AuthorityResult:
    return AuthorityResult(status, _RECEIPT_ID, ARTIFACT_DIGEST, str(path))


def deploy(*, path: Path | None = None) -> AuthorityResult:
    """Atomically publish the closed authority, never replacing a winner."""
    target = _target(path)
    manifest = _manifest()
    if target.exists():
        if _valid(target, manifest):
            return _result("already-current", target)
        if not EvidenceStore._is_initialized_empty(target):
            return _result("authority-conflict", target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".evidence-authority-", dir=target.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        _build(temporary, manifest)
        _fsync(temporary)
        if target.exists() and EvidenceStore._is_initialized_empty(target):
            os.replace(temporary, target)
        elif target.exists():
            return _result("already-current" if _valid(target, manifest) else "authority-conflict", target)
        else:
            try:
                os.link(temporary, target)
            except FileExistsError:
                return _result("already-current" if _valid(target, manifest) else "authority-conflict", target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return _result("published", target)
    finally:
        temporary.unlink(missing_ok=True)


def status(*, path: Path | None = None) -> AuthorityResult:
    """Report authority readiness without creating, repairing, or opening it for write."""
    target = _target(path)
    if not target.exists():
        return _result("missing", target)
    try:
        ready = _valid(target, _manifest())
    except Exception:
        ready = False
    return _result("ready" if ready else "invalid", target)
