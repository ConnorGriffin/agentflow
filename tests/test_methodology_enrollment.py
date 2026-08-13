from pathlib import Path
import shutil

from agentflow.enroll import _install_methodology_skills, _methodology_destination_states


def test_methodology_installer_requires_both_project_local_roots_and_ignores_ambient(
        tmp_path, monkeypatch):
    ambient = tmp_path / "ambient" / "tdd" / "SKILL.md"
    ambient.parent.mkdir(parents=True)
    ambient.write_text("not a pinned project contract")

    def run(command, **_kwargs):
        from types import SimpleNamespace
        if command[0] == "git" and command[-2:] == ["rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout="230e71a55ab07f0cd9beaa61649b583cb9d1bde1\n", stderr="")
        if command[0] == "git":
            return SimpleNamespace(returncode=0, stdout="230e71a55ab07f0cd9beaa61649b583cb9d1bde1\n", stderr="")
        for name in ("tdd", "codebase-design", "domain-modeling"):
            source = Path("/Users/connor/Code/ConnorGriffin/skills/skills") / name
            target = tmp_path / ".agents" / "skills" / name
            shutil.copytree(source, target)
            link = tmp_path / ".claude" / "skills" / name
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(Path("../../.agents/skills") / name)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("agentflow.enroll._run_command", run)
    monkeypatch.setattr("agentflow.enroll._resolved_skill_release",
                        lambda _manifest: ("230e71a55ab07f0cd9beaa61649b583cb9d1bde1", None))
    assert _install_methodology_skills(tmp_path).startswith("DO:")
    assert all(state == "ok" for state in _methodology_destination_states(
        tmp_path, __import__("agentflow.enroll", fromlist=["_manifest"])._manifest()).values())
