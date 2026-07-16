"""Build behind the session coordinator, wired into the daemon's dispatch (issue #103).

This is the seam that turns the rollout phase into action. In **legacy** phase the daemon's
existing build path is untouched; in **draining** phase no new Build of either kind launches,
but the coordinator keeps reconciling the records that still own work; in **coordinated** phase
a ready issue becomes exactly one Build submission, the coordinator owns its continuation,
admission, and completion, and the live board becomes a projection of its running records.

The pure parts — mapping a ready issue to a submission, deriving the phase without disturbing a
never-created store, spotting the current-format sessions a drain must wait on, and projecting
running records — are exercised directly. The production factory wires the coordinator's Build
adapter to the real GitHub PR check and worktree, following the same live-orchestration path the
legacy builder uses (not unit-tested, ADR 0020).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from agentflow.coordinator import (BuildStageAdapter, Coordinator, MODE_COORDINATED, Phase,
                                   Rollout, tracer)
from agentflow.coordinator.rollout import COORDINATED, DRAINING, LEGACY
from agentflow.coordinator.store import default_store_path

BUILD_POOLS = ("claude", "codex")


def build_submission(cfg, issue: dict, tool: str):
    """Translate one ready issue and its chosen tool into a single Build stage submission — the
    minimal facts the coordinator needs (ADR 0030). The durable input pointer is the full build
    brief the provider session runs, so a recovered attempt rebuilds the same prompt. Pure: the
    issue→submission mapping is the test surface. Returns ``None`` when the issue lacks the
    complexity gate a build requires (ADR 0018), so a mis-labelled issue never becomes an
    attempt."""
    from agentflow.coordinator import Submission
    from agentflow.loop import (BUILD_PROMPT, _builder_worktree, _surfaces_phrase,
                                complexity_from_labels, effort_from_labels, slug, ui_surfaces)
    n = issue["number"]
    labels = [lbl["name"] for lbl in issue.get("labels", [])]
    complexity = complexity_from_labels(labels)
    if complexity is None:
        return None
    sl = slug(issue["title"])
    brief = BUILD_PROMPT.format(
        repo=cfg.repo, n=n, title=issue.get("title", ""), body=issue.get("body") or "",
        effort=effort_from_labels(labels).value,
        surfaces=_surfaces_phrase(ui_surfaces(cfg.workdir)))
    return Submission(
        repo=cfg.repo, subject=str(n), stage="build", pool=tool,
        complexity=complexity.value, effort=effort_from_labels(labels).value,
        source=_builder_worktree(cfg, tool, n, sl), claim=True, input_ptr=brief)


def legacy_evidence(live_sessions, coordinator_sources) -> tuple[str, ...]:
    """The current-format sessions a forward drain must wait on: live-board entries not backed by
    a coordinator running record. These are legacy provider sessions still finishing (or stale
    entries) that could be mistaken for coordinator-owned work, so activation waits for them and
    names them rather than clearing them (issue #103). Pure — the test surface."""
    evidence: list[str] = []
    for session in live_sessions:
        if os.path.realpath(session.get("worktree", "")) in coordinator_sources:
            continue  # this board entry is the coordinator's own projection
        repo = session.get("repo", "?")
        number = session.get("number", "?")
        evidence.append(f"{repo}#{number} legacy session live ({session.get('stage', '?')})")
    return tuple(evidence)


def activation_evidence(repos, live_sessions, records) -> tuple[str, ...]:
    """Name every current-format fact that prevents a safe forward activation.

    The live board is only one fact. A legacy Build may also have left its GitHub claim, an
    active PID marker, or a registered/unregistered Build worktree. Coordinator-owned sources
    and claims are excluded; everything else is named rather than cleared or guessed at.
    """
    from agentflow.loop import BUILDING, _run
    from agentflow.runner import _active_marker, _registered_worktrees

    sources = {os.path.realpath(r.source) for r in records if r.source and not r.retired}
    evidence = list(legacy_evidence(live_sessions, sources))
    owned_by_repo = {cfg.repo: tracer.owned_issues(records, cfg.repo) for cfg in repos}
    for cfg in repos:
        claims = _run(["gh", "api", "--paginate", "--slurp", "-X", "GET",
                       f"repos/{cfg.repo}/issues", "-f", "state=open",
                       "-f", f"labels={BUILDING}", "-f", "per_page=100"])
        if claims.returncode != 0:
            evidence.append(f"{cfg.repo} building claims unreadable")
        else:
            try:
                pages = json.loads(claims.stdout or "[]")
            except json.JSONDecodeError:
                evidence.append(f"{cfg.repo} building claims unreadable")
            else:
                if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                    evidence.append(f"{cfg.repo} building claims unreadable")
                    pages = []
                for issue in (item for page in pages for item in page):
                    number = issue.get("number")
                    if isinstance(number, int) and number not in owned_by_repo[cfg.repo]:
                        evidence.append(f"{cfg.repo}#{number} legacy building claim")

        root = Path(cfg.workdir) / ".agentflow" / "worktrees"
        registered = _registered_worktrees(cfg.workdir)
        if registered is None:
            evidence.append(f"{cfg.repo} worktree registry unreadable")
            continue
        registered_paths = {os.path.realpath(path): Path(path) for path, _branch in registered}
        candidates: dict[str, Path] = {}
        for path in registered_paths.values():
            try:
                rel = path.resolve().relative_to(root.resolve())
            except (OSError, ValueError):
                continue
            if len(rel.parts) >= 2 and rel.parts[0] in BUILD_POOLS and rel.parts[1].startswith("issue-"):
                candidates[os.path.realpath(path)] = path
        for tool in BUILD_POOLS:
            for path in (root / tool).glob("issue-*") if (root / tool).exists() else ():
                candidates.setdefault(os.path.realpath(path), path)

        for real, path in sorted(candidates.items()):
            if real in sources:
                continue
            label = f"{cfg.repo} legacy worktree {path}"
            if real not in registered_paths:
                evidence.append(f"{label} is unregistered")
                continue
            marker = _active_marker(path)
            if marker is not None and marker.exists():
                evidence.append(f"{label} has PID marker")
            status = _run(["git", "-C", str(path), "status", "--porcelain",
                           "--untracked-files=all"])
            if status.returncode != 0:
                evidence.append(f"{label} state unreadable")
            elif status.stdout.strip():
                evidence.append(f"{label} is dirty")
            else:
                evidence.append(f"{label} is ambiguous")
    return tuple(dict.fromkeys(evidence))


def resolve_phase(rollout: Rollout, repos, live_sessions, *, store_path=None) -> Phase:
    """Derive this cycle's phase from the durable rollout mode and the observed world, without
    ever creating a store that never existed. In the steady legacy state (no coordinator has run)
    this is a cheap ``legacy`` with no filesystem or GitHub reads."""
    path = Path(store_path or default_store_path())
    records = tracer.load_records(path) if path.exists() else []
    if rollout.mode == MODE_COORDINATED:
        return rollout.phase(legacy_evidence=activation_evidence(repos, live_sessions, records))
    return rollout.phase(coordinator_active=tracer.coordinator_active(records))


def owned_issues(cfg, *, store_path=None) -> set[int]:
    """The issues in ``cfg.repo`` a coordinator record still owns — the set legacy claim
    reclamation must never strip (ADR 0028). Empty (and side-effect free) when no store exists."""
    path = Path(store_path or default_store_path())
    if not path.exists():
        return set()
    return tracer.owned_issues(tracer.load_records(path), cfg.repo)


def owned_worktrees(cfg, *, store_path=None) -> set[str]:
    """Coordinator-owned sources in ``cfg.repo`` that startup recovery must retain."""
    path = Path(store_path or default_store_path())
    if not path.exists():
        return set()
    return {
        os.path.realpath(record.source)
        for record in tracer.load_records(path)
        if record.repo == cfg.repo and record.source and not record.retired
    }


# --- production wiring (live orchestration; not unit-tested, ADR 0020) -------------------

def build_coordinator(_log=None) -> Coordinator:
    """The daemon's one Build coordinator: its Build adapter verifies the real PR outcome and
    reuses the retained worktree, and its admission gate enables Build alone so every other
    logical stage stays queued (issue #103)."""
    adapter = BuildStageAdapter(
        pr_exists=_pr_exists, worktree_ready=_worktree_ready, handoff=_hold_build)
    return Coordinator(adapter=adapter, gate=tracer.build_only_gate,
                       log=_log or (lambda _line: None))


def _pr_exists(record) -> bool:
    """Whether the expected PR is open for the record's owned branch (the Build outcome)."""
    from agentflow.loop import _run
    parsed = _source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, _wt = parsed
    r = _run(["gh", "pr", "list", "--repo", record.repo, "--head", branch,
              "--state", "all", "--json", "headRefName,url", "--limit", "1"])
    if r.returncode != 0:
        return False
    return any(pr.get("headRefName") == branch
               for pr in json.loads(r.stdout or "[]"))


def _source_facts(record):
    if not record.source or "/.agentflow/worktrees/" not in record.source:
        return None
    workdir, tail = record.source.split("/.agentflow/worktrees/", 1)
    if not tail.startswith(f"{record.pool}/issue-") or record.lineage != record.pool:
        return None
    return workdir, f"agentflow/{tail}", Path(record.source)


def _worktree_ready(record) -> bool:
    """Prepare the record's owned branch/worktree before admission (ADR 0030). An existing
    worktree is reused *as it is* — a continuation must keep its local changes, so it is never
    rebuilt — and an absent one is created fresh off ``origin/main`` on the branch the record
    owns. Any git failure returns False, so admission is skipped with no permit and no attempt
    consumed. Live orchestration, not unit-tested (ADR 0020)."""
    from agentflow.loop import _run
    from agentflow.runner import ClaudeRunner, CodexRunner, _worktree_is_registered
    parsed = _source_facts(record)
    if parsed is None:
        return False
    workdir, branch, wt = parsed
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    if wt.exists():
        if not _worktree_is_registered(workdir, wt):
            return False
        current = _run(["git", "-C", str(wt), "branch", "--show-current"])
        if current.returncode != 0 or current.stdout.strip() != branch:
            return False
        try:
            runner.provision(wt)
        except subprocess.CalledProcessError:
            return False
        return True  # retained worktree — reuse across the continuation, never recreate it
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    have = _run(["git", "-C", workdir, "show-ref", "--quiet",
                 f"refs/heads/{branch}"]).returncode == 0
    add = ["git", "-C", workdir, "worktree", "add"]
    add += [str(wt), branch] if have else ["-b", branch, str(wt), "origin/main"]
    if _run(add).returncode != 0:
        return False
    try:
        runner.provision(wt)
    except subprocess.CalledProcessError:
        return False
    return True


def _hold_build(record) -> str | None:
    """Create and prove Build's exhaustion handoff without touching its worktree.

    The issue comment and ``needs-grilling`` label are the durable proof. A repeat after a
    daemon crash observes the same comment and does not notify again; the visible building
    claim is released only after the hold exists.
    """
    from agentflow.intake import apply_intake
    from agentflow.loop import BUILDING, _run, held_build_result
    from agentflow.notify import notify

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    viewed = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "title,labels,comments"])
    if viewed.returncode != 0:
        return None
    try:
        issue = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    labels = [label.get("name", "") for label in issue.get("labels", [])]
    result = held_build_result(
        "continuation budget exhausted", f"the retained worktree `{record.source}`")
    already_posted = any(
        comment.get("body", "").strip() == result.body.strip()
        for comment in issue.get("comments", [])
    )
    apply_intake(record.repo, number, issue.get("title", ""), labels, result)
    _run(["gh", "issue", "edit", str(number), "--repo", record.repo,
          "--remove-label", BUILDING])

    proved = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "labels,comments,url"])
    if proved.returncode != 0:
        return None
    try:
        state = json.loads(proved.stdout or "{}")
    except json.JSONDecodeError:
        return None
    final_labels = {label.get("name") for label in state.get("labels", [])}
    has_comment = any(
        comment.get("body", "").strip() == result.body.strip()
        for comment in state.get("comments", [])
    )
    if "agentflow:needs-grilling" not in final_labels or BUILDING in final_labels or not has_comment:
        return None
    url = state.get("url") or f"https://github.com/{record.repo}/issues/{number}"
    if not already_posted:
        notify("agentflow needs you",
               f"{record.repo} #{number}: Build continuation budget exhausted", url)
    return str(url)


def reconcile_and_project(coord: Coordinator, phase: Phase, *, _log=None) -> list:
    """Reconcile every Build pool and republish the live board as a projection of the running
    records (ADR 0030). Returns the terminal outcomes settled this cycle."""
    from agentflow import live
    outcomes = []
    now = int(time.time())
    for pool in BUILD_POOLS:
        outcomes.extend(coord.cycle(pool, now=now))
    records = tracer.load_records()
    owned = {os.path.realpath(r.source) for r in records if r.source and not r.retired}
    live.replace_projection(
        tracer.live_projection(records),
        owned_worktrees=None if phase.name == COORDINATED else owned,
    )
    return outcomes
