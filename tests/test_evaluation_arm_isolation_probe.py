from __future__ import annotations

import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evaluation-arm-isolation-probe.py"


def _load_probe():
    spec = importlib.util.spec_from_file_location("evaluation_arm_isolation_probe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_local_plan_has_no_provider_credential_or_network_admission(tmp_path):
    probe = _load_probe()
    parent = tmp_path / "parent"
    source, writable = parent / "arm" / "source", parent / "arm" / "output"
    source.mkdir(parents=True); writable.mkdir()

    plan = probe._plan(parent, source, (writable,), None)
    profile = probe._profile(plan)

    assert plan.network is False
    assert set(plan.read_literals) == {Path("/bin/sh"), Path("/private/var/select/sh")}
    assert not any(path in probe._AUTH_HANDLES.values() for path in plan.read_literals)
    assert "network-outbound" not in profile
    for handle in probe._AUTH_HANDLES.values():
        assert str(handle) not in profile
    assert str(SCRIPT) not in profile


def test_provider_plan_admits_only_its_exact_credential_handle(tmp_path, monkeypatch):
    probe = _load_probe()
    executable = tmp_path / "codex"; executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(probe.shutil, "which", lambda _: str(executable))
    parent = tmp_path / "parent"
    source, writable = parent / "arm" / "source", parent / "arm" / "output"
    source.mkdir(parents=True); writable.mkdir()

    profile = probe._profile(probe._plan(parent, source, (writable,), "codex"))

    handle = probe._AUTH_HANDLES["codex"]
    assert f'(allow file-read* (literal {json.dumps(str(handle))}))' in profile
    assert str(probe._AUTH_HANDLES["claude"]) not in profile
    assert f'(allow file-read* (subpath {json.dumps(str(handle.parent))}))' not in profile


@pytest.mark.parametrize("path_of", [
    lambda probe, _: Path("/usr/bin/false"),
    lambda probe, _: probe._AUTH_HANDLES["claude"],
    lambda probe, _: probe._AUTH_HANDLES["codex"],
])
def test_local_plan_rejects_every_external_literal_except_the_shells(tmp_path, path_of):
    probe = _load_probe()
    parent = tmp_path / "parent"; source, writable = parent / "source", parent / "output"
    source.mkdir(parents=True); writable.mkdir()
    with pytest.raises(ValueError, match="external literal"):
        probe._profile(probe.SandboxPlan((), (path_of(probe, parent),), (writable,), parent, False))


def test_provider_plan_rejects_another_provider_executable(tmp_path, monkeypatch):
    probe = _load_probe()
    executables = {"codex": tmp_path / "codex", "claude": tmp_path / "claude"}
    for executable in executables.values():
        executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(probe.shutil, "which", lambda provider: str(executables[provider]))
    parent = tmp_path / "parent"; source, writable = parent / "source", parent / "output"
    source.mkdir(parents=True); writable.mkdir()
    with pytest.raises(ValueError, match="external literal"):
        probe._profile(probe.SandboxPlan((), (executables["claude"],), (writable,), parent, True, "codex"))


def test_plan_rejects_provider_network_in_a_local_only_plan(tmp_path):
    probe = _load_probe()
    parent = tmp_path / "parent"; writable = parent / "output"
    writable.mkdir(parents=True)
    with pytest.raises(ValueError, match="network admission"):
        probe._profile(probe.SandboxPlan((), (), (writable,), parent, True))


@pytest.mark.parametrize("path_of", [lambda probe, parent: Path("/"), lambda probe, parent: parent, lambda probe, parent: probe._REAL_HOME, lambda probe, parent: probe._AUTH_HANDLES["codex"].parent])
def test_profile_rejects_broad_read_admission(tmp_path, path_of):
    probe = _load_probe()
    parent = tmp_path / "parent"; source, writable = parent / "source", parent / "output"
    source.mkdir(parents=True); writable.mkdir()
    with pytest.raises(ValueError):
        probe._profile(probe.SandboxPlan((path_of(probe, parent),), (), (writable,), parent, False))


def test_closed_parser_rejects_echo_stderr_partial_and_extra_fields():
    probe = _load_probe()
    expected = b'{"admitted_readable":true,"sibling_reachable":false,"oracle_reachable":false}'

    assert probe._parse_result(expected) == {"admitted_readable": True, "sibling_reachable": False, "oracle_reachable": False}
    assert probe._parse_result(b"prompt echo\n" + expected) is None
    assert probe._parse_result(expected[:-1]) is None
    assert probe._parse_result(expected[:-1] + b',"extra":true}') is None
    assert probe._parse_result(expected[:-1] + b',"admitted_readable":true}') is None


def test_output_file_reader_rejects_an_oversized_final_result(tmp_path):
    probe = _load_probe()
    output = tmp_path / "output"; output.mkdir()
    result = output / "final.json"
    result.write_bytes(b"{" + b" " * probe._OUTPUT_LIMIT)

    assert probe._read_result(output) is None


def test_output_file_reader_rejects_a_symlink_to_outside_output(tmp_path):
    probe = _load_probe()
    output = tmp_path / "output"; output.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"admitted_readable":true,"sibling_reachable":false,"oracle_reachable":false}', encoding="utf-8")
    os.symlink(outside, output / "final.json")

    assert probe._read_result(output) is None


def test_codex_provider_row_uses_the_anchored_reader_without_a_precheck(tmp_path, monkeypatch):
    probe = _load_probe()
    parent = tmp_path / "parent"; parent.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text('{"admitted_readable":true,"sibling_reachable":false,"oracle_reachable":false}', encoding="utf-8")

    def clone(_, root):
        source = root / "source"; source.mkdir()
        return source

    def environment(root, _):
        output = root / "output"; output.mkdir()
        return {"CODEX_HOME": "task-owned-codex-home"}, (output,)

    calls = 0
    environments = []
    def run(_, **kwargs):
        nonlocal calls
        calls += 1
        environments.append(kwargs["env"])
        if calls == 1:
            return {"outcome": "exited", "exit_status": 0, "stdout": b"codex 1.2.3\n", "stderr": b"", "stdout_bytes": 12, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False, "duration_seconds": 0}
        os.symlink(outside, parent / "codex-order-1" / "output" / "final.json")
        return {"outcome": "exited", "exit_status": 0, "stdout": b"", "stderr": b"", "stdout_bytes": 0, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False, "duration_seconds": 0}

    monkeypatch.setattr(probe, "_detached_bundle_clone", clone)
    monkeypatch.setattr(probe, "_task_environment", environment)
    monkeypatch.setattr(probe, "_plan", lambda *args: object())
    monkeypatch.setattr(probe, "_profile", lambda _: "profile")
    monkeypatch.setattr(probe, "_provider_executable", lambda _: Path("/provider"))
    monkeypatch.setattr(probe, "_run_bounded", run)
    monkeypatch.setattr(Path, "is_file", lambda _: (_ for _ in ()).throw(AssertionError("unsafe precheck")))

    row = probe._probe_provider(parent, "codex", "order-1", probe.SourceBundle(tmp_path / "bundle", "revision"))

    assert row["final_result"] is None
    assert "CODEX_HOME" not in environments[0]
    assert environments[1]["CODEX_HOME"] == "task-owned-codex-home"


def test_git_timeout_is_bounded_and_content_free(monkeypatch):
    probe = _load_probe()

    def timed_out(*args, **kwargs):
        assert kwargs["timeout"] == 30
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(probe.subprocess, "run", timed_out)

    with pytest.raises(RuntimeError, match="^source preparation failed$"):
        probe._git(["status"])


def test_disposable_cleanup_repairs_hostile_provider_permissions(tmp_path):
    probe = _load_probe()
    root = tmp_path / "provider-root"
    hostile = root / "output" / "hostile"
    hostile.mkdir(parents=True)
    (hostile / "provider-bytes").write_text("not retained", encoding="utf-8")
    os.chmod(hostile, 0)
    os.chmod(hostile.parent, 0)

    probe._remove_disposable(root)

    assert not root.exists()


def test_disposable_cleanup_fails_closed_when_absence_cannot_be_proven(tmp_path, monkeypatch):
    probe = _load_probe()
    root = tmp_path / "provider-root"; root.mkdir()
    monkeypatch.setattr(probe.shutil, "rmtree", lambda _: None)

    with pytest.raises(RuntimeError, match="^disposable cleanup failed$"):
        probe._remove_disposable(root)


def test_bounded_runner_marks_truncated_stdout_unparseable(tmp_path):
    probe = _load_probe()
    run = probe._run_bounded([sys.executable, "-c", "import sys; sys.stdout.write('x' * 65537)"], cwd=tmp_path, env=dict(), timeout=10)

    assert run["stdout_truncated"] is True
    assert (None if run["stdout_truncated"] else probe._parse_result(run["stdout"])) is None


def test_bounded_runner_kills_descendants_that_retain_output_pipes(tmp_path):
    probe = _load_probe()
    child = "import time; time.sleep(30)"
    command = [sys.executable, "-c", f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}]); sys.exit(0)"]

    started = time.monotonic()
    run = probe._run_bounded(command, cwd=tmp_path, env=dict(), timeout=1)

    assert run["outcome"] == "exited"
    assert time.monotonic() - started < 3


def test_bounded_runner_kills_a_quiet_descendant_before_returning(tmp_path):
    probe = _load_probe()
    marker = tmp_path / "descendant-survived"
    child = f"import pathlib, time; time.sleep(.3); pathlib.Path({str(marker)!r}).write_text('survived')"
    command = [sys.executable, "-c", f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); sys.exit(0)"]

    run = probe._run_bounded(command, cwd=tmp_path, env=dict(), timeout=10)
    time.sleep(.5)

    assert run["outcome"] == "exited"
    assert not marker.exists()


@pytest.mark.parametrize("stage", [
    {"outcome": "exited", "exit_status": 0, "stderr_bytes": 1, "stdout_truncated": False, "stderr_truncated": False},
    {"outcome": "exited", "exit_status": 0, "stderr_bytes": 65_536, "stdout_truncated": False, "stderr_truncated": True},
    {"outcome": "exited", "exit_status": 1, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False},
])
def test_provider_pass_requires_a_clean_exit_and_no_stderr(stage):
    probe = _load_probe()
    row = {"version": "1.2.3", "provider_stage": stage, "final_result": probe._EXPECTED_PROVIDER_RESULT}

    assert probe._provider_passed(row) is False


def test_provider_pass_rejects_truncated_stdout():
    probe = _load_probe()
    row = {"version": "1.2.3", "provider_stage": {"outcome": "exited", "exit_status": 0, "stderr_bytes": 0, "stdout_truncated": True, "stderr_truncated": False}, "final_result": probe._EXPECTED_PROVIDER_RESULT}

    assert probe._provider_passed(row) is False


def test_provider_pass_accepts_only_the_exact_final_result():
    probe = _load_probe()
    row = {"version": "1.2.3", "provider_stage": {"outcome": "exited", "exit_status": 0, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False}, "final_result": probe._EXPECTED_PROVIDER_RESULT}

    assert probe._provider_passed(row) is True
    row["final_result"] = {**probe._EXPECTED_PROVIDER_RESULT, "oracle_reachable": True}
    assert probe._provider_passed(row) is False


def test_startup_version_is_printable_bounded_and_stderr_free():
    probe = _load_probe()
    clean = {"outcome": "exited", "exit_status": 0, "stdout": b"codex 1.2.3\n", "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False}

    assert probe._version_from_startup(clean) == "codex 1.2.3"
    assert probe._version_from_startup({**clean, "stderr_bytes": 1}) is None
    assert probe._version_from_startup({**clean, "stdout": b"x" * (probe._VERSION_OUTPUT_LIMIT + 1)}) is None
    assert probe._version_from_startup({**clean, "stdout": b"codex\x00"}) is None


def test_real_provider_matrix_has_exactly_four_rows(tmp_path, monkeypatch, capsys):
    probe = _load_probe()
    calls = []

    bundle = probe.SourceBundle(tmp_path / "method.bundle", "pinned-revision")

    def fake_probe(_, provider, order, received_bundle):
        calls.append((provider, order))
        assert received_bundle == bundle
        return {
            "provider": provider, "order": order, "version": "1.2.3",
            "provider_stage": {"outcome": "exited", "exit_status": 0, "stderr_bytes": 0, "stdout_truncated": False, "stderr_truncated": False},
            "final_result": probe._EXPECTED_PROVIDER_RESULT,
        }

    monkeypatch.setattr(probe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(probe.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    system_profile = tmp_path / "system.sb"; system_profile.touch()
    monkeypatch.setattr(probe, "_SYSTEM_PROFILE", system_profile)
    monkeypatch.setattr(probe, "_metadata", lambda: {"metadata": "closed"})
    monkeypatch.setattr(probe, "_capture_source_bundle", lambda _: bundle)
    monkeypatch.setattr(probe, "_probe_provider", fake_probe)

    assert probe.main(["--provider", "claude", "--provider", "codex"]) == 0
    assert calls == [("claude", "order-1"), ("codex", "order-1"), ("codex", "order-2"), ("claude", "order-2")]
    assert "versions" not in json.loads(capsys.readouterr().out)


def test_current_cli_commands_have_one_outer_sandbox_owner(tmp_path):
    probe = _load_probe()
    result, schema = tmp_path / "final.json", tmp_path / "schema.json"

    claude = probe._provider_command("claude", tmp_path, result, schema, "claude")
    codex = probe._provider_command("codex", tmp_path, result, schema, "codex")

    assert claude[claude.index("--permission-mode") + 1] == "bypassPermissions"
    assert claude[claude.index("--model") + 1] == "haiku"
    assert "--settings" not in claude
    assert claude[claude.index("--setting-sources") + 1] == ""
    assert claude[claude.index("--mcp-config") + 1] == '{"mcpServers":{}}'
    assert "--strict-mcp-config" in claude and "--no-session-persistence" in claude
    assert "--max-budget-usd" in claude and "--json-schema" in claude
    assert codex[codex.index("--sandbox") + 1] == "danger-full-access"
    assert codex[codex.index("--ask-for-approval") + 1] == "never"
    assert "--output-last-message" in codex and "--ephemeral" in codex
    assert probe._profile_command("profile", claude)[:4] == ["sandbox-exec", "-p", "profile", "--"]


def test_local_helper_checks_admitted_read_write_and_all_denials(tmp_path):
    probe = _load_probe()
    command = probe._local_helper(tmp_path / "admitted", tmp_path / "out", tmp_path / "sibling", tmp_path / "oracle", tmp_path / "sibling-link", tmp_path / "oracle-link")

    assert command[:2] == ["/bin/sh", "-c"]
    for fact in ("admitted_read", "task_write", "source_write_denied", "sibling_open_denied", "sibling_stat_denied", "sibling_enumeration_denied", "sibling_symlink_open_denied", "oracle_open_denied", "oracle_stat_denied", "oracle_enumeration_denied", "oracle_symlink_open_denied", "unrelated_home_open_denied", "unrelated_home_stat_denied", "unrelated_home_enumeration_denied", "unrelated_home_symlink_open_denied"):
        assert fact in command[2]
    assert "exit 1" in command[2]


def test_coordinator_command_is_exact_and_reusable():
    probe = _load_probe()

    assert probe._coordinator_command(["--local-only"]) == f"python3 {SCRIPT} --local-only"


def test_local_only_cli_emits_only_a_provider_free_boundary(tmp_path, monkeypatch, capsys):
    probe = _load_probe()
    facts = {
        "admitted_read": True, "task_write": True, "source_write_denied": True,
        "sibling_open_denied": True, "sibling_stat_denied": True,
        "sibling_enumeration_denied": True, "sibling_symlink_open_denied": True,
        "oracle_open_denied": True, "oracle_stat_denied": True,
        "oracle_enumeration_denied": True, "oracle_symlink_open_denied": True,
        "unrelated_home_open_denied": True, "unrelated_home_stat_denied": True,
        "unrelated_home_enumeration_denied": True, "unrelated_home_symlink_open_denied": True,
    }

    def fake_run(command, **_):
        profile = command[2]
        assert "network-outbound" not in profile
        assert "claude" not in profile and "codex" not in profile
        assert all(str(handle) not in profile for handle in probe._AUTH_HANDLES.values())
        return {"outcome": "exited", "exit_status": 0, "stdout": json.dumps(facts).encode(),
                "stderr": b"", "stdout_bytes": 1, "stderr_bytes": 0, "stdout_truncated": False,
                "stderr_truncated": False, "duration_seconds": 0}

    monkeypatch.setattr(probe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(probe.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    system_profile = tmp_path / "system.sb"; system_profile.touch()
    monkeypatch.setattr(probe, "_SYSTEM_PROFILE", system_profile)
    monkeypatch.setattr(probe, "_run_bounded", fake_run)
    monkeypatch.setattr(probe, "_metadata", lambda: {"metadata": "closed"})

    assert probe.main(["--local-only"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "local-only"
    assert all(row["facts"] == facts for row in payload["results"])


def test_local_only_cli_rejects_a_false_boundary_fact(tmp_path, monkeypatch, capsys):
    probe = _load_probe()
    facts = {key: True for key in probe._LOCAL_FACT_KEYS}
    facts["oracle_stat_denied"] = False

    monkeypatch.setattr(probe.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(probe.shutil, "which", lambda _: "/usr/bin/sandbox-exec")
    system_profile = tmp_path / "system.sb"; system_profile.touch()
    monkeypatch.setattr(probe, "_SYSTEM_PROFILE", system_profile)
    monkeypatch.setattr(probe, "_metadata", lambda: {"metadata": "closed"})
    monkeypatch.setattr(probe, "_probe_local_boundary", lambda _, order: {
        "order": order, "helper_stage": {"outcome": "exited", "exit_status": 0}, "facts": facts,
    })

    assert probe.main(["--local-only"]) == 1
    assert all(row["facts"]["oracle_stat_denied"] is False for row in json.loads(capsys.readouterr().out)["results"])


def test_detached_bundle_clone_smoke_uses_a_clean_temporary_repository(tmp_path, monkeypatch):
    probe = _load_probe()
    repository = tmp_path / "repository"
    script = repository / "scripts" / "probe.py"
    script.parent.mkdir(parents=True)
    historical = repository / "prior-only.txt"
    historical.write_text("prior-only sentinel\n", encoding="utf-8")
    subprocess.run(["git", "init", str(repository)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Probe Test", "-c", "user.email=probe@example.test", "commit", "-m", "prior"], check=True, capture_output=True)
    prior_revision = subprocess.run(["git", "-C", str(repository), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip()
    historical.unlink()
    script.write_text("# probe\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Probe Test", "-c", "user.email=probe@example.test", "commit", "-m", "snapshot"], check=True, capture_output=True)
    monkeypatch.setattr(probe, "__file__", str(script))

    bundle = probe._capture_source_bundle(tmp_path / "arm")
    script.write_text("# changed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repository), "-c", "user.name=Probe Test", "-c", "user.email=probe@example.test", "commit", "-m", "changed"], check=True, capture_output=True)
    source = probe._detached_bundle_clone(bundle, tmp_path / "clone")

    assert subprocess.run(["git", "-C", str(source), "status", "--porcelain"], text=True, capture_output=True, check=True).stdout == ""
    assert subprocess.run(["git", "-C", str(source), "for-each-ref"], text=True, capture_output=True, check=True).stdout == ""
    assert not (source / ".git" / "objects" / "info" / "alternates").exists()
    assert (source / "scripts" / "probe.py").read_text(encoding="utf-8") == "# probe\n"
    assert subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], text=True, capture_output=True, check=True).stdout.strip() == bundle.revision
    assert subprocess.run(["git", "-C", str(source), "cat-file", "-e", f"{prior_revision}:prior-only.txt"], capture_output=True).returncode != 0


@pytest.mark.skipif(platform.system() != "Darwin", reason="sandbox-exec is macOS-only")
def test_local_only_cli_boundary_emits_a_closed_payload():
    run = subprocess.run([sys.executable, str(SCRIPT), "--local-only"], text=True, capture_output=True, timeout=30)

    assert run.returncode == 0, run.stderr
    assert len(run.stdout.splitlines()) == 1
    payload = json.loads(run.stdout)
    assert payload["mode"] == "local-only"
    assert len(payload["results"]) == 2
    assert all(row["output_retained"] is False and all(row["facts"].values()) for row in payload["results"])
