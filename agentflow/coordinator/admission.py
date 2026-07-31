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

import os
from types import MappingProxyType

# Each pool has an independent five-permit budget; a session reserves its whole demand
# atomically. Every stage gets one initial attempt plus at most two continuations.
PERMIT_BUDGET = 5
ATTEMPT_BUDGET = 3

# The machine ceiling and the per-stage caps the composed admission gate evaluates alongside the
# permit budget (CONTEXT.md). They live here, with the rest of the admission vocabulary, rather
# than in the dispatch layer that used to name them: the gate is assembled in the pipeline, so
# reading them from dispatch made the pipeline defer that import to dodge an import cycle.
# They are limits, never counters — durable running rows remain the only permit ledger.
MACHINE_CEILING = int(os.environ.get("AGENTFLOW_MAX_SESSIONS", "4"))
TRIAGE_CONCURRENCY = int(os.environ.get("AGENTFLOW_TRIAGE_CONCURRENCY", "3"))
BUILD_CONCURRENCY = int(os.environ.get("AGENTFLOW_BUILD_CONCURRENCY", "2"))
STAGE_CAPS = MappingProxyType({
    "triage": TRIAGE_CONCURRENCY,
    "build": BUILD_CONCURRENCY,
    "mockup": 1,
    "respond": 1,
    "research": 1,
})

# Stages that own a branch/worktree. Their tool lineage is pinned across continuations
# and they cannot silently move to the other pool (ADR 0028).
CODE_WRITING = frozenset({"build", "revise", "mockup", "respond"})
# Review's bounded fixes make it lineage-pinned after launch without changing the reviewed
# admission weights above: it stays a short deep stage, not a Build-sized reservation.
LINEAGE_PINNED = CODE_WRITING | {"review"}

# Stages whose subject is an open PR: getting one over the finish line is the #1
# priority, so they drain ahead of issue-bound work (build/mockup/intake) at admission
# (ADR 0039). The tier is a pure function of the stage name — no per-record state.
PR_BOUND = frozenset({"review", "revise", "respond"})

# Stages whose subject is an issue with no PR yet — they create new work. At the reservation
# gate one of these must defer while any PR-bound stage is waiting to start on the same pool,
# so a single high-effort build cannot seize all five permits and starve a review that needs
# one (#293, ADR 0039). Converse/research run in their own capped lanes and are out of scope.
ISSUE_BOUND = frozenset({"build", "mockup", "intake", "attack"})

# The one durable human handoff each stage creates when its budget is exhausted or a
# permanent condition holds it (ADR 0028's exhaustion table).
STAGE_NATIVE_HANDOFF = {
    "intake": "issue:needs-grilling",
    # An attacker that ran out of budget never read the draft, so it has nothing to say about it
    # — and an unattacked draft is never published. The draft goes to the maintainer instead,
    # through intake's own grilling route, so the plan is not lost (ADR 380).
    "attack": "issue:needs-grilling",
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
    # An attack is intake-shaped: one bounded read-only pass over one draft, on either pool
    # (ADR 380). It carries intake's single permit and shares its lane cap — the rounds are
    # triage, so they contend with triage rather than with the build queue. Unlike intake it has
    # standard rows too: the attack runs at the *draft's* complexity dial, so a standard brief is
    # argued with by the standard tier rather than spending deep-model headroom on it.
    ("attack", "claude", "opus", "deep", None): 1,
    ("attack", "claude", "sonnet", "standard", None): 1,
    ("attack", "codex", "sol", "deep", None): 1,
    ("attack", "codex", "terra", "standard", None): 1,
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


def native_handoff_proof(record) -> str | None:
    """The durable proof of a stage-native handoff nobody wrote a richer one for.

    The kind comes from the table above, so a stage adapter with no handoff collaborator, the
    coordinator's own fallback, and the ``handoff_kind`` stamped on the record can never name
    different handoffs for the same stage. An unrecognized stage has no handoff to prove, so it
    withholds the proof and the hold is retried rather than recorded as done."""
    kind = STAGE_NATIVE_HANDOFF.get(record.stage)
    return f"proof:{record.identity}:{kind}" if kind else None


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
