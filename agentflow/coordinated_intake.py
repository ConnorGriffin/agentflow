"""Intake behind the durable session coordinator (issue #106)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agentflow.coordinator import Submission
from agentflow.intake import IntakeResult, apply_intake, intake_prompt, intake_result_is_durable


def intake_submission(cfg, issue: dict, extra: str, tool: str) -> Submission:
    """Map a durable issue snapshot to one idempotent Intake stage submission."""
    n = issue["number"]
    target = issue.get("_intake_target") if extra else None
    source = Path(cfg.workdir) / ".agentflow" / "worktrees" / f"{tool}-intake" / f"issue-{n}"
    snapshot = {
        "number": n, "title": issue.get("title", ""), "body": issue.get("body") or "",
        "labels": [label.get("name", "") for label in issue.get("labels", [])],
        "extra": extra,
    }
    return Submission(repo=cfg.repo, subject=str(n), stage="intake", target=target,
                      pool=tool, complexity="deep", source=str(source), claim=True,
                      input_ptr=json.dumps({"snapshot": snapshot,
                                            "prompt": intake_prompt(cfg.repo, issue, extra)},
                                           sort_keys=True))


def reset_worktree(record) -> bool:
    """Discard and rebuild Intake's read-only checkout from its durable source pointer."""
    from agentflow.loop import _run
    from agentflow.runner import ClaudeRunner, CodexRunner
    if not record.source or not record.input_ptr:
        return False
    try:
        json.loads(record.input_ptr)["snapshot"]
    except (ValueError, KeyError, TypeError):
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
        runner.prepare_worktree_detached(workdir, "origin/main", wt)
        runner.provision(wt)
    except subprocess.CalledProcessError:
        return False
    return True


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
    already = intake_result_is_durable(record.repo, number, result)
    apply_intake(record.repo, number, issue.get("title", snapshot.get("title", "")), labels, result)
    if not intake_result_is_durable(record.repo, number, result):
        return None
    _release_triage(record.repo, number)
    url = f"https://github.com/{record.repo}/issues/{number}"
    if not already and result.route.value in ("grill", "mockup"):
        from agentflow.notify import notify
        notify("agentflow needs you", f"{record.repo} #{number}: {result.route.value}", url)
    return url


def hold_intake(record) -> str | None:
    """Create Intake's single exhaustion handoff and notification."""
    from agentflow.intake import _held
    from agentflow.loop import _release_triage, _run
    from agentflow.notify import notify
    number = int(record.subject)
    result = _held("continuation budget exhausted")
    viewed = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "title,labels,comments"])
    if viewed.returncode != 0:
        return None
    issue = json.loads(viewed.stdout or "{}")
    already = any(c.get("body", "").strip() == result.body.strip()
                  for c in issue.get("comments", []))
    apply_intake(record.repo, number, issue.get("title", ""),
                 [x.get("name", "") for x in issue.get("labels", [])], result)
    if not intake_result_is_durable(record.repo, number, result):
        return None
    _release_triage(record.repo, number)
    url = f"https://github.com/{record.repo}/issues/{number}"
    if not already:
        notify("agentflow needs you", f"{record.repo} #{number}: Intake continuation budget exhausted", url)
    return url
