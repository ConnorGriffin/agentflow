"""The hardening rounds behind the durable session coordinator (ADR 380).

Triage no longer publishes what it drafts. A draft that would have been posted as a ready brief
instead opens an **attack**: one cold session that never saw the drafting session, asked to break
the plan while breaking it is still free. Its objections go back to a *fresh* triage session that
answers them and redrafts, and that redraft is attacked again. Each attacker reads only the newest
draft — never the rounds behind it — so what a settlement is worth must be written into the draft
itself. The argument is over when nothing is left that a human has to decide: a draft the last
attacker had nothing to say about is published, and so is the redraft that answers a last round
whose objections all came with their own fix (ADR 418). What still needs the maintainer — a fork
the attacker named, or a round nobody could read — goes to them (:func:`hold_contested`), never to
a builder. Nothing downstream knows any of this happened.

The rounds are the same claim-transfer chain Build→Review→Revise already uses (ADR 0028): each
round's record hands the issue's ``triaging`` claim to the next round's record inside one
transaction, so the issue is never unowned mid-argument, a daemon that dies between rounds
resumes where it stopped, and a repeat submission is idempotent on the round's identity. The
attack borrows that claim rather than taking one of its own precisely *because* it is triage —
the issue is still being decided, which is what ``triaging`` already means.

This module owns the two submissions that make the chain (:func:`attack_submission`,
:func:`redraft_submission`), the one place a hardened brief reaches GitHub
(:func:`publish_brief`), and the attacker's read-only checkout. What an attacker is *asked* lives
in :mod:`agentflow.attack`; what a redraft is asked lives in :mod:`agentflow.intake`.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentflow import github
from agentflow.attack import AttackResult, attack_prompt, hardening_note, max_rounds
from agentflow.coordinator import Submission
from agentflow.coordinator.providers import PROVIDER_INPUT_V1
from agentflow.intake import (IntakeResult, IntakeRoute, apply_intake, intake_result_is_durable,
                              redraft_prompt)
from agentflow.labels import TRIAGING, release
from agentflow.runner import _run
from agentflow.worktree_ref import WorktreeKind, WorktreeRef


def _chain(record) -> dict | None:
    """One round's durable payload, or ``None`` if it cannot be read.

    Every round in the chain carries the same keys, whichever stage wrote it: the frozen issue
    ``snapshot`` and ``source_ref`` the whole argument is grounded in, the ``base_prompt`` the
    first draft came from, and the ``draft`` currently under attack. Deliberately *no* round
    history rides along: an attacker sees only the newest draft, so anything worth keeping from
    an earlier round has to have been written into that draft (ADR 380). Carrying the keys
    forward rather than re-reading GitHub is what keeps a replayed round byte-identical to the
    one it replaces.
    """
    try:
        payload = json.loads(record.input_ptr or "")
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or "snapshot" not in payload:
        return None
    return payload


def _draft(payload: dict) -> IntakeResult | None:
    """The draft this round is arguing about, decoded from the chain payload."""
    from agentflow.coordinator.intake_stage import decode_result
    try:
        return decode_result(payload["draft"])
    except (ValueError, KeyError, TypeError):
        return None


def _dial(draft: IntakeResult) -> str:
    """The complexity dial the attack rounds run at, from the draft's own sizing (ADR 380).

    The dial triage stamps is the classifier for how much adversarial intensity the ask
    deserves: it picks the attacker's model tier through the same admission table every other
    stage uses, and its round cap through :func:`agentflow.attack.max_rounds`. A draft carrying
    no dial attacks deep — missing sizing is a reason for more scrutiny, not less.
    """
    return draft.complexity.value if draft.complexity else "deep"


def attack_submission(intake_record, draft: IntakeResult, tool: str) -> Submission | None:
    """Open the cold attack on a triage draft, assuming its ``triaging`` claim (ADR 380).

    The round number joins the identity, so each attack is a genuinely new stage with its own
    budget and a repeat can never collide with the round before it. Pure: the mapping is the test
    surface (ADR 0020). ``None`` when the drafting round's durable input cannot be read.
    """
    from agentflow.coordinator.intake_stage import encode_result
    payload = _chain(intake_record)
    ref = WorktreeRef.parse(intake_record.source)
    if payload is None or ref is None:
        return None
    number = int(intake_record.subject)
    round = intake_record.round + 1
    # Round 0's payload is intake's own, which carries no `base_prompt` — its `prompt` *is* the
    # grounding prompt every later round re-grounds from, so that is where the chain's copy
    # starts.
    base_prompt = payload.get("base_prompt") or payload.get("prompt", "")
    snapshot = payload["snapshot"]
    prompt = attack_prompt(intake_record.repo, number,
                           draft.title or snapshot.get("title", ""), draft.body,
                           round=round, max_rounds=max_rounds(draft.complexity))
    return Submission(
        repo=intake_record.repo, subject=intake_record.subject, stage="attack",
        pool=tool, complexity=_dial(draft), round=round, claim=True,
        source=str(WorktreeRef.for_attack(ref.workdir, tool, number).path),
        input_ptr=json.dumps({"format": PROVIDER_INPUT_V1, "snapshot": snapshot,
                              "source_ref": payload["source_ref"], "prompt": prompt,
                              "base_prompt": base_prompt, "draft": encode_result(draft)},
                             sort_keys=True),
        transfer_from=intake_record.identity)


def renewed_attack_submission(attack_record, tool: str) -> Submission | None:
    """Re-open the attack on the same draft after a round whose answer was unreadable (ADR 380).

    An unreadable verdict spent its round without anyone actually reading the draft, and there is
    nothing for the drafter to answer — so the next round attacks the *same* draft rather than
    wasting a triage session redrafting against no objections. Pure; ``None`` when the spent
    round's durable input cannot be read.
    """
    payload = _chain(attack_record)
    ref = WorktreeRef.parse(attack_record.source)
    if payload is None or ref is None:
        return None
    draft = _draft(payload)
    if draft is None:
        return None
    number = int(attack_record.subject)
    round = attack_record.round + 1
    snapshot = payload["snapshot"]
    prompt = attack_prompt(attack_record.repo, number,
                           draft.title or snapshot.get("title", ""), draft.body,
                           round=round, max_rounds=max_rounds(draft.complexity))
    return Submission(
        repo=attack_record.repo, subject=attack_record.subject, stage="attack",
        pool=tool, complexity=_dial(draft), round=round, claim=True,
        source=str(WorktreeRef.for_attack(ref.workdir, tool, number).path),
        input_ptr=json.dumps({"format": PROVIDER_INPUT_V1, "snapshot": snapshot,
                              "source_ref": payload["source_ref"], "prompt": prompt,
                              "base_prompt": payload.get("base_prompt", ""),
                              "draft": payload["draft"]}, sort_keys=True),
        transfer_from=attack_record.identity)


def redraft_submission(attack_record, result: AttackResult, tool: str) -> Submission | None:
    """Open the triage round that answers an attacker, assuming its claim (ADR 380).

    A redraft is an ordinary Intake stage — same adapter, same schema, same read-only profile —
    carrying the completed attack rounds as its round number. Only its prompt differs, and only
    by the draft and the objections appended to the grounding prompt it re-grounds from. The
    redraft that answers the last round the cap allows is told it is the last one, since its
    answer is published rather than attacked (ADR 418). Pure; ``None`` when the attack round's
    durable input cannot be read.
    """
    payload = _chain(attack_record)
    ref = WorktreeRef.parse(attack_record.source)
    if payload is None or ref is None:
        return None
    draft = _draft(payload)
    if draft is None:
        return None
    number = int(attack_record.subject)
    snapshot = payload["snapshot"]
    base_prompt = payload.get("base_prompt", "")
    round = attack_record.round + 1
    cap = max_rounds(draft.complexity)
    prompt = redraft_prompt(base_prompt, draft.title or snapshot.get("title", ""), draft.body,
                            result.objections, round=round, max_rounds=cap, final=round > cap)
    return Submission(
        repo=attack_record.repo, subject=attack_record.subject, stage="intake",
        pool=tool, complexity="deep", round=attack_record.round, claim=True,
        source=str(WorktreeRef.for_intake(ref.workdir, tool, number).path),
        input_ptr=json.dumps({"format": PROVIDER_INPUT_V1, "snapshot": snapshot,
                              "source_ref": payload["source_ref"], "prompt": prompt,
                              "base_prompt": base_prompt}, sort_keys=True),
        transfer_from=attack_record.identity)


def reset_worktree(record) -> bool:
    """Discard and rebuild the attacker's read-only checkout from its durable source pointer."""
    from agentflow.runner import ClaudeRunner, CodexRunner
    if not record.source or not record.input_ptr:
        return False
    payload = _chain(record)
    if payload is None:
        return False
    source_ref = payload.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        return False
    ref = _attack_ref(record)
    if ref is None:
        return False
    wt = Path(ref.path)
    if wt.exists():
        _run(["git", "-C", ref.workdir, "worktree", "remove", "--force", str(wt)])
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    try:
        runner.prepare_worktree_detached(ref.workdir, source_ref, wt)
        runner.provision(wt)
    except subprocess.CalledProcessError:
        return False
    return True


def dispose_worktree(record) -> bool:
    """Remove the attacker's read-only checkout once its round is over, *before* the record
    retires — the same window :func:`agentflow.coordinated_intake.dispose_worktree` closes.
    Idempotent; returns whether the worktree is gone."""
    ref = _attack_ref(record)
    if ref is None:
        return False
    wt = Path(ref.path)
    if wt.exists():
        _run(["git", "-C", ref.workdir, "worktree", "remove", "--force", str(wt)])
    return not wt.exists()


def _attack_ref(record) -> WorktreeRef | None:
    """The record's source parsed as its own ``<pool>-attack/issue-<subject>`` checkout, or
    ``None`` if the source is absent, malformed, or belongs to a different subject/pool/kind.
    The kind check is what keeps an attack from ever operating on the drafting session's intake
    checkout of the same issue, which differs only by lane suffix (ADR 0041/380)."""
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.kind is not WorktreeKind.ATTACK \
            or ref.tool != record.pool or str(ref.number) != str(record.subject):
        return None
    return ref


def attack_claim_ready(record) -> bool:
    """Prove the durable attack record still owns GitHub's triaging claim before admission.

    It is triage's claim, transferred — an attack round is still the issue being decided, and
    the moment that claim is gone something else has taken the issue over.
    """
    labels = github.issue_labels(record.repo, int(record.subject))
    if labels is None:   # fail closed: a read that couldn't reach GitHub stays unknown
        return False
    return TRIAGING in labels


def publish_brief(record, draft: IntakeResult, hardening: str) -> str | None:
    """Write the hardened brief to GitHub, prove it landed, and release the triaging claim.

    This is the *only* place a brief is published (ADR 380). Everything before it is a draft
    nobody has been shown, which is what makes the argument free: a plan that never survives its
    attackers is never something the maintainer had to read, and never something a builder was
    spent on.

    The projection is intake's own — brief into the description, as-filed text preserved below,
    one conversational comment — against the *frozen* snapshot the argument started from, so a
    published brief matches the issue triage actually read. The hardening line rides inside that
    comment rather than as a second one, so the idempotence and durability proofs keep matching
    exactly one comment.
    """
    from dataclasses import replace
    payload = _chain(record)
    if payload is None:
        return None
    snapshot = payload["snapshot"]
    number = int(record.subject)
    source_title = snapshot.get("title", "")
    source_body = snapshot.get("body", "")
    result = replace(draft, hardening=hardening)
    # An unreadable live headline means GitHub couldn't be reached and nothing is published at
    # all; the caller retries rather than treating nothing-written as done.
    live = github.issue_headline(record.repo, number)
    if live is None:
        return None
    apply_intake(record.repo, number, live.title or source_title, sorted(live.labels), result,
                 source_title, source_body)
    if not intake_result_is_durable(record.repo, number, result, source_title, source_body):
        return None
    if not release(record.repo, number, TRIAGING):
        return None
    return f"https://github.com/{record.repo}/issues/{number}"


def apply_objections(record, result: AttackResult) -> str | None:
    """Settle one attack round: publish a cleared draft, hold a contested one out of rounds, or
    keep the claim for the next round.

    Returning ``None`` is not a failure here — it is how a round says the argument is still
    going. The record stays completed-and-unretired with the claim, and the next round's opener
    takes it from there (a redraft against readable objections, a renewed attack after an
    unreadable answer), exactly as a completed Build holds its claim until its Review exists.

    Running out of attackers is not itself an ending. What the last round said decides that
    (ADR 418): objections the drafter can answer get the same redraft every other round's get,
    and *that* redraft is published because no attacker is left to read it
    (:func:`publish_redrafted_brief`) — so this returns ``None`` and the argument continues one
    last step. Only what the drafter cannot answer — a genuine fork the attacker named, or an
    answer nobody could read — reaches the maintainer (:func:`hold_contested`). A builder spent
    on a plan whose open question was never settled is the waste this design exists to prevent;
    a builder spent on a plan whose last edit came with its own wording is not.
    """
    payload = _chain(record)
    if payload is None:
        return None
    draft = _draft(payload)
    if draft is None:
        return None
    if result.survived:
        return publish_brief(record, draft, hardening_note(record.round))
    if record.round >= max_rounds(draft.complexity) and not result.answerable:
        return hold_contested(record, draft, result)
    return None   # the argument continues — the next round's opener assumes the claim


def publish_redrafted_brief(record, draft: IntakeResult) -> str | None:
    """Publish a redraft that has no attacker left to face, or ``None`` while one is still owed.

    Intake's own settlement asks this of every ``ready`` route it decides, because that is the
    one thing a triage session cannot know about its own draft: whether the round cap leaves an
    attacker to read it (ADR 380/418). While one does, the draft stays unpublished and the
    attack opener takes the claim, exactly as before. When none does, this redraft *is* the end
    of the argument — every objection behind it was one the drafter could answer, or it would
    have gone to the maintainer instead of coming back here — so it publishes.
    """
    if record.round < max_rounds(draft.complexity):
        return None
    return publish_brief(record, draft, hardening_note(record.round, answered=True))


def hold_contested(record, draft: IntakeResult, result: AttackResult) -> str | None:
    """Hand a draft the drafter cannot finish on its own to the maintainer (ADR 380/418).

    This is a product outcome, not an infrastructure one: every session ran and answered, and
    what is left is a call nobody in the loop is allowed to make. The comment **leads with that
    call** — the maintainer is here for the fork, not for a numbered list of edits, and burying
    the question under the edits is what made this hold read as busywork. Everything else the
    last attacker raised follows as context. When the answer was unreadable rather than objecting,
    the hold says that instead: an unread draft is not a settled one, and publishing it on our
    own say-so is the one thing this design refuses to do.

    Same crash-safe post-once → prove → notify recipe as :func:`hold_attack`, under its own
    marker tag so the two holds can never satisfy each other's durability proof.
    """
    from agentflow.handoff import (DurableHandoff, Notification, Subject, marked_body,
                                   proof_marker)
    from agentflow.intake import _DISCLAIMER
    number = int(record.subject)
    objections = result.objections.strip() if result.parsed else ""
    if result.forked:
        argument = f"came down to a call only you can make:\n\n{result.forks.strip()}"
    else:
        # The only other way in. An objection the drafter can answer never reaches this hold —
        # it gets its redraft and that redraft is published (:func:`apply_objections`), so what
        # is left here is a round nobody could read.
        argument = "ended on an answer I couldn't read, so the last word on it is missing"
    rest = (f"\n\nThe rest of what that round raised, for context:\n\n{objections}"
            if objections else "")
    reason = f"contested after {record.round} attack round(s)"
    body = (f"{_DISCLAIMER}\n\nI drafted a plan for this and had it torn into by fresh sessions "
            f"that hadn't seen how it was written. After {record.round} round(s) the argument "
            f"{argument}{rest}"
            f"\n\nHere's the draft as it stands:\n\n{draft.body.strip() or '(no draft body)'}"
            f"\n\nSettle what's contested (or run `/agentflow pickup {number}` to drive it live) "
            "and I'll take it from there — your reply restarts triage, and the next draft gets "
            "argued with fresh.")
    marker = proof_marker(record.identity, reason, tag="attack-contested")
    hold = IntakeResult(IntakeRoute.GRILL, marked_body(body, marker))

    def project() -> None:
        # A read that couldn't reach GitHub leaves the hold unprojected, so the envelope proves
        # no marker and retries next cycle — it never holds over an empty read.
        live = github.issue_headline(record.repo, number)
        if live is None:
            return
        apply_intake(record.repo, number, live.title, sorted(live.labels), hold)

    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="attack-contested",
        marker=marker,
        action=project,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: plan {reason}"))
    if url is None:
        return None
    if not release(record.repo, number, TRIAGING):
        return None
    return url


def hold_attack(record) -> str | None:
    """Create the attack's single exhaustion handoff and notification.

    An attacker that ran out of room never read the draft, so it has nothing to say about it —
    and publishing an unattacked draft on the strength of our own spend cap is the one thing this
    whole design refuses to do. So the issue is handed to the maintainer instead, through
    intake's grilling route, carrying the draft that was never argued with: the plan is worth
    something even unhardened, and this way they can settle it in one reply rather than watching
    triage start over from nothing.

    The crash-safe post-once → prove → notify recipe is the shared
    :class:`~agentflow.handoff.DurableHandoff` envelope (ADR 0042). The marker is a hidden tag
    derived from this record and its hold reason, so a repeat after a daemon crash observes it
    and does not re-hold; the reason comes from the persisted record, never a fresh observation,
    so a restart recomposes the same marker.
    """
    from agentflow.handoff import (DurableHandoff, Notification, Subject, marked_body,
                                   proof_marker)
    from agentflow.intake import _DISCLAIMER
    number = int(record.subject)
    reason = record.hold_reason or "continuation budget exhausted"
    payload = _chain(record)
    draft = _draft(payload) if payload is not None else None
    plan = draft.body.strip() if draft is not None and draft.body.strip() else "(nothing usable)"
    body = (f"{_DISCLAIMER}\n\nI drafted a plan for this and then ran out of room having it "
            "picked apart, so nobody has actually argued with it — I'd rather show it to you "
            "than post it as ready on my own say-so.\n\nHere's the draft as it stood:\n\n"
            f"{plan}\n\nTell me what's wrong with it (or run `/agentflow pickup "
            f"{number}` to drive it live) and I'll take it from there.")
    marker = proof_marker(record.identity, reason, tag="attack-hold")
    result = IntakeResult(IntakeRoute.GRILL, marked_body(body, marker))

    def project() -> None:
        # A read that couldn't reach GitHub leaves the hold unprojected, so the envelope proves
        # no marker and retries next cycle — it never holds over an empty read.
        live = github.issue_headline(record.repo, number)
        if live is None:
            return
        apply_intake(record.repo, number, live.title, sorted(live.labels), result)

    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="attack-hold",
        marker=marker,
        action=project,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: attack held — {reason}"))
    if url is None:
        return None
    if not release(record.repo, number, TRIAGING):
        return None
    return url
