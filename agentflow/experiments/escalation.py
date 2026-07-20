"""The reusable standard-first escalation design (Wayfinder #2, #228).

Two load-bearing pieces the winning arm (C) would need in production:

- **Deterministic direct-deep triggers** — a pure function over the *pinned issue snapshot*
  that decides, before any model runs, whether an issue should skip the standard attempt and
  go straight to deep. Same input → same enumerated decision, so the escalation policy is
  auditable and testable without a provider.
- **Bounded evidence carry-forward** — when a standard attempt escalates, the deep attempt is
  handed a *bounded* digest of what the standard attempt already grounded plus its tentative
  decision, so it refines rather than restarts. Escalation must NEVER be a blind resubmission
  of the original prompt; :func:`escalation_prompt` refuses to build one without typed reasons
  and real evidence.

Both stay strictly typed against :class:`agentflow.intake.EscalationReason` — a free-text
reason is a charter-level regression (#228 key interfaces).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentflow.intake import EscalationReason, IntakeResult, IntakeRoute

# --- Deterministic direct-deep triggers -------------------------------------------------
#
# Auditable keyword sets. A direct-deep trigger fires from the issue *as filed* (title, body,
# labels) — never from a model's judgment — so it is reproducible. The sets are intentionally
# conservative: they catch correctness-sensitive / design-heavy work that the historical
# all-deep policy would have sized deep anyway, so a standard attempt on it would likely be
# wasted spend followed by an escalation.

# Correctness-sensitive / design-heavy vocabulary → COMPLEXITY_SIGNALS.
_COMPLEXITY_TERMS = (
    "concurren", "race condition", "deadlock", "atomic", "idempoten", "invariant",
    "migration", "schema change", "security", "credential", "auth", "authentication",
    "authorization", "payment", "billing", "money", "medical", "phi", "merge machinery",
    "coordinator", "state machine", "correctness", "data loss", "rollback", "transaction",
)

# An issue that argues against a recorded decision needs the deep tier to weigh it (ADR
# contradiction is a grill/deep signal). Matches an ADR citation like "ADR 0018".
_ADR_RE = re.compile(r"\badr[\s-]*\d{2,4}\b", re.IGNORECASE)

# A body long enough that a standard attempt would likely under-ground it. Chosen as a round,
# auditable threshold — recorded in the finding, not tuned against outcomes.
_LONG_BODY_CHARS = 6000


def _haystack(issue: dict) -> str:
    return f"{issue.get('title', '')}\n{issue.get('body') or ''}".lower()


def direct_deep_triggers(issue: dict) -> frozenset[EscalationReason]:
    """The enumerated reasons this issue should go **straight to deep**, skipping the standard
    attempt — decided deterministically from the pinned snapshot. Pure; same input → same set.

    An empty set means "run the standard attempt first". A non-empty set means arm C spends a
    single deep attempt directly and never a standard one, tagging that deep attempt with these
    reasons. This is the *direct* half of the trigger design; the other half is a standard
    attempt escalating on its own typed signal.
    """
    text = _haystack(issue)
    reasons: set[EscalationReason] = set()
    if any(term in text for term in _COMPLEXITY_TERMS):
        reasons.add(EscalationReason.COMPLEXITY_SIGNALS)
    if _ADR_RE.search(text):
        reasons.add(EscalationReason.UNRESOLVED_FORK)
    if len(issue.get("body") or "") >= _LONG_BODY_CHARS:
        reasons.add(EscalationReason.EVIDENCE_GAP)
    # Deterministic order: the enum's own declaration order.
    return frozenset(reasons)


# --- Bounded evidence carry-forward -----------------------------------------------------

# Hard caps on the carry-forward so the deep attempt refines rather than re-ingests the world.
# These bounds are what keep escalation from degenerating into "resend everything".
_MAX_DIGEST_CHARS = 1200
_MAX_QUESTION_CHARS = 400


@dataclass(frozen=True, slots=True)
class EscalationEvidence:
    """The bounded hand-off from a standard attempt to its deep step-up. Every text field is
    length-capped at construction so the payload can never grow into a second full prompt.

    This is a *distinct channel* from ``intake_prompt(extra=...)``, which carries a maintainer's
    reply — overloading that path would be a #228 trap. This one carries the model's own prior
    grounding, not a human's."""

    reasons: tuple[EscalationReason, ...]
    tentative_route: str = ""            # the standard attempt's best-guess route
    tentative_complexity: str = ""       # its tentative complexity dial, if it offered one
    evidence_digest: str = ""            # bounded summary of what it already grounded
    open_question: str = ""              # bounded statement of what it could not settle

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(self.reasons))
        object.__setattr__(self, "evidence_digest",
                           _clip(self.evidence_digest, _MAX_DIGEST_CHARS))
        object.__setattr__(self, "open_question",
                           _clip(self.open_question, _MAX_QUESTION_CHARS))

    @property
    def is_actionable(self) -> bool:
        """True only when the deep attempt has something concrete to refine — at least one
        typed reason and some prior grounding. A non-actionable hand-off would make escalation
        a blind rerun, which the prompt builder refuses."""
        return bool(self.reasons) and bool(self.evidence_digest.strip())


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def evidence_from_result(result: IntakeResult, *, digest: str = "",
                         open_question: str = "") -> EscalationEvidence:
    """Build the bounded carry-forward from a standard attempt's parsed result plus the
    grounding text it emitted. The typed reasons come off the result itself; the digest and
    open question are bounded here."""
    return EscalationEvidence(
        reasons=result.escalation_reasons,
        tentative_route=(result.route.value if result.route is not IntakeRoute.ESCALATE else ""),
        tentative_complexity=(result.complexity.value if result.complexity else ""),
        evidence_digest=digest or result.body,
        open_question=open_question or result.detail)


_CARRY_FORWARD_HEADER = "PRIOR STANDARD ATTEMPT (refine — do not restart)"


def escalation_prompt(base_prompt: str, evidence: EscalationEvidence) -> str:
    """The deep attempt's prompt: the durable Intake prompt PLUS one bounded carry-forward
    block. Never a verbatim resubmission — the block always makes the result differ from
    ``base_prompt``, and the deep model is told to *refine* the tentative decision, not
    re-derive it from scratch.

    Refuses (``ValueError``) to build a prompt from a non-actionable hand-off: with no typed
    reason or no prior evidence there is nothing to refine, and appending an empty block would
    be a blind rerun of the original — exactly the degeneration #228 forbids.
    """
    if not evidence.is_actionable:
        raise ValueError(
            "cannot escalate without bounded prior evidence: an escalation with no typed "
            "reason or no grounding digest would be a blind rerun of the original prompt")
    reasons = ", ".join(r.value for r in evidence.reasons)
    lines = [
        base_prompt,
        "",
        f"--- {_CARRY_FORWARD_HEADER} ---",
        "A standard-tier intake already grounded this issue and stepped up to you because:",
        f"  {reasons}",
    ]
    if evidence.tentative_route or evidence.tentative_complexity:
        lines.append(
            "Its tentative decision: "
            f"route={evidence.tentative_route or 'unsure'}, "
            f"complexity={evidence.tentative_complexity or 'unsure'}")
    lines += [
        "Bounded evidence it already gathered (refine from this, do not re-ground from zero):",
        evidence.evidence_digest,
    ]
    if evidence.open_question:
        lines += ["The open question it could not settle:", evidence.open_question]
    lines += [
        "Confirm or correct that tentative decision using this evidence plus your own deeper "
        "grounding, then emit the final decision. Do not restate the original issue back to "
        "yourself.",
        "---",
    ]
    return "\n".join(lines)
