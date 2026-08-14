"""Per-stage session profiles: tool allowlist, wall/turn ceilings (ADR 0044), and reasoning effort (ADR 0046).

Every daemon session used to launch with one full tool surface, personal MCP connectors
leaking in, and a single stage-blind two-hour timeout. This table keys a profile on the
record's ``(stage, complexity, effort)`` — the keys the record already carries — and returns
the read/search allowlist, the wall-clock ceiling, the turn ceiling, and the provider
session reasoning effort (low for Build/Revise; every other stage stays provider-default) for that cell. The
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
# from the fleet's own session streams, read 2026-07-31 — the stage rows over 516 recorded
# sessions (§3b′), the per-cell build and revise tool-call rows over a re-read of the same growing
# store later that morning at 529, three more builds in, none moving a reading (§3b″, #422), and
# the build/revise wall column over 661 launcher sessions in the duration pass (§3b‴). This is the
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
# not of one cell. The build/revise wall column comes from a later duration pass than its tool-call
# column, so the two carry different per-cell samples — each states its own n, and both live in the
# research doc (§3b″ for calls, §3b‴ for walls). The standard-tier wall p95s are readings of
# sessions the old 80-turn cap was cutting off, so they are lower bounds on what the raised
# 160-turn allowance takes — the standard wall ceiling is sized against recorded pace over the full
# allowance (§3b‴), not against these p95s alone.
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
_OBSERVED_P95: dict[tuple[str, str | None, str | None], tuple[int, int]] = {
    ("intake", None, None): (335, 45),
    ("attack", None, None): (109, 44),
    ("review", None, None): (469, 66),
    ("respond", None, None): (477, 51),
    ("research", None, None): (608, 39),
    ("mockup", None, None): (1013, 66),
    ("build", "standard", "low"): (574, 80),  # calls n=33; wall n=98 (§3b‴)
    ("build", "standard", "medium"): (755, 89),  # calls n=17; wall n=45 (§3b‴)
    ("build", "deep", "medium"): (1214, 138),  # calls n=35; wall n=124 (§3b‴)
    ("build", "deep", "high"): (1676, 157),  # calls n=34; wall n=96 (§3b‴)
    ("build", "deep", "extra"): (2351, 146),  # calls n=6; wall n=20 (§3b‴)
    # Inheriting the standard build tier; calls n=12, wall n=18 (§3b‴).
    ("revise", "standard", None): (909, 100),
}

# ADR 498 pins the Fable Build/Revise parent to low reasoning. The work-effort dial still sizes
# ceilings below and maps to the worker rung inside the routing module's rendered lead brief.
# Build ceilings keyed on (complexity, effort) (§3b, ratcheted per cell in §3b″/#416, walls read
# per cell in §3b‴/#421). A cell the research table does not name falls back to the most
# conservative ceiling of its complexity.
# The standard turn ceilings were drawn at 80 and sat *on* the work those cells do — standard/low's
# p95 is exactly 80, standard/medium's is 89, and a revision inheriting the tier reaches 100 — so
# six standard builds and revisions were killed having already done the job. They now carry p95
# with headroom, on the same rule the review stages were ratcheted on. The deep cells are clear
# against their own readings (138, 157, 146 against 200, 200, 300) and are unchanged.
#
# The standard tier's wall was still the drafted 25 minutes when that turn ceiling doubled — a
# limit never measured per cell. The §3b‴ duration pass read it: the measured p95s (574 s, 755 s,
# 909 s for the revise inheritance) sit under 25 minutes, but every one is a reading of a session
# the 80-turn cap was cutting off, and at the pace those near-cap sessions recorded (7.5–12 s per
# tool call) a session using the full 160-call allowance takes roughly 1 200–1 900 s — straddling
# the old 1 500 s wall, the same kill #416 ended arriving at the other dial. Standard now carries
# the deep tier's 45-minute wall; the deep walls are clear against their own §3b‴ readings
# (1 214 s, 1 676 s, 2 351 s against 2 700 s, 2 700 s, 3 600 s) and are unchanged.
_BUILD_CEILINGS: dict[tuple[str, str], tuple[int, int]] = {
    ("standard", "low"): (45 * _MIN, 160),
    ("standard", "medium"): (45 * _MIN, 160),
    ("deep", "medium"): (45 * _MIN, 200),
    ("deep", "high"): (45 * _MIN, 200),
    ("deep", "extra"): (60 * _MIN, 300),
}
_BUILD_DEFAULT: dict[str, tuple[int, int]] = {
    "standard": (45 * _MIN, 160),
    "deep": (45 * _MIN, 200),
}

# #570 keeps a hard attempt cap, but lets a Build's detached child renew only a
# short silent-inactivity lease when it observes durable implementation progress.
# Revise deliberately continues to use the fixed ceilings above: Build is the only
# stage that owns this child-local policy.
_BUILD_LEASES: dict[tuple[str, str], tuple[int, int, int]] = {
    ("standard", "low"): (15 * _MIN, 45 * _MIN, 2 * 60 * _MIN),
    ("standard", "medium"): (15 * _MIN, 45 * _MIN, 2 * 60 * _MIN),
    ("deep", "medium"): (20 * _MIN, 60 * _MIN, 3 * 60 * _MIN),
    ("deep", "high"): (20 * _MIN, 60 * _MIN, 3 * 60 * _MIN),
    ("deep", "extra"): (30 * _MIN, 75 * _MIN, 4 * 60 * _MIN),
}
_BUILD_LEASE_DEFAULT: dict[str, tuple[int, int, int]] = {
    "standard": (15 * _MIN, 45 * _MIN, 2 * 60 * _MIN),
    "deep": (20 * _MIN, 60 * _MIN, 3 * 60 * _MIN),
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
    build_lease: tuple[int, int, int] | None = None  # silent, test grace, immutable cap

    @property
    def read_only(self) -> bool:
        return self.allowed_tools is not None


def _build_ceiling(complexity: str | None, effort: str | None) -> tuple[int, int]:
    key = (complexity or "deep", effort or "")
    if key in _BUILD_CEILINGS:
        return _BUILD_CEILINGS[key]
    return _BUILD_DEFAULT.get(complexity or "deep", _DEFAULT_CEILING)


def _build_lease(complexity: str | None, effort: str | None) -> tuple[int, int, int]:
    key = (complexity or "deep", effort or "")
    return _BUILD_LEASES.get(key, _BUILD_LEASE_DEFAULT.get(complexity or "deep",
                                                            _BUILD_LEASE_DEFAULT["deep"]))


def profile_for_facts(stage: str, complexity: str | None = None,
                      effort: str | None = None,
                      builder_complexity: str | None = None) -> StageProfile:
    """Resolve one production profile without requiring a coordinator Record."""
    if stage == "build":
        _wall, turns = _build_ceiling(complexity, effort)
        lease = _build_lease(complexity, effort)
        return StageProfile(None, lease[2], turns, "low", lease)
    if stage == "revise":
        wall, turns = _build_ceiling(builder_complexity or complexity, effort)
        return StageProfile(None, wall, turns, "low")
    wall, turns = _STAGE_CEILINGS.get(stage, _DEFAULT_CEILING)
    return StageProfile(_READ_ONLY_TOOLS.get(stage), wall, turns)


def profile_for(record) -> StageProfile:
    """Resolve the session profile for a record from its ``(stage, complexity, effort)``.

    Build sizes its ceiling on its own complexity/effort; Revise inherits the original
    builder's Build ceiling through ``builder_complexity`` (ADR 0041); every other stage reads
    the per-stage table. Read-only stages carry a read/search allowlist; the rest keep the full
    surface (``allowed_tools is None``). Both session leads run at low reasoning; their worker
    reasoning rung is prompt-level routing policy, not a provider flag on the parent.
    """
    return profile_for_facts(
        record.stage, record.complexity, record.effort, record.builder_complexity)
