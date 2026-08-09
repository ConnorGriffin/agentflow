"""Hermetic coverage for the executable Codex-parent → Claude-child compatibility probe."""

from __future__ import annotations

import subprocess
import sys
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "codex-claude-child-probe.py"
EVIDENCE = ROOT / "docs" / "research" / "evidence" / "codex-claude-child-probe-2026-08-09.jsonl"


def _probe_module():
    spec = importlib.util.spec_from_file_location("codex_claude_child_probe", PROBE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fake_claude(path: Path, log: Path, code: int) -> None:
    path.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > {log}\n"
        f"exit {code}\n")
    path.chmod(0o755)


def _fake_codex(path: Path, log: Path, claude: Path, *, outer_code: int = 0,
                emit_command: str | None = None) -> None:
    command = emit_command or f"/bin/zsh -lc '{claude} --version'"
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, subprocess, sys\n"
        f"open({str(log)!r}, 'w').write('\\n'.join(sys.argv[1:]))\n"
        f"outer_code = {outer_code}\n"
        "if outer_code:\n"
        "    raise SystemExit(outer_code)\n"
        f"child = subprocess.run([{str(claude)!r}, '--version'], check=False)\n"
        "print(json.dumps({'type': 'item.completed', 'item': {\n"
        f"    'type': 'command_execution', 'command': {command!r},\n"
        "    'exit_code': child.returncode, 'status': 'completed'}}))\n")
    path.chmod(0o755)


def _run_probe(tmp_path: Path, *, claude_code: int = 0, outer_code: int = 0,
               emit_command: str | None = None, evidence_out: Path | None = None):
    codex, claude = tmp_path / "codex", tmp_path / "claude"
    codex_log, claude_log = tmp_path / "codex-argv", tmp_path / "claude-argv"
    _fake_claude(claude, claude_log, claude_code)
    _fake_codex(codex, codex_log, claude, outer_code=outer_code, emit_command=emit_command)
    argv = (
        [sys.executable, str(PROBE), "--codex-cli", str(codex), "--claude-cli", str(claude),
         "--workdir", str(tmp_path)])
    if evidence_out is not None:
        argv += ["--evidence-out", str(evidence_out)]
    result = subprocess.run(argv, text=True, capture_output=True)
    return result, codex_log, claude_log, claude


def test_probe_proves_a_workspace_write_codex_parent_invoked_the_configured_claude(tmp_path):
    result, codex_log, claude_log, claude = _run_probe(tmp_path)

    assert result.returncode == 0
    argv = codex_log.read_text()
    assert ("exec" in argv and "-m" in argv and "gpt-5.6-sol" in argv and "--json" in argv
            and "--sandbox" in argv and "workspace-write" in argv and "--ephemeral" not in argv)
    assert f"--cd\n{tmp_path}" in argv
    assert f"{claude} --version" in argv
    assert "Do not read, edit, create" in argv
    assert claude_log.read_text() == "--version\n"  # the fake Codex really launched the child
    assert '"type": "item.completed"' in result.stdout


def test_probe_preserves_a_nonzero_completed_claude_child(tmp_path):
    result, _codex_log, claude_log, _claude = _run_probe(tmp_path, claude_code=23)

    assert result.returncode == 23
    assert claude_log.read_text() == "--version\n"


def test_probe_preserves_a_codex_provider_launch_error(tmp_path):
    result, _codex_log, claude_log, _claude = _run_probe(tmp_path, outer_code=17)

    assert result.returncode == 17
    assert not claude_log.exists()


def test_probe_rejects_a_zero_codex_exit_without_matching_claude_proof(tmp_path):
    result, _codex_log, claude_log, _claude = _run_probe(
        tmp_path, emit_command="/bin/zsh -lc 'echo --version'")

    assert result.returncode == 2
    assert "no completed configured Claude --version invocation" in result.stderr
    assert claude_log.read_text() == "--version\n"


def test_probe_rejects_a_shell_that_only_echoes_the_configured_claude_command(tmp_path):
    expected = tmp_path / "claude"
    result, _codex_log, claude_log, _claude = _run_probe(
        tmp_path, emit_command=f'/bin/zsh -lc "echo \'{expected} --version\'"')

    assert result.returncode == 2
    assert claude_log.read_text() == "--version\n"


def test_probe_rejects_a_compound_claude_shell_command(tmp_path):
    expected = tmp_path / "claude"
    result, _codex_log, claude_log, _claude = _run_probe(
        tmp_path, emit_command=f'/bin/zsh -lc "{expected} --version && echo done"')

    assert result.returncode == 2
    assert claude_log.read_text() == "--version\n"


def test_captured_probe_event_is_accepted_separately_from_synthetic_negatives():
    module = _probe_module()
    assert module.captured_evidence_status(EVIDENCE.read_text(), "claude") == 0
    assert module.captured_evidence_status(
        EVIDENCE.read_text().replace("gpt-5.6-sol", "gpt-5.6-terra"), "claude") is None
    assert module.captured_evidence_status(
        EVIDENCE.read_text().replace('"ephemeral":false', '"ephemeral":true'), "claude") is None


def test_probe_writes_a_sanitized_sol_parent_envelope(tmp_path):
    evidence = tmp_path / "evidence.jsonl"
    result, _codex_log, _claude_log, claude = _run_probe(tmp_path, evidence_out=evidence)

    assert result.returncode == 0
    assert _probe_module().captured_evidence_status(evidence.read_text(), str(claude)) == 0
