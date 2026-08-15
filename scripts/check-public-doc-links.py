#!/usr/bin/env python3
"""Check relative links in the public-facing project contract."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

DOCUMENTS = [
    Path("README.md"),
    Path("COMPATIBILITY.md"),
    Path("CONTRIBUTING.md"),
    Path("GOVERNANCE.md"),
    Path("SECURITY.md"),
    Path("SUPPORT.md"),
    Path("docs/public-beta.md"),
    Path("docs/getting-started.md"),
    Path("docs/pipeline.md"),
    Path("docs/learning-pipeline.md"),
    Path("docs/capabilities.md"),
    Path("docs/coordinator-operations.md"),
    Path("docs/evidence/README.md"),
]
LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


def main() -> int:
    failures: list[str] = []
    checked = 0
    for document in DOCUMENTS:
        text = document.read_text(encoding="utf-8")
        for target in LINK_RE.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            checked += 1
            path_text = unquote(target.split("#", 1)[0]).strip("<>")
            resolved = (document.parent / path_text).resolve()
            if not resolved.exists():
                failures.append(f"{document}: {target}")

    if failures:
        print("Broken relative documentation links:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"Documentation link check passed for {checked} relative link(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
