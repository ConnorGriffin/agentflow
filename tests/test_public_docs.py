from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]

HELP_PATHS = (
    ("doctor",),
    ("enroll",),
    ("check",),
    ("capacity", "calibrate"),
    ("service", "install"),
    ("status",),
    ("resume",),
    ("pool", "status"),
    ("learning", "report"),
)

REQUIRED_README_TARGETS = (
    "docs/getting-started.md",
    "docs/pipeline.md",
    "docs/coordinator-operations.md",
    "docs/capabilities.md",
    "docs/evidence/README.md",
    "docs/learning-pipeline.md",
    "CONTRIBUTING.md",
    "PRODUCT.md",
    "DESIGN.md",
    "CONTEXT.md",
    "docs/adr/README.md",
    "docs/public-beta.md",
    "SUPPORT.md",
)


def test_readme_is_small_and_points_to_each_public_owner():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert len(readme.splitlines()) <= 200
    assert all(f"]({target})" in readme for target in REQUIRED_README_TARGETS)


@pytest.mark.parametrize("path", HELP_PATHS)
def test_documented_help_paths_are_non_mutating(path):
    result = subprocess.run(
        ["uv", "run", "agentflow", *path, "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout
