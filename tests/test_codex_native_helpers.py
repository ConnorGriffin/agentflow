"""Hermetic coverage for the 0.144.0 native-helper compatibility adapter (#509)."""

from __future__ import annotations

import time
from pathlib import Path

from agentflow import codex_native_helpers as helpers


def test_is_supported_version_true_on_the_exact_supported_build():
    assert helpers.is_supported_version("codex-cli 0.144.0\n") is True


def test_is_supported_version_false_on_any_other_build():
    assert helpers.is_supported_version("codex-cli 0.145.0\n") is False


def test_is_supported_version_false_on_unparseable_output():
    assert helpers.is_supported_version("not a version string\n") is False


def test_is_supported_version_fails_closed_on_none():
    """``None`` is the runner probe's own signal for a version it could not read (a nonzero exit,
    an unlaunchable binary, or a hung ``--version``). The gate treats absence of proof as no
    proof — never as capability."""
    assert helpers.is_supported_version(None) is False


def test_is_supported_version_fails_closed_on_empty_output():
    assert helpers.is_supported_version("") is False


def test_build_role_overrides_writes_one_owner_only_file_per_route(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    routes = (
        ("af_codex_luna_medium", "gpt-5.6-luna", "medium"),
        ("af_codex_terra_medium", "gpt-5.6-terra", "medium"),
    )

    overrides = helpers.build_role_overrides(routes)
    argv = overrides.argv

    assert argv[0] == "--strict-config"
    config_files = [argv[i + 1].split("=", 1)[1].strip('"')
                    for i, tok in enumerate(argv) if tok == "-c" and "config_file=" in argv[i + 1]]
    assert len(config_files) == 2
    directory = Path(config_files[0]).parent
    assert directory.parent == tmp_path
    assert directory == overrides.directory
    assert oct(directory.stat().st_mode)[-3:] == "700"
    for path_str in config_files:
        path = Path(path_str)
        assert oct(path.stat().st_mode)[-3:] == "600"
        body = path.read_text()
        assert 'model_reasoning_effort = "medium"' in body
        assert "developer_instructions" in body
        assert "gpt-5.6-luna" in body or "gpt-5.6-terra" in body


def test_build_role_overrides_never_carries_routing_provenance_or_bans(tmp_path, monkeypatch):
    """The generated files hold only the allowlisted model/effort/instructions — never the
    routing table's provenance, bans, or ladders (#509 evidence/security constraint)."""
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    argv = helpers.build_role_overrides((("af_codex_luna_low", "gpt-5.6-luna", "low"),)).argv
    config_file = next(argv[i + 1].split("=", 1)[1].strip('"')
                       for i, tok in enumerate(argv) if tok == "-c" and "config_file=" in argv[i + 1])
    body = Path(config_file).read_text()
    assert set(line.split(" =", 1)[0] for line in body.strip().splitlines()) == {
        "model", "model_reasoning_effort", "developer_instructions"}


def test_build_role_overrides_returns_nothing_for_an_empty_route_set():
    overrides = helpers.build_role_overrides(())
    assert overrides.argv == []
    assert overrides.directory is None


def test_stale_role_directories_are_swept_before_a_fresh_one_is_created(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    stale = tmp_path / f"{helpers._ROLE_DIR_PREFIX}stale"
    stale.mkdir()
    old = time.time() - helpers.STALE_ROLE_DIR_SECONDS - 60
    import os
    os.utime(stale, (old, old))
    fresh = tmp_path / f"{helpers._ROLE_DIR_PREFIX}fresh"
    fresh.mkdir()

    helpers.build_role_overrides((("af_codex_luna_low", "gpt-5.6-luna", "low"),))

    assert not stale.exists()
    assert fresh.exists()  # not stale — a bounded sweep never touches a recent directory


def test_cleanup_role_dirs_removes_the_directory_on_success(tmp_path, monkeypatch):
    """The public runner/provider seam: build real launch overrides through the same function
    the Codex runner calls, then prove the launch supervisor's own cleanup call — given only the
    typed ``directory`` field, never the argv — removes it."""
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    overrides = helpers.build_role_overrides((("af_codex_luna_low", "gpt-5.6-luna", "low"),))
    assert overrides.directory.exists()

    helpers.cleanup_role_dirs(overrides.directory)

    assert not overrides.directory.exists()


def test_cleanup_role_dirs_removes_the_directory_after_a_provider_failure(tmp_path, monkeypatch):
    """A provider that dies (nonzero exit/signal) still leaves its role directory owned by the
    same launch — cleanup does not depend on how the provider ended, only that this launch did."""
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    overrides = helpers.build_role_overrides((("af_codex_luna_low", "gpt-5.6-luna", "low"),))

    # Simulate the launch supervisor's failure-path cleanup call (same call, independent of the
    # provider's own exit status — see agentflow/coordinator/_launch_child.py).
    helpers.cleanup_role_dirs(overrides.directory)

    assert not overrides.directory.exists()


def test_cleanup_role_dirs_is_a_safe_no_op_for_a_launch_with_no_role_directory():
    helpers.cleanup_role_dirs(None)  # never raises
    helpers.cleanup_role_dirs("")  # never raises


def test_cleanup_role_dirs_ignores_a_directory_it_did_not_generate(tmp_path, monkeypatch):
    """A caller can never point cleanup at an arbitrary path — even a directory that happens to
    look plausible is rejected unless :func:`_owned_role_dir` recognizes it as this module's
    own generated directory."""
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    foreign = tmp_path.parent / "not-mine"
    foreign.mkdir(exist_ok=True)

    helpers.cleanup_role_dirs(foreign)

    assert foreign.exists()


def test_build_role_overrides_removes_a_partial_generation_immediately_on_failure(
        tmp_path, monkeypatch):
    """A failure partway through writing role files (second file collides/fails) must not leave
    the first file's directory for the 24h sweep to eventually find."""
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    routes = (
        ("af_codex_luna_medium", "gpt-5.6-luna", "medium"),
        ("af_codex_terra_medium", "gpt-5.6-terra", "medium"),
    )
    real_open = helpers.os.open
    calls = {"n": 0}

    def flaky_open(path, flags, mode=0o777, *args, **kwargs):
        # Only the role-file creation calls (positional 3-arg form, no dir_fd) are simulated;
        # shutil.rmtree's own internal os.open calls (dir_fd-relative) must pass through real.
        if not args and not kwargs and str(path).endswith(".toml"):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("simulated partial-generation failure")
        return real_open(path, flags, mode, *args, **kwargs)

    monkeypatch.setattr(helpers.os, "open", flaky_open)
    before = set(tmp_path.glob(f"{helpers._ROLE_DIR_PREFIX}*"))

    try:
        helpers.build_role_overrides(routes)
        assert False, "expected the simulated OSError to propagate"
    except OSError:
        pass

    after = set(tmp_path.glob(f"{helpers._ROLE_DIR_PREFIX}*"))
    assert after == before  # the partial directory was removed immediately, not left behind


def test_owned_role_dir_rejects_a_symlink_standing_in_for_a_real_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    real_target = tmp_path.parent / "outside-target"
    real_target.mkdir(exist_ok=True)
    link = tmp_path / f"{helpers._ROLE_DIR_PREFIX}evil"
    link.symlink_to(real_target, target_is_directory=True)

    assert helpers._owned_role_dir(link) is None
    assert real_target.exists()  # never touched


def test_owned_role_dir_rejects_a_traversal_or_absolute_override(tmp_path, monkeypatch):
    """A crafted directory outside the temp root — even one whose name carries the right
    prefix — must never resolve to a deletion target, whether checked directly through
    :func:`_owned_role_dir` or handed to :func:`cleanup_role_dirs` as if it were the launch's own
    typed ``directory`` field."""
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    outside = tmp_path.parent / f"{helpers._ROLE_DIR_PREFIX}outside"
    outside.mkdir(exist_ok=True)

    assert helpers._owned_role_dir(outside) is None
    helpers.cleanup_role_dirs(outside)
    assert outside.exists()  # never removed


def test_owned_role_dir_rejects_a_name_that_does_not_match_the_generated_grammar(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    wrong = tmp_path / f"{helpers._ROLE_DIR_PREFIX}has spaces"
    wrong.mkdir()

    assert helpers._owned_role_dir(wrong) is None


def _symlinked_tempdir(tmp_path):
    """A ``gettempdir()`` stand-in shaped like macOS's real one (``/var`` -> ``/private/var``):
    the raw path ``tempfile`` reports is a symlink to the canonical directory this module's own
    ``_owned_role_dir`` checks resolve to."""
    real = tmp_path / "private" / "var"
    real.mkdir(parents=True)
    raw = tmp_path / "var"
    raw.symlink_to(real, target_is_directory=True)
    return raw, real


def test_build_role_overrides_canonicalizes_a_symlinked_temp_root(tmp_path, monkeypatch):
    """A raw ``gettempdir()`` that is itself a symlink (macOS: ``/var`` -> ``/private/var``) must
    not produce a role directory ``_owned_role_dir`` then rejects as foreign — the directory this
    function creates, and returns as its typed ``directory`` field, is built under the resolved
    (``realpath``) base, matching every check ``cleanup_role_dirs`` re-runs against that field."""
    raw, real = _symlinked_tempdir(tmp_path)
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(raw))

    overrides = helpers.build_role_overrides((("af_codex_luna_low", "gpt-5.6-luna", "low"),))

    config_file = next(overrides.argv[i + 1].split("=", 1)[1].strip('"')
                       for i, tok in enumerate(overrides.argv)
                       if tok == "-c" and "config_file=" in overrides.argv[i + 1])
    directory = Path(config_file).parent
    assert directory == overrides.directory
    assert directory.parent == real  # created under the canonical base, not the raw symlink
    assert oct(directory.stat().st_mode)[-3:] == "700"


def test_cleanup_role_dirs_removes_directories_under_a_symlinked_temp_root(tmp_path, monkeypatch):
    raw, real = _symlinked_tempdir(tmp_path)
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(raw))
    overrides = helpers.build_role_overrides((("af_codex_luna_low", "gpt-5.6-luna", "low"),))
    assert overrides.directory.exists()
    assert overrides.directory.parent == real

    helpers.cleanup_role_dirs(overrides.directory)

    assert not overrides.directory.exists()


def test_owned_role_dir_still_rejects_foreign_traversal_and_symlinks_under_a_symlinked_root(
        tmp_path, monkeypatch):
    raw, real = _symlinked_tempdir(tmp_path)
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(raw))

    outside = tmp_path / f"{helpers._ROLE_DIR_PREFIX}outside"
    outside.mkdir()
    assert helpers._owned_role_dir(outside) is None

    link = real / f"{helpers._ROLE_DIR_PREFIX}evil"
    link.symlink_to(outside, target_is_directory=True)
    assert helpers._owned_role_dir(link) is None
    assert outside.exists()  # never touched


def test_stale_sweep_is_bounded_per_call(tmp_path, monkeypatch):
    monkeypatch.setattr(helpers.tempfile, "gettempdir", lambda: str(tmp_path))
    old = time.time() - helpers.STALE_ROLE_DIR_SECONDS - 60
    import os
    for i in range(helpers.ROLE_DIR_SWEEP_BUDGET + 5):
        d = tmp_path / f"{helpers._ROLE_DIR_PREFIX}stale-{i}"
        d.mkdir()
        os.utime(d, (old, old))

    helpers._sweep_stale_role_dirs()

    remaining = list(tmp_path.glob(f"{helpers._ROLE_DIR_PREFIX}*"))
    assert len(remaining) == 5  # budget reclaims all but the last 5 in one bounded pass
