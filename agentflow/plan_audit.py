"""The plan audit — one cold adversarial read of a build brief before anything is built
(ADR 380).

Intake grounds an issue and routes it `ready-for-agent`; until now the next dispatch cycle
picked it straight up and started building. Nothing between the two ever re-read the brief and
asked whether the *plan* was right — only whether the paperwork matched the decision. A
confidently wrong brief therefore cost a full build, a review, and a revise round before anyone
noticed.

This module owns the audit's rubric prompt and its verdict, and nothing else. A fresh session
with no memory of the triage that wrote the brief re-reads it against the actual repository and
returns exactly one of two answers:

- **countersign** — the brief survives; the issue joins the build queue as it always did.
- **bounce** — the brief does not survive; the issue leaves the build queue and becomes a held,
  needs-grilling issue whose comment is the auditor's numbered objections.

A bounce is projected through intake's own grilling route (:func:`bounce_result`), which is what
makes the maintainer's reply re-run triage for free — the second adversarial cycle costs nothing
to build. Because that comment must be recognizable as *ours*, the bounce body carries intake's
disclaimer line: the held-issue sweep decides "the maintainer answered" by looking for that
marker, so objections posted without it would read as a maintainer reply and re-trigger triage in
a loop with nobody in it.

Parsing is **fail-safe in the same direction intake already fails**: anything we cannot read as a
confident countersign bounces. An empty objection list is a successful audit, not a lazy one — but
an *unreadable* verdict is not an audit at all, and never countersigns.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

from agentflow.intake import COUNTERSIGNED, IntakeResult, IntakeRoute, _DISCLAIMER, _fill
from agentflow.shell_crib import SHELL_CRIB

# The provider-neutral shape the audit's terminal decision must match. Each runner adapter
# translates it into that CLI's native structured-output surface, so the parser validates a real
# object rather than scavenging JSON out of reasoning prose — the same contract intake uses.
PLAN_AUDIT_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "verdict": {"type": "string", "enum": ["countersign", "bounce"]},
        "objections": {"type": "string"},
    },
    "required": ["verdict", "objections"],
}


class PlanAuditVerdict(str, Enum):
    COUNTERSIGN = "countersign"  # the brief survived the audit — it may be built
    BOUNCE = "bounce"            # the brief did not — hold it for the maintainer


@dataclass(frozen=True, slots=True)
class PlanAuditResult:
    verdict: PlanAuditVerdict
    objections: str = ""   # the numbered objections to post on a bounce
    parsed: bool = True    # False when the fail-safe produced this rather than the auditor
    detail: str = ""


def _bounced(detail: str, objections: str) -> PlanAuditResult:
    return PlanAuditResult(PlanAuditVerdict.BOUNCE, objections, parsed=False, detail=detail)


# What we post when the audit produced no readable verdict at all. The maintainer still gets a
# real, actionable objection — "nobody checked this" is itself the finding — rather than an empty
# hold they cannot answer.
_UNREADABLE_OBJECTIONS = (
    "1. **This brief has not actually been audited.** *Evidence:* the audit session ended "
    "without returning a readable verdict, so not one claim in the brief was checked against "
    "the code. *Why it breaks the build if unfixed:* dispatching now would build an unaudited "
    "plan, which is exactly the failure this step exists to prevent. *Cheapest fix:* reply here "
    "and I'll re-scope and re-audit it.")

# What we post when the auditor said "bounce" but gave us nothing to act on. It still holds — a
# bounce is a bounce — but the maintainer is told the objections themselves went missing rather
# than being shown a blank list they cannot answer.
_EMPTY_OBJECTIONS = (
    "1. **The audit rejected this brief but its objections did not come back.** *Evidence:* the "
    "audit returned a bounce verdict with an empty objection list. *Why it breaks the build if "
    "unfixed:* something in the plan failed the audit and we cannot see what. *Cheapest fix:* "
    "reply here and I'll re-scope and re-audit it.")


def parse_plan_audit(payload: str) -> PlanAuditResult:
    """Validate an audit session's structured decision. Pure, fail-safe (test surface).

    The CLI enforces :data:`PLAN_AUDIT_RESULT_SCHEMA` natively, so the payload is the decision
    object itself. Only an explicit ``countersign`` countersigns; every other outcome — an
    unreadable payload, a missing or unknown verdict, a bounce with no objections, or an
    exception — bounces, carrying objections the maintainer can actually answer. Never raises.
    """
    try:
        data = json.loads(payload.strip() or "null")
        if not isinstance(data, dict):
            return _bounced("audit output was not a structured decision object",
                            _UNREADABLE_OBJECTIONS)
        raw = str(data.get("verdict", "")).strip().lower()
        try:
            verdict = PlanAuditVerdict(raw)
        except ValueError:
            return _bounced("audit decision carried no valid verdict", _UNREADABLE_OBJECTIONS)
        objections = str(data.get("objections", "")).strip()
        if verdict is PlanAuditVerdict.COUNTERSIGN:
            # A countersign posts nothing, so whatever the auditor wrote alongside it is
            # discarded rather than kept as text with nowhere to go.
            return PlanAuditResult(verdict)
        if not objections:
            return _bounced("audit bounced with no objections", _EMPTY_OBJECTIONS)
        return PlanAuditResult(verdict, objections)
    except Exception as e:  # noqa: BLE001 — fail-safe, never propagate
        return _bounced(f"parse error: {type(e).__name__}", _UNREADABLE_OBJECTIONS)


_BOUNCE_PREFACE = (
    f"{_DISCLAIMER}\n\n"
    "Before building this I re-read the brief cold against the code, and it isn't ready to "
    "build yet. Here's what I'd need settled first:\n\n")

_BOUNCE_CLOSING = (
    "\n\nReply here with your answers — or run `/agentflow pickup` to drive it live — and I'll "
    "re-scope this and audit it again.")


def bounce_body(objections: str) -> str:
    """The comment one bounce posts. Pure (test surface).

    Opens with intake's disclaimer line, so the held-issue sweep reads this comment as **ours**
    and only a genuine maintainer reply re-runs triage. Without it the objections would look like
    a maintainer answer and triage would immediately re-run on its own words.
    """
    return f"{_BOUNCE_PREFACE}{objections.strip()}{_BOUNCE_CLOSING}"


def bounce_result(result: PlanAuditResult) -> IntakeResult:
    """A bounce expressed as intake's own grilling route. Pure (test surface).

    Reusing the route rather than inventing a second hold is the whole economy of the bounce:
    intake's projection already removes ``ready-for-agent``, applies ``agentflow:needs-grilling``,
    clears the dials, and posts the body as a comment — and the held-issue sweep already knows
    how to resume from it. The audit needs no new hold, no new label state, and no new
    notification path.

    The title is left empty so the projection never retitles the issue: the audit judges the
    plan, it does not rewrite it.
    """
    return IntakeResult(IntakeRoute.GRILL, bounce_body(result.objections), parsed=result.parsed)


# The countersign marker as GitHub shows it to a human looking at the issue.
COUNTERSIGN_COLOUR = "0e8a16"
COUNTERSIGN_DESCRIPTION = "A cold plan audit countersigned this brief — it may be built"


def countersigned(labels) -> bool:
    """Whether a plan audit has countersigned the brief currently on this issue. Pure.

    The marker is a managed prefixed label, so intake's projection clears it on any re-route:
    an issue bounced back to grilling and later promoted to ready again carries no stale
    countersign, and its *new* brief is audited fresh.
    """
    return COUNTERSIGNED in set(labels)


PLAN_AUDIT_PROMPT = """You are agentflow's plan audit for {repo} issue #{n}. An earlier session
grounded this issue and wrote the Agent Brief below; you have no memory of that session and owe
its conclusions nothing. Your job is to decide, cold, whether this plan should be built AT ALL —
before a builder spends a full session on it.

You return ONE of two verdicts: "countersign" (build it as written) or "bounce" (hold it for the
maintainer, with your objections). There is no third answer and no revision loop: you never
rewrite the brief, never edit the issue, never touch the code, and never post to GitHub. You are
READ-ONLY. The harness applies your verdict for you.

The brief as written:
Title: {title}
---
{body}
---

OPEN THE FILES. An auditor that only reads the brief has failed. Read the actual code in this
checkout before you judge anything — especially every factual claim the brief makes about it.

AUDIT RUBRIC — five axes. Judge every one:

1. **Grounding** — every load-bearing factual claim, especially anything in the brief's Verified
   section, is checked by opening the named files.
2. **Acceptance** — the criteria are observable from outside, and green genuinely means done,
   with no unstated work smuggled in.
3. **Interface shape** — the proposed front door judged against the engineering charter:
   interface far simpler than implementation, the deletion test on any new module, no seam built
   before its second caller. A brief that says nothing about interface shape is itself an
   objection.
4. **Scope and complexity budget** — out-of-scope is explicit; an edge case earns handling only
   if it's reachable from the inputs the acceptance criteria describe; speculative hardening in a
   plan is a defect.
5. **Cost** — the effort dial and the implied blast radius are proportionate to the ask.

WRITING OBJECTIONS. Number them. Each one carries three things:
- the **evidence** — what you opened and what you found there,
- **why it breaks the build if unfixed**,
- the **cheapest fix** that would settle it.

Write them in the maintainer's own domain terms — talk about the app's behavior, not about code
symbols, file paths, or ADR numbers. They are posted verbatim as a question the maintainer
answers, so each objection must be answerable by a human who has not read your session.

TASTE IS NOT AN OBJECTION. "I would have designed it differently" is not a finding. Object only
where the plan is wrong, unverifiable, or would produce a build the acceptance criteria cannot
judge. An EMPTY objection list is a SUCCESSFUL audit, not a lazy one — countersign a good brief
without hunting for something to say.

Your final response IS the verdict as a structured object — the harness enforces its schema
natively, so you do not hand-write or fence the JSON; just produce these fields:
- "verdict": "countersign" | "bounce"
- "objections": the numbered Markdown objections for a "bounce"; the empty string for a
  "countersign" (a countersign posts nothing, so anything written here is discarded)""" \
    + SHELL_CRIB


def plan_audit_prompt(repo: str, issue: dict) -> str:
    """The durable provider input for one plan audit stage."""
    return _fill(PLAN_AUDIT_PROMPT, repo=repo, n=str(issue["number"]),
                 title=issue.get("title", ""),
                 body=(issue.get("body") or "").strip() or "(no description)")
