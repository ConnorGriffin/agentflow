"""Bounded AgentFlow-owned Codex worker command (#555)."""

from __future__ import annotations

import argparse
import os
import signal
import stat
import subprocess
import sys
from typing import TextIO

from agentflow.routing import RoutingConfigError, routing
from agentflow.runner import CodexRunner

MAX_TIMEOUT_S = 900
_EFFORT_LABELS = frozenset({"low", "medium", "high", "extra"})
_WORKER_PREAMBLE = ("You are an AgentFlow-routed Codex worker. Complete only the task supplied "
                   "in the stdin block for this turn.")


def worker_argv(worker: str, effort: str) -> list[str]:
    """Build a routed worker invocation with model and effort as explicit CLI arguments."""
    if effort not in _EFFORT_LABELS:
        raise ValueError(f"unknown routed effort label: {effort!r}")
    model = routing.codex_worker_cli_identifier(worker)
    rung = routing.worker_reasoning(effort)
    argv = CodexRunner().structured_argv(_WORKER_PREAMBLE, model, os.getcwd())
    return argv[:-1] + ["-c", f"model_reasoning_effort={rung}", argv[-1]]


def _private_stdin(stream: TextIO) -> None:
    """Read a mode-0600 regular stdin file without accepting a path-bearing argument."""
    info = os.fstat(stream.fileno())
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("stdin must be a private regular file (mode 0600)")
    if info.st_size == 0:
        raise ValueError("worker task must not be empty")


def run(worker: str, effort: str, timeout: int, stdin: TextIO) -> int:
    """Run one worker and return its result, terminating its complete process group on timeout."""
    if not 0 < timeout <= MAX_TIMEOUT_S:
        raise ValueError(f"timeout must be between 1 and {MAX_TIMEOUT_S} seconds")
    _private_stdin(stdin)
    argv = worker_argv(worker, effort)
    process = subprocess.Popen(argv, cwd=os.getcwd(), stdin=stdin, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        output, _ = process.communicate(timeout=timeout)
        if output:
            sys.stdout.write(output)
        return process.returncode
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            output, _ = process.communicate()
        # ``communicate`` returns the complete buffered stream on its retry, including any
        # partial bytes attached to TimeoutExpired; printing the exception payload too duplicates it.
        if output:
            sys.stdout.write(output)
        print(f"agentflow: Codex worker timed out after {timeout}s", file=sys.stderr)
        return 124


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="agentflow-codex-worker")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--effort", required=True)
    parser.add_argument("--timeout", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        raise SystemExit(run(args.worker, args.effort, args.timeout, sys.stdin))
    except (OSError, RoutingConfigError, ValueError) as exc:
        print(f"agentflow: Codex worker failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
