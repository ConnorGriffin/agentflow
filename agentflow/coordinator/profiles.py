"""Per-stage session profiles: tool allowlist, wall/turn ceilings (ADR 0044), and reasoning effort (ADR 0046).

Every daemon session used to launch with one full tool surface, personal MCP connectors
leaking in, and a single stage-blind two-hour timeout. This table keys a profile on the
record's ``(stage, complexity, effort)`` — the keys the record already carries — and returns
the read/search allowlist, the wall-clock ceiling, the turn ceiling, and the provider
reasoning-effort rung (build/revise only; every other stage stays provider-default) for that cell. The
values are taken verbatim from the research table
(``docs/research/session-profiles-and-ceilings-draft.md`` §3a/§3b); they are calibration and
are expected to ratchet once per-attempt telemetry (#223) fills the thin cells.

Read-only stages (Intake, Research, plan Audit) get a read/search allowlist and no edit tools. Review is a
bounded code-writing stage: it keeps the full edit/test surface so the independent reviewer can
ship clear fixes before recording its exact-head verdict. Every other stage also keeps the full
surface (``allowed_tools is None``). The runner
pins the MCP set to strict mode and re-supplies only Codebase Memory to every stage, and adds
that server to a read-only stage's ``--tools`` allowlist so an exploration stage keeps the same
code-graph access Build has (#244). Revise inherits the original builder's Build ceiling via
``builder_complexity`` (ADR 0041).
"""

from __future__ import annotations

from dataclasses import dataclass

_MIN = 60

# Read/search allowlists for the read-only stages (§3a). Every one omits the edit tools; the
# scheduling/plan-mode surface history showed loaded-in-every-session-and-used-in-none is
# omitted too. Intake carries ToolSearch + WebFetch per §3a; Research adds both web tools;
# Review deliberately is not in this map: its review-and-fix contract needs edit tools.
_READ_ONLY_TOOLS: dict[str, tuple[str, ...]] = {
    "intake": ("Read", "Bash", "Grep", "Glob", "ToolSearch", "WebFetch"),
    "research": ("Read", "Bash", "Grep", "Glob", "ToolSearch", "WebSearch", "WebFetch"),
    # The plan audit must open the files the brief cites, so it gets intake's read/search surface
    # — and no edit tools at all: it judges the plan, it never rewrites the brief or the code
    # (ADR 380).
    "audit": ("Read", "Bash", "Grep", "Glob", "ToolSearch", "WebFetch"),
}

# The edit capabilities a read-only profile withholds. The allowlist (``--tools``) removes them
# from the loaded surface and the settings ``permissions.deny`` block is a second, independent
# strip of the same tools — either mechanism alone removes them from the session's tool surface
# entirely (verified against the CLI init event), so a read-only stage cannot exercise them. That
# unreachability *is* the fail-closed guarantee (ADR 0044 pt 1/5): the capability is absent, not
# present-and-caught. The deny block earns its keep as defense in depth if the allowlist ever
# regresses.
WITHHELD_EDIT_TOOLS: tuple[str, ...] = ("Edit", "Write", "NotebookEdit")

# Wall (seconds) + turn ceilings for the non-Build stages (§3b). Thin-sample stages ship the
# conservative drafted numbers now and ratchet once #223's telemetry fills their cells.
_STAGE_CEILINGS: dict[str, tuple[int, int]] = {
    "intake": (20 * _MIN, 40),
    # The audit re-reads one brief against the code — the same bounded read-only shape as intake,
    # so it carries the same ceiling (ADR 380).
    "audit": (20 * _MIN, 40),
    "review": (15 * _MIN, 40),
    "respond": (15 * _MIN, 40),
    "converse": (15 * _MIN, 40),
    "research": (30 * _MIN, 80),
    "mockup": (60 * _MIN, 200),
}

# The reasoning-effort rung each work-effort dial maps to at launch (ADR 0046). Build and revise
# launch the provider at the mapped rung; every other stage leaves the provider default (``None``).
# ``extra`` maps to the provider "extra high" rung (``xhigh``). ``max`` is never wired by the
# daemon (manual-only escape hatch, ADR 0046). The argv builders clamp a rung above a provider's
# own ladder to its top rung rather than failing the launch — inert for today's models, which all
# accept these rungs, but kept as defensive design for a future model with a shorter ladder.
_REASONING_BY_EFFORT: dict[str, str] = {
    "low": "low",
    "medium": "medium",
    "high": "high",
    "extra": "xhigh",
}

# Build ceilings keyed on (complexity, effort) (§3b). A cell the research table does not name
# falls back to the most conservative ceiling of its complexity.
_BUILD_CEILINGS: dict[tuple[str, str], tuple[int, int]] = {
    ("standard", "low"): (25 * _MIN, 80),
    ("standard", "medium"): (25 * _MIN, 80),
    ("deep", "medium"): (45 * _MIN, 200),
    ("deep", "high"): (45 * _MIN, 200),
    ("deep", "extra"): (60 * _MIN, 300),
}
_BUILD_DEFAULT: dict[str, tuple[int, int]] = {
    "standard": (25 * _MIN, 80),
    "deep": (45 * _MIN, 200),
}

# The legacy uniform two-hour wall, kept only as the fallback for a stage the table never names
# so an unrecognized stage is never accidentally strangled by a tight ceiling.
_DEFAULT_CEILING = (2 * 3600, 200)


@dataclass(frozen=True, slots=True)
class StageProfile:
    """The launch envelope for one session: its read/search allowlist (``None`` keeps the full
    edit/test surface), its wall-clock ceiling, its turn ceiling, and the provider reasoning-effort
    rung set at launch (``None`` leaves the provider default)."""

    allowed_tools: tuple[str, ...] | None
    wall_ceiling_s: int
    turn_ceiling: int
    reasoning_effort: str | None = None

    @property
    def read_only(self) -> bool:
        return self.allowed_tools is not None


def _build_ceiling(complexity: str | None, effort: str | None) -> tuple[int, int]:
    key = (complexity or "deep", effort or "")
    if key in _BUILD_CEILINGS:
        return _BUILD_CEILINGS[key]
    return _BUILD_DEFAULT.get(complexity or "deep", _DEFAULT_CEILING)


def profile_for(record) -> StageProfile:
    """Resolve the session profile for a record from its ``(stage, complexity, effort)``.

    Build sizes its ceiling on its own complexity/effort; Revise inherits the original
    builder's Build ceiling through ``builder_complexity`` (ADR 0041); every other stage reads
    the per-stage table. Read-only stages carry a read/search allowlist; the rest keep the full
    surface (``allowed_tools is None``).
    """
    stage = record.stage
    if stage == "build":
        wall, turns = _build_ceiling(record.complexity, record.effort)
        return StageProfile(None, wall, turns, _REASONING_BY_EFFORT.get(record.effort))
    if stage == "revise":
        wall, turns = _build_ceiling(record.builder_complexity or record.complexity, record.effort)
        return StageProfile(None, wall, turns, _REASONING_BY_EFFORT.get(record.effort))
    wall, turns = _STAGE_CEILINGS.get(stage, _DEFAULT_CEILING)
    return StageProfile(_READ_ONLY_TOOLS.get(stage), wall, turns)
