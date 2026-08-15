from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from dataclasses import asdict
from pathlib import Path

from agentflow.coordinator.record import COMPLETED, HELD, Record
from agentflow.coordinator.store import SCHEMA_VERSION, Store
from agentflow.coordinator.telemetry import (
    AttemptTelemetry, AttemptUsage, read_attempts, read_attempts_with_health,
    record_attempt, telemetry_dir)
from agentflow.review_policy import ReviewAction, ReviewAssignment, ReviewAxis, ReviewFinding, ReviewState


ROOT = Path(__file__).parents[1]


def _attempt(token, identity, *, finalized, started=10, usage=None):
    return AttemptTelemetry(
        token=token, identity=identity, repo="owner/repo", subject="42", stage="review",
        pool="codex", model="sol", complexity="standard", effort=None, reasoning_effort=None,
        attempt=1, continuation=False, restart_resumes=0, round=0, conflict_round=0,
        verified=True, outcome="", cause="none", classification="", started_at=started,
        finalized_at=finalized, usage=usage or AttemptUsage())


def _state(tmp_path):
    state = tmp_path / "state"
    store_path = state / "coordinator" / "records.db"
    store = Store(store_path)
    return state, store_path, store


def _run(state, *args):
    return subprocess.run(
        ["uv", "run", "agentflow", *args], cwd=ROOT,
        env=os.environ | {"AGENTFLOW_STATE": str(state)}, text=True,
        capture_output=True, timeout=30)


def test_learning_report_public_command_is_compact_deterministic_and_aggregates(tmp_path):
    state, path, store = _state(tmp_path)
    review = Record("review-id", "review", "codex", 1, repo="owner/repo", subject="42",
                    state=COMPLETED, subject_revision="a" * 40)
    review.__dict__.update(ReviewState(
        assignment=ReviewAssignment(axis=ReviewAxis.STANDARDS), findings=(ReviewFinding(
            ReviewAction.FIX, "private", "private"),)).record_fields())
    revise = Record("revise-id", "revise", "codex", 1, repo="owner/repo", subject="42",
                    state=HELD, subject_revision="b" * 40, round=2)
    other = Record("other-id", "review", "codex", 1, repo="other/repo", subject="99",
                   state=COMPLETED)
    for record in (review, revise, other):
        assert store.upsert(record)
    store.close()
    record_attempt(path, _attempt("first", "review-id", finalized=1_704_067_100, started=0,
                                  usage=AttemptUsage(input_tokens=2, output_tokens=3, cost_usd=1.5)))
    record_attempt(path, _attempt("second", "review-id", finalized=1_704_067_300, started=1_704_067_200,
                                  usage=AttemptUsage(cached_input_tokens=4)))
    record_attempt(path, _attempt("revise", "revise-id", finalized=1_704_067_200, started=1_704_067_100))
    record_attempt(path, _attempt("other", "other-id", finalized=1_704_067_200))

    first = _run(state, "learning", "report", "--repo", "owner/repo",
                 "--from", "2024-01-01", "--to", "2024-01-02")
    second = _run(state, "learning", "report", "--repo", "owner/repo",
                  "--from", "2024-01-01", "--to", "2024-01-02")

    assert first.returncode == second.returncode == 0, first.stderr
    assert first.stdout == second.stdout
    assert first.stdout.endswith("\n") and " " not in first.stdout
    data = json.loads(first.stdout)
    assert data["schema"] == "agentflow-learning-report-v1"
    assert data["status"] == "complete" and data["telemetry_entries_read"] == 4
    assert data["summary"]["terminal_records"] == {"completed": 1, "held": 1}
    assert data["summary"]["revision_required_rate"] == {"numerator": 1, "denominator": 1}
    assert data["summary"]["revise_rounds"] == {"total": 1, "mean": {"numerator": 1, "denominator": 1}}
    assert data["summary"]["attempts"] == 3
    assert data["summary"]["elapsed_seconds"] == {"total": 200, "attempts_known": 2, "attempts_unknown": 1}
    assert data["summary"]["tokens"] == {"total": 9, "attempts_known": 2, "attempts_unknown": 1}
    assert data["summary"]["cost_usd"] == {"total": 1.5, "attempts_known": 1, "attempts_unknown": 2}
    assert data["finding_groups"] == [{"stage": "review", "axis": "standards", "action": "fix_before_completion", "count": 1}]
    assert [item["stage"] for item in data["subjects"][0]["records"]] == ["review", "revise"]


def test_learning_report_uses_utc_half_open_window_and_degrades_for_bad_telemetry(tmp_path):
    state, path, store = _state(tmp_path)
    record = Record("edge", "review", "codex", 1, repo="owner/repo", subject="x", state=COMPLETED)
    assert store.upsert(record)
    store.close()
    record_attempt(path, _attempt("edge", "edge", finalized=1_704_067_200))  # 2024-01-01T00:00:00Z
    directory = telemetry_dir(path)
    (directory / "bad.json").write_text("{", encoding="utf-8")

    excluded = _run(state, "learning", "report", "--repo", "owner/repo",
                    "--from", "2023-12-31", "--to", "2024-01-01")
    included = _run(state, "learning", "report", "--repo", "owner/repo",
                    "--from", "2024-01-01", "--to", "2024-01-02")

    assert json.loads(excluded.stdout)["summary"]["terminal_subjects"] == 0
    data = json.loads(included.stdout)
    assert data["status"] == "degraded" and data["telemetry_entries_skipped"] == 1


def test_learning_report_counts_distinct_and_legacy_revise_rounds_and_unavailable_review(tmp_path):
    state, path, store = _state(tmp_path)
    records = (
        Record("r1", "revise", "codex", 1, repo="owner/repo", subject="x", state=COMPLETED, round=3),
        Record("r2", "revise", "codex", 1, repo="owner/repo", subject="x", state=COMPLETED, round=3),
        Record("legacy", "revise", "codex", 1, repo="owner/repo", subject="x", state=HELD),
        Record("broken", "review", "codex", 1, repo="owner/repo", subject="x", state=COMPLETED,
               review_findings="not-json"),
    )
    for record in records:
        assert store.upsert(record)
        record_attempt(path, _attempt(record.identity, record.identity, finalized=1_704_067_200))
    store.close()

    result = _run(state, "learning", "report", "--repo", "owner/repo",
                  "--from", "2024-01-01", "--to", "2024-01-02")

    data = json.loads(result.stdout)
    assert data["summary"]["revise_rounds"]["total"] == 2
    assert data["summary"]["review_state_unavailable"] == 1


def test_learning_report_empty_telemetry_is_complete_and_non_mapping_usage_is_skipped(tmp_path):
    state, path, store = _state(tmp_path)
    store.close()
    empty = _run(state, "learning", "report", "--repo", "owner/repo",
                 "--from", "2024-01-01", "--to", "2024-01-02")
    directory = telemetry_dir(path)
    directory.mkdir()
    (directory / "incompatible.json").write_text(json.dumps({"usage": []}), encoding="utf-8")
    degraded = _run(state, "learning", "report", "--repo", "owner/repo",
                    "--from", "2024-01-01", "--to", "2024-01-02")

    assert json.loads(empty.stdout)["status"] == "complete"
    data = json.loads(degraded.stdout)
    assert data["status"] == "degraded" and data["telemetry_entries_skipped"] == 1


def test_legacy_reader_keeps_non_mapping_usage_while_learning_health_skips_it(tmp_path):
    path = tmp_path / "records.db"
    entry = _attempt("legacy", "legacy", finalized=1_704_067_200)
    payload = entry.__dict__ | {"usage": []}
    directory = telemetry_dir(path)
    directory.mkdir()
    (directory / "legacy.json").write_text(json.dumps(payload), encoding="utf-8")

    (legacy,) = read_attempts(path)
    strict = read_attempts_with_health(path)

    assert legacy.identity == "legacy" and legacy.usage == AttemptUsage()
    assert strict.entries == [] and strict.skipped == 1


def test_learning_report_skips_bad_timestamps_but_emits_other_facts(tmp_path):
    state, path, store = _state(tmp_path)
    for identity in ("good", "bad"):
        assert store.upsert(Record(identity, "review", "codex", 1, repo="owner/repo",
                                   subject=identity, state=COMPLETED))
    store.close()
    record_attempt(path, _attempt("good", "good", finalized=1_704_067_200))
    bad = asdict(_attempt("bad", "bad", finalized=1_704_067_200)) | {"started_at": "bad"}
    (telemetry_dir(path) / "bad.json").write_text(json.dumps(bad), encoding="utf-8")

    result = _run(state, "learning", "report", "--repo", "owner/repo",
                  "--from", "2024-01-01", "--to", "2024-01-02")

    data = json.loads(result.stdout)
    assert result.returncode == 0 and data["status"] == "degraded"
    assert data["telemetry_entries_skipped"] == 1
    assert [item["subject"] for item in data["subjects"]] == ["good"]


def test_learning_required_arguments_and_unavailable_store_exit_two_without_creation(tmp_path):
    state = tmp_path / "state"
    missing = _run(state, "learning", "report")
    unavailable = _run(state, "learning", "report", "--repo", "owner/repo",
                       "--from", "2024-01-01", "--to", "2024-01-02")

    assert missing.returncode == 2 and "--repo" in missing.stderr
    assert unavailable.returncode == 2 and unavailable.stdout == ""
    assert not (state / "coordinator" / "records.db").exists()


def test_learning_rejects_old_store_without_migrating_it(tmp_path):
    state = tmp_path / "state"
    path = state / "coordinator" / "records.db"
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).close()

    result = _run(state, "learning", "report", "--repo", "owner/repo",
                  "--from", "2024-01-01", "--to", "2024-01-02")

    assert result.returncode == 2 and result.stdout == ""
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION


def test_learning_cli_invokes_no_forbidden_operational_actions(tmp_path, monkeypatch, capsys):
    from agentflow import cli, github
    from agentflow.canary_attribution import CanaryAttributionAuthority
    from agentflow.coordinator import providers
    from agentflow.evidence import EvidenceStore
    from agentflow.operational_safety import OperationalSafety
    from agentflow.review_policy import ReviewState

    state, path, store = _state(tmp_path)
    assert store.upsert(Record("terminal", "review", "codex", 1, repo="owner/repo",
                               subject="42", state=COMPLETED))
    store.close()
    record_attempt(path, _attempt("terminal", "terminal", finalized=1_704_067_200))
    monkeypatch.setenv("AGENTFLOW_STATE", str(state))

    def forbidden(*_args, **_kwargs):
        raise AssertionError("learning report invoked a forbidden operation")

    monkeypatch.setattr(github, "_gh", forbidden)
    monkeypatch.setattr(providers, "provider_command", forbidden)
    monkeypatch.setattr(OperationalSafety, "observe", forbidden)
    monkeypatch.setattr(CanaryAttributionAuthority, "_participate_in_admission", forbidden)
    monkeypatch.setattr(EvidenceStore, "evaluate", forbidden)
    monkeypatch.setattr(EvidenceStore, "promote", forbidden)
    monkeypatch.setattr(ReviewState, "record_fields", forbidden)

    assert cli.main(["learning", "report", "--repo", "owner/repo",
                     "--from", "2024-01-01", "--to", "2024-01-02"]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == "agentflow-learning-report-v1"


def test_learning_health_reports_unreadable_directory_as_unknown(tmp_path, monkeypatch):
    directory = telemetry_dir(tmp_path / "records.db")
    directory.mkdir()
    original = Path.iterdir

    def unreadable(path):
        if path == directory:
            raise OSError("simulated directory I/O failure")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", unreadable)

    report = read_attempts_with_health(tmp_path / "records.db")

    assert report.entries == [] and report.skipped is None
