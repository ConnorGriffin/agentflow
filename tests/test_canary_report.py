from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentflow.canary_attribution import (
    ATTRIBUTION_CONTRACT_VERSION, ROW_DIGEST_DOMAIN, CanaryAttribution,
    CanaryAttributionRefused, _digest, validate_canary_attribution,
)
from agentflow.canary_report import (
    CANARY_REPORT_REFUSAL_CODES, REPORT_MANIFEST, REPORT_MANIFEST_DIGEST, REPORT_VERSION,
    SCHEMA_FINGERPRINT, AttemptTelemetryReader, CanaryAttemptFact, CanaryAttemptProjection,
    CanaryReporter, CanaryReportRefused,
)
from agentflow.coordinator.errors import StoreUnavailable
from agentflow.coordinator.telemetry import AttemptTelemetry, AttemptUsage


IDENTITY = "octo/app|635|build|-"
_DEFAULT = object()


class Store:
    def __init__(self, path, attribution=_DEFAULT):
        self.path = path
        self.attribution = attribution_for(IDENTITY) if attribution is _DEFAULT else attribution

    def read_canary_attribution(self, identity):
        if isinstance(self.attribution, Exception):
            raise self.attribution
        return self.attribution


class Telemetry:
    def __init__(self, attempts=()):
        self.attempts = attempts
        self.reads = 0

    def read(self, identity):
        self.reads += 1
        return CanaryAttemptProjection(identity, self.attempts)


def _snapshot_631_fixture(report, *, now):
    if report is None:
        return {"status": "missing", "state": None}
    measures = report.measures
    return {
        "status": "reported",
        "state": report.result,
        "age_source": measures.evidence_finalized_at,
        "snapshot_age": None if measures.evidence_age_missing else now - measures.evidence_finalized_at,
        "age_missing": measures.evidence_age_missing,
        "missingness": (measures.duration_missing, measures.token_missing, measures.cost_missing),
    }


def attribution_for(identity):
    fields = {
        "stage_identity": identity, "repository": "octo/app", "route_cell_digest": "a" * 64,
        "receipt_binding": "b" * 64, "method_revision": "c" * 40, "cohort_id": "d" * 64,
        "contract_version": ATTRIBUTION_CONTRACT_VERSION,
    }
    return validate_canary_attribution(CanaryAttribution(
        **fields, attribution_digest=_digest({"domain": ROW_DIGEST_DOMAIN, **fields})))


def durable_attempt(identity=IDENTITY, *, token="one", verified=False, cause="permanent",
                    classification="permanent", started=2, finalized=5, tokens=3, cost=1.5):
    return asdict(AttemptTelemetry(
        token=token, identity=identity, repo="octo/app", subject="635", stage="build",
        pool="codex", model="gpt-5", complexity="deep", effort="high", reasoning_effort=None,
        attempt=1, continuation=True, restart_resumes=1, round=0, conflict_round=0,
        verified=verified, outcome="" if not verified else "verified", cause=cause,
        classification=classification, started_at=started, finalized_at=finalized,
        usage=AttemptUsage(input_tokens=tokens, cost_usd=cost)))


def fact(token, *, verified=False, cause="unknown", classification="incomplete",
         started=10, finalized=20, tokens=None, cost=None):
    return CanaryAttemptFact(IDENTITY, token, verified, cause, classification,
                             started, finalized, tokens, cost)


def reporter(tmp_path, telemetry=(), attribution=_DEFAULT):
    store = Store(tmp_path / "records.db", attribution)
    return CanaryReporter(store, telemetry=Telemetry(telemetry), now=lambda: 99)


def test_schema_boundary_and_closed_refusal_vocabulary(tmp_path):
    value = reporter(tmp_path).report(IDENTITY, REPORT_VERSION)
    assert value.result == "block_recommendation"
    assert REPORT_MANIFEST_DIGEST == "d80ad3d7e1819f09856d2421e25c4199d55016e2f2afb6b8be7ebdd63a81557b"
    assert hashlib.sha256(REPORT_MANIFEST).hexdigest() == REPORT_MANIFEST_DIGEST
    assert SCHEMA_FINGERPRINT == "72b9dfc4ac98d3ce17fa9a3d0db7c3af764686b2df21c08e65c93a927edfb91c"
    conn = sqlite3.connect(tmp_path / "canary-reports.db")
    assert conn.execute("PRAGMA user_version").fetchone() == (1,)
    assert [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")] == [
        "canary_reports", "canary_reports_no_update", "canary_reports_no_delete"]
    conn.execute("CREATE TABLE extra (x)")
    conn.close()
    with pytest.raises(CanaryReportRefused, match="report_store_unavailable"):
        reporter(tmp_path).report("another", REPORT_VERSION)
    assert CANARY_REPORT_REFUSAL_CODES == frozenset({
        "unsupported_report_version", "attribution_absent", "attribution_unavailable",
        "attribution_invalid", "telemetry_invalid", "report_store_unavailable"})


@pytest.mark.parametrize(("attempts", "result"), [
    ((fact("a", verified=True), fact("b", cause="permanent", classification="permanent")), "observation"),
    ((fact("a", cause="permanent", classification="permanent"),), "rollback_recommendation"),
    ((fact("a"),), "block_recommendation"),
])
def test_result_precedence_and_measures(tmp_path, attempts, result):
    value = reporter(tmp_path, attempts).report(IDENTITY, REPORT_VERSION)
    assert value.result == result
    assert value.measures.attempt_count == len(attempts)
    assert value.measures.duration_seconds == 10 * len(attempts)


def test_zero_attempts_are_missing_not_measured_zero(tmp_path):
    measures = reporter(tmp_path).report(IDENTITY, REPORT_VERSION).measures
    assert (measures.attempt_count, measures.duration_seconds, measures.token_count, measures.cost_usd) == (0, None, None, None)
    assert measures.duration_missing and measures.token_missing and measures.cost_missing
    assert measures.evidence_age_missing


def test_attempt_telemetry_reader_selects_identity_and_skips_malformed(tmp_path):
    directory = tmp_path / "telemetry"
    directory.mkdir()
    good = durable_attempt()
    (directory / "b.json").write_text(json.dumps(good))
    (directory / "a.json").write_text("not json")
    (directory / "c.json").write_text(json.dumps({**good, "identity": "octo/app|636|build|-", "token": "other"}))
    (directory / "d.json").write_text(json.dumps({
        "identity": IDENTITY, "token": "forged", "verified": True, "cause": "none",
        "classification": "complete", "started_at": 1, "finalized_at": 2,
        "usage": {"output_tokens": 4}}))
    projection = AttemptTelemetryReader(tmp_path / "records.db").read(IDENTITY)
    assert projection.attempts == (
        fact("one", cause="permanent", classification="permanent", started=2, finalized=5,
             tokens=3, cost=1.5),)


@pytest.mark.parametrize(("attribution", "code"), [
    (None, "attribution_absent"), (StoreUnavailable("private"), "attribution_unavailable"),
    (CanaryAttributionRefused("wrong_scope"), "attribution_invalid"),
    (attribution_for("wrong"), "attribution_invalid"),
    (replace(attribution_for(IDENTITY), receipt_binding="f" * 64), "attribution_invalid"),
])
def test_attribution_refusal_mappings_persist_nothing(tmp_path, attribution, code):
    with pytest.raises(CanaryReportRefused) as refused:
        reporter(tmp_path, attribution=attribution).report(IDENTITY, REPORT_VERSION)
    assert refused.value.code == code
    assert not (tmp_path / "canary-reports.db").exists()


def test_malformed_projection_and_duplicate_tokens_refuse(tmp_path):
    duplicate = (fact("same"), fact("same", finalized=22))
    with pytest.raises(CanaryReportRefused, match="telemetry_invalid"):
        reporter(tmp_path, duplicate).report(IDENTITY, REPORT_VERSION)
    malformed = CanaryAttemptFact(IDENTITY, "bad", False, "unknown", "incomplete", 3, 2, None, None)
    with pytest.raises(CanaryReportRefused, match="telemetry_invalid"):
        reporter(tmp_path, (malformed,)).report(IDENTITY, REPORT_VERSION)


def test_reopen_and_concurrent_winner_never_rereads_existing_sources(tmp_path):
    source = Telemetry((fact("one", verified=True, tokens=4, cost=2.0),))
    store = Store(tmp_path / "records.db")
    first = CanaryReporter(store, telemetry=source, now=lambda: 7).report(IDENTITY, REPORT_VERSION)
    changed = Telemetry((fact("two", cause="permanent", classification="permanent"),))
    assert CanaryReporter(store, telemetry=changed, now=lambda: 8).report(IDENTITY, REPORT_VERSION) == first
    assert changed.reads == 0
    other = "octo/app|635|review|-"
    other_store = Store(tmp_path / "records.db", attribution_for(other))
    def call():
        return CanaryReporter(other_store, telemetry=Telemetry((CanaryAttemptFact(other, "x", False, "unknown", "incomplete", 1, 2, None, None),)), now=lambda: 9).report(other, REPORT_VERSION)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: call(), range(2)))
    assert values[0] == values[1]


def test_precommit_crash_retries_from_a_fresh_telemetry_snapshot(tmp_path, monkeypatch):
    store = Store(tmp_path / "records.db")
    telemetry = Telemetry((fact("first", cause="permanent", classification="permanent"),))
    value = CanaryReporter(store, telemetry=telemetry, now=lambda: 5)
    def crash(name):
        if name == "before-commit":
            raise RuntimeError("precommit crash")
    monkeypatch.setattr(CanaryReporter, "_checkpoint", staticmethod(crash))
    with pytest.raises(RuntimeError, match="precommit crash"):
        value.report(IDENTITY, REPORT_VERSION)
    monkeypatch.setattr(CanaryReporter, "_checkpoint", staticmethod(lambda _name: None))
    telemetry.attempts = (fact("retry", verified=True),)
    assert value.report(IDENTITY, REPORT_VERSION).result == "observation"


def test_postcommit_crash_reopens_the_committed_row_without_reading_sources(tmp_path, monkeypatch):
    store = Store(tmp_path / "records.db")
    source = Telemetry((fact("committed", verified=True),))
    def crash(name):
        if name == "after-commit":
            raise RuntimeError("lost acknowledgement")
    monkeypatch.setattr(CanaryReporter, "_checkpoint", staticmethod(crash))
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        CanaryReporter(store, telemetry=source).report(IDENTITY, REPORT_VERSION)
    monkeypatch.setattr(CanaryReporter, "_checkpoint", staticmethod(lambda _name: None))
    changed = Telemetry((fact("changed", cause="permanent", classification="permanent"),))
    reopened = CanaryReporter(store, telemetry=changed).report(IDENTITY, REPORT_VERSION)
    assert reopened.result == "observation" and changed.reads == 0


def test_corrupt_and_newer_report_stores_refuse_before_sources(tmp_path):
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    corrupt_path = corrupt / "canary-reports.db"
    corrupt_path.write_bytes(b"not sqlite")
    corrupt_source = Telemetry((fact("ignored", verified=True),))
    with pytest.raises(CanaryReportRefused, match="report_store_unavailable"):
        CanaryReporter(Store(corrupt / "records.db"), telemetry=corrupt_source).report(
            IDENTITY, REPORT_VERSION)
    assert corrupt_source.reads == 0

    newer = tmp_path / "newer"
    newer.mkdir()
    initial = reporter(newer)
    initial.report(IDENTITY, REPORT_VERSION)
    conn = sqlite3.connect(newer / "canary-reports.db")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()
    newer_source = Telemetry((fact("ignored-too", verified=True),))
    with pytest.raises(CanaryReportRefused, match="report_store_unavailable"):
        CanaryReporter(Store(newer / "records.db"), telemetry=newer_source).report(
            IDENTITY, REPORT_VERSION)
    assert newer_source.reads == 0


@pytest.mark.parametrize(("attempts", "state", "reason"), [
    ((fact("verified", verified=True),), "observation", None),
    ((fact("terminal", cause="permanent", classification="permanent"),),
     "rollback_recommendation", "terminal_permanent_no_verified_attempt"),
    ((fact("incomplete"),), "block_recommendation", "telemetry_incomplete_or_insufficient"),
])
def test_downstream_629_631_mapping_fixture(tmp_path, attempts, state, reason):
    report = reporter(tmp_path, attempts).report(IDENTITY, REPORT_VERSION)
    reconciliation = {
        "observation": ("observation", "verified_attempt"),
        "rollback_recommendation": (
            "rollback_recommendation", "terminal_permanent_no_verified_attempt"),
        "block_recommendation": (
            "block_recommendation", "telemetry_incomplete_or_insufficient"),
    }
    reconciliation_state, advisory_reason = reconciliation[report.result]
    snapshot = {
        **_snapshot_631_fixture(report, now=100),
        "receipt_pointer": (report.receipt_binding, report.method_revision, report.cohort_id),
        "report_key": (report.stage_identity, report.report_version),
        "hold_reason": None if report.result == "observation" else advisory_reason,
    }
    assert reconciliation_state == state
    assert snapshot["state"] == state and snapshot["hold_reason"] == reason
    assert snapshot["receipt_pointer"] == ("b" * 64, "c" * 40, "d" * 64)
    assert snapshot["report_key"] == (IDENTITY, REPORT_VERSION)


def test_631_mapping_fixture_preserves_age_missingness_and_report_absence(tmp_path):
    measured = reporter(tmp_path, (
        fact("early", verified=True, started=10, finalized=40, tokens=3, cost=1.5),
        fact("late", verified=True, started=50, finalized=90, tokens=7, cost=2.5),
    )).report(IDENTITY, REPORT_VERSION)
    snapshot = _snapshot_631_fixture(measured, now=100)
    assert snapshot == {
        "status": "reported", "state": "observation", "age_source": 90, "snapshot_age": 10,
        "age_missing": False, "missingness": (False, False, False),
    }

    zero_dir = tmp_path / "zero"
    zero_dir.mkdir()
    zero_snapshot = _snapshot_631_fixture(
        reporter(zero_dir).report(IDENTITY, REPORT_VERSION), now=100)
    assert zero_snapshot["state"] == "block_recommendation"
    assert (zero_snapshot["age_source"], zero_snapshot["snapshot_age"],
            zero_snapshot["age_missing"], zero_snapshot["missingness"]) == (
                None, None, True, (True, True, True))

    refused_dir = tmp_path / "refused"
    refused_dir.mkdir()
    with pytest.raises(CanaryReportRefused, match="attribution_absent"):
        reporter(refused_dir, attribution=None).report(IDENTITY, REPORT_VERSION)
    assert _snapshot_631_fixture(None, now=100) == {"status": "missing", "state": None}


def test_report_does_not_mutate_telemetry_or_store(tmp_path):
    directory = tmp_path / "telemetry"
    directory.mkdir()
    path = directory / "attempt.json"
    path.write_text(json.dumps(durable_attempt(token="a", verified=True, cause="none",
                                               classification="complete", started=1, finalized=2,
                                               tokens=4)))
    before = path.read_bytes()
    store_path = tmp_path / "records.db"
    CanaryReporter(Store(store_path)).report(IDENTITY, REPORT_VERSION)
    assert path.read_bytes() == before and not store_path.exists()
