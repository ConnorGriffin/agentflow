from __future__ import annotations

import hashlib
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from agentflow import cli
from agentflow import evaluation_authority
from agentflow.evidence import (ApprovedAuthority, AuthorityPointer, EvidenceError, EvidenceStore,
                                PromotionReceiptReader)
from agentflow.effective_policy import EffectivePolicyResolver, PINNED_EVALUATION_POLICY


REPOSITORY = "ConnorGriffin/agentflow"
REVISION = "a" * 40


class NoOverlay:
    def read(self, _repository, _revision):
        return None


def _production_resolver() -> EffectivePolicyResolver:
    return EffectivePolicyResolver(
        promotion_receipts=PromotionReceiptReader.for_production(), overlay_source=NoOverlay())


def test_shipped_manifest_is_the_locked_authority_artifact():
    artifact = Path("agentflow/_data/evaluation-authority-bootstrap-v1.json").read_bytes()

    assert len(artifact) == 2628
    assert artifact.endswith(b"\n")
    assert hashlib.sha256(artifact).hexdigest() == (
        "a0e90b5b41c87ff67f257315cc6578b0b181249037f1ced2bac827cd3670d1ec")
    manifest = json.loads(artifact)
    assert hashlib.sha256(json.dumps(manifest["receipt"], sort_keys=True, separators=(",", ":")).encode()).hexdigest() == (
        "f39ec2e8a6eeff7718ad3db5a58a1bc762aec46f7e59c9cddd6f4b0121707562")
    assert manifest["candidate"]["event_ids"] == ["event-4225ac80985e788e14c279f143d399c2"]
    assert manifest["receipt"] == PINNED_EVALUATION_POLICY.receipts[0].value()


def test_runtime_manifest_validation_rejects_a_nested_receipt_mismatch(monkeypatch):
    raw = Path("agentflow/_data/evaluation-authority-bootstrap-v1.json").read_bytes()
    manifest = json.loads(raw)
    manifest["receipt"]["approval_id"] = "approval-b13219d0ab285fc314f64e66a4b1a9e6"
    altered = json.dumps(manifest, separators=(",", ":")).encode() + b"\n"

    class Resource:
        def read_bytes(self):
            return altered

    class Package:
        def joinpath(self, _name):
            return Resource()

    monkeypatch.setattr(evaluation_authority, "files", lambda _package: Package())
    monkeypatch.setattr(evaluation_authority, "ARTIFACT_DIGEST",
                        hashlib.sha256(altered).hexdigest())

    with pytest.raises(EvidenceError, match="evaluation authority artifact was not accepted"):
        evaluation_authority._manifest()


@pytest.mark.parametrize("alter", (
    lambda raw: b" " + raw,
    lambda raw: json.dumps({
        **json.loads(raw),
        "candidate": {**json.loads(raw)["candidate"],
                      "event_ids": ["event-5225ac80985e788e14c279f143d399c2"]},
    }, separators=(",", ":")).encode() + b"\n",
))
def test_runtime_manifest_validation_rejects_wrong_length_or_event(monkeypatch, alter):
    altered = alter(Path("agentflow/_data/evaluation-authority-bootstrap-v1.json").read_bytes())

    class Resource:
        def read_bytes(self):
            return altered

    class Package:
        def joinpath(self, _name):
            return Resource()

    monkeypatch.setattr(evaluation_authority, "files", lambda _package: Package())
    monkeypatch.setattr(evaluation_authority, "ARTIFACT_DIGEST",
                        hashlib.sha256(altered).hexdigest())

    with pytest.raises(EvidenceError, match="evaluation authority artifact was not accepted"):
        evaluation_authority._manifest()


def test_deploy_publishes_once_then_reports_current_and_status_ready(tmp_path):
    target = tmp_path / "evidence.db"

    published = evaluation_authority.deploy(path=target)
    before = target.read_bytes()
    current = evaluation_authority.deploy(path=target)

    assert published.status == "published"
    assert current.status == "already-current"
    assert target.read_bytes() == before
    assert evaluation_authority.status(path=target).status == "ready"


def test_deploy_and_status_fail_closed_for_a_divergent_target(tmp_path):
    target = tmp_path / "evidence.db"
    target.write_bytes(b"not an evidence store")

    assert evaluation_authority.deploy(path=target).status == "authority-conflict"
    assert evaluation_authority.status(path=target).status == "invalid"
    assert target.read_bytes() == b"not an evidence store"


def test_deploy_replaces_an_empty_initialized_evidence_store(tmp_path):
    target = tmp_path / "evidence.db"
    EvidenceStore(path=target)

    assert evaluation_authority.deploy(path=target).status == "published"
    assert evaluation_authority.status(path=target).status == "ready"


def test_status_reports_missing_without_creating_anything(tmp_path):
    target = tmp_path / "missing" / "evidence.db"

    assert evaluation_authority.status(path=target).status == "missing"
    assert not target.parent.exists()


def test_simultaneous_deployers_converge_on_one_complete_authority(tmp_path):
    target = tmp_path / "evidence.db"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: evaluation_authority.deploy(path=target), range(2)))

    assert {result.status for result in results} <= {"published", "already-current"}
    assert "published" in {result.status for result in results}
    assert evaluation_authority.status(path=target).status == "ready"


def test_cli_emits_only_content_free_closed_status_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))

    assert cli.main(["evidence-authority", "status"]) == 0
    missing = json.loads(capsys.readouterr().out)
    assert missing["status"] == "missing"
    assert set(missing) == {"status", "receipt_id", "artifact_digest", "path"}
    assert cli.main(["evidence-authority", "deploy"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "published"
    assert cli.main(["evidence-authority", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "ready"


def test_cli_conflict_and_invalid_statuses_are_nonzero_and_content_free(
        tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))
    target = tmp_path / "state" / "evidence" / "evidence.db"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"not an evidence store")

    assert cli.main(["evidence-authority", "status"]) == 1
    invalid = json.loads(capsys.readouterr().out)
    assert invalid["status"] == "invalid"
    assert set(invalid) == {"status", "receipt_id", "artifact_digest", "path"}
    assert cli.main(["evidence-authority", "deploy"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "authority-conflict"


def test_production_reader_is_lazy_and_preserves_query_only_failure(tmp_path, monkeypatch):
    target = tmp_path / "state" / "evidence" / "evidence.db"
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))

    reader = PromotionReceiptReader.for_production()

    assert reader.path == target
    assert not target.exists()
    try:
        reader.read("receipt-evaluation-contract-v1")
    except EvidenceError:
        pass
    else:
        raise AssertionError("missing production authority was accepted")


def test_readable_authority_with_a_wrong_receipt_fails_closed(tmp_path):
    target = tmp_path / "evidence.db"
    assert evaluation_authority.deploy(path=target).status == "published"
    with sqlite3.connect(target) as connection:
        connection.execute("UPDATE receipts SET approval_id='approval-wrong'")

    assert evaluation_authority.status(path=target).status == "invalid"
    assert evaluation_authority.deploy(path=target).status == "authority-conflict"


def test_deploy_constructs_authority_only_through_the_governed_verbs(tmp_path, monkeypatch):
    calls: list[str] = []
    for name in ("observe", "evaluate", "nominate", "promote"):
        original = getattr(EvidenceStore, name)

        def wrapped(self, *args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(EvidenceStore, name, wrapped)

    assert evaluation_authority.deploy(path=tmp_path / "evidence.db").status == "published"
    assert calls == ["observe", "evaluate", "nominate", "promote"]


def test_deploy_does_not_depend_on_evidence_test_verifier(tmp_path, monkeypatch):
    def test_fake(*_args, **_kwargs):
        raise AssertionError("production authority used the Evidence test fake")

    monkeypatch.setattr(evaluation_authority, "FakeAuthorityVerifier", test_fake, raising=False)

    assert evaluation_authority.deploy(path=tmp_path / "evidence.db").status == "published"


def test_manifest_verifier_accepts_only_its_exact_approved_pointer():
    manifest = evaluation_authority._manifest()
    pointer = evaluation_authority._pointer(manifest["source"])
    approval = manifest["approval"]
    approved = ApprovedAuthority(pointer, approval["approval_id"], approval["approved_revision"],
                                 approval["approved_hash"], approval["approved_scope"],
                                 approval["verifier_id"], approval["verifier_version"],
                                 approval["outcome"])
    verifier = evaluation_authority._ManifestVerifier(pointer, approved)
    other = AuthorityPointer(pointer.authority_kind, pointer.repository, pointer.locator,
                             "f" * 40, pointer.content_hash_algorithm, pointer.content_hash,
                             pointer.scope)

    assert verifier.verify(pointer) == approved
    assert verifier.verify(other) is None
    with pytest.raises(AttributeError):
        verifier._approval = approved


@pytest.mark.parametrize("state", ("absent", "unreadable", "corrupt", "wrong_schema"))
def test_shipped_resolver_maps_unavailable_production_authority_to_missing_receipt(
        tmp_path, monkeypatch, state):
    target = tmp_path / "state" / "evidence" / "evidence.db"
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))
    if state == "unreadable":
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not an evidence store")
        target.chmod(0)
    elif state == "corrupt":
        target.parent.mkdir(parents=True)
        target.write_bytes(b"not an evidence store")
    elif state == "wrong_schema":
        target.parent.mkdir(parents=True)
        with sqlite3.connect(target) as connection:
            connection.execute("PRAGMA user_version = 999")

    try:
        result = _production_resolver().brief_for(REPOSITORY, "review", REVISION)
    finally:
        if state == "unreadable":
            target.chmod(0o600)

    assert result.hold_code == "missing_receipt"


@pytest.mark.parametrize(("column", "value"), (
    ("approval_id", "approval-wrong"),
    ("authority_repository", "Other/agentflow"),
))
def test_shipped_resolver_maps_readable_mismatched_authority_to_invalid_receipt(
        tmp_path, monkeypatch, column, value):
    target = tmp_path / "state" / "evidence" / "evidence.db"
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))
    assert evaluation_authority.deploy(path=target).status == "published"
    with sqlite3.connect(target) as connection:
        connection.execute(f"UPDATE receipts SET {column}=?", (value,))

    result = _production_resolver().brief_for(REPOSITORY, "review", REVISION)

    assert result.hold_code == "invalid_receipt"


def test_shipped_resolver_reads_every_pinned_field_from_deployed_authority(tmp_path, monkeypatch):
    target = tmp_path / "state" / "evidence" / "evidence.db"
    monkeypatch.setenv("AGENTFLOW_STATE", str(tmp_path / "state"))
    assert evaluation_authority.deploy(path=target).status == "published"

    receipt = PromotionReceiptReader.for_production().read("receipt-evaluation-contract-v1")
    result = _production_resolver().brief_for(REPOSITORY, "review", REVISION)

    assert evaluation_authority._receipt_value(receipt) == PINNED_EVALUATION_POLICY.receipts[0].value()
    assert result.status == "ready"
    assert result.receipts == PINNED_EVALUATION_POLICY.receipts


@pytest.mark.parametrize("cutpoint", ("build", "fsync", "link"))
def test_prepublication_faults_leave_absence_and_no_temporary_authority(
        tmp_path, monkeypatch, cutpoint):
    target = tmp_path / "evidence" / "evidence.db"

    def fail(*_args, **_kwargs):
        raise OSError("injected pre-publication fault")

    if cutpoint == "build":
        monkeypatch.setattr(evaluation_authority, "_build", fail)
    elif cutpoint == "fsync":
        monkeypatch.setattr(evaluation_authority, "_fsync", fail)
    else:
        monkeypatch.setattr(evaluation_authority.os, "link", fail)

    with pytest.raises(OSError, match="injected pre-publication fault"):
        evaluation_authority.deploy(path=target)

    assert not target.exists()
    assert not list(target.parent.glob(".evidence-authority-*"))
    assert evaluation_authority.status(path=target).status == "missing"
    with pytest.raises(EvidenceError):
        PromotionReceiptReader(path=target)


def test_existing_winner_survives_prepublication_fault_and_reader_sees_complete_store(
        tmp_path, monkeypatch):
    target = tmp_path / "evidence" / "evidence.db"
    assert evaluation_authority.deploy(path=target).status == "published"
    winner = target.read_bytes()

    def fail(*_args, **_kwargs):
        raise AssertionError("existing authority must not enter publication")

    monkeypatch.setattr(evaluation_authority, "_build", fail)
    result = evaluation_authority.deploy(path=target)

    assert result.status == "already-current"
    assert target.read_bytes() == winner
    assert not list(target.parent.glob(".evidence-authority-*"))
    assert evaluation_authority._receipt_value(
        PromotionReceiptReader(path=target).read("receipt-evaluation-contract-v1")
    ) == PINNED_EVALUATION_POLICY.receipts[0].value()
