#!/usr/bin/env python3
"""Scan the tracked tree or every reachable Git blob for secrets and private paths."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path

MAX_BLOB_BYTES = 20 * 1024 * 1024

PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "GitHub token": re.compile(
        rb"(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "OpenAI API key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "Anthropic API key": re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Google API key": re.compile(rb"\bAIza[A-Za-z0-9_-]{30,}\b"),
    "Google OAuth secret": re.compile(rb"\bGOCSPX-[A-Za-z0-9_-]{20,}\b"),
    "GitLab token": re.compile(rb"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "Hugging Face token": re.compile(rb"\bhf_[A-Za-z0-9]{30,}\b"),
    "npm token": re.compile(rb"\bnpm_[A-Za-z0-9]{30,}\b"),
    "PyPI token": re.compile(rb"\bpypi-[A-Za-z0-9_-]{40,}\b"),
    "Stripe live key": re.compile(rb"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    "Discord webhook": re.compile(
        rb"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/[0-9]+/[A-Za-z0-9_-]+"
    ),
    "credential-bearing URL": re.compile(rb"https?://[^\s/:]+:[^\s/@]+@"),
    "Connor private ntfy host": re.compile(
        rb"\bntfy\.home\." + rb"connorcg\.com\b",
        re.I,
    ),
    "Connor local path": re.compile(
        rb"(?:/Users/" + rb"connor/|~/Code/" + rb"ConnorGriffin/|"
        rb"Users-connor-Code-" + rb"ConnorGriffin-)",
        re.I,
    ),
}


def run(*args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        args,
        check=True,
        input=input_bytes,
        capture_output=True,
    ).stdout


def tracked_files() -> Iterable[tuple[str, bytes]]:
    for raw_path in run("git", "ls-files", "-z").split(b"\0"):
        if not raw_path:
            continue
        path = Path(raw_path.decode("utf-8", "surrogateescape"))
        if path.is_file():
            yield str(path), path.read_bytes()


def reachable_blobs() -> Iterable[tuple[str, bytes]]:
    seen: set[str] = set()
    for line in run("git", "rev-list", "--objects", "--all").decode(
        "utf-8", "surrogateescape"
    ).splitlines():
        object_id, _, path = line.partition(" ")
        if object_id in seen:
            continue
        seen.add(object_id)
        object_type = run("git", "cat-file", "-t", object_id).strip()
        if object_type != b"blob":
            continue
        size = int(run("git", "cat-file", "-s", object_id))
        if size > MAX_BLOB_BYTES:
            print(
                f"SKIP {object_id[:12]}:{path or '<unknown>'}: "
                f"{size} bytes exceeds scan limit",
                file=sys.stderr,
            )
            continue
        yield f"{object_id}:{path or '<unknown>'}", run("git", "cat-file", "blob", object_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--history",
        action="store_true",
        help="scan every unique blob reachable from refs instead of the current tracked tree",
    )
    args = parser.parse_args()

    findings: list[tuple[str, str]] = []
    scanned = 0
    source = reachable_blobs() if args.history else tracked_files()
    for location, data in source:
        scanned += 1
        for label, pattern in PATTERNS.items():
            if pattern.search(data):
                findings.append((label, location))

    if findings:
        for label, location in findings:
            print(f"{label}: {location}", file=sys.stderr)
        print(
            f"Audit failed: {len(findings)} finding(s) across {scanned} file/blob(s).",
            file=sys.stderr,
        )
        return 1

    scope = "reachable history" if args.history else "tracked tree"
    print(f"Audit passed: {scanned} file/blob(s) scanned in {scope}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
