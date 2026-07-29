"""Intake behind the durable session coordinator (issue #106)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentflow import github
from agentflow.coordinator import Submission
from agentflow.coordinator.providers import PROVIDER_INPUT_V1
from agentflow.intake import IntakeResult, apply_intake, intake_prompt, intake_result_is_durable
from agentflow.labels import TRIAGING, release
from agentflow.runner import _run
from agentflow.worktree_ref import WorktreeKind, WorktreeRef


def intake_submission(cfg, issue: dict, extra: str, tool: str) -> Submission | None:
    """Map a durable issue snapshot to one idempotent Intake stage submission."""
    n = issue["number"]
    target = issue.get("_intake_target") if extra else None
    source_path = WorktreeRef.for_intake(cfg.workdir, tool, n).path
    snapshot = {
        "number": n, "title": issue.get("title", ""), "body": issue.get("body") or "",
        "labels": [label.get("name", "") for label in issue.get("labels", [])],
        "extra": extra,
    }
    resolved = _run(["git", "-C", cfg.workdir, "rev-parse", "origin/main"])
    source_ref = resolved.stdout.strip() if resolved.returncode == 0 else ""
    if not source_ref:
        return None
    return Submission(repo=cfg.repo, subject=str(n), stage="intake", target=target,
                      pool=tool, complexity="deep", source=str(source_path), claim=True,
                      input_ptr=json.dumps({"format": PROVIDER_INPUT_V1,
                                            "snapshot": snapshot, "source_ref": source_ref,
                                            "prompt": intake_prompt(cfg.repo, issue, extra)},
                                           sort_keys=True))


def reset_worktree(record) -> bool:
    """Discard and rebuild Intake's read-only checkout from its durable source pointer."""
    from agentflow.runner import ClaudeRunner, CodexRunner
    if not record.source or not record.input_ptr:
        return False
    try:
        payload = json.loads(record.input_ptr)
        snapshot = payload["snapshot"]
        source_ref = payload["source_ref"]
    except (ValueError, KeyError, TypeError):
        return False
    if not isinstance(source_ref, str) or not source_ref:
        return False
    ref = _intake_ref(record)
    if ref is None:
        return False
    workdir = ref.workdir
    wt = Path(ref.path)
    if wt.exists():
        _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    try:
        runner.prepare_worktree_detached(workdir, source_ref, wt)
        runner.provision(wt)
    except subprocess.CalledProcessError:
        return False
    # Fetch any issue-body screenshots into the read-only worktree so the vision-capable
    # model can Read them (issue #191). Fail closed: a fetch failure leaves no image and
    # intake falls back to text-only routing — it never wedges preparation.
    try:
        from agentflow.intake_attachments import ATTACHMENTS_DIRNAME, stage_attachments
        stage_attachments(snapshot.get("body", ""), wt / ATTACHMENTS_DIRNAME)
    except Exception:  # noqa: BLE001 — image ingestion is best-effort, never fatal to intake
        pass
    return True


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


def intake_claim_ready(record) -> bool:
    """Prove the durable Intake record still owns GitHub's triaging claim before admission."""
    labels = github.issue_labels(record.repo, int(record.subject))
    if labels is None:   # fail closed: a read that couldn't reach GitHub stays unknown
        return False
    return TRIAGING in labels


def apply_route(record, result: IntakeResult) -> str | None:
    """Idempotently project the already-durable route, proving it before claim release.

    A ``grill`` or ``mockup`` route is a handoff — it asks a human for something — so it goes
    through the shared :class:`~agentflow.handoff.DurableHandoff` envelope (ADR 0042): the
    route's own comment is the durable marker, and the operator is pinged exactly once, when
    that comment is newly posted, under the key the envelope derives. Every other route hands
    the issue on to the pipeline and is projected without a ping, exactly as before.

    Either way projection is idempotent across partial writes: a projection interrupted after
    its comment landed is finished on the way out, so the remaining title/body/label mutations
    are never stranded behind the envelope's post-once gate.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject
    try:
        snapshot = json.loads(record.input_ptr or "")["snapshot"]
        number = int(record.subject)
    except (ValueError, KeyError, TypeError):
        return None
    source_title = snapshot.get("title", "")
    source_body = snapshot.get("body", "")

    def project() -> None:
        # An unreadable live headline means GitHub couldn't be reached and the route is not
        # projected at all.
        live = github.issue_headline(record.repo, number)
        if live is None:
            return
        apply_intake(record.repo, number, live.title or source_title,
                     sorted(live.labels), result, source_title, source_body)

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
        if hands_off:
            project()   # the post-once gate skipped an interrupted projection — finish it
        return None
    if not release(record.repo, number, TRIAGING):
        return None
    return f"https://github.com/{record.repo}/issues/{number}"


def hold_intake(record) -> str | None:
    """Create Intake's single exhaustion handoff and notification.

    The crash-safe post-once → prove → notify-once recipe is the shared
    :class:`~agentflow.handoff.DurableHandoff` envelope (ADR 0042): the held-route comment is
    the durable marker, so a repeat after a daemon crash observes the same comment and neither
    re-holds nor pings again. Projecting that comment (and its state label) is the marker-posting
    ``action``; releasing the triaging claim is stage bookkeeping that runs once the handoff
    confirms the marker landed.

    The durable hold reason picks the comment: a permanent provider condition stopped the
    session before the model read anything, so that handoff names the provider failure and its
    remediation instead of the generic "I couldn't ground this" ask, which would send the
    maintainer hunting for a decision that was never made (issue #328). The reason also says
    *which* permanent condition it was, so a rejected request or a spend ceiling gets its own
    diagnosis instead of re-authenticate advice for a healthy sign-in (issue #342). Every other
    hold reason keeps the grounding-ambiguity copy. Only the body differs — route, state label,
    and the exactly-once envelope are identical either way; the reason comes from the persisted
    record, never a fresh observation, so a restart recomposes the same marker."""
    from agentflow.coordinator.coordinator import (PERMANENT_HOLD_REASON,
                                                   parse_permanent_hold_reason)
    from agentflow.handoff import DurableHandoff, Notification, Subject
    from agentflow.intake import _held, _provider_failed
    number = int(record.subject)
    reason = record.hold_reason or "continuation budget exhausted"
    if reason.startswith(PERMANENT_HOLD_REASON):
        result = _provider_failed(reason, parse_permanent_hold_reason(reason).value)
    else:
        result = _held(reason)

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
        marker=result.body,
        action=project,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: Intake held — {reason}"))
    if url is None:
        return None
    if not release(record.repo, number, TRIAGING):
        return None
    return url
