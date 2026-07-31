"""Per-stage session profiles: tool allowlist, wall/turn ceilings (ADR 0044), and reasoning effort (ADR 0046).

Every daemon session used to launch with one full tool surface, personal MCP connectors
leaking in, and a single stage-blind two-hour timeout. This table keys a profile on the
record's ``(stage, complexity, effort)`` — the keys the record already carries — and returns
the read/search allowlist, the wall-clock ceiling, the turn ceiling, and the provider
reasoning-effort rung (build/revise only; every other stage stays provider-default) for that cell. The
allowlists are taken verbatim from the research table
(``docs/research/session-profiles-and-ceilings-draft.md`` §3a); the ceilings began there too and
have since been ratcheted onto the fleet's own recorded distribution (§3b′, #410; §3b″ per build
cell, #416) — the ratchet that table anticipated once per-attempt telemetry (#223) filled its
thin cells.

Read-only stages (Intake, Research, Attack) get a read/search allowlist and no edit tools. Review is a
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
    # An attacker must open the files the draft cites, so it gets intake's read/search surface —
    # and no edit tools at all: it breaks the plan, it never rewrites the draft or the code, and
    # it never touches the issue (ADR 380).
    "attack": ("Read", "Bash", "Grep", "Glob", "ToolSearch", "WebFetch"),
}

# The edit capabilities a read-only profile withholds. The allowlist (``--tools``) removes them
# from the loaded surface and the settings ``permissions.deny`` block is a second, independent
# strip of the same tools — either mechanism alone removes them from the session's tool surface
# entirely (verified against the CLI init event), so a read-only stage cannot exercise them. That
# unreachability *is* the fail-closed guarantee (ADR 0044 pt 1/5): the capability is absent, not
# present-and-caught. The deny block earns its keep as defense in depth if the allowlist ever
# regresses.
WITHHELD_EDIT_TOOLS: tuple[str, ...] = ("Edit", "Write", "NotebookEdit")

# Wall (seconds) + turn ceilings for the non-Build stages (§3b). Every ceiling sits clear of the
# work its stage is recorded needing — that headroom is the whole point, so the ceiling kills a
# runaway and never an ordinary long session (#410). ``_OBSERVED_P95`` below carries the
# distribution each cell was set from; keep the two in step when either moves.
_STAGE_CEILINGS: dict[str, tuple[int, int]] = {
    "intake": (20 * _MIN, 80),
    # An attack re-reads one draft against the code — the same bounded read-only shape as intake,
    # so it carries the same ceiling (ADR 380).
    "attack": (20 * _MIN, 80),
    "review": (30 * _MIN, 120),
    "respond": (20 * _MIN, 80),
    "converse": (20 * _MIN, 80),
    "research": (30 * _MIN, 80),
    "mockup": (60 * _MIN, 200),
}

# The 95th percentile wall (seconds) and tool calls of work each cell has been recorded needing,
# from the fleet's own session streams — 516 recorded sessions, read 2026-07-31. This is the
# evidence the ceiling tables are set against, not decoration: the §3b rule is that a ceiling sits
# well above the work, and the first table drifted under it unnoticed. Review's cap was drawn at 40
# from an n=70 sample; by n=210 the p90 was 55 tool calls and 31 reviews had been killed at the cap
# having already read the diff and run the suite (40–70 tool calls of finished work apiece) without
# recording a verdict. A cell absent here has no recorded sessions to justify a number.
#
# Keyed the way the ceilings themselves resolve — ``(stage, complexity, effort)`` — so a reading and
# the ceiling it justifies cannot drift apart. Build's ceiling is per cell, so its readings are too:
# a pooled build figure is what hid the standard tier sitting on its own limit while six standard
# builds and revisions were killed at it (#416). ``(None, None)`` marks a stage whose ceiling does
# not vary by cell. The revise row is a reading of the whole standard tier it inherits (ADR 0041),
# not of one cell. A ``None`` wall is a reading that counted tool calls only — the per-cell build
# pass did, and the wall ceilings are not in evidence there (§3b″).
#
# Tool calls rather than the provider's own reported turn counter — the quantity ``--max-turns``
# really does bound — because that counter is censored by the very ceiling being calibrated: every
# session the cap killed reports exactly the cap plus one, while the sessions it did not kill
# report well past it, so a capped stage's turn distribution describes neither population. Tool
# calls are read off the session stream and nothing in the launch truncates them.
#
# A tool call is not a turn: one turn may issue several at once (one review stopped at a cap of 40
# had issued 196). But a session's turns never exceed its tool calls by more than two across the
# whole sample, so a ceiling clear of a tool-call p95 is clear of the turn p95 to within two turns —
# against margins of tens here (§3b′).
#
# p95 rather than max, on purpose. A ceiling censors its own distribution — review's longest
# recorded session ran 899 s against a 900 s wall, so how long it actually needed is unknown — and
# the tail is genuinely unbounded: one review ran 335 tool calls. No ceiling that still kills a
# runaway can clear that, so the bar is p95 with headroom, and the headroom is what makes an
# ordinary long session survive.
_OBSERVED_P95: dict[tuple[str, str | None, str | None], tuple[int | None, int]] = {
    ("intake", None, None): (335, 45),
    ("attack", None, None): (109, 44),
    ("review", None, None): (469, 66),
    ("respond", None, None): (477, 51),
    ("research", None, None): (608, 39),
    ("mockup", None, None): (1013, 66),
    ("build", "standard", "low"): (None, 80),  # n=33
    ("build", "standard", "medium"): (None, 89),  # n=17
    ("build", "deep", "medium"): (None, 138),  # n=35
    ("build", "deep", "high"): (None, 157),  # n=34
    ("build", "deep", "extra"): (None, 146),  # n=6
    ("revise", "standard", None): (None, 100),  # n=12, inheriting the standard build tier
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

# Build ceilings keyed on (complexity, effort) (§3b, ratcheted per cell in §3b″/#416). A cell the
# research table does not name falls back to the most conservative ceiling of its complexity.
# The standard turn ceilings were drawn at 80 and sat *on* the work those cells do — standard/low's
# p95 is exactly 80, standard/medium's is 89, and a revision inheriting the tier reaches 100 — so
# six standard builds and revisions were killed having already done the job. They now carry p95
# with headroom, on the same rule the review stages were ratcheted on. The deep cells are clear
# against their own readings (138, 157, 146 against 200, 200, 300) and are unchanged.
_BUILD_CEILINGS: dict[tuple[str, str], tuple[int, int]] = {
    ("standard", "low"): (25 * _MIN, 160),
    ("standard", "medium"): (25 * _MIN, 160),
    ("deep", "medium"): (45 * _MIN, 200),
    ("deep", "high"): (45 * _MIN, 200),
    ("deep", "extra"): (60 * _MIN, 300),
}
_BUILD_DEFAULT: dict[str, tuple[int, int]] = {
    "standard": (25 * _MIN, 160),
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
