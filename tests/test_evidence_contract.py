from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from agentflow.evidence import EvidenceError
from agentflow.evidence_contract import validate_fixtures


def _corpus(tmp_path: Path) -> Path:
    target = tmp_path / "evidence"
    shutil.copytree("docs/evidence", target)
    return target


def test_invalid_filename_and_duplicate_version_slug_fail_closed_in_sorted_order(tmp_path):
    corpus = _corpus(tmp_path)
    (corpus / "00-invalid.json").write_text("{}")
    with pytest.raises(EvidenceError, match=r"00-invalid\.json: suffix$"):
        validate_fixtures(corpus)
    (corpus / "00-invalid.json").unlink()
    (corpus / "negative-producer-refuted-v2.json").write_text("{}")
    with pytest.raises(EvidenceError, match=r"positive-producer-refuted-v2\.json: suffix$"):
        validate_fixtures(corpus)


@pytest.mark.parametrize("change", ["missing", "mismatch"])
def test_each_manifest_is_exact_and_required(tmp_path, change):
    corpus = _corpus(tmp_path)
    manifest = corpus / "contract-v2.json"
    if change == "missing":
        manifest.unlink()
    else:
        manifest.write_text('{"version":2}')
    with pytest.raises(EvidenceError, match=r"contract-v2\.json: manifest$"):
        validate_fixtures(corpus)


def test_directory_and_known_file_io_errors_use_only_sentinel_or_basename(tmp_path, monkeypatch):
    corpus = _corpus(tmp_path)
    original_iterdir = Path.iterdir

    def unreadable_directory(path):
        if path == corpus:
            raise OSError("private directory detail")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", unreadable_directory)
    with pytest.raises(EvidenceError, match=r"<corpus>: io$"):
        validate_fixtures(corpus)
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    original_read_text = Path.read_text

    def unreadable_file(path, *args, **kwargs):
        if path.name == "positive-producer-refuted-v2.json":
            raise OSError("private file detail")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", unreadable_file)
    with pytest.raises(EvidenceError, match=r"positive-producer-refuted-v2\.json: io$"):
        validate_fixtures(corpus)


def test_decoded_fault_precedence_selects_redaction_before_shape_type_and_vocabulary(tmp_path):
    corpus = _corpus(tmp_path)
    (corpus / "negative-redaction-precedence-v2.json").write_text(
        '{"envelope_kind":"producer_fact","prompt":7,"observed_at":true,"links":['
        '{"ordinal":99,"relation":"unknown","target_event_id":"?"}]}'
    )
    validate_fixtures(corpus)


def test_cli_error_is_one_sanitized_line_with_no_rejected_content(tmp_path):
    corpus = _corpus(tmp_path)
    (corpus / "secret-value.json").write_text('{"payload":"do-not-echo"}')
    result = subprocess.run(
        [sys.executable, "-m", "agentflow.evidence_contract", str(corpus)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "evidence contract invalid: secret-value.json: suffix\n"
    assert "do-not-echo" not in result.stderr


def test_missing_subject_kind_is_shape_not_an_inferred_subject_shape(tmp_path):
    corpus = _corpus(tmp_path)
    fixture = corpus / "positive-producer-lineage-v2.json"
    body = fixture.read_text().replace('"subject_kind": "document", ', "")
    fixture.write_text(body)

    with pytest.raises(EvidenceError, match=r"positive-producer-lineage-v2\.json: shape$"):
        validate_fixtures(corpus)


@pytest.mark.parametrize("nested", [False, True])
def test_reason_is_recursively_redacted_before_shape_without_echo(tmp_path, nested):
    corpus = _corpus(tmp_path)
    fixture = corpus / "positive-producer-lineage-v2.json"
    body = fixture.read_text()
    if nested:
        body = body.replace('"subject_kind": "document"',
                            '"reason": "private-rationale", "subject_kind": "document"')
    else:
        body = body.replace('"envelope_kind": "producer_fact"',
                            '"reason": "private-rationale", "envelope_kind": "producer_fact"')
    fixture.write_text(body)

    with pytest.raises(EvidenceError) as caught:
        validate_fixtures(corpus)
    assert str(caught.value) == "positive-producer-lineage-v2.json: redaction"
    assert "reason" not in str(caught.value)
    assert "private-rationale" not in str(caught.value)
    result = subprocess.run(
        [sys.executable, "-m", "agentflow.evidence_contract", str(corpus)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == (
        "evidence contract invalid: positive-producer-lineage-v2.json: redaction\n")
    assert "reason" not in result.stderr
    assert "private-rationale" not in result.stderr


@pytest.mark.parametrize("member,value", [
    ("ordinal", "true"),
    ("relation", "[]"),
    ("target_event_id", "[]"),
    ("envelope_kind", "[]"),
    ("subject_kind", "[]"),
])
def test_invalid_link_primitives_return_sanitized_type_without_traceback(
        tmp_path, member, value):
    corpus = _corpus(tmp_path)
    fixture = corpus / "positive-producer-lineage-v2.json"
    body = fixture.read_text()
    if member == "ordinal":
        body = body.replace('"ordinal": 0', f'"ordinal": {value}')
    elif member == "relation":
        body = body.replace('"relation": "derives_from"', f'"relation": {value}')
    elif member == "target_event_id":
        body = body.replace('"target_event_id": "event-prior"',
                            f'"target_event_id": {value}')
    elif member == "envelope_kind":
        body = body.replace('"envelope_kind": "producer_fact"',
                            f'"envelope_kind": {value}')
    else:
        body = body.replace('"subject_kind": "document"', f'"subject_kind": {value}')
    fixture.write_text(body)

    with pytest.raises(EvidenceError, match=r"positive-producer-lineage-v2\.json: type$"):
        validate_fixtures(corpus)
    result = subprocess.run(
        [sys.executable, "-m", "agentflow.evidence_contract", str(corpus)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == "evidence contract invalid: positive-producer-lineage-v2.json: type\n"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("basename", [
    "contract-v2.json",
    "positive-producer-lineage-v2.json",
])
def test_invalid_utf8_known_file_is_sanitized_io_without_traceback(tmp_path, basename):
    corpus = _corpus(tmp_path)
    (corpus / basename).write_bytes(b"\xffprivate-bytes")

    with pytest.raises(EvidenceError) as caught:
        validate_fixtures(corpus)
    assert str(caught.value) == f"{basename}: io"
    assert "private-bytes" not in str(caught.value)
    result = subprocess.run(
        [sys.executable, "-m", "agentflow.evidence_contract", str(corpus)],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr == f"evidence contract invalid: {basename}: io\n"
    assert "UnicodeDecodeError" not in result.stderr
    assert "Traceback" not in result.stderr
    assert "private-bytes" not in result.stderr


def test_readme_remains_excluded_from_fixture_decoding(tmp_path):
    corpus = _corpus(tmp_path)
    (corpus / "README.md").write_bytes(b"\xffnot-a-wire-fixture")
    validate_fixtures(corpus)
