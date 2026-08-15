from __future__ import annotations

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from agentflow.canary_attribution import (
    ATTRIBUTION_CONTRACT_VERSION, CanaryAttribution, CanaryAttributionRefused,
)
from agentflow.canary_report import (
    CANARY_REPORT_REFUSAL_CODES, REPORT_MANIFEST, REPORT_MANIFEST_DIGEST, REPORT_VERSION,
    SCHEMA_FINGERPRINT, AttemptTelemetryReader, CanaryAttemptFact, CanaryAttemptProjection,
    CanaryReporter, CanaryReportRefused,
)
from agentflow.coordinator.errors import StoreUnavailable


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


def attribution_for(identity):
    return CanaryAttribution(identity, "octo/app", "a" * 64, "b" * 64, "c" * 40,
                             "d" * 64, ATTRIBUTION_CONTRACT_VERSION, "e" * 64)


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
    good = {"identity": IDENTITY, "token": "one", "verified": False, "cause": "permanent",
            "classification": "permanent", "started_at": 2, "finalized_at": 5,
            "continuation": True, "restart_resumes": 1, "usage": {"input_tokens": 3, "cost_usd": 1.5}}
    (directory / "b.json").write_text(json.dumps(good))
    (directory / "a.json").write_text("not json")
    (directory / "c.json").write_text(json.dumps({**good, "identity": "octo/app|636|build|-", "token": "other"}))
    projection = AttemptTelemetryReader(tmp_path / "records.db").read(IDENTITY)
    assert projection.attempts == (fact("one", cause="permanent", classification="permanent", started=2, finalized=5, tokens=3, cost=1.5),)


@pytest.mark.parametrize(("attribution", "code"), [
    (None, "attribution_absent"), (StoreUnavailable("private"), "attribution_unavailable"),
    (CanaryAttributionRefused("wrong_scope"), "attribution_invalid"),
    (attribution_for("wrong"), "attribution_invalid"),
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


def test_precommit_failure_retries_from_a_fresh_telemetry_snapshot(tmp_path):
    store = Store(tmp_path / "records.db")
    telemetry = Telemetry((fact("first", cause="permanent", classification="permanent"),))
    value = CanaryReporter(store, telemetry=telemetry, now=lambda: 5)
    open_report_store = value._open
    calls = 0
    def crash_before_commit():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise sqlite3.OperationalError("simulated precommit crash")
        return open_report_store()
    value._open = crash_before_commit
    with pytest.raises(CanaryReportRefused, match="report_store_unavailable"):
        value.report(IDENTITY, REPORT_VERSION)
    telemetry.attempts = (fact("retry", verified=True),)
    assert value.report(IDENTITY, REPORT_VERSION).result == "observation"


def test_report_does_not_mutate_telemetry_or_store(tmp_path):
    directory = tmp_path / "telemetry"
    directory.mkdir()
    path = directory / "attempt.json"
    path.write_text(json.dumps({"identity": IDENTITY, "token": "a", "verified": True, "cause": "none",
                                "classification": "complete", "started_at": 1, "finalized_at": 2,
                                "usage": {"output_tokens": 4}}))
    before = path.read_bytes()
    store_path = tmp_path / "records.db"
    CanaryReporter(Store(store_path)).report(IDENTITY, REPORT_VERSION)
    assert path.read_bytes() == before and not store_path.exists()
