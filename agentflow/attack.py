"""The attack — one cold session trying to break a draft brief before it is ever published
(ADR 380).

Intake used to ground an issue and publish its brief in one move: whatever the grounding session
talked itself into became the plan a builder was spent on. Nothing ever argued with it, so a
confidently wrong brief cost a full build, a review, and a revise round before anyone noticed.

Triage now drafts instead of publishing, and hands the draft to an **attacker**: a fresh session
that carries nothing from the session that wrote it and is asked to break the plan, not to admire
it. Triage answers the objections, redrafts, and is attacked again; only the draft that survives
its rounds is published as the ready brief. By the time an issue is ready-for-agent it has already
been argued with, so the cold session that picks it up is the *builder* — nothing downstream
reopens the plan.

This module owns the attacker's rubric prompt and its objections, and nothing else. The rounds
themselves are the intake↔attack record chain in :mod:`agentflow.coordinated_attack`.

Parsing is fail-safe, but its safe direction is not intake's. An objection we cannot read is not
an objection: a session that returned nothing readable did not clear the draft, so an unreadable
verdict spends a round *without* being mistaken for a clean bill of health (:func:`parse_attack`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from agentflow.intake import _fill
from agentflow.shell_crib import SHELL_CRIB

# How many times one draft may be attacked before the argument goes to the maintainer instead.
# Scaled by the complexity dial the draft itself carries — the dial triage already stamps *is*
# the classifier for how much adversarial intensity the ask deserves (ADR 380): a standard brief
# gets one cold read, a deep one earns up to three. Bounded because an unbounded argument is a
# way to never ship: each round costs a real session, and a plan that is still contested after
# three cold attackers is not going to be settled by a fourth — that is a maintainer's call.
_ROUNDS_BY_COMPLEXITY = {"standard": 1, "deep": 3}
MAX_ATTACK_ROUNDS = max(_ROUNDS_BY_COMPLEXITY.values())


def max_rounds(complexity) -> int:
    """The attack-round cap for one draft, from its own complexity dial. Pure (test surface).

    A draft carrying no dial gets the deep cap: the dial is missing exactly when triage was least
    sure of its sizing, which is when the plan deserves more scrutiny, not less.
    """
    key = getattr(complexity, "value", complexity)
    return _ROUNDS_BY_COMPLEXITY.get(key, MAX_ATTACK_ROUNDS)

# The provider-neutral shape the attacker's terminal answer must match. Each runner adapter
# translates it into that CLI's native structured-output surface, so the parser validates a real
# object rather than scavenging JSON out of reasoning prose — the same contract intake uses.
ATTACK_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "objections": {"type": "string"},
    },
    "required": ["objections"],
}


@dataclass(frozen=True, slots=True)
class AttackResult:
    objections: str = ""    # the numbered objections; empty means the draft survived this round
    parsed: bool = True     # False when the fail-safe produced this rather than the attacker
    detail: str = ""

    @property
    def survived(self) -> bool:
        """Whether this round actually cleared the draft.

        Only a *readable* empty objection list clears it. An unreadable answer means nobody
        attacked the draft, which is the one thing that must never be mistaken for nobody
        finding anything wrong with it.
        """
        return self.parsed and not self.objections


def parse_attack(payload: str) -> AttackResult:
    """Validate an attack session's structured answer. Pure, fail-safe (test surface).

    The CLI enforces :data:`ATTACK_RESULT_SCHEMA` natively, so the payload is the answer object
    itself. Anything unreadable — a non-object, a missing field, an exception — returns an
    unparsed result, which spends the round but never counts as the draft surviving. Never raises.
    """
    try:
        data = json.loads(payload.strip() or "null")
        if not isinstance(data, dict):
            return AttackResult(parsed=False, detail="attack output was not a structured object")
        if "objections" not in data:
            return AttackResult(parsed=False, detail="attack answer carried no objections field")
        return AttackResult(str(data.get("objections") or "").strip())
    except Exception as e:  # noqa: BLE001 — fail-safe, never propagate
        return AttackResult(parsed=False, detail=f"parse error: {type(e).__name__}")


ATTACK_PROMPT = """You are attacking a draft plan for {repo} issue #{n}. Another session read this
repository and drafted the plan below; you have no memory of that session and owe its conclusions
nothing. It has NOT been published — nobody has acted on it, and your job is to break it now,
while breaking it is free.

This is attack round {round} of at most {max_rounds}. You are READ-ONLY: never edit the issue,
never post to GitHub, never touch the code. Your entire output is the structured answer described
at the end. The session that wrote this draft will get your objections and answer them.

The draft plan:
Title: {title}
---
{body}
---

If the draft carries an `## Answered objections` section, that is the drafter standing its ground
against an earlier attacker. You never see those earlier rounds — the draft in front of you is the
whole argument — so judge each answered objection on its written evidence like any other claim:
re-file it only if the answer does not hold against the code.

OPEN THE FILES. An attacker that only reads the draft has failed — the draft is exactly the
artifact you cannot trust. Read the actual code in this checkout before you object to anything,
and especially before you accept anything.

ATTACK ON FIVE AXES. Judge every one:

1. **Grounding** — every load-bearing factual claim, especially anything under the draft's
   Verified section, checked by opening the named files. A claim you cannot confirm is an
   objection even if it sounds right.
2. **Acceptance** — the criteria are observable from outside, and green genuinely means done,
   with no unstated work smuggled in and no criterion that passes on a broken build.
3. **Interface shape** — the proposed front door judged against the engineering charter:
   interface far simpler than implementation, the deletion test on any new module, no seam built
   before its second caller. A draft that says nothing about interface shape is itself an
   objection.
4. **Scope and complexity budget** — out-of-scope is explicit; an edge case earns handling only
   if it is reachable from the inputs the acceptance criteria describe; speculative hardening in
   a plan is a defect.
5. **Cost** — the effort dial and the implied blast radius are proportionate to the ask.

WRITING OBJECTIONS. Number them. Each one carries three things:
- the **evidence** — what you opened and what you found there,
- **why it breaks the build if unfixed**,
- the **cheapest fix** that would settle it.

TASTE IS NOT AN OBJECTION. "I would have designed it differently" is not a finding. Object only
where the plan is wrong, unverifiable, or would produce a build its own acceptance criteria cannot
judge. Do not manufacture an objection to look thorough: an EMPTY objection list is a SUCCESSFUL
attack on a draft that deserved to survive, and saying so is how a good plan gets built today
instead of next round. Equally, do not go easy on a draft because it reads well — a fluent plan
built on a claim that isn't true is exactly what you are here to catch.

Your final response IS the structured answer — the harness enforces its schema natively, so you
do not hand-write or fence the JSON; just produce this field:
- "objections": the numbered Markdown objections, or the empty string if the draft survives""" \
    + SHELL_CRIB


def attack_prompt(repo: str, number: int, title: str, body: str, *,
                  round: int = 1, max_rounds: int = MAX_ATTACK_ROUNDS) -> str:
    """The durable provider input for one attack round.

    Deliberately carries no history of earlier rounds: an attacker reads the newest draft and
    nothing else, so every round is genuinely cold. What earlier attackers forced the drafter to
    settle survives *inside* the draft — fixed, or defended under `## Answered objections` — which
    is the only place a settlement can live if the next cold reader is to weigh it on evidence
    rather than on deference (ADR 380).
    """
    return _fill(ATTACK_PROMPT, repo=repo, n=str(number), title=title,
                 body=body.strip() or "(no draft body)", round=str(round),
                 max_rounds=str(max_rounds))


def hardening_note(rounds: int) -> str:
    """The one line the published brief carries about the argument behind it. Pure (test surface).

    The maintainer reads the brief, not our records, so what the rounds cost has to be visible in
    the brief's own comment or it may as well not have happened. Only a draft that ran out of
    objections is ever published — one that ran out of *rounds* goes to the maintainer instead
    (:func:`agentflow.coordinated_attack.hold_contested`) — so this note has exactly one story to
    tell.
    """
    if rounds <= 0:
        return ""
    times = "once" if rounds == 1 else ("twice" if rounds == 2 else f"{rounds} times")
    return (f"Before posting this I had it torn into {times} by fresh sessions that hadn't "
            "seen how it was written — the last one had nothing left to object to.")
