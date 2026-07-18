"""The reviewed five-permit admission matrix (ADR 0029), owned once.

This is the single source of truth for how many of a pool's five permits one provider
attempt reserves. The coordinator turns these demands, together with the independent
gates, into an atomic reservation on the running-record ledger. The numbers are
production configuration: a change requires a reviewed PR with updated evidence, not a
runtime override (ADR 0029). Tests may inject alternative maps, but never weaken these.

The matrix is pure data plus one lookup, so it is exercised directly without any
coordinator, store, or provider (see tests/test_coordinator_admission.py).
"""

from __future__ import annotations

from types import MappingProxyType

# Each pool has an independent five-permit budget; a session reserves its whole demand
# atomically. Every stage gets one initial attempt plus at most two continuations.
PERMIT_BUDGET = 5
ATTEMPT_BUDGET = 3

# Stages that own a branch/worktree. Their tool lineage is pinned across continuations
# and they cannot silently move to the other pool (ADR 0028).
CODE_WRITING = frozenset({"build", "revise", "mockup", "respond"})

# The one durable human handoff each stage creates when its budget is exhausted or a
# permanent condition holds it (ADR 0028's exhaustion table).
STAGE_NATIVE_HANDOFF = {
    "intake": "issue:needs-grilling",
    "build": "issue:needs-grilling",
    "mockup": "issue:needs-mockup",
    "review": "pr:parked",
    "revise": "pr:parked",
    "respond": "pr:parked",
    "converse": "ask:needs-you",
    # Research holds by releasing its shared claim so the ticket is simply eligible again next
    # cycle (ADR 0037) — there is no operator-facing park; the release itself is the handoff.
    "research": "ticket:claim-released",
}

# Legacy orchestration labels normalize to a logical stage before lookup so the ambiguous
# display lanes can never turn Revise into Build or Mockup into Intake (ADR 0029/0030).
_STAGE_ALIASES = {
    "triage": "intake",
    "triaging": "intake",
    "reviewing": "review",
    "building": "build",
}

# The exact reviewed rows from ADR 0029. Any known-pool row that is missing falls back to
# the full five permits (exclusive fallback); an unknown pool has no ledger to charge.
_ADMISSION_ROWS = {
    ("intake", "claude", "opus", "deep", None): 1,
    ("intake", "codex", "sol", "deep", None): 1,
    ("review", "claude", "opus", "deep", None): 1,
    ("review", "codex", "sol", "deep", None): 2,
    ("revise", "claude", "sonnet", "standard", None): 3,
    ("revise", "claude", "opus", "deep", None): 3,
    ("revise", "codex", "terra", "standard", None): 4,
    ("revise", "codex", "sol", "deep", None): 4,
    ("respond", "claude", "opus", "deep", None): 3,
    ("respond", "codex", "sol", "deep", None): 5,
    ("mockup", "claude", "opus", "deep", None): 5,
    ("mockup", "codex", "sol", "deep", None): 5,
    # A Conversation turn is a bounded methodology session (ADR 0034). ADR 0034 leaves the
    # converse admission calibration to the prototype; a modest two-permit footprint keeps
    # headroom so an interactive Ask turn admits ahead of background work without starving it.
    ("converse", "claude", "opus", "deep", None): 2,
    ("converse", "codex", "sol", "deep", None): 2,
    # An unattended research session is a bounded deep investigation (ADR 0037). It runs in its
    # own stage lane with a cap of one, so a modest two-permit footprint keeps pool headroom for
    # the builds the balancer is pacing rather than letting a single research run reserve a pool.
    ("research", "claude", "opus", "deep", None): 2,
    ("research", "codex", "sol", "deep", None): 2,
}
for _pool, _model, _complexity, _demands in (
    ("claude", "sonnet", "standard", (3, 4, 5, 5)),
    ("claude", "opus", "deep", (4, 4, 5, 5)),
    ("codex", "terra", "standard", (4, 5, 5, 5)),
    ("codex", "sol", "deep", (5, 5, 5, 5)),
):
    for _effort, _demand in zip(("low", "medium", "high", "extra"), _demands, strict=True):
        _ADMISSION_ROWS[("build", _pool, _model, _complexity, _effort)] = _demand
ADMISSION_MATRIX = MappingProxyType(_ADMISSION_ROWS)

# The concrete model each pool runs for a given complexity — a validation of the model the
# runner selected, not a second sizing dial (ADR 0029).
MODEL_FOR = MappingProxyType({
    ("claude", "deep"): "opus",
    ("claude", "standard"): "sonnet",
    ("codex", "deep"): "sol",
    ("codex", "standard"): "terra",
})


def normalize_stage(stage: str) -> str:
    """Map a legacy orchestration label to its logical stage. Idempotent for the six
    logical stages, so callers that already know their stage pass it straight through."""
    return _STAGE_ALIASES.get(stage, stage)


def admission_demand(stage, pool, model, complexity, effort=None):
    """The permits one attempt reserves on ``pool``. ``None`` means the pool itself is
    unknown, so there is no budget to charge and the attempt is inadmissible. A known pool
    with no exact row reserves all five (exclusive fallback)."""
    if pool not in {"claude", "codex"}:
        return None
    return ADMISSION_MATRIX.get(
        (normalize_stage(stage), pool, model, complexity, effort), PERMIT_BUDGET)
