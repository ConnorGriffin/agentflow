"""Immutable RouteCell selection and admitted launch policy (#646)."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading

import pytest

from agentflow.operational_safety import (
    CheckEvidence,
    DETERMINISTIC_CHECKS,
    LaunchConfigV1,
    ObservationRequest,
    SafetyRefused,
    decode_launch_config,
    encode_launch_config,
    launch_config_digest,
)
from agentflow.config import RuntimeConfig
from agentflow.coordinator.store import OperationalSafetyOnly, SafetySources, Store
from agentflow.coordinator.record import Record
from agentflow.coordinator.store import (
    ROUTE_ADMISSION_REFUSAL_CODES,
    RouteAdmissionRefused,
)
from agentflow.coordinator.providers import provider_command
from agentflow.coordinator.launcher import LocalLauncher
from agentflow.loop import RepoConfig
from agentflow.routing import (
    RouteSelection,
    reachable_route_selections,
    reconcile_route_cells,
    routing,
)


def test_routing_materializes_profile_specific_frozen_launch_policy(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "321")

    build = routing.select_route(
        "octo/app", "build", "claude", "fable",
        complexity="deep", effort="extra",
    )
    revise = routing.select_route(
        "octo/app", "revise", "codex", "sol",
        complexity="deep", builder_complexity="standard", effort=None,
    )

    assert build == RouteSelection(
        repository="octo/app",
        stage="build",
        provider="claude",
        model="fable",
        route_id="production/build/deep/extra",
        launch_config=LaunchConfigV1(
            schema="agentflow-launch-v1",
            provider="claude",
            internal_model="fable",
            cli_model="fable",
            stage_profile_id="build/deep/extra",
            reasoning_effort="low",
            turn_ceiling=300,
            wall_ceiling_s=321,
            build_lease=None,
            allowed_tools=None,
            sandbox_policy="workspace-write",
            result_schema_json=None,
            result_schema_digest=None,
        ),
    )
    assert revise.route_id == "production/revise/standard/default"
    assert revise.launch_config.stage_profile_id == "revise/standard/default"
    assert revise.launch_config.cli_model == "gpt-5.6-sol"
    assert routing.select_route(
        "octo/app", "review", "codex", "luna",
        complexity="standard").route_id == "production/review/standard"
    assert routing.select_route(
        "octo/app", "attack", "claude", "opus",
        complexity="deep").route_id == "production/attack/deep"
    with pytest.raises(FrozenInstanceError):
        build.route_id = "production/build/deep/low"


def test_launch_config_v1_has_fixed_canonical_bytes_and_digest():
    config = LaunchConfigV1(
        "agentflow-launch-v1", "codex", "sol", "gpt-5.6-sol",
        "build/deep/high", "low", 200, 10800, (1, 2, 3), None,
        "workspace-write", None, None,
    )
    expected = (
        b'{"allowed_tools":null,"build_lease":[1,2,3],'
        b'"cli_model":"gpt-5.6-sol","internal_model":"sol",'
        b'"provider":"codex","reasoning_effort":"low",'
        b'"result_schema_digest":null,"result_schema_json":null,'
        b'"sandbox_policy":"workspace-write","schema":"agentflow-launch-v1",'
        b'"stage_profile_id":"build/deep/high","turn_ceiling":200,'
        b'"wall_ceiling_s":10800}'
    )

    assert encode_launch_config(config) == expected
    assert launch_config_digest(config) == (
        "cf04245ad4043213cfab37298355414e711e49847f022ddd9a081e057f3c2c58")
    assert decode_launch_config(expected) == config


def test_structured_route_selections_seal_shared_stage_contracts():
    from agentflow.stage_result_contracts import stage_result_schema

    for stage in ("intake", "attack", "review"):
        selection = routing.select_route(
            "octo/app", stage, "codex", "sol", complexity="deep")
        expected = json.dumps(
            stage_result_schema(stage), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False)
        assert selection.launch_config.result_schema_json == expected
        assert selection.launch_config.result_schema_digest == sha256(
            expected.encode()).hexdigest()
    assert routing.select_route(
        "octo/app", "build", "codex", "sol", complexity="deep",
        effort="high").launch_config.result_schema_json is None


@pytest.mark.parametrize(("change", "value"), [
    ("extra", "secret"),
    ("turn_ceiling", True),
    ("wall_ceiling_s", 0),
    ("sandbox_policy", "read-only"),
    ("result_schema_digest", "0" * 64),
    ("stage_profile_id", "build/deep/unknown"),
    ("internal_model", " sol"),
    ("allowed_tools", ["Read", "Read"]),
])
def test_launch_config_v1_refuses_nonclosed_or_inconsistent_input(change, value):
    selection = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    raw = json.loads(encode_launch_config(selection.launch_config))
    raw[change] = value
    content = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(RuntimeError):
        decode_launch_config(content)


def test_launch_config_v1_refuses_missing_duplicate_and_noncanonical_members():
    content = encode_launch_config(routing.select_route(
        "octo/app", "intake", "claude", "opus", complexity="deep").launch_config)
    raw = json.loads(content)
    del raw["cli_model"]
    missing = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
    duplicate = content.replace(b'{"allowed_tools":', b'{"schema":"other","allowed_tools":')
    noncanonical = json.dumps(json.loads(content)).encode()

    for invalid in (missing, duplicate, noncanonical):
        with pytest.raises(RuntimeError):
            decode_launch_config(invalid)


def test_reconciliation_covers_governed_and_converse_only_workspace_routes(tmp_path):
    governed = RepoConfig("octo/app", str(tmp_path))
    overlap = RepoConfig("octo/overlap", str(tmp_path))
    workspace = RepoConfig("octo/workspace-only", str(tmp_path))
    config = RuntimeConfig(
        (governed, overlap), (overlap, workspace), Path(tmp_path / "config.toml"))
    path = tmp_path / "coordinator.db"
    store = Store(
        path,
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )

    expected = reachable_route_selections(config)
    first = reconcile_route_cells(config, store)
    second = reconcile_route_cells(config, store)

    assert first == second
    assert {cell.repository for cell in first} == {
        "octo/app", "octo/overlap", "octo/workspace-only"}
    workspace_cells = [cell for cell in first if cell.repository == "octo/workspace-only"]
    assert {cell.stage for cell in workspace_cells} == {"converse"}
    assert {cell.provider for cell in workspace_cells} == {"claude", "codex"}
    overlap_cells = [cell for cell in first if cell.repository == "octo/overlap"]
    assert {cell.stage for cell in overlap_cells} > {"converse"}
    assert len({cell.digest for cell in first}) == len(first)
    assert {cell.route_id for cell in first} == {
        selection.route_id for selection in expected
    }
    assert "production/build/deep/extra" in {cell.route_id for cell in first}
    assert "production/build/deep/default" in {cell.route_id for cell in first}
    assert "production/build/standard/low" in {cell.route_id for cell in first}
    assert "production/revise/deep/default" in {cell.route_id for cell in first}
    assert all(store.route_cell_state(cell.digest).route_cell_digest == cell.digest
               for cell in first)
    digests = {cell.digest for cell in first}
    store.close()

    reopened = Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))
    assert {reopened.decode_committed_launch(digest).route_cell.digest
            for digest in digests} == digests
    reopened.close()


def test_route_identity_v2_keeps_model_versioned_and_provider_logical(tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    sol = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    luna = routing.select_route(
        "octo/app", "review", "codex", "luna", complexity="deep")
    claude = routing.select_route(
        "octo/app", "review", "claude", "opus", complexity="deep")

    sol_cell = store.register_route_selection(sol)
    luna_cell = store.register_route_selection(luna)
    claude_cell = store.register_route_selection(claude)

    assert sol_cell.key == luna_cell.key
    assert sol_cell.digest != luna_cell.digest
    assert claude_cell.key != sol_cell.key
    assert store.route_cell_state(sol_cell.digest).route_cell_digest == sol_cell.digest
    with pytest.raises(SafetyRefused, match="not active"):
        store.route_cell_state(luna_cell.digest)
    store.close()


def test_review_and_attack_tiers_are_independent_active_routes(tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    for stage in ("review", "attack"):
        standard = routing.select_route(
            "octo/app", stage, "codex", "terra", complexity="standard")
        deep = routing.select_route(
            "octo/app", stage, "codex", "sol", complexity="deep")
        standard_cell = store.register_route_selection(standard)
        deep_cell = store.register_route_selection(deep)
        assert standard.launch_config.stage_profile_id == f"{stage}/standard"
        assert deep.launch_config.stage_profile_id == f"{stage}/deep"
        assert standard.route_id != deep.route_id
        assert standard_cell.key != deep_cell.key
        assert store.route_cell_state(standard_cell.digest).route_cell_digest == standard_cell.digest
        assert store.route_cell_state(deep_cell.digest).route_cell_digest == deep_cell.digest
    store.close()


def test_store_selection_identity_is_pure_and_matches_registration(tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    selection = routing.select_route(
        "octo/app", "attack", "codex", "sol", complexity="deep")
    before = tuple(store._conn.execute(
        "SELECT (SELECT COUNT(*) FROM safety_launch_configs),"
        " (SELECT COUNT(*) FROM safety_route_cells),"
        " (SELECT COUNT(*) FROM safety_route_state),"
        " (SELECT COUNT(*) FROM safety_canary_state)").fetchone())
    sql_actions = []

    def deny_sql(action, *_args):
        sql_actions.append(action)
        return sqlite3.SQLITE_DENY

    store._conn.set_authorizer(deny_sql)

    identity = store.route_selection_identity(selection)
    store._conn.set_authorizer(None)

    after = tuple(store._conn.execute(
        "SELECT (SELECT COUNT(*) FROM safety_launch_configs),"
        " (SELECT COUNT(*) FROM safety_route_cells),"
        " (SELECT COUNT(*) FROM safety_route_state),"
        " (SELECT COUNT(*) FROM safety_canary_state)").fetchone())
    registered = store.register_route_selection(selection)
    assert before == after == (0, 0, 0, 0)
    assert sql_actions == []
    assert (identity.route_id, identity.route_cell_digest,
            identity.launch_config_digest) == (
                registered.route_id, registered.digest,
                registered.launch_config_digest)
    store.close()


def test_store_resolves_one_decoded_envelope_and_closes_refusal_codes(tmp_path):
    assert ROUTE_ADMISSION_REFUSAL_CODES == {
        "missing", "stale", "mismatched", "unreadable", "quarantined"}
    with pytest.raises(TypeError):
        RouteAdmissionRefused("other")
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-646", "review", "codex", 1,
        repo="octo/app", model="sol", complexity="deep")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    store.register_route_selection(selection)

    admitted = store.resolve_admitted_launch(
        stored.identity, stored.revision, selection.route_id)
    assert admitted.route_cell.repository == "octo/app"
    assert admitted.launch_config == selection.launch_config

    def reserve(envelope):
        assert store._conn.in_transaction
        return envelope.route_cell.digest

    assert store.consume_admitted_launch(
        stored.identity, stored.revision, selection.route_id,
        reserve=reserve) == admitted.route_cell.digest

    with pytest.raises(RouteAdmissionRefused) as stale:
        store.resolve_admitted_launch(stored.identity, stored.revision - 1, selection.route_id)
    assert stale.value.code == "stale"

    with pytest.raises(RouteAdmissionRefused) as mismatched:
        store.resolve_admitted_launch(stored.identity, stored.revision, "production/absent")
    assert mismatched.value.code == "mismatched"

    other = routing.select_route(
        "other/repo", "review", "codex", "sol", complexity="deep")
    store.register_route_selection(other)
    sibling_record, *_ = store.submit(Record(
        "stage-646-sibling", "review", "codex", 1,
        repo="third/repo", model="sol", complexity="deep"))
    with pytest.raises(RouteAdmissionRefused) as sibling:
        store.resolve_admitted_launch(
            sibling_record.identity, sibling_record.revision, other.route_id)
    assert sibling.value.code == "missing"
    store.close()


def test_store_resolution_uses_only_durable_route_selector_facts(monkeypatch, tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-persisted-646", "review", "codex", 1,
        repo="octo/app", model="sol", complexity="deep")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    store.register_route_selection(selection)
    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "not-an-integer")
    monkeypatch.setattr(routing, "_models", {})
    monkeypatch.setattr(routing, "select_route", lambda *_args, **_kwargs: pytest.fail(
        "Store reread current routing"))

    admitted = store.resolve_admitted_launch(
        stored.identity, stored.revision, selection.route_id)

    assert admitted.launch_config == selection.launch_config
    store.close()


def test_store_registration_refuses_values_not_rematerialized_by_routing(tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    selection = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    altered = (
        replace(selection, model="opus", launch_config=replace(
            selection.launch_config, internal_model="opus", cli_model="opus")),
        replace(selection, launch_config=replace(selection.launch_config, wall_ceiling_s=1)),
        replace(selection, launch_config=replace(
            selection.launch_config, allowed_tools=("Read",), sandbox_policy="read-only")),
        replace(selection, launch_config=replace(selection.launch_config, reasoning_effort="high")),
    )

    for candidate in altered:
        with pytest.raises(SafetyRefused):
            store.register_route_selection(candidate)

    registered = store.register_route_selection(selection)

    assert registered.route_id == selection.route_id
    store.close()


@pytest.mark.parametrize(("stage", "profile_id"), [
    ("build", "build/deep"),
    ("revise", "revise/deep/default/extra"),
])
def test_store_registration_refuses_malformed_profile_tokens_before_indexing(
        stage, profile_id, tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    selection = routing.select_route(
        "octo/app", stage, "codex", "sol", complexity="deep",
        effort="high" if stage == "build" else None,
        builder_complexity="deep" if stage == "revise" else None)
    object.__setattr__(selection.launch_config, "stage_profile_id", profile_id)

    with pytest.raises(SafetyRefused):
        store.register_route_selection(selection)
    store.close()


def test_provider_argv_consumes_only_the_decoded_admitted_envelope(
        monkeypatch, tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-provider-646", "intake", "claude", 1,
        repo="octo/app", model="opus", complexity="deep",
        source=str(tmp_path), input_ptr="scope the issue")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "intake", "claude", "opus", complexity="deep")
    store.register_route_selection(selection)
    admitted = store.resolve_admitted_launch(
        stored.identity, stored.revision, selection.route_id)

    before = provider_command(stored, admitted)
    monkeypatch.setattr(routing, "cli_identifier", lambda *_: (_ for _ in ()).throw(
        AssertionError("routing was reread")))
    monkeypatch.setattr(
        "agentflow.coordinator.profiles.profile_for",
        lambda *_: (_ for _ in ()).throw(AssertionError("profile was reread")))
    monkeypatch.setattr(
        "agentflow.coordinator.profiles.WITHHELD_EDIT_TOOLS", ("Read",))
    monkeypatch.setattr(
        "agentflow.coordinator.providers.stage_result_schema",
        lambda *_: (_ for _ in ()).throw(AssertionError("schema was reread")))
    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "1")

    assert provider_command(stored, admitted) == before
    assert before[before.index("--model") + 1] == selection.launch_config.cli_model
    assert before[before.index("--max-turns") + 1] == str(
        selection.launch_config.turn_ceiling)
    store.close()


def test_codex_argv_consumes_only_the_decoded_admitted_envelope(monkeypatch, tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-codex-provider-646", "intake", "codex", 1,
        repo="octo/app", model="sol", complexity="deep",
        source=str(tmp_path), input_ptr="scope the issue")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "intake", "codex", "sol", complexity="deep")
    store.register_route_selection(selection)
    admitted = store.resolve_admitted_launch(
        stored.identity, stored.revision, selection.route_id)
    monkeypatch.setattr(
        "agentflow.runner._write_output_schema", lambda _schema: "/schema.json")

    before = provider_command(stored, admitted)
    monkeypatch.setattr(routing, "cli_identifier", lambda *_: (_ for _ in ()).throw(
        AssertionError("routing was reread")))
    monkeypatch.setattr(
        "agentflow.coordinator.profiles.profile_for",
        lambda *_: (_ for _ in ()).throw(AssertionError("profile was reread")))
    monkeypatch.setattr(
        "agentflow.coordinator.providers.stage_result_schema",
        lambda *_: (_ for _ in ()).throw(AssertionError("schema was reread")))

    assert provider_command(stored, admitted) == before
    assert before[before.index("-m") + 1] == selection.launch_config.cli_model
    assert before[before.index("--sandbox") + 1] == selection.launch_config.sandbox_policy
    store.close()


def test_launcher_supervision_consumes_the_same_decoded_admitted_envelope(
        monkeypatch, tmp_path):
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-launcher-646", "build", "codex", 5,
        repo="octo/app", model="sol", complexity="deep", effort="high",
        source=str(tmp_path), input_ptr="build it")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "build", "codex", "sol", complexity="deep", effort="high")
    store.register_route_selection(selection)
    admitted = store.resolve_admitted_launch(
        stored.identity, stored.revision, selection.route_id)
    launcher = LocalLauncher()

    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "1")
    monkeypatch.setattr(
        "agentflow.coordinator.profiles.profile_for",
        lambda *_: (_ for _ in ()).throw(AssertionError("profile was reread")))

    assert launcher._session_timeout_for(stored, admitted) == (
        selection.launch_config.wall_ceiling_s)
    assert launcher._build_lease_for(stored, admitted) == selection.launch_config.build_lease
    store.close()


def test_local_launcher_start_threads_one_envelope_to_argv_and_supervision(
        monkeypatch, tmp_path):
    selection = routing.select_route(
        "octo/app", "build", "codex", "sol", complexity="deep", effort="high")
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    waiting = Record(
        "stage-start-646", "build", "codex", 5,
        repo="octo/app", model="sol", complexity="deep", effort="high",
        source=str(tmp_path), input_ptr="build it", launch_token="token-646")
    store.register_route_selection(selection)
    from agentflow.operational_safety import decode_admitted_launch
    admitted = decode_admitted_launch(store._operational_safety.resolve(
        waiting.repo, waiting.stage, waiting.pool, waiting.model, selection.route_id))
    consumed = []
    spawned = []

    class Child:
        def wait(self, timeout):
            return 0

    class LaunchStore:
        path = tmp_path / "coordinator.db"

        @staticmethod
        def record_of(_identity):
            return replace(waiting, start_fact="started", family="42")

    def command(record, envelope):
        consumed.append((record, envelope))
        return ["provider"]

    monkeypatch.setattr(
        "agentflow.coordinator.launcher.subprocess.Popen",
        lambda argv, cwd=None: spawned.append((argv, cwd)) or Child())
    launcher = LocalLauncher(provider_command=command, timeout=0.1)

    result = launcher.start(waiting, LaunchStore(), admitted)

    assert result.fact == "started" and result.family == "42"
    assert consumed == [(waiting, admitted)]
    child_argv = spawned[0][0]
    assert str(selection.launch_config.wall_ceiling_s) in child_argv
    lease_at = child_argv.index("--build-lease")
    assert child_argv[lease_at + 2:lease_at + 5] == [
        str(value) for value in selection.launch_config.build_lease]
    assert child_argv[-1] == "provider"
    store.close()


def test_changed_config_keeps_route_identity_inactive_and_survives_reopen(monkeypatch, tmp_path):
    path = tmp_path / "coordinator.db"
    store = Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))
    record = Record(
        "stage-change-646", "review", "codex", 1,
        repo="octo/app", model="sol", complexity="deep")
    stored, *_ = store.submit(record)
    first = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    first_cell = store.register_route_selection(first)
    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "321")
    changed = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    changed_cell = store.register_route_selection(changed)

    assert changed.route_id == first.route_id
    assert changed_cell.digest != first_cell.digest
    admitted = store.resolve_admitted_launch(
        stored.identity, stored.revision, first.route_id)
    assert admitted.route_cell.digest == first_cell.digest
    assert encode_launch_config(admitted.launch_config) == encode_launch_config(
        first.launch_config)
    store.close()

    reopened = Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))
    admitted_after_reopen = reopened.resolve_admitted_launch(
        stored.identity, stored.revision, first.route_id)
    assert admitted_after_reopen == admitted
    reopened.close()


def test_exact_digest_decoder_reads_inactive_historical_version_after_reopen(
        monkeypatch, tmp_path):
    path = tmp_path / "coordinator.db"
    store = Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))
    first = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    first_cell = store.register_route_selection(first)
    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "321")
    changed = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    changed_cell = store.register_route_selection(changed)
    with store._operational_safety._transaction():
        store._operational_safety._activate_pointer(
            first_cell.key, first_cell.digest, changed_cell.digest)
        store._conn.execute(
            "UPDATE safety_canary_state SET active_digest = ?, generation = generation + 1"
            " WHERE cell_key = ?", (changed_cell.digest, first_cell.key))
    expected = store.decode_committed_launch(first_cell.digest)
    store.close()

    reopened = Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))
    assert reopened.decode_committed_launch(first_cell.digest) == expected
    with pytest.raises(RouteAdmissionRefused) as missing:
        reopened.decode_committed_launch("0" * 64)
    assert missing.value.code == "missing"

    mismatched_config = replace(
        first.launch_config, provider="claude", internal_model="opus", cli_model="opus")
    config_bytes = encode_launch_config(mismatched_config)
    config_digest = sha256(config_bytes).hexdigest()
    body = {
        "repository": first.repository,
        "stage": first.stage,
        "provider": first.provider,
        "model": first.model,
        "route_id": first.route_id,
        "launch_config_digest": config_digest,
    }
    body_text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    cell_digest = sha256(body_text.encode()).hexdigest()
    reopened._conn.execute(
        "INSERT INTO safety_launch_configs VALUES (?, ?)",
        (config_digest, config_bytes))
    reopened._conn.execute(
        "INSERT INTO safety_route_cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cell_digest, first_cell.key, first.repository, first.stage, first.provider,
         first.model, first.route_id, config_digest, body_text))
    with pytest.raises(RouteAdmissionRefused) as mismatched:
        reopened.decode_committed_launch(cell_digest)
    assert mismatched.value.code == "mismatched"

    reopened._conn.execute(
        "UPDATE safety_launch_configs SET content = ? WHERE digest = ?",
        (b"{}", changed_cell.launch_config_digest))
    with pytest.raises(RouteAdmissionRefused) as unreadable:
        reopened.decode_committed_launch(changed_cell.digest)
    assert unreadable.value.code == "unreadable"
    reopened.close()


def test_store_closes_quarantined_and_unreadable_route_refusals(tmp_path):
    class Checks:
        def __init__(self):
            self.results = {}

        def read(self, evidence_ref):
            return self.results[evidence_ref]

    checks = Checks()
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources(check_evidence=checks)),
    )
    record = Record(
        "stage-refusal-646", "review", "codex", 1,
        repo="octo/app", model="sol", complexity="deep")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    cell = store.register_route_selection(selection)
    declaration = next(item for item in DETERMINISTIC_CHECKS
                       if item.identifier == "route-health")
    for ordinal in ("first", "second"):
        ref = f"route/{ordinal}"
        request = ObservationRequest(
            "octo/app", "issue-646", "abc123", "route-health", "1", cell.digest, ref)
        checks.results[ref] = CheckEvidence(
            f"observation-{ordinal}", request.repository, request.subject,
            request.subject_revision, request.check_id, request.check_version,
            request.route_cell_digest, declaration.digest, "fail", ref,
            f"authority-verified:{ref}")
        store._operational_safety.observe(request)

    with pytest.raises(RouteAdmissionRefused) as quarantined:
        store.resolve_admitted_launch(stored.identity, stored.revision, selection.route_id)
    assert quarantined.value.code == "quarantined"
    store.close()

    corrupt = Store(
        tmp_path / "corrupt.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    corrupt_record, *_ = corrupt.submit(record)
    corrupt_cell = corrupt.register_route_selection(selection)
    corrupt._conn.execute(
        "UPDATE safety_launch_configs SET content = ? WHERE digest = ?",
        (b"{}", corrupt_cell.launch_config_digest))
    with pytest.raises(RouteAdmissionRefused) as unreadable:
        corrupt.resolve_admitted_launch(
            corrupt_record.identity, corrupt_record.revision, selection.route_id)
    assert unreadable.value.code == "unreadable"
    corrupt.close()


def test_two_store_activation_between_resolve_and_reserve_is_stale_without_capacity(
        monkeypatch, tmp_path):
    path = tmp_path / "coordinator.db"
    store = Store(
        path,
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-race-646", "review", "codex", 1,
        repo="octo/app", model="sol", complexity="deep")
    stored, *_ = store.submit(record)
    first = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    first_cell = store.register_route_selection(first)
    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "321")
    changed = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    changed_cell = store.register_route_selection(changed)
    other_store = Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))
    reservations = []

    def activate_changed_cell():
        owner = other_store._operational_safety
        with owner._transaction():
            owner._activate_pointer(first_cell.key, first_cell.digest, changed_cell.digest)
            other_store._conn.execute(
                "UPDATE safety_canary_state SET active_digest = ?, generation = generation + 1"
                " WHERE cell_key = ?",
                (changed_cell.digest, first_cell.key))

    def reserve(admitted):
        assert store._conn.in_transaction
        reservations.append(admitted)

    with pytest.raises(RouteAdmissionRefused) as stale:
        store.consume_admitted_launch(
            stored.identity, stored.revision, first.route_id,
            reserve=reserve, before_reserve=activate_changed_cell)
    assert stale.value.code == "stale"
    assert reservations == []
    other_store.close()
    store.close()


def test_two_store_corruption_between_resolve_and_reserve_is_unreadable_without_capacity(
        tmp_path):
    path = tmp_path / "coordinator.db"
    store = Store(
        path,
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-corrupt-race-646", "review", "codex", 1,
        repo="octo/app", model="sol", complexity="deep")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    cell = store.register_route_selection(selection)
    other_store = Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))
    reservations = []

    def corrupt_active_config():
        owner = other_store._operational_safety
        with owner._transaction():
            other_store._conn.execute(
                "UPDATE safety_launch_configs SET content = ? WHERE digest = ?",
                (b"{}", cell.launch_config_digest))

    def reserve(admitted):
        assert store._conn.in_transaction
        reservations.append(admitted)

    with pytest.raises(RouteAdmissionRefused) as unreadable:
        store.consume_admitted_launch(
            stored.identity, stored.revision, selection.route_id,
            reserve=reserve, before_reserve=corrupt_active_config)
    assert unreadable.value.code == "unreadable"
    assert reservations == []
    other_store.close()
    store.close()


def test_reconciliation_resumes_partial_concurrent_work_and_isolates_one_bad_route(tmp_path):
    config = RuntimeConfig(
        (RepoConfig("octo/app", str(tmp_path)),), (), Path(tmp_path / "config.toml"))
    selections = reachable_route_selections(config)
    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    for selection in selections[:5]:
        store.register_route_selection(selection)

    results = []
    threads = [threading.Thread(
        target=lambda: results.append(reconcile_route_cells(config, store)))
        for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [results[0], results[0]]
    assert len(results[0]) == len(selections)

    refused_route = selections[3]

    class OneBadRoute:
        def register_route_selection(self, selection):
            if selection == refused_route:
                raise SafetyRefused("private corrupt route detail")
            return store.register_route_selection(selection)

    readable = reconcile_route_cells(config, OneBadRoute())
    assert len(readable) == len(selections) - 1
    assert all(cell.route_id != refused_route.route_id or
               (cell.repository, cell.stage, cell.provider, cell.model) != (
                   refused_route.repository, refused_route.stage,
                   refused_route.provider, refused_route.model)
               for cell in readable)
    store.close()


def test_selection_and_launch_reject_cross_identity_facts(monkeypatch, tmp_path):
    for facts in (
        ("octo/app", "unknown", "codex", "sol"),
        ("octo/app", "review", "claude", "sol"),
        ("not-a-repository", "review", "codex", "sol"),
    ):
        with pytest.raises(ValueError):
            routing.select_route(*facts, complexity="deep")

    monkeypatch.setenv("AGENTFLOW_SESSION_TIMEOUT", "zero")
    with pytest.raises(ValueError):
        routing.select_route(
            "octo/app", "review", "codex", "sol", complexity="deep")
    monkeypatch.delenv("AGENTFLOW_SESSION_TIMEOUT")

    store = Store(
        tmp_path / "coordinator.db",
        admission_mode=OperationalSafetyOnly(SafetySources()),
    )
    record = Record(
        "stage-mismatch-646", "review", "codex", 1,
        repo="octo/app", model="sol", complexity="deep",
        source=str(tmp_path), input_ptr="review it")
    stored, *_ = store.submit(record)
    selection = routing.select_route(
        "octo/app", "review", "codex", "sol", complexity="deep")
    store.register_route_selection(selection)
    admitted = store.resolve_admitted_launch(
        stored.identity, stored.revision, selection.route_id)
    crossed = replace(admitted, route_cell=replace(
        admitted.route_cell, repository="other/repo"))
    with pytest.raises(SafetyRefused):
        provider_command(stored, crossed)
    with pytest.raises(SafetyRefused):
        LocalLauncher()._session_timeout_for(stored, crossed)
    store.close()


def test_populated_mapping_v1_ledger_requires_operator_reconciliation_without_mutation(
        tmp_path):
    from agentflow.canary_attribution import (
        ATTRIBUTION_CONTRACT_VERSION,
        ROW_DIGEST_DOMAIN,
        _schema_row_valid,
    )

    path = tmp_path / "coordinator.db"
    legacy = Store(path)
    config_bytes = b'{"effort":"high","model":"gpt-5","timeout":900}'
    config_digest = sha256(config_bytes).hexdigest()
    body = {
        "repository": "octo/app",
        "stage": "build",
        "provider": "codex",
        "model": "gpt-5",
        "route_id": "primary",
        "launch_config_digest": config_digest,
    }
    body_text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    cell_digest = sha256(body_text.encode()).hexdigest()
    key_source = {
        "repository": "octo/app", "stage": "build", "provider": "codex",
        "model": "gpt-5", "route_id": "primary",
    }
    cell_key = sha256(json.dumps(
        key_source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    state_source = {
        "cell_key": cell_key, "active": cell_digest,
        "quarantined": cell_digest, "generation": 0,
    }
    state_id = sha256(json.dumps(
        state_source, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    legacy._conn.execute(
        "INSERT INTO safety_launch_configs VALUES (?, ?)",
        (config_digest, config_bytes))
    legacy._conn.execute(
        "INSERT INTO safety_route_cells VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (cell_digest, cell_key, "octo/app", "build", "codex", "gpt-5",
         "primary", config_digest, body_text))
    legacy._conn.execute(
        "INSERT INTO safety_route_state VALUES (?, ?, ?, ?, ?, 0)",
        (cell_key, cell_digest, cell_digest, "action-legacy", state_id))
    legacy._conn.execute(
        "INSERT INTO safety_canary_state VALUES (?, ?, NULL, NULL, NULL, 0, 0)",
        (cell_key, cell_digest))
    legacy._conn.execute(
        "INSERT INTO safety_actions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("action-legacy", "legacy-quarantine", "quarantine", cell_digest,
         "d" * 64, "legacy/evidence", "operator-reconciles-v1", "{}"))
    attribution_facts = {
        "stage_identity": "legacy-stage",
        "repository": "octo/app",
        "route_cell_digest": cell_digest,
        "receipt_binding": "b" * 64,
        "method_revision": "a" * 40,
        "cohort_id": cell_key,
        "contract_version": ATTRIBUTION_CONTRACT_VERSION,
    }
    attribution_digest = sha256(json.dumps(
        {"domain": ROW_DIGEST_DOMAIN, **attribution_facts},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    attribution_row = (*attribution_facts.values(), attribution_digest)
    legacy._conn.execute(
        "INSERT INTO canary_attributions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        attribution_row)
    before = tuple(legacy._conn.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()), tuple(
            legacy._conn.execute("SELECT * FROM safety_route_cells").fetchall()), tuple(
            legacy._conn.execute("SELECT * FROM safety_route_state").fetchall()), tuple(
            legacy._conn.execute("SELECT * FROM safety_canary_state").fetchall()), tuple(
            legacy._conn.execute("SELECT * FROM safety_actions").fetchall()), tuple(
            legacy._conn.execute("SELECT * FROM canary_attributions").fetchall())
    legacy.close()

    with pytest.raises(SafetyRefused, match="requires operator reconciliation"):
        Store(path, admission_mode=OperationalSafetyOnly(SafetySources()))

    reopened = Store(path)
    after = tuple(reopened._conn.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()), tuple(
            reopened._conn.execute("SELECT * FROM safety_route_cells").fetchall()), tuple(
            reopened._conn.execute("SELECT * FROM safety_route_state").fetchall()), tuple(
            reopened._conn.execute("SELECT * FROM safety_canary_state").fetchall()), tuple(
            reopened._conn.execute("SELECT * FROM safety_actions").fetchall()), tuple(
            reopened._conn.execute("SELECT * FROM canary_attributions").fetchall())
    assert after == before
    assert _schema_row_valid(*after[-1][0]) == 1
    reopened.close()
