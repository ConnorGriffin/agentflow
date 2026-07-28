from __future__ import annotations

import subprocess
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
FORBIDDEN_PARTS = {".agentflow", ".claude", ".impeccable", "node_modules"}


def _assert_release_contents(names: list[str]) -> None:
    split_names = [set(Path(name).parts) for name in names]
    assert not any(parts & FORBIDDEN_PARTS for parts in split_names)
    assert any(name.endswith("agentflow/webui/dist/index.html") for name in names)


def test_release_artifacts_contain_only_the_runtime_and_built_console(tmp_path):
    result = subprocess.run(
        [
            "uv",
            "build",
            "--no-build-isolation",
            "--out-dir",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    wheel = next(tmp_path.glob("*.whl"))
    source = next(tmp_path.glob("*.tar.gz"))

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    with tarfile.open(source) as archive:
        source_names = archive.getnames()

    _assert_release_contents(wheel_names)
    _assert_release_contents(source_names)

    assert wheel.stat().st_size < 2_000_000
    assert source.stat().st_size < 5_000_000
