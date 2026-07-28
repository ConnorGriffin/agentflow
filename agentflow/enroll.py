"""agentflow enroll — finish wiring a repo into the fleet after `enroll-standards.sh`.

Two jobs:

- sweep any bare pre-enrollment needs-grilling / needs-mockup labels to the agentflow:*
  form (the original job, called by `enroll-standards.sh`);
- declare the repo's user-facing surfaces, so the mechanical UI-evidence gate (ADR 0018)
  is either armed or deliberately headless rather than silently inert.

Usage:
  python -m agentflow.enroll <owner/repo>            # sweep legacy labels
  python -m agentflow.enroll audit                   # fleet declaration census
  python -m agentflow.enroll surfaces <dir> [--apply]  # propose/apply one repo's line
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from agentflow.intake import sweep_legacy_labels
from agentflow.loop import UI_SURFACES_NONE, SurfaceDeclaration, surface_declaration

# Directory names that hold a user-facing surface when a repo has one. Deliberately narrow:
# a wrong guess here writes a declaration that either misses real UI or gates a backend path.
# A bare `src` is not on the list — in a Node service it is the server, not the page.
_UI_DIR_NAMES = ("frontend", "webui", "public", "www", "ui", "client", "static")
# Never look inside these: build output and vendored code aren't authored surfaces, and
# `docs/` must stay outside every declaration or committed screenshots would trip the gate.
_SKIP_DIRS = {".git", ".agentflow", "node_modules", "dist", "build", ".venv", "venv",
              "__pycache__", "docs", "mockups", "tests", "test", "archive", "coverage"}
_MAX_DEPTH = 3

_DECLARATION_KEY = "ui-surfaces:"


def propose_surfaces(workdir: str) -> tuple[str, ...]:
    """The surfaces this checkout looks like it has — empty means headless.

    Detection lives here, in enrollment, and nowhere near the merge path: the gate reads a
    written declaration and never guesses (ADR 0018). Prefixes end in `/` so a declared
    `frontend/` can't also claim `frontend-notes.md`; a repo whose whole UI is one root
    file (a Google Apps Script sidebar, say) declares that file literally.
    """
    root = Path(workdir)
    found: list[Path] = []
    for dirpath, dirnames, _files in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        depth = 0 if str(rel) == "." else len(rel.parts)
        dirnames[:] = sorted(d for d in dirnames
                             if d not in _SKIP_DIRS and not d.startswith("."))
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
            continue
        for name in list(dirnames):
            if name in _UI_DIR_NAMES:
                found.append(Path(dirpath) / name)
                dirnames.remove(name)   # a surface's own subdirs are part of that surface
    if found:
        return tuple(_prefix_for(root, path) for path in found)
    return tuple(sorted(p.name for p in root.glob("*.html")))


def _prefix_for(root: Path, path: Path) -> str:
    """A surface directory's declared prefix — its `src/` when it has one (a bundled app
    keeps config, lockfiles and build output beside the authored source)."""
    rel = path.relative_to(root).as_posix()
    return f"{rel}/src/" if (path / "src").is_dir() else f"{rel}/"


def declaration_line(surfaces: tuple[str, ...]) -> str:
    """The AGENTS.md line for a proposal — `none` when the repo is headless."""
    return f"{_DECLARATION_KEY} {', '.join(surfaces) if surfaces else UI_SURFACES_NONE}"


def write_declaration(workdir: str, surfaces: tuple[str, ...]) -> str:
    """Add the declaration to the repo's AGENTS.md, keeping everything already in it.

    Idempotent: a repo that already declares anything is left exactly as it is, so re-running
    the backfill never overwrites a hand-tuned line. Returns a human-readable outcome.
    """
    target = Path(workdir) / "AGENTS.md"
    if not target.exists():
        return f"SKIP: no AGENTS.md in {workdir} — run enroll-standards.sh --apply first"
    if surface_declaration(workdir).declared:
        return "ok:   already declared — leaving it alone"
    backup = target.with_name("AGENTS.md.pre-agentflow")
    if not backup.exists():
        shutil.copy2(target, backup)
    existing = target.read_text()
    separator = "" if existing.endswith("\n") or not existing else "\n"
    target.write_text(f"{existing}{separator}\n{declaration_line(surfaces)}\n")
    return f"DO:   wrote '{declaration_line(surfaces)}' to {target}"


def newly_gated_prs(repo: str, surfaces: tuple[str, ...]) -> list[int] | None:
    """The open PRs that this declaration would newly park — measured, not guessed.

    Returns ``None`` when GitHub can't be read, so an operator never reads an unreachable
    API as "nothing would be affected".
    """
    from agentflow import github
    from agentflow.gate import ui_evidence_gap
    if not surfaces:
        return []
    rows = github.list_open_prs(repo)
    if rows is None:
        return None
    return [row.number for row in rows
            if ui_evidence_gap(repo, row.number, list(surfaces))]


def audit_lines(repos) -> list[str]:
    """One line per enrolled repo plus a census tail, naming every undeclared repo."""
    lines = []
    undeclared = []
    for cfg in repos:
        declaration = surface_declaration(cfg.workdir)
        lines.append(f"  {cfg.repo}: {_audit_state(declaration)}")
        if not declaration.declared:
            undeclared.append(cfg.repo)
    declared = len(lines) - len(undeclared)
    lines.append(f"{declared} declared / {len(undeclared)} undeclared")
    if undeclared:
        lines.append("undeclared (the UI-evidence gate cannot fire there): "
                     + ", ".join(undeclared))
    return lines


def _audit_state(declaration: SurfaceDeclaration) -> str:
    if declaration.surfaces:
        return ", ".join(declaration.surfaces)
    return UI_SURFACES_NONE if declaration.headless else "UNDECLARED"


def checkout_repo(workdir: str) -> str:
    """The `owner/name` this checkout pushes to, or empty when it can't be resolved."""
    from agentflow.runner import _run
    r = _run(["git", "-C", workdir, "remote", "get-url", "origin"])
    if r.returncode != 0:
        return ""
    url = (r.stdout or "").strip().removesuffix(".git")
    if not url:
        return ""
    return "/".join(url.replace(":", "/").split("/")[-2:])


def _surfaces_command(workdir: str, apply: bool) -> None:
    print(f"UI surfaces — {workdir}")
    current = surface_declaration(workdir)
    if current.declared:
        print(f"  ok:   already declares {_audit_state(current)}")
        return
    proposal = propose_surfaces(workdir)
    print(f"  proposal: {declaration_line(proposal)}")
    repo = checkout_repo(workdir)
    if not repo:
        print("  WARN: could not resolve the GitHub repo — impact on open PRs is unknown")
    else:
        affected = newly_gated_prs(repo, proposal)
        if affected is None:
            print("  WARN: could not read open PRs — impact is unknown; re-run when GitHub is reachable")
        elif affected:
            print("  impact: these open PRs would newly need screenshots: "
                  + ", ".join(f"#{n}" for n in affected))
        else:
            print("  impact: no open PR would newly need screenshots")
    if apply:
        print(f"  {write_declaration(workdir, proposal)}")
    else:
        print("  (dry run — nothing changed; pass --apply to write it)")


def _audit_command() -> None:
    from agentflow.daemon import REPOS
    print("UI-surface declarations across the enrolled fleet")
    for line in audit_lines(REPOS):
        print(line)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in args
    positional = [a for a in args if a != "--apply"]
    if positional[:1] == ["audit"] and len(positional) == 1:
        _audit_command()
        return
    if positional[:1] == ["surfaces"] and len(positional) == 2:
        _surfaces_command(positional[1], apply)
        return
    if len(positional) != 1 or "/" not in positional[0]:
        print("usage: python -m agentflow.enroll <owner/repo>\n"
              "       python -m agentflow.enroll audit\n"
              "       python -m agentflow.enroll surfaces <dir> [--apply]", file=sys.stderr)
        sys.exit(2)
    repo = positional[0]
    print(f"Sweeping legacy labels in {repo}...")
    changed = sweep_legacy_labels(repo)
    if not changed:
        print("  nothing to change — all issues already use agentflow:* vocabulary")
    else:
        for line in changed:
            print(f"  {line}")
        print(f"  {len(changed)} issue(s) updated")


if __name__ == "__main__":
    main()
