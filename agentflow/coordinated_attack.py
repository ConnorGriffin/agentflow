"""The hardening rounds behind the durable session coordinator (ADR 380).

Triage no longer publishes what it drafts. A draft that would have been posted as a ready brief
instead opens an **attack**: one cold session that never saw the drafting session, asked to break
the plan while breaking it is still free. Its objections go back to a *fresh* triage session that
answers them and redrafts, and that redraft is attacked again. Each attacker reads only the newest
draft — never the rounds behind it — so what a settlement is worth must be written into the draft
itself. Only a draft that runs out of objections is published, so an issue is `ready-for-agent`
exactly when the argument about it is over; one that runs out of *rounds* still contested goes to
the maintainer (:func:`hold_contested`), never to a builder. Nothing downstream knows any of this
happened.

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
from agentflow.coordinator.verification import PREPARED, payload_preview, unprepared
from agentflow.intake import (IntakeResult, IntakeRoute, apply_intake, intake_result_is_durable,
                              redraft_prompt)
from agentflow.labels import TRIAGING, release
from agentflow.prompts import stage_prompt_spec
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


def _capability(record, ref: WorktreeRef) -> tuple[str, str]:
    """Recover capability facts for current and pre-#582 durable chain records."""
    root = getattr(record, "capability_root", None) or ref.workdir
    raw = getattr(record, "capability_context", "{}") or "{}"
    return root, raw


def attack_submission(intake_record, draft: IntakeResult, tool: str) -> Submission | None:
    """Open the cold attack on a triage draft, assuming its ``triaging`` claim (ADR 380).

    The round number and the cycle's target (the maintainer reply that opened it) join the
    identity, so each attack is a genuinely new stage with its own budget and a repeat can never
    collide with the round before it — nor with a retired round of an earlier cycle. Pure: the mapping is the test
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
    capability_root, capability_context = _capability(intake_record, ref)
    prompt = stage_prompt_spec("attack").render(prompt=attack_prompt(
        intake_record.repo, number, draft.title or snapshot.get("title", ""), draft.body,
        round=round, max_rounds=max_rounds(draft.complexity)))
    return Submission(
        repo=intake_record.repo, subject=intake_record.subject, stage="attack",
        target=intake_record.target, pool=tool, complexity=_dial(draft), round=round, claim=True,
        source=str(WorktreeRef.for_attack(ref.workdir, tool, number).path),
        input_ptr=json.dumps({"format": PROVIDER_INPUT_V1, "snapshot": snapshot,
                              "source_ref": payload["source_ref"], "prompt": prompt,
                              "base_prompt": base_prompt, "draft": encode_result(draft)},
                             sort_keys=True),
        transfer_from=intake_record.identity, capability_root=capability_root,
        capability_context=capability_context)


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
    capability_root, capability_context = _capability(attack_record, ref)
    prompt = stage_prompt_spec("attack").render(prompt=attack_prompt(
        attack_record.repo, number, draft.title or snapshot.get("title", ""), draft.body,
        round=round, max_rounds=max_rounds(draft.complexity)))
    return Submission(
        repo=attack_record.repo, subject=attack_record.subject, stage="attack",
        target=attack_record.target, pool=tool, complexity=_dial(draft), round=round, claim=True,
        source=str(WorktreeRef.for_attack(ref.workdir, tool, number).path),
        input_ptr=json.dumps({"format": PROVIDER_INPUT_V1, "snapshot": snapshot,
                              "source_ref": payload["source_ref"], "prompt": prompt,
                              "base_prompt": payload.get("base_prompt", ""),
                              "draft": payload["draft"]}, sort_keys=True),
        transfer_from=attack_record.identity, capability_root=capability_root,
        capability_context=capability_context)


def redraft_submission(attack_record, result: AttackResult, tool: str) -> Submission | None:
    """Open the triage round that answers an attacker, assuming its claim (ADR 380).

    A redraft is an ordinary Intake stage — same adapter, same schema, same read-only profile —
    carrying the completed attack rounds as its round number. Only its prompt differs, and only
    by the draft and the objections appended to the grounding prompt it re-grounds from. Pure;
    ``None`` when the attack round's durable input cannot be read.
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
    capability_root, capability_context = _capability(attack_record, ref)
    prompt = stage_prompt_spec("intake").render(prompt=redraft_prompt(
        base_prompt, draft.title or snapshot.get("title", ""), draft.body,
        result.objections, round=attack_record.round + 1,
        max_rounds=max_rounds(draft.complexity)))
    return Submission(
        repo=attack_record.repo, subject=attack_record.subject, stage="intake",
        target=attack_record.target, pool=tool, complexity="deep", round=attack_record.round, claim=True,
        source=str(WorktreeRef.for_intake(ref.workdir, tool, number).path),
        input_ptr=json.dumps({"format": PROVIDER_INPUT_V1, "snapshot": snapshot,
                              "source_ref": payload["source_ref"], "prompt": prompt,
                              "base_prompt": base_prompt}, sort_keys=True),
        transfer_from=attack_record.identity, capability_root=capability_root,
        capability_context=capability_context)


def reset_worktree(record):
    """Discard and rebuild the attacker's read-only checkout from its durable source pointer.

    Every refusal is named (#405), and an unreadable chain payload is quoted only through the
    bounded single-line preview — the draft under attack rides in that payload, so it must never
    be copied into the record, the daemon log, or the published snapshot."""
    from agentflow.runner import CheckoutRefused, ClaudeRunner, CodexRunner
    if not record.source or not record.input_ptr:
        return unprepared("source-missing",
                          "the round carries no checkout pointer and chain payload to rebuild "
                          "the argument from")
    payload = _chain(record)
    if payload is None:
        return unprepared("input-unreadable", payload_preview("input_ptr", record.input_ptr))
    source_ref = payload.get("source_ref")
    if not isinstance(source_ref, str) or not source_ref:
        return unprepared("source-ref-invalid",
                          f"the chain's frozen commit is not a usable ref: {source_ref!r}")
    ref = _attack_ref(record)
    if ref is None:
        return unprepared("worktree-ref-unreadable",
                          f"the round's checkout pointer does not parse as this issue's own "
                          f"attack worktree: {record.source!r}")
    wt = Path(ref.path)
    if wt.exists():
        _run(["git", "-C", ref.workdir, "worktree", "remove", "--force", str(wt)])
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    try:
        runner.prepare_worktree_detached(ref.workdir, source_ref, wt)
        runner.provision(wt)
    except CheckoutRefused as refused:
        # Carried through untouched: the checkout named which state it is in, and only it can
        # tell a sibling that will finish from a lock only a human will lift (#406).
        return refused.refusal
    except subprocess.CalledProcessError as e:
        return unprepared("checkout-failed",
                          f"preparing the read-only checkout at {wt} from {source_ref[:12]} "
                          f"exited {e.returncode}")
    return PREPARED


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


def attack_claim_ready(record):
    """Prove the durable attack record still owns GitHub's triaging claim before admission.

    It is triage's claim, transferred — an attack round is still the issue being decided, and
    the moment that claim is gone something else has taken the issue over. The issue must also
    still be open: closing it does not strip its labels, so a chain whose issue closed
    mid-argument would otherwise keep arguing a decision nobody can act on (#438); refusing
    here keeps the session unspent while :func:`coordinated_intake._retire_dead_intakes`
    retires the record.
    """
    standing = github.issue_standing(record.repo, int(record.subject))
    if standing is None:   # fail closed: a read that couldn't reach GitHub stays unknown
        return unprepared("claim-unreadable",
                          f"GitHub did not answer for {record.repo}#{record.subject}, so the "
                          f"transferred triaging claim cannot be proved")
    if TRIAGING not in standing.labels:
        return unprepared("claim-released",
                          f"{record.repo}#{record.subject} no longer carries the triaging claim; "
                          f"something else has taken the issue over")
    if standing.state == "CLOSED":
        return unprepared("subject-closed",
                          f"{record.repo}#{record.subject} was closed mid-argument")
    return PREPARED


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

    A draft that runs out of rounds still contested is **never published**: a builder spent on a
    plan three cold readers couldn't settle is the exact waste this design exists to prevent, so
    the argument goes to the maintainer with the surviving objections as the question
    (:func:`hold_contested`). Ordinary disagreement never gets that far — a triage round that
    hits a genuine fork routes to grilling itself, which ends the chain and hands the issue over.
    """
    payload = _chain(record)
    if payload is None:
        return None
    draft = _draft(payload)
    if draft is None:
        return None
    if result.survived:
        return publish_brief(record, draft, hardening_note(record.round))
    if record.round >= max_rounds(draft.complexity):
        if result.remedied_only:
            # The clock ran out, but the attacker itself says every surviving objection carries
            # its own complete fix and none is a fork (#418). That is the same class of objection
            # the redraft loop absorbs between rounds — a ready draft, not a contested one — so
            # the brief is published with the fixes riding in it, and only a genuine fork ever
            # reaches the maintainer. Publication no longer requires unanimous silence.
            return publish_brief(record, _draft_with_remedies(draft, result),
                                 hardening_note(record.round, remedied=True))
        return hold_contested(record, draft, result)
    return None   # the argument continues — the next round's opener assumes the claim


def _draft_with_remedies(draft: IntakeResult, result: AttackResult) -> IntakeResult:
    """The published form of a draft whose final round ended all-remedied: the attacker's
    objections ride in the brief with their fixes, addressed to the builder. Pure (test surface).

    The fixes are appended rather than re-drafted because a redraft would face a fresh cold
    reader on new text — the exact non-convergence the round cap exists to stop. The builder
    self-scopes from the brief, so fixes stated against the brief's own criteria are applied
    where applying matters.
    """
    from dataclasses import replace
    addendum = ("\n\n## Final-round objections — apply these fixes\n\n"
                "The last attacker's surviving objections, each carrying its own fix; the round "
                "cap ended the argument before a redraft could absorb them. Build with every "
                "fix applied — they are part of this brief, not commentary on it.\n\n"
                f"{result.objections.strip()}")
    return replace(draft, body=draft.body.rstrip() + addendum)


def hold_contested(record, draft: IntakeResult, result: AttackResult) -> str | None:
    """Hand a draft that ran out of rounds still contested to the maintainer (ADR 380).

    This is a product outcome, not an infrastructure one: every session ran and answered, the
    drafter and its attackers just never converged. The maintainer gets the newest draft and the
    surviving objections as the question, through intake's own grilling route — one reply
    restarts triage exactly as it does for any other held issue, and the next draft earns a
    fresh set of attackers. When the last round's answer was unreadable rather than objecting,
    the hold says that instead: an unread draft is not a settled one, and publishing it on our
    own say-so is the one thing this design refuses to do.

    Same crash-safe post-once → prove → notify recipe as :func:`hold_attack`, under its own
    marker tag so the two holds can never satisfy each other's durability proof.
    """
    from agentflow.handoff import (DurableHandoff, Notification, Subject, marked_body,
                                   proof_marker)
    from agentflow.intake import _DISCLAIMER
    number = int(record.subject)
    if result.parsed and result.fork.strip():
        # The fork leads (#418): the maintainer's question is the choice itself, not a numbered
        # list of edits with patches attached.
        argument = (f"comes down to a question only you can settle:\n\n{result.fork.strip()}"
                    + (f"\n\nThe full objections behind it:\n\n{result.objections.strip()}"
                       if result.objections.strip() else ""))
    elif result.parsed and result.objections.strip():
        argument = f"still has objections I couldn't settle:\n\n{result.objections.strip()}"
    else:
        argument = "ended on an answer I couldn't read, so the last word on it is missing"
    reason = f"contested after {record.round} attack round(s)"
    body = (f"{_DISCLAIMER}\n\nI drafted a plan for this and had it torn into by fresh sessions "
            f"that hadn't seen how it was written. After {record.round} round(s) the argument "
            f"{argument}\n\nHere's the draft as it stands:\n\n{draft.body.strip() or '(no draft body)'}"
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

    Two ways a round ends with the draft still unargued, and they owe the maintainer different
    accounts. A round that was never started — because the working copy it needs is pinned open
    on the machine — says so and asks for the machine to be cleared, claiming no spend and no
    attempt (#406). Otherwise: an attacker that ran out of room never read the draft, so it has
    nothing to say about it —
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
    from agentflow.coordinator.coordinator import (refused_before_start,
                                                   refused_before_start_detail)
    from agentflow.handoff import (DurableHandoff, Notification, Subject, marked_body,
                                   proof_marker)
    from agentflow.intake import _DISCLAIMER
    number = int(record.subject)
    reason = record.hold_reason or "continuation budget exhausted"
    payload = _chain(record)
    draft = _draft(payload) if payload is not None else None
    plan = draft.body.strip() if draft is not None and draft.body.strip() else "(nothing usable)"
    if refused_before_start(reason):
        # The round never started, so "ran out of room" would be a lie about work nobody did —
        # and so would any hint that the draft below has been tested. Same route, same draft,
        # honest account of why it is still unargued (#406).
        body = (f"{_DISCLAIMER}\n\nI have a draft plan for this and nobody has argued with it "
                "yet. The round that was meant to pick it apart never started, so I'd rather "
                "show you the draft than post it as ready on my own say-so.\n\nIt hasn't "
                "started because the private, throwaway copy of the repository I work in is "
                "pinned open on the machine I run on and holding changes, and I won't touch a "
                "working copy somebody deliberately pinned. Nothing was spent getting here: no "
                "session ran, no attempt was used, and no budget was drawn down. What's in the "
                f"way is only this:\n\n> {refused_before_start_detail(reason)}\n\nHere's the "
                f"draft as it stands, untested:\n\n{plan}\n\nRelease that working copy and I'll "
                "run the argument this draft is still owed. Or tell me what's wrong with it "
                f"here (or run `/agentflow pickup {number}` to drive it live) and I'll take it "
                "from there.")
    else:
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
