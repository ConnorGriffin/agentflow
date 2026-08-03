"""Intake behind the durable session coordinator (issue #106)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentflow import github
from agentflow.coordinator import Submission
from agentflow.coordinator.providers import PROVIDER_INPUT_V1
from agentflow.coordinator.verification import PREPARED, payload_preview, unprepared
from agentflow.intake import (IntakeResult, IntakeRoute, apply_intake, intake_prompt,
                              intake_result_is_durable)
from agentflow.labels import TRIAGING, release
from agentflow.runner import _run
from agentflow.worktree_ref import WorktreeKind, WorktreeRef


def intake_submission(cfg, issue: dict, extra: str, comments: str, tool: str) -> Submission | None:
    """Map a durable issue snapshot to one idempotent Intake stage submission.

    ``comments`` is required rather than defaulted: a caller that forgets to read the issue's
    thread would otherwise silently go back to triaging issues from their body alone, with nothing
    failing to say so. Like the rest of the snapshot it is frozen at submission time, which is what
    keeps the durable input reproducible.
    """
    n = issue["number"]
    target = issue.get("_intake_target") if extra else None
    source_path = WorktreeRef.for_intake(cfg.workdir, tool, n).path
    snapshot = {
        "number": n, "title": issue.get("title", ""), "body": issue.get("body") or "",
        "labels": [label.get("name", "") for label in issue.get("labels", [])],
        "extra": extra, "comments": comments,
    }
    resolved = _run(["git", "-C", cfg.workdir, "rev-parse", "origin/main"])
    source_ref = resolved.stdout.strip() if resolved.returncode == 0 else ""
    if not source_ref:
        return None
    return Submission(repo=cfg.repo, subject=str(n), stage="intake", target=target,
                      pool=tool, complexity="deep", source=str(source_path), claim=True,
                      input_ptr=json.dumps({"format": PROVIDER_INPUT_V1,
                                            "snapshot": snapshot, "source_ref": source_ref,
                                            "prompt": intake_prompt(cfg.repo, issue, extra,
                                                                    comments)},
                                           sort_keys=True))


def reset_worktree(record):
    """Discard and rebuild Intake's read-only checkout from its durable source pointer.

    Every refusal is named (#405). ``input_ptr`` is durable external text a crash or a hand edit
    can corrupt, so an unreadable one is quoted through the bounded, single-line preview rather
    than copied into the record, the log, and the snapshot."""
    from agentflow.runner import ClaudeRunner, CodexRunner
    if not record.source or not record.input_ptr:
        return unprepared("source-missing",
                          "the record carries no checkout pointer and provider payload to "
                          "rebuild triage from")
    try:
        payload = json.loads(record.input_ptr)
        snapshot = payload["snapshot"]
        source_ref = payload["source_ref"]
    except (ValueError, KeyError, TypeError):
        return unprepared("input-unreadable", payload_preview("input_ptr", record.input_ptr))
    if not isinstance(source_ref, str) or not source_ref:
        return unprepared("source-ref-invalid",
                          f"the payload's frozen commit is not a usable ref: {source_ref!r}")
    ref = _intake_ref(record)
    if ref is None:
        return unprepared("worktree-ref-unreadable",
                          f"the record's checkout pointer does not parse as this issue's own "
                          f"intake worktree: {record.source!r}")
    workdir = ref.workdir
    wt = Path(ref.path)
    if wt.exists():
        _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    try:
        runner.prepare_worktree_detached(workdir, source_ref, wt)
        runner.provision(wt)
    except subprocess.CalledProcessError as e:
        return unprepared("checkout-failed",
                          f"preparing the read-only checkout at {wt} from {source_ref[:12]} "
                          f"exited {e.returncode}")
    # Fetch any issue-body screenshots into the read-only worktree so the vision-capable
    # model can Read them (issue #191). Fail closed: a fetch failure leaves no image and
    # intake falls back to text-only routing — it never wedges preparation.
    try:
        from agentflow.intake_attachments import ATTACHMENTS_DIRNAME, stage_attachments
        stage_attachments(snapshot.get("body", ""), wt / ATTACHMENTS_DIRNAME)
    except Exception:  # noqa: BLE001 — image ingestion is best-effort, never fatal to intake
        pass
    return PREPARED


def dispose_worktree(record) -> bool:
    """Remove Intake's read-only checkout once its route is durable, *before* the record retires
    (issue #106). A completed-but-not-retired record's source is still in the coordinator's
    owned-sources set, so it is protected; the instant it retires that protection drops, and any
    checkout still on disk would then read as ambiguous legacy activation evidence.
    Disposing here closes that window.
    Idempotent: an already-removed worktree is a no-op success. Returns whether the worktree is
    gone — a stubborn checkout that could not be removed returns ``False`` so settlement retries
    rather than retiring over ambiguous evidence."""
    ref = _intake_ref(record)
    if ref is None:
        return False
    workdir = ref.workdir
    wt = Path(ref.path)
    if wt.exists():
        _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    return not wt.exists()


def _intake_ref(record) -> WorktreeRef | None:
    """The record's source parsed as its own ``<pool>-intake/issue-<subject>`` checkout, or
    ``None`` if the source is absent, malformed, or belongs to a different subject/pool/kind —
    the single guard both worktree operations share (ADR 0041)."""
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.kind is not WorktreeKind.INTAKE \
            or ref.tool != record.pool or str(ref.number) != str(record.subject):
        return None
    return ref


def intake_claim_ready(record):
    """Prove the durable Intake record still owns GitHub's triaging claim before admission —
    on an issue that is still open. Closing an issue does not strip its labels, so the label
    alone would admit a session to triage a closed issue (#438); refusing here keeps the
    session unspent while :func:`_retire_dead_intakes` retires the record."""
    standing = github.issue_standing(record.repo, int(record.subject))
    if standing is None:   # fail closed: a read that couldn't reach GitHub stays unknown
        return unprepared("claim-unreadable",
                          f"GitHub did not answer for {record.repo}#{record.subject}, so the "
                          f"triaging claim cannot be proved")
    if TRIAGING not in standing.labels:
        return unprepared("claim-released",
                          f"{record.repo}#{record.subject} no longer carries the triaging claim; "
                          f"something else has taken the issue over")
    if standing.state == "CLOSED":
        return unprepared("subject-closed",
                          f"{record.repo}#{record.subject} was closed while its triage waited")
    return PREPARED


def _retire_dead_intakes(coord) -> None:
    """Retire an Intake — or the attack round carrying its transferred claim — whose issue has
    been closed under it, before a session is spent triaging or arguing a subject nothing can
    act on (#438), the same disposition :func:`coordinated_revise._retire_dead_revises` takes
    for a Revise of a gone PR.

    Fixing an issue by hand while its triage sits queued is a legitimate operator action, and
    closing the issue does not strip its labels: the record would otherwise pass its claim
    proof the moment pool headroom returned and launch a real session against the closed
    issue. This runs every reconcile pass, before admission, over the durable records. The
    triaging label comes off the closed issue *before* the record retires, so the operator's
    label view agrees; a release that cannot prove the label is gone leaves the record for a
    later pass rather than retiring over a claim GitHub still shows.

    Trust boundary: an unreadable GitHub answer never retires — ``None`` means "couldn't
    check", never "closed". Only a definite CLOSED state retires the record."""
    from agentflow.coordinated_review import _kill_running_family
    from agentflow.coordinator import tracer
    from agentflow.coordinator.record import RUNNING
    for record in tracer.load_records():
        if record.stage not in ("intake", "attack") or record.retired or not record.claim:
            continue
        state = github.issue_state(record.repo, int(record.subject))
        if state != "CLOSED":
            continue  # unreadable (None) fails closed; an open issue is live triage work
        if record.state == RUNNING:
            _kill_running_family(record)
        if not release(record.repo, int(record.subject), TRIAGING):
            continue  # the label must provably come off before the record drops the claim
        coord.retire_stale_intake(record.identity)


def apply_route(record, result: IntakeResult) -> str | None:
    """Idempotently project the already-durable route, proving it before claim release.

    A ``ready`` route is **not projected here at all** (ADR 380). It is a *draft*: nothing is
    written to the issue, the triaging claim is retained, and this returns ``None`` so the record
    stays completed-and-unretired until the attack round it opens assumes the claim. A brief
    reaches GitHub exactly once, from :func:`agentflow.coordinated_attack.publish_brief`, after it
    has survived its attackers — so a draft that never survives is never something the maintainer
    had to read.

    A ``grill`` or ``mockup`` route is a handoff — it asks a human for something — so it goes
    through the shared :class:`~agentflow.handoff.DurableHandoff` envelope (ADR 0042): the
    route's own comment is the durable marker, and the operator is pinged once the envelope can
    prove that comment exists, under the key it derives. Every other route hands the issue on to
    the pipeline and is projected without a ping, exactly as before.

    Either way projection is idempotent across partial writes: a projection interrupted after
    its comment landed is finished on the way out, so the remaining title/body/label mutations
    are never stranded behind the envelope's post-once gate. That repair happens **once** and
    the route then ends either way — an issue a maintainer has since retitled or re-labelled is
    theirs, and re-deciding this every cycle would keep overwriting their edit.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject
    try:
        snapshot = json.loads(record.input_ptr or "")["snapshot"]
        number = int(record.subject)
    except (ValueError, KeyError, TypeError):
        return None
    if result.route is IntakeRoute.READY:
        return None   # a draft, not a decision — the attack round takes it from here
    source_title = snapshot.get("title", "")
    source_body = snapshot.get("body", "")

    def project() -> bool:
        # An unreadable live headline means GitHub couldn't be reached and the route is not
        # projected at all; the caller retries rather than treating nothing-written as done.
        live = github.issue_headline(record.repo, number)
        if live is None:
            return False
        apply_intake(record.repo, number, live.title or source_title,
                     sorted(live.labels), result, source_title, source_body)
        return True

    route = result.route.value
    hands_off = route in ("grill", "mockup")
    if not hands_off:
        project()
    elif DurableHandoff().hand_off(
            Subject(repo=record.repo, number=number, kind="issue"),
            identity=record.identity, stage=f"intake-route:{route}",
            marker=result.body.strip(),
            action=project,
            notification=Notification(
                "agentflow needs you", f"{record.repo} #{number}: {route}")) is None:
        return None
    if not intake_result_is_durable(record.repo, number, result, source_title, source_body):
        if not hands_off:
            return None
        # The route's comment is durable, so the post-once gate skipped a projection that never
        # finished — or the issue has been edited since. Those look identical from here, so
        # finish the projection once and let the route end on whatever that leaves.
        if not project():
            return None   # GitHub was unreachable, so nothing was written — retry instead
    if not release(record.repo, number, TRIAGING):
        return None
    return f"https://github.com/{record.repo}/issues/{number}"


def hold_intake(record) -> str | None:
    """Create Intake's single exhaustion handoff and notification.

    The crash-safe post-once → prove → notify recipe is the shared
    :class:`~agentflow.handoff.DurableHandoff` envelope (ADR 0042). The marker is a hidden tag
    derived from this record and its hold reason, carried at the end of the held-route comment,
    so a repeat after a daemon crash observes it and does not re-hold. It cannot be the comment's
    own text: the grounding-ambiguity copy is the *same words* for every hold reason, so a
    resumed intake holding for a completely different reason would read as already handed off and
    post nothing at all. Projecting the comment (and its state label) is the marker-posting
    ``action``; releasing the triaging claim is stage bookkeeping that runs once the handoff
    confirms the marker landed.

    The durable hold reason picks the comment: a permanent provider condition stopped the
    session before the model read anything, so that handoff names the provider failure and its
    remediation instead of the generic "I couldn't ground this" ask, which would send the
    maintainer hunting for a decision that was never made (issue #328). The reason also says
    *which* permanent condition it was, so a rejected request or a spend ceiling gets its own
    diagnosis instead of re-authenticate advice for a healthy sign-in (issue #342) — and an
    environment that could not carry a session at all names the machine, not the coding agent's
    provider (issue #386). Every other hold reason keeps the grounding-ambiguity copy. Only the
    body differs — route, state label, and the envelope are identical either way; the reason
    comes from the persisted record, never a fresh observation, so a restart recomposes the same
    marker."""
    from dataclasses import replace as replace_result

    from agentflow.coordinator.coordinator import (PERMANENT_HOLD_REASON,
                                                   parse_permanent_hold_reason)
    from agentflow.handoff import (DurableHandoff, Notification, Subject, marked_body,
                                   proof_marker)
    from agentflow.intake import _held, _provider_failed
    number = int(record.subject)
    reason = record.hold_reason or "continuation budget exhausted"
    if reason.startswith(PERMANENT_HOLD_REASON):
        result = _provider_failed(reason, parse_permanent_hold_reason(reason).value)
    else:
        result = _held(reason)
    # A hold posted before this record carried its own marker is still proof of itself, so an
    # issue already held when the daemon deploys is never commented on twice.
    legacy_marker = result.body
    marker = proof_marker(record.identity, reason, tag="intake-hold")
    result = replace_result(result, body=marked_body(result.body, marker))

    def project() -> None:
        # A read that couldn't reach GitHub leaves the hold unprojected, so the envelope proves
        # no marker and retries next cycle — it never holds over an empty read.
        live = github.issue_headline(record.repo, number)
        if live is None:
            return
        apply_intake(record.repo, number, live.title, sorted(live.labels), result)

    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="intake-hold",
        marker=marker,
        action=project,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: Intake held — {reason}"),
        also_proven_by=legacy_marker)
    if url is None:
        return None
    if not release(record.repo, number, TRIAGING):
        return None
    return url
