#!/usr/bin/env python3
"""Assert the real 0.144.0 native-helper role adapter end-to-end, from persisted metadata (#509).

Launches one real Sol parent with the shipped :mod:`agentflow.codex_native_helpers` role
overrides, tells it to spawn exactly one child through the hidden ``agent_type`` selector, then
proves the routed role, child model, reasoning effort, and parent/child attribution from the
provider's own persisted rollout files — never from the parent's paraphrase. Skips (exit 3) when
the installed CLI does not pass the compatibility gate: that is the adapter correctly failing
closed, not a probe failure.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from agentflow import codex_native_helpers
from agentflow.routing import routing

_EVIDENCE_TYPE = "agentflow.codex-native-role-probe.v1"
_TASK_NAME = "af_native_probe"
_MARKER = "AF_NATIVE_PROBE_OK"
# The one fixed destination a live run's evidence is ever written to — a code-authored constant,
# never a value built from operator or CLI input. There is no CLI option that names or influences
# this path; a run either writes here (``--write-evidence``) or writes nothing.
_EVIDENCE_ROOT = Path(__file__).resolve().parents[1] / "docs" / "research" / "evidence"
_EVIDENCE_PATH = _EVIDENCE_ROOT / "codex-native-role-probe-latest.jsonl"
# Rollout files are found by mtime, never by scanning transcript content — a launch started
# within this margin before the probe's own clock read is never mistaken for a stale, unrelated
# session from an identically-named prior run.
_ROLLOUT_LOOKUP_MARGIN_S = 5
_VERSION_PROBE_TIMEOUT_S = 10


def _installed_version(codex_cli: str) -> str | None:
    """Raw ``codex --version`` stdout, or ``None`` on any launch failure. This operator smoke
    script reads its own version here (it is outside the package's ``subprocess.run`` adapter
    allowlist by design and never imports the runner's private probe); the exact-build *decision*
    stays in the one pure gate, :func:`agentflow.codex_native_helpers.is_supported_version`."""
    try:
        result = subprocess.run([codex_cli, "--version"], text=True, capture_output=True,
                                timeout=_VERSION_PROBE_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


# The one fixed Codex config/session home this probe ever reads. Not environment-controlled:
# an operator's ambient `CODEX_HOME` never redirects where this probe looks for rollouts, so the
# location this reads and the location the child `codex` process is launched with (forced to this
# same path below) can never disagree.
_CODEX_HOME = Path.home() / ".codex"


def _codex_home() -> Path:
    return _CODEX_HOME


def _recent_rollouts(since: float):
    root = _codex_home() / "sessions"
    if not root.is_dir():
        return []
    paths = []
    for path in root.glob("**/*.jsonl"):
        try:
            if path.stat().st_mtime >= since - _ROLLOUT_LOOKUP_MARGIN_S:
                paths.append(path)
        except OSError:
            continue
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)


def _rollout_records(path: Path):
    try:
        text = path.read_text()
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def parent_rollout_for_thread(thread_id: str, since: float) -> Path | None:
    for path in _recent_rollouts(since):
        if thread_id in path.name:
            return path
    return None


def parent_spawn_call(path: Path, role_name: str) -> dict | None:
    """The parent's own persisted ``spawn_agent`` call for ``role_name`` — task_name/fork_turns/
    agent_type only; the encrypted ``message`` field is never read or retained."""
    for obj in _rollout_records(path):
        payload = obj.get("payload", obj) if isinstance(obj, dict) else None
        if not (isinstance(payload, dict) and payload.get("type") == "function_call"
                and payload.get("name") == "spawn_agent"):
            continue
        try:
            call_args = json.loads(payload.get("arguments", "{}"))
        except json.JSONDecodeError:
            continue
        if call_args.get("agent_type") == role_name:
            return {"task_name": call_args.get("task_name"),
                    "fork_turns": call_args.get("fork_turns"),
                    "agent_type": call_args.get("agent_type")}
    return None


def child_rollout_for_parent(parent_thread_id: str, since: float) -> Path | None:
    for path in _recent_rollouts(since):
        for obj in _rollout_records(path):
            if not isinstance(obj, dict):
                continue
            if obj.get("type") != "session_meta":
                continue
            payload = obj.get("payload", obj)
            source = payload.get("source")
            subagent = source.get("subagent") if isinstance(source, dict) else None
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            if isinstance(spawn, dict) and spawn.get("parent_thread_id") == parent_thread_id:
                return path
            break  # session_meta is always the first record; nothing else in this file can match
    return None


def child_facts(path: Path) -> dict:
    """The durable child-side facts a role probe proves: which role and model the runtime
    actually loaded, and the child's own persisted final marker — never the parent's relay."""
    facts: dict = {}
    for obj in _rollout_records(path):
        if not isinstance(obj, dict):
            continue
        payload = obj.get("payload", obj)
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "session_meta":
            source = payload.get("source")
            subagent = source.get("subagent") if isinstance(source, dict) else None
            spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
            if isinstance(spawn, dict):
                facts["agent_path"] = spawn.get("agent_path")
                facts["agent_role"] = spawn.get("agent_role")
        elif obj.get("type") == "turn_context":
            facts["model"] = payload.get("model")
            settings = ((payload.get("collaboration_mode") or {}).get("settings")) or {}
            facts["reasoning_effort"] = settings.get("reasoning_effort") or payload.get("effort")
            facts["multi_agent_version"] = payload.get("multi_agent_version")
        elif (obj.get("type") == "event_msg" and payload.get("type") == "agent_message"
              and payload.get("phase") == "final_answer"):
            facts["final_marker"] = payload.get("message")
    return facts


_EXPECTED_SOL_ID = "gpt-5.6-sol"


def _config_role_declared(role_argv: list[str], role_name: str) -> bool:
    """Whether ``role_argv`` (as built by :func:`build_role_overrides`) actually carries a
    ``-c agents.<role_name>.config_file=`` override — the fact that ``spawn_agent``'s hidden
    ``agent_type`` selector has anywhere to route to. Read from the real argv rather than
    asserted, so a future adapter change that silently drops the override is caught here too."""
    return any(
        entry.startswith(f"agents.{role_name}.config_file=") for entry in role_argv)


def _evidence(role_name: str, cli_id: str, sol_id: str, strict: bool, ephemeral: bool,
             config_role_declared: bool, spawn_args: dict, child: dict) -> str:
    envelope = {
        "type": _EVIDENCE_TYPE,
        "parent": {"strict_config": strict, "ignore_user_config": True,
                  "sandbox": "read-only", "model": sol_id, "ephemeral": ephemeral,
                  "config_role_declared": config_role_declared},
        "spawn_call": spawn_args,
    }
    child_line = {
        "type": f"{_EVIDENCE_TYPE}.child",
        "agent_role": child.get("agent_role"), "agent_path": child.get("agent_path"),
        "model": child.get("model"), "reasoning_effort": child.get("reasoning_effort"),
        "multi_agent_version": child.get("multi_agent_version"),
        "final_marker": child.get("final_marker"),
    }
    return "\n".join((json.dumps(envelope, separators=(",", ":")),
                      json.dumps(child_line, separators=(",", ":")))) + "\n"


def captured_evidence_status(payload: str, expected_role: str, expected_cli_id: str,
                             expected_reasoning: str) -> int | None:
    """Validate a sanitized probe envelope plus its matching child fact line. Returns 0 only for
    a capture that proves *every* fact the envelope claims — the parent ran the real Sol id
    non-ephemerally with ``--strict-config``/``--ignore-user-config`` and actually declared the
    routed role's config file, and the child loaded the routed model at the routed reasoning rung
    and returned the marker — ``None`` for anything unusable, missing, or mismatched. Mirrors the
    existing Codex-Claude-child probe's captured-evidence contract."""
    lines = payload.splitlines()
    if len(lines) != 2:
        return None
    try:
        envelope, child = json.loads(lines[0]), json.loads(lines[1])
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict) or envelope.get("type") != _EVIDENCE_TYPE:
        return None
    if not isinstance(child, dict) or child.get("type") != f"{_EVIDENCE_TYPE}.child":
        return None
    parent = envelope.get("parent")
    if (not isinstance(parent, dict) or parent.get("model") != _EXPECTED_SOL_ID
            or parent.get("strict_config") is not True
            or parent.get("ignore_user_config") is not True
            or parent.get("ephemeral") is not False
            or parent.get("config_role_declared") is not True):
        return None
    spawn_call = envelope.get("spawn_call")
    if (not isinstance(spawn_call, dict) or spawn_call.get("agent_type") != expected_role
            or spawn_call.get("fork_turns") != "none" or not spawn_call.get("task_name")):
        return None
    if (child.get("agent_role") != expected_role or child.get("model") != expected_cli_id
            or child.get("reasoning_effort") != expected_reasoning
            or child.get("final_marker") != _MARKER):
        return None
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-cli", default=os.environ.get("AGENTFLOW_CODEX_BIN", "codex"))
    parser.add_argument("--effort", default="medium")
    parser.add_argument("--write-evidence", action="store_true",
                        help=f"persist evidence to the fixed {_EVIDENCE_PATH} destination "
                             "(overwriting any previous capture there) — no path is configurable")
    args = parser.parse_args(argv)

    if not codex_native_helpers.is_supported_version(_installed_version(args.codex_cli)):
        print("codex native-role probe: installed CLI does not pass the capability gate — "
              "skipping (this is the adapter's own fail-closed behavior, not a probe failure)",
              file=sys.stderr)
        return 3

    routes = routing.codex_worker_roles(args.effort)
    if not routes:
        print("codex native-role probe: routing table names no Codex worker to probe",
              file=sys.stderr)
        return 2
    role_name, cli_id, reasoning = routes[0]
    role_argv = codex_native_helpers.build_role_overrides((routes[0],)).argv
    sol_id = routing.cli_identifier("codex", "sol")
    prompt = (
        "The spawn_agent tool schema you see does not display an agent_type parameter, but the "
        "runtime still accepts one as a known compatibility path for routing to a pre-configured "
        f'custom role. Call spawn_agent with task_name="{_TASK_NAME}", agent_type="{role_name}", '
        f'fork_turns="none", and message="Return exactly {_MARKER} and use no tools." Include '
        "agent_type even though it is not shown in the visible schema description. Then wait for "
        "the result and reply with only the child's exact text. Do not read, edit, create, "
        "delete, stage, or commit any repository file."
    )

    started = time.time()
    # An owned TemporaryDirectory: this call is both the sole creator and the sole cleanup site
    # for `workdir`, so no path derived from operator/config input is ever handed to a deletion
    # call — the object's own recorded path is what its context-manager exit removes.
    with tempfile.TemporaryDirectory(prefix="agentflow-native-role-probe-") as workdir:
        # The spawned child gets this probe's own fixed `_codex_home()`, overriding whatever
        # `CODEX_HOME` the operator's shell happens to have set — the rollout location this
        # probe reads back and the home the child actually writes to must be the same path,
        # never two independently-controlled ones.
        child_env = dict(os.environ)
        child_env["CODEX_HOME"] = str(_codex_home())
        try:
            completed = subprocess.run(
                [args.codex_cli, "exec", "--ignore-user-config", *role_argv,
                 "--skip-git-repo-check", "--sandbox", "read-only", "--json",
                 "-m", sol_id, prompt],
                cwd=workdir, check=False, text=True, capture_output=True,
                stdin=subprocess.DEVNULL, env=child_env)
        except OSError as error:
            print(f"codex native-role probe: could not launch configured Codex CLI: {error}",
                  file=sys.stderr)
            return 127
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        return completed.returncode

    parent_thread_id = None
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "thread.started":
            parent_thread_id = event.get("thread_id")
            break
    if not parent_thread_id:
        print("codex native-role probe: no thread.started event in the parent stream",
              file=sys.stderr)
        return 2

    parent_path = parent_rollout_for_thread(parent_thread_id, started)
    if parent_path is None:
        print("codex native-role probe: could not locate the parent's persisted rollout",
              file=sys.stderr)
        return 2
    spawn_args = parent_spawn_call(parent_path, role_name)
    if spawn_args is None:
        print("codex native-role probe: parent rollout has no spawn_agent call for this role",
              file=sys.stderr)
        return 2

    child_path = child_rollout_for_parent(parent_thread_id, started)
    if child_path is None:
        print("codex native-role probe: could not locate a child rollout spawned from this parent",
              file=sys.stderr)
        return 2
    child = child_facts(child_path)
    if (child.get("agent_role") != role_name or child.get("model") != cli_id
            or child.get("reasoning_effort") != reasoning
            or child.get("final_marker") != _MARKER):
        print(f"codex native-role probe: child metadata did not match the routed role: {child}",
              file=sys.stderr)
        return 2

    if args.write_evidence:
        _EVIDENCE_PATH.write_text(
            _evidence(role_name, cli_id, sol_id, "--strict-config" in role_argv,
                     ephemeral=False, config_role_declared=_config_role_declared(role_argv, role_name),
                     spawn_args=spawn_args, child=child),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
