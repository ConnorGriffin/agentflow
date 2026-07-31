"""Recovery classification for an ended attempt with no verified outcome (issue #225).

When a stage attempt ends without producing its required outcome and no capacity/server
interruption explains the ending, a *fresh* provider session is only worth spending when it has
new state to act on. Replaying the same durable prompt in a fresh session with no new evidence,
retained work, or changed input is waste, not resilience.

The stage adapter — which owns its worktree and its notion of a required outcome — classifies
what a fresh attempt would have to work with; the coordinator turns that classification into a
bounded continuation, a single targeted repair, or a park. The bounded ``envelope`` carries only
durable facts (the attempt number, the missing outcome, and any retained worktree) into the fresh
session so it resumes rather than restarts; the prior event stream is never injected.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentflow.coordinator.admission import ATTEMPT_BUDGET

# The three kinds of new state a fresh attempt might have to act on.
PROGRESS = "progress"   # durable partial work or a changed input — continue within the budget
REPAIR = "repair"       # a clean exit that only left its outcome missing — one targeted repair
NO_NEW_STATE = "none"   # nothing new — a fresh session would replay an identical prompt


@dataclass(frozen=True)
class Recovery:
    """What a fresh attempt of an ended stage would have new to act on, and the bounded facts to
    hand it. A stage with no classification defaults to ``progress`` so the historical
    continue-within-budget behavior is preserved until a stage opts in."""

    kind: str = PROGRESS
    envelope: str = ""


def _envelope(record, missing: str, *, worktree: str | None = None) -> str:
    """Bounded durable facts for a fresh session — never a transcript of the prior attempt."""
    lines = [
        "Recovery context for this session (durable facts only, not a transcript):",
        f"- This is a continuation; {record.attempts} of {ATTEMPT_BUDGET} attempts have run.",
        f"- The prior attempt ended without the required outcome: {missing}.",
    ]
    miss = getattr(record, "verify_miss", "")
    if miss:
        lines.append(f"- The verifier's exact failed check was — {miss}. Satisfy that check; "
                     "work behind it may already be durable and must not be redone.")
    if worktree:
        lines.append(
            f"- Your work in progress is retained at {worktree}; build on it, do not restart.")
    lines.append("- Produce the missing outcome; do not repeat work already completed.")
    return "\n".join(lines)


def targeted_repair(record, missing: str) -> Recovery:
    """A read-only stage that exited cleanly without its required outcome owns no durable partial
    work, so a fresh full replay would be identical. Grant exactly one targeted repair naming the
    exact missing proof; the coordinator stops any further identical replay (issue #225)."""
    return Recovery(REPAIR, _envelope(record, missing))


def contract_repair(record, error: str) -> Recovery:
    """The attempt finished its actual work and only stated the outcome in a shape the contract
    rejects. Re-running the work would repeat every external call to reach an answer already
    reached, so grant exactly one repair turn that names the parser's own error and asks for
    nothing but a corrected statement of that same outcome (issue #332)."""
    lines = [
        "Recovery context for this session (durable facts only, not a transcript):",
        f"- This is a continuation; {record.attempts} of {ATTEMPT_BUDGET} attempts have run.",
        "- The prior attempt completed its work but stated the outcome in a rejected shape:",
        f"  {error}.",
    ]
    if record.source:
        lines.append(f"- That work is retained at {record.source}; it is done — do not redo it.")
    lines.append(
        "- Correct only the outcome statement. Do not re-run the review, re-read the diff, or "
        "make any further GitHub calls — restate the same conclusion in a valid shape.")
    return Recovery(REPAIR, "\n".join(lines))


def durable_progress(record, missing: str) -> Recovery:
    """A worktree-owning stage carries its partial work forward across attempts, so a continuation
    is genuinely new state — not an identical replay — as long as it resumes that work. Continue
    within the attempt budget, but hand the fresh session a bounded envelope pointing it at the
    retained worktree instead of re-running the same prompt from scratch (issue #225)."""
    return Recovery(PROGRESS, _envelope(record, missing, worktree=record.source))
