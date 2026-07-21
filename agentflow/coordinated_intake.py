"""Intake behind the durable session coordinator (issue #106)."""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path

from agentflow import github
from agentflow.coordinator import Submission
from agentflow.coordinator.providers import PROVIDER_INPUT_V1
from agentflow.intake import IntakeResult, apply_intake, intake_prompt, intake_result_is_durable
from agentflow.worktree_ref import WorktreeKind, WorktreeRef


def intake_submission(cfg, issue: dict, extra: str, tool: str) -> Submission | None:
    """Map a durable issue snapshot to one idempotent Intake stage submission."""
    from agentflow.loop import _run
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
    from agentflow.loop import _run
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
    checkout still on disk would then read as ambiguous legacy activation evidence
    (:func:`agentflow.coordinated_build.activation_evidence`). Disposing here closes that window.
    Idempotent: an already-removed worktree is a no-op success. Returns whether the worktree is
    gone — a stubborn checkout that could not be removed returns ``False`` so settlement retries
    rather than retiring over ambiguous evidence."""
    from agentflow.loop import _run
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
    from agentflow.loop import TRIAGING
    labels = github.issue_labels(record.repo, int(record.subject))
    if labels is None:   # fail closed: a read that couldn't reach GitHub stays unknown
        return False
    return TRIAGING in labels


def apply_route(record, result: IntakeResult) -> str | None:
    """Idempotently project the already-durable route, proving it before claim release."""
    from agentflow.loop import _release_triage
    try:
        snapshot = json.loads(record.input_ptr or "")["snapshot"]
        number = int(record.subject)
    except (ValueError, KeyError, TypeError):
        return None
    # The live title + labels read in one snapshot doesn't fit a typed helper, so it goes
    # through the module's named escape hatch (ADR 0040); None means GitHub couldn't be
    # reached and the route is not projected.
    issue = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "title,labels"], parse_json=True)
    if not isinstance(issue, dict):
        return None
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    source_title = snapshot.get("title", "")
    source_body = snapshot.get("body", "")
    apply_intake(record.repo, number, issue.get("title", source_title), labels, result,
                 source_title, source_body)
    if not intake_result_is_durable(record.repo, number, result, source_title, source_body):
        return None
    if not _release_triage(record.repo, number):
        return None
    url = f"https://github.com/{record.repo}/issues/{number}"
    if result.route.value in ("grill", "mockup"):
        from agentflow.notify import notify
        sequence_id = sha256(
            f"{record.identity}:intake-route:{result.route.value}".encode()
        ).hexdigest()[:12]
        if not notify("agentflow needs you", f"{record.repo} #{number}: {result.route.value}",
                      url, sequence_id):
            return None
    return url


def hold_intake(record) -> str | None:
    """Create Intake's single exhaustion handoff and notification."""
    from agentflow.intake import _held
    from agentflow.loop import _release_triage
    from agentflow.notify import notify
    number = int(record.subject)
    reason = record.hold_reason or "continuation budget exhausted"
    result = _held(reason)
    # Multi-field live read via the named escape hatch (ADR 0040); None means GitHub
    # couldn't be reached, so the hold is not projected.
    issue = github.api(["issue", "view", str(number), "--repo", record.repo,
                        "--json", "title,labels,comments"], parse_json=True)
    if not isinstance(issue, dict):
        return None
    apply_intake(record.repo, number, issue.get("title", ""),
                 [x.get("name", "") for x in issue.get("labels", [])], result)
    if not intake_result_is_durable(record.repo, number, result):
        return None
    if not _release_triage(record.repo, number):
        return None
    url = f"https://github.com/{record.repo}/issues/{number}"
    if record.notifications == 0:
        sequence_id = sha256(f"{record.identity}:intake-hold".encode()).hexdigest()[:12]
        if not notify("agentflow needs you", f"{record.repo} #{number}: Intake held — {reason}",
                      url, sequence_id):
            return None
    return url
