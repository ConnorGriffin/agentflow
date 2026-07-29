from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).parents[1]
FORBIDDEN_PARTS = {".agentflow", ".claude", ".impeccable", "node_modules"}


def _assert_release_contents(names: list[str], charter_path: str) -> None:
    split_names = [set(Path(name).parts) for name in names]
    assert not any(parts & FORBIDDEN_PARTS for parts in split_names)
    assert any(name.endswith("agentflow/webui/dist/index.html") for name in names)
    assert any(name.endswith(charter_path) for name in names)


def _install_artifact(artifact: Path, environment: Path) -> Path:
    subprocess.run(
        [sys.executable, "-m", "venv", str(environment)],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        [str(environment / "bin" / "pip"), "install", "--no-deps", str(artifact)],
        check=True,
        text=True,
        capture_output=True,
        timeout=120,
    )
    return environment / "bin" / "python"


def _installed_provider_charters(python: Path, workdir: Path) -> list[str]:
    workdir.mkdir()
    clean_home = workdir / "home"
    clean_home.mkdir()
    script = """
import json
from agentflow.coordinator.providers import provider_command
from agentflow.coordinator.record import Record

prompts = []
for pool, model in (("claude", "opus"), ("codex", "gpt-5.6-sol")):
    record = Record(
        f"{pool}-build", "build", pool, 1,
        model=model, source=".", input_ptr="do the stage",
        complexity="deep", effort="high",
    )
    command = provider_command(record)
    prompts.append(command[command.index("-p") + 1] if pool == "claude" else command[-1])
print(json.dumps(prompts))
"""
    result = subprocess.run(
        [str(python), "-c", script],
        cwd=workdir,
        env=os.environ | {"HOME": str(clean_home), "PYTHONPATH": ""},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


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
        wheel_charter = archive.read(
            next(name for name in wheel_names if name.endswith("agentflow/_data/CHARTER.md"))
        )
    with tarfile.open(source) as archive:
        source_names = archive.getnames()
        source_charter_member = next(
            member for member in archive.getmembers()
            if member.name.endswith("standards/CHARTER.md")
        )
        source_charter_file = archive.extractfile(source_charter_member)
        assert source_charter_file is not None
        source_charter = source_charter_file.read()

    _assert_release_contents(wheel_names, "agentflow/_data/CHARTER.md")
    _assert_release_contents(source_names, "standards/CHARTER.md")
    canonical_charter = (ROOT / "standards" / "CHARTER.md").read_bytes()
    assert wheel_charter == source_charter == canonical_charter

    assert wheel.stat().st_size < 2_000_000
    assert source.stat().st_size < 5_000_000

    expected_charter = canonical_charter.decode()
    environments = {}
    for name, artifact in (("wheel", wheel), ("sdist", source)):
        environment = tmp_path / f"{name}-environment"
        python = _install_artifact(artifact, environment)
        prompts = _installed_provider_charters(python, tmp_path / f"{name}-runtime")
        assert all(prompt.count(expected_charter) == 1 for prompt in prompts)
        environments[name] = environment

    environment = environments["wheel"]
    assert (environment / "bin" / "agentflow-capacity-helper").is_file()
    checkout = tmp_path / "enrolled-repository"
    checkout.mkdir()
    config = tmp_path / "agentflow.toml"
    config.write_text(
        f"""
[[repositories]]
repo = "owner/repository"
workdir = "{checkout}"
""".lstrip()
    )
    check = subprocess.run(
        [
            str(environment / "bin" / "agentflow"),
            "check",
            "--config",
            str(config),
        ],
        env=os.environ | {"AGENTFLOW_STATE": str(tmp_path / "state")},
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert check.returncode == 0, check.stderr
    assert check.stdout.strip() == "configuration valid: 1 repository (0 workspace)"
