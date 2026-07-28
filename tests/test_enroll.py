"""Explicit enrollment protects agentflow's working area from Git status."""

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from agentflow.enroll import (audit_lines, declaration_line, newly_gated_prs,
                              propose_surfaces, write_declaration)
from agentflow.loop import surface_declaration


SCRIPT = Path(__file__).parents[1] / "scripts" / "enroll-standards.sh"
CHARTER = Path(__file__).parents[1] / "standards" / "CHARTER.md"


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
                               "PATH": "/usr/bin:/bin"})


def _shared_global(home: Path) -> Path:
    shared = home / "Code" / "ConnorGriffin" / "dotfiles" / "agents" / "AGENTS.md"
    shared.parent.mkdir(parents=True)
    shared.write_text("# Shared preferences\n")
    return shared


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
    assert ignore.with_name(".gitignore.pre-agentflow").read_text() == ".venv/\n*.log\n"


def test_repeated_enrollment_adds_agentflow_rule_exactly_once(tmp_path):
    ignore = tmp_path / ".gitignore"
    ignore.write_text(".venv/\n")

    _enroll(tmp_path, apply=True)
    _enroll(tmp_path, apply=True)

    assert ignore.read_text().splitlines().count(".agentflow/") == 1


def test_global_wiring_makes_both_tools_share_one_file(tmp_path):
    dotfiles = tmp_path / "Code" / "ConnorGriffin" / "dotfiles"
    shared = _shared_global(tmp_path)

    claude_global = tmp_path / ".claude" / "CLAUDE.md"
    claude_global.parent.mkdir()
    claude_global.symlink_to(dotfiles / "claude" / "CLAUDE.md")

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
        (tmp_path / "brewgen" / "frontend" / "src").mkdir(parents=True)
        (tmp_path / "brewgen" / "frontend" / "public").mkdir()
        (tmp_path / "brewgen" / "backend").mkdir()

        assert propose_surfaces(str(tmp_path)) == ("brewgen/frontend/src/",)

    def test_a_flat_frontend_declares_the_directory(self, tmp_path):
        (tmp_path / "frontend").mkdir()
        (tmp_path / "frontend" / "index.html").write_text("<html>")
        (tmp_path / "ciq_autotune").mkdir()

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
        "agentflow.gate.github.api",
        lambda args, **kwargs: {"files": [{"path": p} for p in files[int(args[2])]],
                                "body": "", "comments": []})

    assert newly_gated_prs("o/ciq", ("frontend/",)) == [476]


def test_impact_of_an_unreadable_listing_is_unknown_not_empty(monkeypatch):
    monkeypatch.setattr("agentflow.github.list_open_prs", lambda repo: None)

    assert newly_gated_prs("o/ciq", ("frontend/",)) is None
