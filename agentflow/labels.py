"""The GitHub labels the pipeline reads and writes, and the claim they express.

Two kinds of vocabulary live here. The **dials** intake stamps on a build issue
(`agentflow:complexity:*`, `agentflow:effort:*`, `agentflow:mockup:*`) and the way a
stage decodes them; and the **claims** a stage takes before it runs — one label per lane,
stamped on the subject so a concurrent or next-cycle pass skips work an agent already
owns, and dropped again once the lane's own settlement is durable.

Both are shared: dispatch selects on them, the stage adapters prove them, and the
operator sees them on the issue. Keeping the strings in one module means a lane's claim
is named the same way wherever it is taken, proved, or released.
"""

from __future__ import annotations

import re

from agentflow import github
from agentflow.intake import STATE_LABELS
from agentflow.runner import Complexity, Effort, MockupScope

_COMPLEXITY_LABEL = re.compile(r"^agentflow:complexity:(standard|deep)$")
_EFFORT_LABEL = re.compile(r"^agentflow:effort:(low|medium|high|extra)$")
_MOCKUP_SCOPE_LABEL = re.compile(r"^agentflow:mockup:(local|surface)$")


# Intake stamps one label per dial, but a human can add a second by hand. Taking whichever came
# first makes the answer depend on the order a label listing happened to arrive in, so a duplicate
# is settled by rank instead: the most cautious stamp on the issue wins, whatever the order.
_COMPLEXITY_RANK = (Complexity.STANDARD, Complexity.DEEP)
_EFFORT_RANK = (Effort.LOW, Effort.MEDIUM, Effort.HIGH, Effort.EXTRA)


def complexity_from_labels(labels: list[str]) -> Complexity | None:
    """The issue's model-size dial from its `agentflow:complexity:*` label. Hard gate
    — no build without one (ADR 0018). Two stamps resolve to `deep`."""
    found = [Complexity(m.group(1)) for m in map(_COMPLEXITY_LABEL.match, labels) if m]
    return max(found, key=_COMPLEXITY_RANK.index) if found else None


def effort_from_labels(labels: list[str]) -> Effort:
    """The issue's effort dial from its `agentflow:effort:*` label; defaults to
    `medium` when absent (guidance, not a hard gate — ADR 0018). Two stamps resolve to
    the larger."""
    found = [Effort(m.group(1)) for m in map(_EFFORT_LABEL.match, labels) if m]
    return max(found, key=_EFFORT_RANK.index) if found else Effort.MEDIUM


def mockup_scope_from_labels(labels: list[str]) -> MockupScope:
    """The parked mockup issue's scope from its `agentflow:mockup:*` label; defaults to
    `local` when absent so a legacy park (or a dropped label) draws the safer, narrower
    round rather than reopening the whole surface (ADR 0048). Pure (test surface)."""
    for name in labels:
        m = _MOCKUP_SCOPE_LABEL.match(name)
        if m:
            return MockupScope(m.group(1))
    return MockupScope.LOCAL


BUILDING = "agentflow:building"   # dispatch claim — an agent is building this issue
TRIAGING = "agentflow:triaging"   # dispatch claim — a grounding session owns this issue
DRAWING = "agentflow:drawing-mockup"   # dispatch claim — a session is drawing this issue's variants
RESEARCH_TICKET = "wayfinder:research"   # the one AFK-able planning ticket type the daemon may run
RESOLVING = "wayfinder:resolving"        # shared claim — a session (human or daemon) owns this ticket
AWAITING_DISPOSITION = "wayfinder:awaiting-disposition"  # completed research needs operator judgment
# The opposite outcome to awaiting-disposition: an unattended research run ended without a ruling
# the contract accepts, so the ticket is handed back and never dispatched again.
RESEARCH_PARKED = "wayfinder:parked"

HELD_LABELS = {"agentflow:needs-grilling", "agentflow:needs-mockup"}

# Out of the intake queue: a resolved state label, a live triaging claim, or a live
# build/mockup claim. An issue already owned by a downstream lane (`agentflow:building`,
# `agentflow:drawing-mockup`) is mid-pipeline, not un-triaged, so intake never re-claims it —
# even after the reconciler strips its stale `triaging` label (#201).
TRIAGE_SKIP = set(STATE_LABELS) | {TRIAGING, BUILDING, DRAWING}

# The wayfinder type labels the daemon must never dispatch: grilling/prototype/task stay
# human-in-the-loop (ADR 0037). A ticket carrying any of these is refused even if it is also
# mislabeled `wayfinder:research`, so the AFK boundary never leaks.
WAYFINDER_NON_RESEARCH = frozenset({"wayfinder:grilling", "wayfinder:prototype", "wayfinder:task"})

# The produced-variants comment carries INTAKE_MARK ("agentflow intake") so `awaiting_recheck`
# reads it as OURS — never as a maintainer reply, which would self-trigger the resume path.
# MOCKUP_MARK additionally distinguishes it from intake's park/kickoff comment, so the produce
# phase draws exactly one round and never re-draws.
MOCKUP_MARK = "mockup variants"

# Each lane's claim label as GitHub shows it to a human looking at the issue. The description
# must stay true whether the owning record is queued or running (#159) — it names ownership,
# never a live process.
_CLAIM_LABELS = {
    BUILDING: ("fbca04", "An agent is building this issue"),
    TRIAGING: ("d4c5f9", "Intake ownership claim — prevents duplicate dispatch"),
    DRAWING: ("fef2c0", "A session is drawing mockup variants for this issue"),
    RESOLVING: ("5319e7", "A session is resolving this planning ticket"),
}


def claim(repo: str, n: int, label: str) -> bool:
    """Mark issue n as owned by this lane *before* its session runs, so a concurrent or
    next-cycle dispatch skips it (closes the no-label-yet window every lane has between
    selection and its own durable state). Ensures the label first and reports whether
    ownership was actually made visible; a stage refuses submission when it cannot
    establish this guard, and `wayfinder:resolving` is shared, so a human session and the
    daemon never both grab the same ticket."""
    colour, description = _CLAIM_LABELS[label]
    if not github.create_label(repo, label, colour, description):
        return False
    return github.add_label(repo, n, label)


def release(repo: str, n: int, label: str) -> bool:
    """Drop this lane's claim once its own outcome is durable (the state label, the posted
    round, the opened PR dedups from here) or the session ended. Only the owning finalizer
    releases it, so an unreadable coordinator store clears nothing.
    Returns durable proof that GitHub no longer carries the claim."""
    def claim_present() -> bool | None:
        labels = github.issue_labels(repo, n)
        if labels is None:
            return None
        return label in labels

    before = claim_present()
    if before is None:
        return False
    if not before:
        return True  # an earlier settlement released it before interruption
    if not github.remove_label(repo, n, label):
        return False
    return claim_present() is False
