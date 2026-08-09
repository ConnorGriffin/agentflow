#!/usr/bin/env python3
"""Assert the local Codex-parent → Claude-child CLI boundary without provider work."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path

from agentflow.routing import routing


_SHELLS = frozenset({"sh", "bash", "dash", "ksh", "zsh", "fish"})
_EVIDENCE_TYPE = "agentflow.codex-claude-child-probe.v1"


def _command_runs_claude_version(command: object, claude_cli: str) -> bool:
    """Whether a command-execution payload proves the configured Claude version command ran.

    Codex reports a shell-wrapped string in normal operation (for example ``zsh -lc 'claude
    --version'``), but test and adapter shapes can also carry argv. Only a direct argv or one
    known shell's single command body is evidence; textual mentions in arbitrary arguments are not.
    """
    if isinstance(command, (list, tuple)):
        tokens = [str(part) for part in command]
    elif isinstance(command, str):
        try:
            tokens = shlex.split(command)
        except ValueError:
            return False
    else:
        return False
    if tokens == [claude_cli, "--version"]:
        return True
    if (len(tokens) != 3 or Path(tokens[0]).name not in _SHELLS
            or tokens[1] not in {"-c", "-lc"}):
        return False
    try:
        body = shlex.split(tokens[2])
    except ValueError:
        return False
    return body == [claude_cli, "--version"]


def _completed_child_status(output: str, claude_cli: str) -> int | None:
    """Return the proven Claude child status, or ``None`` when JSONL proof is unusable."""
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        if not isinstance(event, dict):
            return None
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        if item.get("status") != "completed":
            continue
        exit_code = item.get("exit_code")
        if (not isinstance(exit_code, int) or isinstance(exit_code, bool)
                or not _command_runs_claude_version(item.get("command"), claude_cli)):
            continue
        return exit_code
    return None


def _probe_evidence(output: str, claude_cli: str, sol_model: str) -> str | None:
    """Return sanitized JSONL evidence for the constructed Sol launch and proven child event."""
    child = None
    for line in output.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        item = event.get("item") if isinstance(event, dict) else None
        if (event.get("type") == "item.completed" and isinstance(item, dict)
                and item.get("type") == "command_execution"
                and item.get("status") == "completed"
                and _command_runs_claude_version(item.get("command"), claude_cli)):
            child = event
            break
    if child is None:
        return None
    envelope = {
        "type": _EVIDENCE_TYPE,
        "parent": {"model": sol_model, "json": True, "sandbox": "workspace-write",
                   "ignore_user_config": True, "ephemeral": False},
    }
    return "\n".join((json.dumps(envelope, separators=(",", ":")),
                      json.dumps(child, separators=(",", ":")))) + "\n"


def captured_evidence_status(payload: str, claude_cli: str) -> int | None:
    """Validate a sanitized probe envelope plus its matching raw child event."""
    lines = payload.splitlines()
    if len(lines) != 2:
        return None
    try:
        envelope = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    sol_model = routing.cli_identifier("codex", "sol")
    if not isinstance(envelope, dict) or envelope.get("type") != _EVIDENCE_TYPE:
        return None
    parent = envelope.get("parent")
    if parent != {"model": sol_model, "json": True, "sandbox": "workspace-write",
                  "ignore_user_config": True, "ephemeral": False}:
        return None
    return _completed_child_status(lines[1], claude_cli)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-cli", default="codex")
    parser.add_argument("--claude-cli", default="claude")
    parser.add_argument("--workdir", default=str(Path.cwd()))
    parser.add_argument("--evidence-out", type=Path)
    args = parser.parse_args(argv)
    prompt = (
        f"Run exactly: {shlex.quote(args.claude_cli)} --version. Do not read, edit, create, "
        "delete, stage, or commit any repository file."
    )
    sol_model = routing.cli_identifier("codex", "sol")
    try:
        completed = subprocess.run([
            args.codex_cli, "exec", "-m", sol_model, "--json", "--sandbox", "workspace-write",
            "--cd", args.workdir, "--ignore-user-config", prompt], check=False, text=True,
            capture_output=True)
    except OSError as error:
        print(f"codex probe: could not launch configured Codex CLI: {error}", file=sys.stderr)
        return 127
    # Keep provider diagnostics observable. The JSONL is itself ordinary Codex command output.
    sys.stdout.write(completed.stdout)
    sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        return completed.returncode
    child_status = _completed_child_status(completed.stdout, args.claude_cli)
    if child_status is None:
        print("codex probe: no completed configured Claude --version invocation was proven",
              file=sys.stderr)
        return 2
    if args.evidence_out is not None:
        evidence = _probe_evidence(completed.stdout, args.claude_cli, sol_model)
        if evidence is None:
            print("codex probe: cannot create evidence without a usable completed child event",
                  file=sys.stderr)
            return 2
        args.evidence_out.write_text(evidence, encoding="utf-8")
    return child_status


if __name__ == "__main__":
    raise SystemExit(main())
