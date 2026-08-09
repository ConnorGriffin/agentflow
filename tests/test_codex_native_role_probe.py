"""Hermetic coverage for the executable native-helper role probe (#509).

The parsing/matching logic is exercised directly against constructed rollout fixtures (no real
Codex spawn); ``test_captured_probe_event_is_accepted_separately_from_synthetic_negatives``
additionally re-validates the real, sanitized evidence this probe captured against the installed
Codex CLI 0.144.0."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "codex-native-role-probe.py"
EVIDENCE = ROOT / "docs" / "research" / "evidence" / "codex-native-role-probe-2026-08-09.jsonl"


def _probe_module():
    spec = importlib.util.spec_from_file_location("codex_native_role_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _spawn_call_record(task_name: str, agent_type: str | None, fork_turns: str = "none") -> dict:
    args = {"task_name": task_name, "fork_turns": fork_turns,
            "message": "gAAAA-encrypted-not-real-content"}
    if agent_type is not None:
        args["agent_type"] = agent_type
    return {"type": "response_item",
            "payload": {"type": "function_call", "name": "spawn_agent",
                       "arguments": json.dumps(args)}}


def _write_rollout(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_parent_spawn_call_extracts_only_the_typed_fields_never_the_encrypted_message(tmp_path):
    module = _probe_module()
    path = tmp_path / "parent.jsonl"
    _write_rollout(path, [
        _spawn_call_record("af_native_probe", "af_codex_luna_medium"),
    ])

    call = module.parent_spawn_call(path, "af_codex_luna_medium")

    assert call == {"task_name": "af_native_probe", "fork_turns": "none",
                    "agent_type": "af_codex_luna_medium"}


def test_parent_spawn_call_is_none_when_agent_type_was_omitted(tmp_path):
    """The observed real-world negative: Sol sometimes calls spawn_agent without agent_type,
    which silently inherits the parent's own model rather than the routed role. The probe must
    treat this as no proof, never as a partial success."""
    module = _probe_module()
    path = tmp_path / "parent.jsonl"
    _write_rollout(path, [_spawn_call_record("af_native_probe", None)])

    assert module.parent_spawn_call(path, "af_codex_luna_medium") is None


def test_parent_spawn_call_is_none_for_a_different_role(tmp_path):
    module = _probe_module()
    path = tmp_path / "parent.jsonl"
    _write_rollout(path, [_spawn_call_record("af_native_probe", "af_codex_terra_medium")])

    assert module.parent_spawn_call(path, "af_codex_luna_medium") is None


def test_child_rollout_for_parent_matches_on_thread_spawn_and_ignores_non_subagent_sources(tmp_path, monkeypatch):
    module = _probe_module()
    codex_home = tmp_path / "codex-home"
    monkeypatch.setattr(module, "_CODEX_HOME", codex_home)
    sessions = codex_home / "sessions" / "2026" / "08" / "09"
    unrelated = sessions / "rollout-a.jsonl"
    _write_rollout(unrelated, [{"type": "session_meta", "payload": {"source": "exec"}}])
    child = sessions / "rollout-b.jsonl"
    _write_rollout(child, [{
        "type": "session_meta",
        "payload": {"source": {"subagent": {"thread_spawn": {
            "parent_thread_id": "parent-1", "agent_path": "/root/af_native_probe",
            "agent_role": "af_codex_luna_medium"}}}}}])

    found = module.child_rollout_for_parent("parent-1", since=0)

    assert found == child
    assert module.child_rollout_for_parent("parent-missing", since=0) is None


def test_child_facts_reads_role_model_reasoning_and_the_final_marker(tmp_path):
    module = _probe_module()
    path = tmp_path / "child.jsonl"
    _write_rollout(path, [
        {"type": "session_meta", "payload": {"source": {"subagent": {"thread_spawn": {
            "parent_thread_id": "p", "agent_path": "/root/af_native_probe",
            "agent_role": "af_codex_luna_medium"}}}}},
        {"type": "turn_context", "payload": {
            "model": "gpt-5.6-luna", "multi_agent_version": "v2", "effort": "medium",
            "collaboration_mode": {"settings": {"reasoning_effort": "medium"}}}},
        {"type": "event_msg", "payload": {"type": "agent_message", "phase": "final_answer",
                                          "message": "AF_NATIVE_PROBE_OK"}},
    ])

    facts = module.child_facts(path)

    assert facts == {
        "agent_path": "/root/af_native_probe", "agent_role": "af_codex_luna_medium",
        "model": "gpt-5.6-luna", "reasoning_effort": "medium",
        "multi_agent_version": "v2", "final_marker": "AF_NATIVE_PROBE_OK",
    }


def test_captured_probe_event_is_accepted_separately_from_synthetic_negatives():
    module = _probe_module()
    payload = EVIDENCE.read_text()

    assert module.captured_evidence_status(
        payload, "af_codex_luna_medium", "gpt-5.6-luna", "medium") == 0
    assert module.captured_evidence_status(
        payload.replace("af_codex_luna_medium", "af_codex_terra_medium"),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None
    assert module.captured_evidence_status(
        payload.replace("AF_NATIVE_PROBE_OK", "something else"),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None
    assert module.captured_evidence_status(
        payload.replace('"fork_turns":"none"', '"fork_turns":"all"'),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None


def test_captured_evidence_status_rejects_wrong_reasoning():
    """The observed real-world negative this closes: the routed reasoning rung the routing table
    resolved is never actually confirmed against the child unless this check exists."""
    module = _probe_module()
    payload = EVIDENCE.read_text()

    assert module.captured_evidence_status(
        payload, "af_codex_luna_medium", "gpt-5.6-luna", "high") is None
    assert module.captured_evidence_status(
        payload.replace('"reasoning_effort":"medium"', '"reasoning_effort":"high"'),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None


def test_captured_evidence_status_rejects_missing_or_mismatched_parent_fields():
    """Every fact the envelope claims about the parent must actually be validated — a captured
    envelope that merely names the right model, or drops a claimed field, must never pass."""
    module = _probe_module()
    payload = EVIDENCE.read_text()

    # A missing parent envelope key must fail, not be treated as vacuously satisfied.
    assert module.captured_evidence_status(
        payload.replace('"strict_config":true,', ""),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None
    assert module.captured_evidence_status(
        payload.replace('"ignore_user_config":true,', ""),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None
    assert module.captured_evidence_status(
        payload.replace('"config_role_declared":true', '"config_role_declared":false'),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None
    # Ephemeral flips true: the durable-thread contract native spawn_agent depends on is broken.
    assert module.captured_evidence_status(
        payload.replace('"ephemeral":false', '"ephemeral":true'),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None
    # A parent model that is not the real Sol id is never accepted, however routed the child is.
    assert module.captured_evidence_status(
        payload.replace('"model":"gpt-5.6-sol"', '"model":"gpt-5.6-terra"'),
        "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None


def test_captured_evidence_status_rejects_a_missing_task_name():
    module = _probe_module()
    envelope = json.dumps({
        "type": "agentflow.codex-native-role-probe.v1",
        "parent": {"model": "gpt-5.6-sol", "strict_config": True, "ignore_user_config": True,
                  "ephemeral": False, "config_role_declared": True},
        "spawn_call": {"fork_turns": "none", "agent_type": "af_codex_luna_medium"},
    })
    child = json.dumps({
        "type": "agentflow.codex-native-role-probe.v1.child",
        "agent_role": "af_codex_luna_medium", "model": "gpt-5.6-luna",
        "reasoning_effort": "medium", "final_marker": "AF_NATIVE_PROBE_OK",
    })
    module = _probe_module()
    assert module.captured_evidence_status(
        envelope + "\n" + child, "af_codex_luna_medium", "gpt-5.6-luna", "medium") is None


def test_config_role_declared_reads_the_real_built_argv():
    module = _probe_module()
    argv = ["--strict-config", "-c",
           'agents.af_codex_luna_medium.description="x"', "-c",
           'agents.af_codex_luna_medium.config_file="/tmp/x/af_codex_luna_medium.toml"']

    assert module._config_role_declared(argv, "af_codex_luna_medium") is True
    assert module._config_role_declared(argv, "af_codex_terra_medium") is False
    assert module._config_role_declared([], "af_codex_luna_medium") is False


def test_evidence_path_is_a_fixed_constant_under_the_owned_root():
    """No CLI input names or influences the write destination — it is a code-authored constant,
    always a direct child of :data:`_EVIDENCE_ROOT`."""
    module = _probe_module()

    assert module._EVIDENCE_PATH.parent == module._EVIDENCE_ROOT
    assert module._EVIDENCE_PATH.name == "codex-native-role-probe-latest.jsonl"


def test_probe_rejects_an_evidence_out_option_the_cli_no_longer_accepts(tmp_path):
    """The resolver production surface (an operator-supplied evidence path) is gone entirely —
    argparse itself rejects the removed flag rather than any runtime path check."""
    result = subprocess.run(
        [sys.executable, str(PROBE), "--codex-cli", "codex",
         "--evidence-out", "../escape.jsonl"],
        text=True, capture_output=True)

    assert result.returncode == 2
    assert "unrecognized arguments" in result.stderr


def test_probe_workdir_is_removed_via_its_own_owned_temporarydirectory_cleanup(tmp_path):
    """The probe's per-run ``cwd`` is created and destroyed by one owned
    ``tempfile.TemporaryDirectory`` — no raw path it built is ever handed to a separate deletion
    call, and the directory is gone once the probe exits."""
    marker = tmp_path / "cwd.txt"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'codex-cli 0.144.0'; exit 0; fi\n"
        f"pwd > {marker}\n"
        "exit 7\n"
    )
    fake_codex.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(PROBE), "--codex-cli", str(fake_codex)],
        text=True, capture_output=True)

    assert result.returncode == 7
    workdir = Path(marker.read_text().strip())
    assert workdir.name.startswith("agentflow-native-role-probe-")
    assert not workdir.exists()


def test_codex_home_ignores_the_operator_environment(monkeypatch):
    """An ambient `CODEX_HOME` in the operator's shell must never redirect where this probe
    looks for rollouts — the fixed `Path.home() / ".codex"` constant is the only source."""
    module = _probe_module()
    monkeypatch.setenv("CODEX_HOME", "/tmp/attacker-controlled-home")

    assert module._codex_home() == module._CODEX_HOME
    assert module._codex_home() != Path("/tmp/attacker-controlled-home")


def test_probe_child_env_carries_the_same_fixed_codex_home(tmp_path, monkeypatch):
    """The spawned `codex` child must see the identical `CODEX_HOME` this probe reads back
    from (`Path.home() / ".codex"`), even when the operator's ambient environment names a
    different one — otherwise the probe and the child could silently disagree about where
    rollouts land."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "operator-set-elsewhere"))
    marker = tmp_path / "seen-codex-home.txt"
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"--version\" ]; then echo 'codex-cli 0.144.0'; exit 0; fi\n"
        "printf '%s' \"$CODEX_HOME\" > " + str(marker) + "\n"
        "exit 7\n"
    )
    fake_codex.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(PROBE), "--codex-cli", str(fake_codex)],
        text=True, capture_output=True)

    assert result.returncode == 7
    assert marker.read_text() == str(Path.home() / ".codex")
    assert marker.read_text() != str(tmp_path / "operator-set-elsewhere")


def test_probe_exits_with_a_skip_code_when_capability_is_unavailable(tmp_path):
    fake_codex = tmp_path / "codex"
    fake_codex.write_text("#!/bin/sh\necho 'codex-cli 0.999.0'\nexit 0\n")
    fake_codex.chmod(0o755)

    result = subprocess.run(
        [sys.executable, str(PROBE), "--codex-cli", str(fake_codex)],
        text=True, capture_output=True)

    assert result.returncode == 3
    assert "does not pass the capability gate" in result.stderr
