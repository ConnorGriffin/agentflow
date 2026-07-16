"""Intake behind the durable session coordinator (issue #106)."""

from __future__ import annotations

import json
import subprocess
from hashlib import sha256
from pathlib import Path

from agentflow.coordinator import Submission
from agentflow.coordinator.providers import PROVIDER_INPUT_V1
from agentflow.intake import IntakeResult, apply_intake, intake_prompt, intake_result_is_durable


def intake_submission(cfg, issue: dict, extra: str, tool: str) -> Submission | None:
    """Map a durable issue snapshot to one idempotent Intake stage submission."""
    from agentflow.loop import _run
    n = issue["number"]
    target = issue.get("_intake_target") if extra else None
    source_path = Path(cfg.workdir) / ".agentflow" / "worktrees" / f"{tool}-intake" / f"issue-{n}"
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
        payload["snapshot"]
        source_ref = payload["source_ref"]
    except (ValueError, KeyError, TypeError):
        return False
    if not isinstance(source_ref, str) or not source_ref:
        return False
    wt = Path(record.source)
    marker = f"/.agentflow/worktrees/{record.pool}-intake/issue-{record.subject}"
    if marker not in record.source:
        return False
    workdir = record.source.split("/.agentflow/worktrees/", 1)[0]
    if wt.exists():
        _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    try:
        runner.prepare_worktree_detached(workdir, source_ref, wt)
        runner.provision(wt)
    except subprocess.CalledProcessError:
        return False
    return True


def intake_claim_ready(record) -> bool:
    """Prove the durable Intake record still owns GitHub's triaging claim before admission."""
    from agentflow.loop import TRIAGING, _run
    viewed = _run(["gh", "issue", "view", str(record.subject), "--repo", record.repo,
                   "--json", "labels"])
    if viewed.returncode != 0:
        return False
    try:
        labels = json.loads(viewed.stdout or "{}").get("labels", [])
    except ValueError:
        return False
    return TRIAGING in {label.get("name") for label in labels if isinstance(label, dict)}


def apply_route(record, result: IntakeResult) -> str | None:
    """Idempotently project the already-durable route, proving it before claim release."""
    from agentflow.loop import _release_triage, _run
    try:
        snapshot = json.loads(record.input_ptr or "")["snapshot"]
        number = int(record.subject)
    except (ValueError, KeyError, TypeError):
        return None
    viewed = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "title,labels"])
    if viewed.returncode != 0:
        return None
    issue = json.loads(viewed.stdout or "{}")
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    apply_intake(record.repo, number, issue.get("title", snapshot.get("title", "")), labels, result)
    if not intake_result_is_durable(record.repo, number, result):
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
    from agentflow.loop import _release_triage, _run
    from agentflow.notify import notify
    number = int(record.subject)
    reason = record.hold_reason or "continuation budget exhausted"
    result = _held(reason)
    viewed = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "title,labels,comments"])
    if viewed.returncode != 0:
        return None
    issue = json.loads(viewed.stdout or "{}")
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
