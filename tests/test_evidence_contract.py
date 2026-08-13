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
