"""Explicit enrollment protects agentflow's working area from Git status."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from agentflow import github
from agentflow.enroll import (_audit_command, _converge_and_ship, _install_file,
                              _skills_problem, _SYNC_BRANCH, audit_lines, checkout_repo,
                              configured_repositories, declaration_line, enroll_repository,
                              main, newly_gated_prs, propose_surfaces, sync_fleet,
                              write_declaration)
from agentflow.repo_facts import surface_declaration


SCRIPT = Path(__file__).parents[1] / "scripts" / "enroll-standards.sh"
CHARTER = Path(__file__).parents[1] / "standards" / "CHARTER.md"
ROOT = Path(__file__).parents[1]


def _git_init(repo: Path, *, origin: str | None = None) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"],
                    cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"],
                    cwd=repo, check=True, capture_output=True)
    if origin:
        subprocess.run(["git", "remote", "add", "origin", origin],
                        cwd=repo, check=True, capture_output=True)
    (repo / ".gitkeep").write_text("")
    _git_commit_all(repo, "init")


def _git_commit_all(repo: Path, message: str = "commit") -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", message], cwd=repo, check=True,
                    capture_output=True)


def _enroll(repo: Path, *, apply: bool) -> subprocess.CompletedProcess:
    args = ["bash", str(SCRIPT)]
    if apply:
        args.append("--apply")
    args.append(str(repo))
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          env={**os.environ, "PATH": "/usr/bin:/bin"})


def _wire_global(home: Path, *, apply: bool) -> subprocess.CompletedProcess:
    args = ["bash", str(SCRIPT)]
    if apply:
        args.append("--apply")
    return subprocess.run(args, check=True, text=True, capture_output=True,
                          env={**os.environ, "HOME": str(home),
                               "AGENTFLOW_SHARED_GLOBAL": str(
                                   home / "shared" / "AGENTS.md"
                               ),
                               "AGENTFLOW_RETIRED_CLAUDE_GLOBAL": str(
                                   home / "retired" / "CLAUDE.md"
                               ),
                               "PATH": "/usr/bin:/bin"})


def _shared_global(home: Path) -> Path:
    shared = home / "shared" / "AGENTS.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("# Shared preferences\n")
    return shared


def test_global_wiring_requires_an_explicit_shared_instructions_file(tmp_path):
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        text=True,
        capture_output=True,
        env={**os.environ, "HOME": str(tmp_path), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 1
    assert "AGENTFLOW_SHARED_GLOBAL" in result.stdout
    assert "ConnorGriffin" not in result.stdout


def test_enrollment_dry_run_does_not_create_gitignore(tmp_path):
    _enroll(tmp_path, apply=False)

    assert not (tmp_path / ".gitignore").exists()


def test_enrollment_apply_creates_gitignore_with_agentflow_rule(tmp_path):
    _enroll(tmp_path, apply=True)

    assert (tmp_path / ".gitignore").read_text() == ".agentflow/\n"


def test_enrollment_preserves_existing_ignore_content(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text(".venv/\n*.log\n")

    _enroll(tmp_path, apply=True)

    assert ignore.read_text() == ".venv/\n*.log\n.agentflow/\n"
    assert not ignore.with_name(".gitignore.pre-agentflow").exists()


def test_enrollment_preserves_a_preexisting_gitignore_backup(tmp_path):
    ignore = tmp_path / ".gitignore"
    backup = tmp_path / ".gitignore.pre-agentflow"
    ignore.write_text(".venv/\n")
    backup.write_text("user backup\n")

    _enroll(tmp_path, apply=True)

    assert backup.read_text() == "user backup\n"


def test_repeated_enrollment_adds_agentflow_rule_exactly_once(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text(".venv/\n")

    _enroll(tmp_path, apply=True)
    _enroll(tmp_path, apply=True)

    assert ignore.read_text().splitlines().count(".agentflow/") == 1


def test_global_wiring_makes_both_tools_share_one_file(tmp_path):
    shared = _shared_global(tmp_path)

    claude_global = tmp_path / ".claude" / "CLAUDE.md"
    claude_global.parent.mkdir()
    claude_global.symlink_to(tmp_path / "retired" / "CLAUDE.md")

    codex_global = tmp_path / ".codex" / "AGENTS.md"
    codex_global.parent.mkdir()
    codex_global.symlink_to(CHARTER)

    _wire_global(tmp_path, apply=True)

    assert claude_global.readlink() == shared
    assert codex_global.readlink() == shared
    assert f"@{CHARTER}" in shared.read_text()


def test_global_wiring_creates_missing_tool_directories(tmp_path):
    shared = _shared_global(tmp_path)

    _wire_global(tmp_path, apply=True)

    assert (tmp_path / ".claude" / "CLAUDE.md").readlink() == shared
    assert (tmp_path / ".codex" / "AGENTS.md").readlink() == shared


def test_global_wiring_preserves_hand_written_file_and_unknown_link(tmp_path):
    _shared_global(tmp_path)

    claude_global = tmp_path / ".claude" / "CLAUDE.md"
    claude_global.parent.mkdir()
    claude_global.write_text("hand written\n")

    codex_global = tmp_path / ".codex" / "AGENTS.md"
    codex_global.parent.mkdir()
    codex_global.symlink_to("/tmp/unmanaged-agent-instructions")

    _wire_global(tmp_path, apply=True)

    assert claude_global.read_text() == "hand written\n"
    assert claude_global.with_name("CLAUDE.md.pre-agentflow").read_text() == "hand written\n"
    assert codex_global.readlink() == Path("/tmp/unmanaged-agent-instructions")


def test_seeded_agents_file_declares_ui_surfaces(tmp_path):
    # Issue #337: a newly enrolled repo must start declared one way or the other, so the
    # fleet can tell "headless on purpose" from "nobody filled this in".
    _enroll(tmp_path, apply=True)

    assert surface_declaration(str(tmp_path)).headless


def test_enrollment_dry_run_seeds_no_declaration(tmp_path):
    _enroll(tmp_path, apply=False)

    assert not (tmp_path / "AGENTS.md").exists()


class TestSurfaceProposal:
    """The backfill's guess, on the checkout shapes actually in the fleet."""

    def test_a_bundled_app_declares_its_authored_source(self, tmp_path):
        (tmp_path / "sample-app" / "frontend" / "src").mkdir(parents=True)
        (tmp_path / "sample-app" / "frontend" / "public").mkdir()
        (tmp_path / "sample-app" / "backend").mkdir()

        assert propose_surfaces(str(tmp_path)) == ("sample-app/frontend/src/",)

    def test_a_flat_frontend_declares_the_directory(self, tmp_path):
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "index.html").write_text("<html>")
        (tmp_path / "analysis_engine").mkdir()

        assert propose_surfaces(str(tmp_path)) == ("frontend/",)

    def test_a_served_directory_wins_over_the_server_source(self, tmp_path):
        (tmp_path / "public").mkdir()
        (tmp_path / "src").mkdir()   # a Node server, not a page

        assert propose_surfaces(str(tmp_path)) == ("public/",)

    def test_a_single_page_repo_declares_that_file(self, tmp_path):
        (tmp_path / "Sidebar.html").write_text("<html>")
        (tmp_path / "Code.js").write_text("// apps script")

        assert propose_surfaces(str(tmp_path)) == ("Sidebar.html",)

    def test_a_headless_repo_proposes_none(self, tmp_path):
        (tmp_path / "sandboxlib").mkdir()
        (tmp_path / "tests").mkdir()

        assert propose_surfaces(str(tmp_path)) == ()
        assert declaration_line(propose_surfaces(str(tmp_path))) == "ui-surfaces: none"

    def test_committed_screenshots_are_never_a_declared_surface(self, tmp_path):
        # docs/screenshots/** is the evidence channel — declaring it would make the proof
        # itself trip the gate.
        (tmp_path / "docs" / "screenshots" / "issue-1").mkdir(parents=True)
        (tmp_path / "docs" / "public").mkdir()

        assert propose_surfaces(str(tmp_path)) == ()


class TestSurfaceBackfill:
    def test_apply_writes_the_line_without_losing_content(self, tmp_path):
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# repo\n\nprofile: reviewed\n")
        (tmp_path / "frontend").mkdir()

        write_declaration(str(tmp_path), propose_surfaces(str(tmp_path)))

        assert "profile: reviewed" in agents.read_text()
        assert surface_declaration(str(tmp_path)).surfaces == ("frontend/",)

    def test_reapplying_never_overwrites_an_existing_declaration(self, tmp_path):
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# repo\n\nui-surfaces: hand/tuned/\n")

        write_declaration(str(tmp_path), ("frontend/",))
        write_declaration(str(tmp_path), ("frontend/",))

        assert agents.read_text().count("ui-surfaces:") == 1
        assert surface_declaration(str(tmp_path)).surfaces == ("hand/tuned/",)


def test_audit_names_every_repo_that_answered_nothing(tmp_path):
    declared, headless, silent = (tmp_path / n for n in ("a", "b", "c"))
    for path, text in ((declared, "ui-surfaces: frontend/\n"),
                       (headless, "ui-surfaces: none\n"),
                       (silent, "profile: reviewed\n")):
        path.mkdir()
        (path / "AGENTS.md").write_text(text)
    repos = [SimpleNamespace(repo=f"o/{p.name}", workdir=str(p))
             for p in (declared, headless, silent)]

    report = audit_lines(repos)

    assert "2 declared / 1 undeclared" in report
    assert report[-1].endswith("o/c")


def test_impact_names_the_open_prs_that_would_newly_need_screenshots(monkeypatch):
    rows = [SimpleNamespace(number=476), SimpleNamespace(number=475)]
    monkeypatch.setattr("agentflow.github.list_open_prs", lambda repo: rows)
    files = {476: ["frontend/diagnose.js"],
             475: ["frontend/plan.js", "docs/screenshots/issue-462/f7cf507/plan.png"]}
    monkeypatch.setattr(
        "agentflow.gate.github.pr_content",
        lambda repo, pr: github.PrContent(body="", paths=tuple(files[pr]), comments=[]))

    assert newly_gated_prs("o/ciq", ("frontend/",)) == [476]


def test_impact_of_an_unreadable_listing_is_unknown_not_empty(monkeypatch):
    monkeypatch.setattr("agentflow.github.list_open_prs", lambda repo: None)

    assert newly_gated_prs("o/ciq", ("frontend/",)) is None


def test_audit_names_a_repo_whose_headless_answer_its_checkout_contradicts(tmp_path):
    # Enrolment seeds `none` without looking at the repo, so a repo with a UI can be
    # "answered" and still have the gate switched off. That must stay visible.
    seeded = tmp_path / "seeded"
    (seeded / "frontend").mkdir(parents=True)
    (seeded / "AGENTS.md").write_text("ui-surfaces: none\n")
    genuine = tmp_path / "genuine"
    (genuine / "sandboxlib").mkdir(parents=True)
    (genuine / "AGENTS.md").write_text("ui-surfaces: none\n")
    repos = [SimpleNamespace(repo=f"o/{p.name}", workdir=str(p)) for p in (seeded, genuine)]

    report = audit_lines(repos)

    assert report[-1].endswith("o/seeded")
    assert "o/genuine" not in report[-1]


class TestTheImpactPreviewNamesThisCheckoutsOwnRepo:
    """Which repo's open PRs the preview measures — it must be this checkout's, or none."""

    def _init(self, repo: Path, origin: str) -> None:
        for cmd in (["init", "-q"], ["remote", "add", "origin", origin]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)

    def test_a_checkout_reached_by_a_differently_cased_path_still_resolves(self, tmp_path):
        # A checkout can be spelled one way on disk and another in the enrolled
        # list; a case-insensitive filesystem serves both, and the preview must still work.
        repo = tmp_path / "SampleApp"
        repo.mkdir()
        self._init(repo, "git@github.com:o/SampleApp.git")
        other_case = tmp_path / "sampleapp"
        if not other_case.is_dir():
            pytest.skip("case-sensitive filesystem — the two spellings are different repos")

        assert checkout_repo(str(other_case)) == "o/SampleApp"

    def test_a_directory_inside_a_checkout_does_not_borrow_the_enclosing_repo(self, tmp_path):
        repo = tmp_path / "outer"
        (repo / "inner").mkdir(parents=True)
        self._init(repo, "git@github.com:o/outer.git")

        assert checkout_repo(str(repo / "inner")) == ""


class TestSurfacesCommand:
    """The operator's command, driven the way an operator runs it."""

    def _stub_github(self, monkeypatch):
        monkeypatch.setattr("agentflow.enroll.checkout_repo", lambda workdir: "o/ciq")
        monkeypatch.setattr("agentflow.github.list_open_prs",
                            lambda repo: [SimpleNamespace(number=476)])
        monkeypatch.setattr("agentflow.gate.github.pr_content",
                            lambda _repo, _pr: github.PrContent(
                                body="", paths=("frontend/diagnose.js",), comments=[]))

    def _repo_with_a_frontend(self, tmp_path):
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "index.html").write_text("<html>")
        (tmp_path / "AGENTS.md").write_text("# repo\n\nprofile: reviewed\n")
        return tmp_path

    def test_the_default_run_shows_its_plan_and_its_impact_and_writes_nothing(
            self, tmp_path, monkeypatch, capsys):
        repo = self._repo_with_a_frontend(tmp_path)
        before = (repo / "AGENTS.md").read_text()
        self._stub_github(monkeypatch)

        main(["surfaces", str(repo)])

        out = capsys.readouterr().out
        assert "ui-surfaces: frontend/" in out
        assert out.index("#476") < out.index("dry run")   # impact comes before any write
        assert (repo / "AGENTS.md").read_text() == before

    def test_apply_writes_it_and_re_running_changes_nothing(self, tmp_path, monkeypatch):
        repo = self._repo_with_a_frontend(tmp_path)
        self._stub_github(monkeypatch)

        main(["surfaces", str(repo), "--apply"])
        written = (repo / "AGENTS.md").read_text()
        main(["surfaces", str(repo), "--apply"])

        assert surface_declaration(str(repo)).surfaces == ("frontend/",)
        assert (repo / "AGENTS.md").read_text() == written

    def test_a_seeded_headless_answer_is_corrected_when_the_repo_has_a_ui(
            self, tmp_path, monkeypatch, capsys):
        # End to end: enrolment seeds `none` on a repo with a frontend, and the command the
        # enrolment note points at must fix that rather than call the repo already answered.
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "index.html").write_text("<html>")
        _enroll(tmp_path, apply=True)
        self._stub_github(monkeypatch)

        main(["surfaces", str(tmp_path), "--apply"])

        out = capsys.readouterr().out
        assert "WARN" in out
        assert surface_declaration(str(tmp_path)).surfaces == ("frontend/",)
        assert "ui-surfaces: none" not in (tmp_path / "AGENTS.md").read_text()

    def test_a_genuinely_headless_repo_is_left_alone(self, tmp_path, monkeypatch, capsys):
        (tmp_path / "sandboxlib").mkdir()
        agents = tmp_path / "AGENTS.md"
        agents.write_text("# repo\n\nui-surfaces: none\n")
        self._stub_github(monkeypatch)

        main(["surfaces", str(tmp_path), "--apply"])

        assert agents.read_text() == "# repo\n\nui-surfaces: none\n"
        assert "already declares none" in capsys.readouterr().out


class TestFleetEnumerator:
    """4.1 — one shared enumerator, and the dead `REPOS` import is gone."""

    def test_configured_repositories_reads_the_same_source_the_daemon_uses(
            self, tmp_path, monkeypatch):
        checkout = tmp_path / "project"
        checkout.mkdir()
        config = tmp_path / "config.toml"
        config.write_text(f'[[repositories]]\nrepo = "o/project"\nworkdir = "{checkout}"\n')
        monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))

        repos = configured_repositories()

        assert [r.repo for r in repos] == ["o/project"]

    def test_audit_command_no_longer_imports_the_dead_daemon_repos(
            self, tmp_path, monkeypatch, capsys):
        # Regression for the live ImportError: `_audit_command` used to do
        # `from agentflow.daemon import REPOS`, which no longer exists.
        checkout = tmp_path / "project"
        checkout.mkdir()
        (checkout / "AGENTS.md").write_text("ui-surfaces: none\n")
        config = tmp_path / "config.toml"
        config.write_text(f'[[repositories]]\nrepo = "o/project"\nworkdir = "{checkout}"\n')
        monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))

        _audit_command()

        out = capsys.readouterr().out
        assert "o/project: none" in out


class TestConvergeMode:
    """4.2 — converge rewrites the two bundled `_asset_text` targets and nothing else."""

    def _drifted_repo(self, tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("AGENTFLOW_CONFIG", str(tmp_path / "missing-config.toml"))
        monkeypatch.setattr("agentflow.enroll._tooling_problem", lambda _surfaces: None)
        monkeypatch.setattr(
            "agentflow.enroll._install_methodology_skills",
            lambda _root: "ok:   methodology contracts supplied by focused fixture",
        )
        repo = tmp_path / "repo"
        _git_init(repo, origin="git@github.com:o/repo.git")
        skill = repo / ".agents" / "skills" / "agentflow" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("stale content, not the pinned bytes\n")
        claude_skill = repo / ".claude" / "skills"
        claude_skill.mkdir(parents=True)
        (claude_skill / "agentflow").symlink_to(Path("../../.agents/skills/agentflow"))
        _git_commit_all(repo, "drift")
        return repo

    def test_plain_apply_refuses_a_drifted_bundled_skill(self, tmp_path, monkeypatch):
        repo = self._drifted_repo(tmp_path, monkeypatch)
        skill = repo / ".agents" / "skills" / "agentflow" / "SKILL.md"
        before = skill.read_text()

        report = enroll_repository(str(repo), apply=True)

        assert skill.read_text() == before
        assert report.ready is False

    def test_converge_rewrites_the_drifted_bundled_skill(self, tmp_path, monkeypatch, capsys):
        repo = self._drifted_repo(tmp_path, monkeypatch)
        skill = repo / ".agents" / "skills" / "agentflow" / "SKILL.md"
        pinned = (ROOT / "skills" / "agentflow" / "SKILL.md").read_text()

        enroll_repository(str(repo), apply=True, converge=True)

        out = capsys.readouterr().out
        assert skill.read_text() == pinned
        assert "DO:   rewrote" in out
        assert "managed AgentFlow skill is drifted" not in out
        assert "rolled back" not in out

    def test_install_file_overwrite_rewrites_drifted_content(self, tmp_path):
        path = tmp_path / "file.txt"
        path.write_text("old")

        result = _install_file(path, "new", overwrite=True)

        assert result == f"DO:   rewrote {path} to the pinned content"
        assert path.read_text() == "new"

    def test_install_file_overwrite_still_refuses_a_non_regular_file_path(self, tmp_path):
        occupied = tmp_path / "occupied"
        occupied.mkdir()

        result = _install_file(occupied, "content", overwrite=True)

        assert result.startswith("WARN:")
        assert occupied.is_dir()

    def test_no_other_call_site_passes_overwrite_true(self):
        source = (ROOT / "agentflow" / "enroll.py").read_text()
        # The plan's fixed pair: the bundled SKILL.md and the screenshot harness.
        assert source.count("overwrite=converge") == 2
        assert "overwrite=True" not in source

    def test_a_fully_drifted_vendored_skill_pack_is_reported_not_converged(self, tmp_path, monkeypatch):
        # Decision 3 in 4.2: converge has no reproducible content for connor_skills
        # (ui-craft, drive-local-webapp) — a drifted destination there is never a
        # blocking precondition under converge, and it is never rewritten either.
        monkeypatch.setattr(
            "agentflow.enroll._resolved_skill_release",
            lambda manifest: (manifest["connor_skills"]["commit"], None),
        )
        monkeypatch.setattr(
            "agentflow.enroll._public_skill_destination_states",
            lambda root, manifest: {
                (".agents/skills", "ui-craft"): "drifted",
                (".claude/skills", "ui-craft"): "drifted",
                (".agents/skills", "drive-local-webapp"): "ok",
                (".claude/skills", "drive-local-webapp"): "ok",
            },
        )

        assert _skills_problem(tmp_path, ("frontend/",), converge=True) is None
        assert _skills_problem(tmp_path, ("frontend/",), converge=False) is not None

    def test_a_partially_installed_vendored_pack_still_refuses_under_converge(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "agentflow.enroll._resolved_skill_release",
            lambda manifest: (manifest["connor_skills"]["commit"], None),
        )
        monkeypatch.setattr(
            "agentflow.enroll._public_skill_destination_states",
            lambda root, manifest: {
                (".agents/skills", "ui-craft"): "drifted",
                (".claude/skills", "ui-craft"): "absent",
                (".agents/skills", "drive-local-webapp"): "ok",
                (".claude/skills", "drive-local-webapp"): "ok",
            },
        )

        assert _skills_problem(tmp_path, ("frontend/",), converge=True) is not None


class TestEnrollSync:
    """4.3 — the fleet-wide converge sweep and its exit-code contract."""

    def test_dry_run_reports_the_plan_and_always_exits_zero(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        repo.mkdir()
        cfg = SimpleNamespace(repo="o/repo", workdir=str(repo))

        exit_code = sync_fleet([cfg], apply=False)

        out = capsys.readouterr().out
        assert "PLAN: converge o/repo" in out
        assert "1 converged / 0 already current / 0 failed / 0 skipped (dirty)" in out
        assert exit_code == 0

    def test_a_dirty_checkout_is_skipped_not_failed(self, tmp_path, capsys):
        repo = tmp_path / "repo"
        _git_init(repo)
        (repo / "untracked.txt").write_text("x")
        cfg = SimpleNamespace(repo="o/dirty", workdir=str(repo))

        exit_code = sync_fleet([cfg], apply=True)

        out = capsys.readouterr().out
        assert "SKIP: o/dirty — checkout is dirty" in out
        assert "0 converged / 0 already current / 0 failed / 1 skipped (dirty)" in out
        assert exit_code == 0

    def test_a_failing_pr_create_is_a_convergence_failure_and_drives_exit_one(
            self, tmp_path, monkeypatch, capsys):
        # Exercises `sync_fleet`'s own bucketing/exit-code logic; the git-level detail of
        # *why* a repo fails to converge (unsigned commit, failed push, failed `gh pr
        # create`) is covered directly against `_converge_and_ship` below.
        repo = tmp_path / "repo"
        _git_init(repo)
        cfg = SimpleNamespace(repo="o/repo", workdir=str(repo))
        monkeypatch.setattr("agentflow.enroll._repo_drift",
                            lambda root: (["agentflow-skill: drifted"], []))
        monkeypatch.setattr(
            "agentflow.enroll._converge_and_ship",
            lambda root, repo: (False, "gh pr create failed — authentication required"),
        )

        exit_code = sync_fleet([cfg], apply=True)

        out = capsys.readouterr().out
        assert "WARN: o/repo failed to converge — gh pr create failed" in out
        assert "0 converged / 0 already current / 1 failed / 0 skipped (dirty)" in out
        assert exit_code == 1

    def _headless_capabilities(self, *, agentflow_skill_status="ok"):
        required_ok_ids = [
            "repository-instructions", "agentflow-skill", "codebase-memory",
        ]
        rows = [
            SimpleNamespace(id=cap_id, status=agentflow_skill_status if cap_id == "agentflow-skill"
                             else "ok", required=True)
            for cap_id in required_ok_ids
        ]
        rows += [
            SimpleNamespace(id=ui_id, status="missing", required=False)
            for ui_id in ("ui-craft", "drive-local-webapp", "screenshot-harness", "playwright")
        ]
        return rows

    def test_a_headless_repo_with_only_ui_tier_drift_is_already_current(
            self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _git_init(repo)
        cfg = SimpleNamespace(repo="o/headless", workdir=str(repo))
        monkeypatch.setattr(
            "agentflow.enroll.doctor",
            lambda root: SimpleNamespace(capabilities=self._headless_capabilities()),
        )

        exit_code = sync_fleet([cfg], apply=False)

        out = capsys.readouterr().out
        assert "ok:   o/headless is already current" in out
        assert "0 converged / 1 already current / 0 failed / 0 skipped (dirty)" in out
        assert "PLAN: converge" not in out
        for ui_id in ("ui-craft", "drive-local-webapp", "screenshot-harness", "playwright"):
            assert f"note: {ui_id}: missing (not required here)" in out
        assert exit_code == 0

    def test_a_required_row_drifted_still_plans_convergence(
            self, tmp_path, monkeypatch, capsys):
        repo = tmp_path / "repo"
        _git_init(repo)
        cfg = SimpleNamespace(repo="o/headless", workdir=str(repo))
        monkeypatch.setattr(
            "agentflow.enroll.doctor",
            lambda root: SimpleNamespace(
                capabilities=self._headless_capabilities(agentflow_skill_status="drifted")
            ),
        )

        exit_code = sync_fleet([cfg], apply=False)

        out = capsys.readouterr().out
        assert "PLAN: converge o/headless" in out
        assert "1 converged / 0 already current / 0 failed / 0 skipped (dirty)" in out
        assert exit_code == 0


class TestConvergeAndShip:
    """The per-repo apply/commit/push/PR sequence 4.3 point 4 describes."""

    def test_the_commit_is_dco_signed_and_a_push_failure_is_reported(
            self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _git_init(repo)
        skill_dir = repo / ".agents" / "skills" / "agentflow"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("content")
        monkeypatch.setattr("agentflow.enroll.enroll_repository",
                            lambda *a, **k: SimpleNamespace(ready=True))
        monkeypatch.setattr(
            "agentflow.enroll._run_command",
            lambda command, **kw: subprocess.run(command, capture_output=True, text=True),
        )

        ok, detail = _converge_and_ship(repo, "o/repo")

        assert ok is False
        assert "git push failed" in detail
        # The checkout is restored to its original branch afterwards, so read the sweep
        # commit off the sync branch rather than off HEAD.
        log = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%B", _SYNC_BRANCH],
            check=True, capture_output=True, text=True).stdout
        assert "Signed-off-by: Tester <tester@example.com>" in log

    def test_a_failing_gh_pr_create_is_reported_as_a_failure(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _git_init(repo)
        skill_dir = repo / ".agents" / "skills" / "agentflow"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("content")
        monkeypatch.setattr("agentflow.enroll.enroll_repository",
                            lambda *a, **k: SimpleNamespace(ready=True))

        def fake_run(command, **kw):
            if "push" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.run(command, capture_output=True, text=True)

        monkeypatch.setattr("agentflow.enroll._run_command", fake_run)
        monkeypatch.setattr(
            "agentflow.github.create_pr",
            lambda repo, **kw: github.IssueCreation(error="authentication required"),
        )

        ok, detail = _converge_and_ship(repo, "o/repo")

        assert ok is False
        assert "gh pr create failed" in detail
        assert "authentication required" in detail

    def test_a_successful_run_restores_the_original_branch(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _git_init(repo)
        skill_dir = repo / ".agents" / "skills" / "agentflow"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("content")
        monkeypatch.setattr("agentflow.enroll.enroll_repository",
                            lambda *a, **k: SimpleNamespace(ready=True))

        def fake_run(command, **kw):
            if "push" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.run(command, capture_output=True, text=True)

        monkeypatch.setattr("agentflow.enroll._run_command", fake_run)
        monkeypatch.setattr(
            "agentflow.github.create_pr",
            lambda repo, **kw: github.IssueCreation(url="https://example.test/pr/1"),
        )

        ok, detail = _converge_and_ship(repo, "o/repo")

        assert ok is True
        assert "checkout left on" not in detail
        branch = subprocess.run(["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short",
                                  "HEAD"], capture_output=True, text=True).stdout.strip()
        assert branch == "main" or branch == "master"
        local_branches = subprocess.run(
            ["git", "-C", str(repo), "branch", "--list", _SYNC_BRANCH],
            capture_output=True, text=True,
        ).stdout
        assert _SYNC_BRANCH in local_branches

    def test_a_successful_run_with_a_failed_restore_is_reported_as_a_failure(
            self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _git_init(repo)
        skill_dir = repo / ".agents" / "skills" / "agentflow"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("content")
        monkeypatch.setattr("agentflow.enroll.enroll_repository",
                            lambda *a, **k: SimpleNamespace(ready=True))

        def fake_run(command, **kw):
            if "push" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            if "checkout" in command and _SYNC_BRANCH not in command:
                # The restore checkout back to the original branch, not the sync branch's
                # own creation — force it to fail as if something perturbed the checkout.
                return subprocess.CompletedProcess(command, 1, "", "error: cannot restore")
            return subprocess.run(command, capture_output=True, text=True)

        monkeypatch.setattr("agentflow.enroll._run_command", fake_run)
        monkeypatch.setattr(
            "agentflow.github.create_pr",
            lambda repo, **kw: github.IssueCreation(url="https://example.test/pr/1"),
        )

        ok, detail = _converge_and_ship(repo, "o/repo")

        # The underlying converge/commit/push/PR sequence succeeded, but the restore
        # didn't — the operator must see this under sync_fleet's WARN: line, not DO:.
        assert ok is False
        assert "checkout left on" in detail
        assert "cannot restore" in detail

    def test_a_push_failure_still_restores_the_original_branch(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _git_init(repo)
        skill_dir = repo / ".agents" / "skills" / "agentflow"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("content")
        original_branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        monkeypatch.setattr("agentflow.enroll.enroll_repository",
                            lambda *a, **k: SimpleNamespace(ready=True))
        monkeypatch.setattr(
            "agentflow.enroll._run_command",
            lambda command, **kw: subprocess.run(command, capture_output=True, text=True),
        )

        ok, detail = _converge_and_ship(repo, "o/repo")

        assert ok is False
        assert "git push failed" in detail
        branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert branch == original_branch

    def test_a_detached_head_is_restored_to_the_same_commit_not_a_branch_named_head(
            self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _git_init(repo)
        skill_dir = repo / ".agents" / "skills" / "agentflow"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("content")
        original_sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(repo), "checkout", original_sha], check=True,
                        capture_output=True)
        monkeypatch.setattr("agentflow.enroll.enroll_repository",
                            lambda *a, **k: SimpleNamespace(ready=True))

        def fake_run(command, **kw):
            if "push" in command:
                return subprocess.CompletedProcess(command, 0, "", "")
            return subprocess.run(command, capture_output=True, text=True)

        monkeypatch.setattr("agentflow.enroll._run_command", fake_run)
        monkeypatch.setattr(
            "agentflow.github.create_pr",
            lambda repo, **kw: github.IssueCreation(url="https://example.test/pr/1"),
        )

        ok, detail = _converge_and_ship(repo, "o/repo")

        assert ok is True
        branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True,
        )
        assert branch.returncode != 0  # still detached, not a branch literally named HEAD
        sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()
        assert sha == original_sha

    def test_enroll_repository_raising_still_restores_the_branch_and_propagates(
            self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _git_init(repo)
        original_branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        def boom(*a, **k):
            raise RuntimeError("filesystem write failed")

        monkeypatch.setattr("agentflow.enroll.enroll_repository", boom)
        monkeypatch.setattr(
            "agentflow.enroll._run_command",
            lambda command, **kw: subprocess.run(command, capture_output=True, text=True),
        )

        with pytest.raises(RuntimeError, match="filesystem write failed"):
            _converge_and_ship(repo, "o/repo")

        branch = subprocess.run(
            ["git", "-C", str(repo), "symbolic-ref", "--quiet", "--short", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert branch == original_branch


class TestSweepEnumerationExitCodes:
    """4.1's exit-code table for `--audit` / `--sync`, at the enumerator boundary."""

    def test_a_bad_config_raises_configuration_error_before_any_repo_is_touched(
            self, tmp_path, monkeypatch):
        from agentflow.config import ConfigurationError

        config = tmp_path / "config.toml"
        config.write_text('[[repositories]]\nrepo = "o/missing"\nworkdir = "/does/not/exist"\n')
        monkeypatch.setenv("AGENTFLOW_CONFIG", str(config))

        with pytest.raises(ConfigurationError):
            configured_repositories()
