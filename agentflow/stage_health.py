"""Whether each pipeline stage is still producing anything (#430).

The pre-publish attack spent 37 rounds on 15 drafts and published none of them, and every
one of those rounds is recorded as having finished cleanly — because for that stage
"finished cleanly" means the round captured an answer, not that a brief was ever published.
Nothing anywhere said so; a human noticed an idle fleet at 99% budget free and traced it
back. This module is the fact that would have said it.

The one public read is :func:`stage_health`: per stage, over its last
:data:`DROUGHT_WINDOW` finished attempts, how many produced the stage's own *stage outcome*
(CONTEXT.md: the durable fact a stage must produce before the pipeline can advance) and how
many sessions those attempts cost. The daemon publishes that on its snapshot; the operator
briefing decides which of them read as broken. Nothing here decides that, and nothing here
touches GitHub — every fact comes from the per-attempt records the coordinator already
writes for every session it spends.

Counting is deliberately conservative in one direction. An attempt still in flight is not
counted at all, so a quiet fleet under-reports rather than raising a false alarm — absence
has to be provable, which is the whole point of the signal.
"""

from __future__ import annotations

from agentflow.coordinator.admission import ATTEMPT_BUDGET
from agentflow.coordinator.attack_stage import decode_result
from agentflow.coordinator.store import default_store_path
from agentflow.coordinator.telemetry import read_attempts

#: How many finished attempts a stage is judged over. A stage with fewer than this many
#: finished attempts is not judged at all — one window is what makes a drought a fact rather
#: than noise on a fleet that has barely run.
DROUGHT_WINDOW = 10

# Each stage's own name, what it must produce before the pipeline can advance, and where the
# missing output pools. All three are the operator's words, not the engine's: a stage-drought
# row names a stage, so there is no GitHub object to open and the third column has to say
# where to go looking instead. Every "where" is unique — two broken stages must never read the
# same. A stage absent from this table is not named here in the operator's terms and so raises
# nothing; the projection never invents copy for a stage it cannot describe.
_STAGES = {
    "intake": ("triage", "routed an issue",
               "open issues still carrying no state label"),
    "attack": ("pre-publish attack", "published a hardened brief",
               "held drafts and ready-for-agent holdbacks"),
    "build": ("build", "opened a pull request",
              "issues marked ready-for-agent with no pull request behind them"),
    "review": ("review", "recorded a review verdict",
               "open PRs waiting on a verdict"),
    "revise": ("revise", "pushed a revision",
               "open PRs whose review findings are still standing"),
    "mockup": ("mockup", "committed a mockup",
               "issues held for a mockup"),
    "respond": ("respond", "posted a reply",
                "PRs carrying a comment of yours that was never answered"),
    "converse": ("converse", "appended a reply",
                 "conversations waiting on their next turn"),
    "research": ("research", "recorded findings",
                 "research tickets with no findings recorded against them"),
}


def _attack_published(sessions) -> bool:
    """Whether this attack attempt cleared its draft for publication.

    The attack stage is the reason this module exists. Its own verification proves only that
    the round captured a readable answer, which is true of a round that tore the draft apart
    and of one that never published anything — exactly the blindness #418 surfaced. What the
    stage must actually produce is a published brief, and a brief is published only when the
    round cleared the draft: no surviving objections, or objections that all carry their own
    fix and no fork. Anything unreadable stays uncleared, as it does everywhere else.
    """
    for session in sessions:
        try:
            result = decode_result(session.outcome)
        except (AttributeError, TypeError, ValueError):
            continue  # an outcome this build cannot read is not one that published anything
        if result.survived or result.remedied_only:
            return True
    return False


# Every other stage verifies exactly the artifact it must produce — an opened PR, a recorded
# verdict, a pushed revision — so its own durable verification is the production fact.
_PRODUCED = {"attack": _attack_published}


def _produced(stage: str, sessions) -> bool:
    rule = _PRODUCED.get(stage)
    return rule(sessions) if rule else any(s.verified for s in sessions)


def _finished(sessions) -> bool:
    """Whether this attempt is over, judged only from what its own sessions recorded.

    An attempt ends either by producing its outcome or by spending its whole continuation
    budget on failing to. An attempt that has done neither may still get another session, so
    it is not counted — a stage caught mid-run must never read as a stage producing nothing.
    """
    return (any(s.verified for s in sessions)
            or max(s.attempt for s in sessions) >= ATTEMPT_BUDGET)


def stage_health(store_path=None, *, window: int = DROUGHT_WINDOW) -> list[dict]:
    """Every pipeline stage that has finished attempts on record, with its production over the
    last ``window`` of them: the stage's own name, what it produces, how many finished attempts
    that window actually held, how many produced the stage outcome, and the sessions they cost.

    One attempt is one logical stage's whole run — its first session plus any continuation —
    so a stage that burned three sessions carrying one piece of work reads as one attempt that
    cost three sessions, never as three attempts. Ordered by stage name, so two stages in the
    same state always publish in the same order.
    """
    attempts: dict[str, list] = {}
    for session in read_attempts(store_path or default_store_path()):
        attempts.setdefault(session.identity, []).append(session)

    by_stage: dict[str, list[list]] = {}
    for sessions in attempts.values():
        sessions.sort(key=lambda s: (s.attempt, s.finalized_at))
        if _finished(sessions) and sessions[0].stage in _STAGES:
            by_stage.setdefault(sessions[0].stage, []).append(sessions)

    rows = []
    for stage, stage_attempts in by_stage.items():
        name, produces, check = _STAGES[stage]
        stage_attempts.sort(key=lambda sessions: sessions[-1].finalized_at)
        recent = stage_attempts[-window:]
        rows.append({
            "stage": name, "produces": produces, "check": check, "window": window,
            "attempts": len(recent),
            "produced": sum(1 for sessions in recent if _produced(stage, sessions)),
            "sessions_spent": sum(len(sessions) for sessions in recent),
        })
    return sorted(rows, key=lambda row: row["stage"])
