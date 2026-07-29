"""What an enrolled repo declares about itself in its AGENTS.md/CLAUDE.md header.

A repo states its autonomy profile, its user-facing surfaces, and who may resume its
issues in a few `key: value` lines at the top of its agent instructions. Reading those
lines is the same job wherever it happens — dispatch decides whether a merge needs a
human, the stage prompts name the surfaces a screenshot must cover, and enrollment
audits and rewrites the declaration — so the parsing lives here once rather than
travelling with any one of them.

Every read is fail-safe by omission: a repo that says nothing gets the conservative
default (`reviewed`, no surfaces, owner-only), never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_PROFILE_RE = re.compile(r"^profile:\s*(autonomous|reviewed|guarded)", re.MULTILINE)
_UI_SURFACES_RE = re.compile(r"^ui-surfaces:\s*(.+)$", re.MULTILINE)
_ALLOWLIST_RE = re.compile(r"^intake-allowlist:\s*(.+)", re.MULTILINE)


def repo_profile(workdir: str) -> str:
    """The repo's autonomy profile from its AGENTS.md/CLAUDE.md `profile:` line.
    Defaults to `reviewed` (ADR 0002) — the safe middle, never auto-merge by accident."""
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = Path(workdir) / name
        if p.exists():
            m = _PROFILE_RE.search(p.read_text(errors="replace"))
            if m:
                return m.group(1)
    return "reviewed"


UI_SURFACES_NONE = "none"


@dataclass(frozen=True, slots=True)
class SurfaceDeclaration:
    """What a repo says about its user-facing surfaces — and whether it said anything.

    `ui-surfaces: none` is the explicit headless answer: declared, with no surfaces. Silence
    is a third state, not the same answer written differently — it keeps the UI-evidence gate
    inert exactly as before (ADR 0018) while staying visible to the enrollment audit.
    """

    surfaces: tuple[str, ...] = ()
    declared: bool = False

    @property
    def headless(self) -> bool:
        """Declared as having no user-facing surface on purpose."""
        return self.declared and not self.surfaces


def surface_declaration(workdir: str) -> SurfaceDeclaration:
    """Read the repo's AGENTS.md/CLAUDE.md `ui-surfaces:` line — a comma-separated list of
    path prefixes (e.g. `agentflow/static/, frontend/`), the literal `none` for a repo that
    is headless on purpose, or nothing at all. A change under one of the prefixes needs a
    before/after screenshot: the charter's UI-evidence gate (ADR 0018) reads this per repo
    instead of a hardcoded example."""
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = Path(workdir) / name
        if p.exists():
            m = _UI_SURFACES_RE.search(p.read_text(errors="replace"))
            if m:
                values = [s.strip() for s in m.group(1).split(",") if s.strip()]
                if [v.lower() for v in values] == [UI_SURFACES_NONE]:
                    return SurfaceDeclaration(declared=True)
                if values:
                    return SurfaceDeclaration(surfaces=tuple(values), declared=True)
    return SurfaceDeclaration()


def ui_surfaces(workdir: str) -> list[str]:
    """The repo's effective UI-surface prefixes — empty for a headless or undeclared repo."""
    return list(surface_declaration(workdir).surfaces)


def surfaces_phrase(declaration: SurfaceDeclaration) -> str:
    """How to name the repo's UI surfaces to a builder/reviewer prompt."""
    if declaration.surfaces:
        return ", ".join(f"`{s}`" for s in declaration.surfaces)
    if declaration.headless:
        return "none — this repo is headless, so no screenshot is required"
    return "any user-facing surface (frontend, UI templates, etc.)"


def intake_allowlist(repo: str, workdir: str) -> set[str]:
    """Authors whose comments can trigger a resume or re-intake. Always includes the repo
    owner; extend via an `intake-allowlist: alice, bob` line in AGENTS.md/CLAUDE.md."""
    owner = repo.split("/")[0]
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = Path(workdir) / name
        if p.exists():
            m = _ALLOWLIST_RE.search(p.read_text(errors="replace"))
            if m:
                extra = {s.strip() for s in m.group(1).split(",") if s.strip()}
                return {owner} | extra
    return {owner}
