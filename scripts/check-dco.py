#!/usr/bin/env python3
"""Require a DCO sign-off matching each non-merge commit's author email."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

SIGNOFF_RE = re.compile(
    r"^Signed-off-by:\s*.+?\s*<([^>]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", help="base commit (exclusive)")
    parser.add_argument("head", help="head commit (inclusive)")
    args = parser.parse_args()

    commits = git("rev-list", "--reverse", "--no-merges", f"{args.base}..{args.head}").split()
    failures: list[str] = []
    for commit in commits:
        author_email, subject, body = git(
            "show",
            "-s",
            "--format=%aE%n%s%n%B",
            commit,
        ).split("\n", 2)
        signoffs = {email.casefold() for email in SIGNOFF_RE.findall(body)}
        if author_email.casefold() not in signoffs:
            failures.append(f"{commit[:12]} {subject}")

    if failures:
        print("DCO sign-off missing or does not match the author email:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("Amend each commit with `git commit --amend -s`.", file=sys.stderr)
        return 1

    print(f"DCO check passed for {len(commits)} commit(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
