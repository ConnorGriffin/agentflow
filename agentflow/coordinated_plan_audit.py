"""The plan audit behind the durable session coordinator (ADR 380).

Mirrors :mod:`agentflow.coordinated_intake` — a read-only checkout rebuilt from ``origin/main``,
a GitHub projection proved durable before the claim is released, and one exhaustion hold — but
projects a verdict rather than a route. The two projections differ in exactly the way the two
verdicts do: a countersign stamps one marker label and touches nothing else, while a bounce is
projected through intake's own grilling route so the maintainer's reply re-runs triage for free.

The exhaustion hold is where the mirror breaks, deliberately. Intake holds by routing the issue
to the maintainer because nothing is settled yet; the audit holds against a decision that *is*
settled, so it preserves it and diagnoses itself instead (see :func:`hold_audit`).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from agentflow import github
from agentflow.coordinator import Submission
from agentflow.coordinator.providers import PROVIDER_INPUT_V1
from agentflow.intake import COUNTERSIGNED, apply_intake, intake_result_is_durable
from agentflow.labels import AUDITING, release
from agentflow.plan_audit import (COUNTERSIGN_COLOUR, COUNTERSIGN_DESCRIPTION, PlanAuditResult,
                                  PlanAuditVerdict, bounce_result, plan_audit_prompt)
from agentflow.runner import _run
from agentflow.worktree_ref import WorktreeKind, WorktreeRef


def brief_fingerprint(issue: dict) -> str:
    """Which *brief* this audit is of. Pure (test surface).

    The audit's subject is not the issue, it is the plan currently written on the issue — so the
    submission's identity has to move when the plan does. Two audits of the same brief are the
    same audit and must share one record (resubmitting an issue already audited this cycle opens
    nothing new); an audit of a brief that has since been rewritten is a *different* audit and
    needs its own.

    That distinction is requirement 7 of ADR 380, and it is load-bearing rather than cosmetic.
    An issue only returns to the build queue after a bounce by going back through triage, which
    rewrites the brief — without this the second audit would silently reuse the first one's
    retired record, no countersign would ever be stamped, and the issue would sit `ready-for-agent`
    forever, never audited and never built.
    """
    title = issue.get("title", "") or ""
    body = issue.get("body", "") or ""
    return hashlib.sha256(f"{title}\n{body}".encode()).hexdigest()[:16]


def plan_audit_submission(cfg, issue: dict, tool: str) -> Submission | None:
    """Map a durable issue snapshot to one idempotent plan audit stage submission.

    The submission's identity is the issue *and the brief on it* — see
    :func:`brief_fingerprint` for why the brief has to be part of it.
    """
    n = issue["number"]
    source_path = WorktreeRef.for_plan_audit(cfg.workdir, tool, n).path
    snapshot = {
        "number": n, "title": issue.get("title", ""), "body": issue.get("body") or "",
        "labels": [label.get("name", "") for label in issue.get("labels", [])],
    }
    resolved = _run(["git", "-C", cfg.workdir, "rev-parse", "origin/main"])
    source_ref = resolved.stdout.strip() if resolved.returncode == 0 else ""
    if not source_ref:
        return None
    return Submission(repo=cfg.repo, subject=str(n), stage="audit",
                      target=brief_fingerprint(issue),
                      pool=tool, complexity="deep", source=str(source_path), claim=True,
                      input_ptr=json.dumps({"format": PROVIDER_INPUT_V1,
                                            "snapshot": snapshot, "source_ref": source_ref,
                                            "prompt": plan_audit_prompt(cfg.repo, issue)},
                                           sort_keys=True))


def reset_worktree(record) -> bool:
    """Discard and rebuild the audit's read-only checkout from its durable source pointer."""
    from agentflow.runner import ClaudeRunner, CodexRunner
    if not record.source or not record.input_ptr:
        return False
    try:
        source_ref = json.loads(record.input_ptr)["source_ref"]
    except (ValueError, KeyError, TypeError):
        return False
    if not isinstance(source_ref, str) or not source_ref:
        return False
    ref = _audit_ref(record)
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
    return True


def dispose_worktree(record) -> bool:
    """Remove the audit's read-only checkout once its verdict is durable, *before* the record
    retires — the same window :func:`agentflow.coordinated_intake.dispose_worktree` closes.
    Idempotent; returns whether the worktree is gone."""
    ref = _audit_ref(record)
    if ref is None:
        return False
    workdir = ref.workdir
    wt = Path(ref.path)
    if wt.exists():
        _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    return not wt.exists()


def _audit_ref(record) -> WorktreeRef | None:
    """The record's source parsed as its own ``<pool>-audit/issue-<subject>`` checkout, or
    ``None`` if the source is absent, malformed, or belongs to a different subject/pool/kind.
    The kind check is what keeps an audit from ever operating on the intake checkout of the same
    issue, which differs only by lane suffix (ADR 0041/380)."""
    ref = WorktreeRef.parse(record.source)
    if ref is None or ref.kind is not WorktreeKind.AUDIT \
            or ref.tool != record.pool or str(ref.number) != str(record.subject):
        return None
    return ref


def audit_claim_ready(record) -> bool:
    """Prove the durable audit record still owns GitHub's auditing claim before admission.

    The audit owns its own claim lane (ADR 380). Intake's claim means something different — a
    grounding session is still deciding — and the audit deliberately refuses to run while one is
    live, so the two can never be the same label.
    """
    labels = github.issue_labels(record.repo, int(record.subject))
    if labels is None:   # fail closed: a read that couldn't reach GitHub stays unknown
        return False
    return AUDITING in labels


def stamp_countersign(repo: str, number: int) -> bool:
    """Record a clean verdict as the durable countersign, and prove it landed.

    Both audits end here: the unattended one finishing its record, and the by-hand one a
    maintainer runs inline through `build <N>` when the queue's audit hasn't reached the issue
    yet. Neither may report success on a write it could not read back — an unproven marker would
    let a brief nobody actually cleared into the build queue.
    """
    if not github.create_label(repo, COUNTERSIGNED, COUNTERSIGN_COLOUR, COUNTERSIGN_DESCRIPTION):
        return False
    if not github.add_label(repo, number, COUNTERSIGNED):
        return False
    labels = github.issue_labels(repo, number)
    return labels is not None and COUNTERSIGNED in labels


def apply_verdict(record, result: PlanAuditResult) -> str | None:
    """Idempotently project the already-durable verdict, proving it before claim release."""
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    if result.verdict is PlanAuditVerdict.COUNTERSIGN:
        return _countersign(record.repo, number)
    return _bounce(record, number, result)


def _countersign(repo: str, number: int) -> str | None:
    """Mark the brief audited and change nothing else — no comment, no retitle, no body edit.

    The marker is the whole projection: the issue was already ``ready-for-agent``, and the audit's
    only finding was that it deserved to stay that way.

    An issue that stopped being ready while the audit ran is no longer the audit's business — a
    maintainer or a resumed triage moved it, and stamping a countersign on a held issue would
    leave a stale "may be built" mark sitting on something nobody cleared. There is nothing to
    project in that case and nothing to retry, so the verdict settles without a marker.
    """
    live = github.issue_labels(repo, number)
    if live is None:
        return None   # fail closed: an unreadable read is not proof the issue moved on
    if "ready-for-agent" in live and not stamp_countersign(repo, number):
        return None
    if not release(repo, number, AUDITING):
        return None
    return f"https://github.com/{repo}/issues/{number}"


def _bounce(record, number: int, result: PlanAuditResult) -> str | None:
    """Project a bounce as intake's grilling route, behind the shared durable-handoff envelope.

    A bounce asks a human for something, so it goes through
    :class:`~agentflow.handoff.DurableHandoff` (ADR 0042) exactly as intake's own grill route
    does: the objections comment is the durable marker, and the operator is pinged once the
    envelope can prove that comment exists. Projection is idempotent across partial writes — one
    interrupted after its comment landed is finished on the way out, so the remaining label
    mutations are never stranded behind the post-once gate.
    """
    from agentflow.handoff import DurableHandoff, Notification, Subject
    bounced = bounce_result(result)

    def project() -> bool:
        # An unreadable live headline means GitHub couldn't be reached and the bounce is not
        # projected at all; the caller retries rather than treating nothing-written as done.
        live = github.issue_headline(record.repo, number)
        if live is None:
            return False
        apply_intake(record.repo, number, live.title, sorted(live.labels), bounced)
        return True

    if DurableHandoff().hand_off(
            Subject(repo=record.repo, number=number, kind="issue"),
            identity=record.identity, stage="plan-audit-bounce",
            marker=bounced.body.strip(),
            action=project,
            notification=Notification(
                "agentflow needs you",
                f"{record.repo} #{number}: plan audit bounced the brief")) is None:
        return None
    if not intake_result_is_durable(record.repo, number, bounced):
        # The comment is durable, so the post-once gate skipped a projection that never finished
        # — or the issue has been edited since. Finish it once and let the bounce end on that.
        if not project():
            return None   # GitHub was unreachable, so nothing was written — retry instead
    if not release(record.repo, number, AUDITING):
        return None
    return f"https://github.com/{record.repo}/issues/{number}"


def hold_audit(record) -> str | None:
    """Create the audit's single exhaustion handoff and notification.

    Exhaustion is not silence — but it is also **not a finding about the brief**. A session that
    ran out of room never read the plan, so it has nothing to say about it, and unwinding a
    settled decision on the strength of our own spend cap would send a perfectly good brief on a
    maintainer round-trip it never earned. So this hold is the one place the audit deviates from
    intake's discipline: the issue keeps ``ready-for-agent`` and its dials, no grilling route is
    projected, and the only thing written is a comment diagnosing the *auditor* (ADR 380).

    That leaves the issue ready with no held label, which the daemon's resume sweep will never
    wake on — so the comment names the by-hand resume in plain words. Nothing re-audits it in the
    meantime either: the held record is terminal for this brief, so the next cycle's audit pass
    reserves the issue and moves on rather than auditing it forever.

    The crash-safe post-once → prove → notify recipe is the shared
    :class:`~agentflow.handoff.DurableHandoff` envelope (ADR 0042). The marker is a hidden tag
    derived from this record and its hold reason, so a repeat after a daemon crash observes it and
    does not re-hold; the reason comes from the persisted record, never a fresh observation, so a
    restart recomposes the same marker.
    """
    from agentflow.handoff import (DurableHandoff, Notification, Subject, marked_body,
                                   proof_marker)
    from agentflow.intake import _DISCLAIMER
    number = int(record.subject)
    reason = record.hold_reason or "continuation budget exhausted"
    body = (f"{_DISCLAIMER}\n\nI ran out of room re-reading this brief against the code before "
            "building it, so nothing here has actually been audited — this says nothing about "
            "the plan itself, which is still settled and still ready.\n\nI've left it that way "
            f"rather than sending it back to you over my own budget. Run `/agentflow build "
            f"{number}` to audit and build it now, or `/agentflow pickup {number}` to drive it "
            "live — I won't pick it up again on my own.")
    marker = proof_marker(record.identity, reason, tag="plan-audit-hold")

    def project() -> None:
        # A comment and nothing else: no label projection, so the ready settlement survives the
        # hold untouched. A failed post leaves no marker, so the envelope retries next cycle.
        github.comment(record.repo, number, marked_body(body, marker))

    url = DurableHandoff().hand_off(
        Subject(repo=record.repo, number=number, kind="issue"),
        identity=record.identity, stage="plan-audit-hold",
        marker=marker,
        action=project,
        notification=Notification(
            "agentflow needs you", f"{record.repo} #{number}: plan audit held — {reason}"))
    if url is None:
        return None
    if not release(record.repo, number, AUDITING):
        return None
    return url
