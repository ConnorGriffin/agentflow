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


def legacy_evidence(live_sessions, running_sources) -> tuple[str, ...]:
    """The current-format sessions a forward drain must wait on: live-board entries not backed by
    a coordinator running record. These are legacy provider sessions still finishing (or stale
    entries) that could be mistaken for coordinator-owned work, so activation waits for them and
    names them rather than clearing them (issue #103). Pure — the test surface."""
    evidence: list[str] = []
    for session in live_sessions:
        if session.get("worktree") in running_sources:
            continue  # this board entry is the coordinator's own projection
        repo = session.get("repo", "?")
        number = session.get("number", "?")
        evidence.append(f"{repo}#{number} legacy session live ({session.get('stage', '?')})")
    return tuple(evidence)


def resolve_phase(rollout: Rollout, live_sessions, *, store_path=None) -> Phase:
    """Derive this cycle's phase from the durable rollout mode and the observed world, without
    ever creating a store that never existed. In the steady legacy state (no coordinator has run)
    this is a cheap ``legacy`` with no filesystem or GitHub reads."""
    path = Path(store_path or default_store_path())
    records = tracer.load_records(path) if path.exists() else []
    if rollout.mode == MODE_COORDINATED:
        running_sources = {r.source for r in records if r.state == "running"}
        return rollout.phase(legacy_evidence=legacy_evidence(live_sessions, running_sources))
    return rollout.phase(coordinator_active=tracer.coordinator_active(records))


def owned_issues(cfg, *, store_path=None) -> set[int]:
    """The issues in ``cfg.repo`` a coordinator record still owns — the set legacy claim
    reclamation must never strip (ADR 0028). Empty (and side-effect free) when no store exists."""
    path = Path(store_path or default_store_path())
    if not path.exists():
        return set()
    return tracer.owned_issues(tracer.load_records(path), cfg.repo)


# --- production wiring (live orchestration; not unit-tested, ADR 0020) -------------------

def build_coordinator(_log=None) -> Coordinator:
    """The daemon's one Build coordinator: its Build adapter verifies the real PR outcome and
    reuses the retained worktree, and its admission gate enables Build alone so every other
    logical stage stays queued (issue #103)."""
    adapter = BuildStageAdapter(pr_exists=_pr_exists, worktree_ready=_worktree_ready)
    return Coordinator(adapter=adapter, gate=tracer.build_only_gate,
                       log=_log or (lambda _line: None))


def _pr_exists(record) -> bool:
    """Whether the expected PR is open for the record's owned branch (the Build outcome)."""
    from agentflow.loop import _run
    repo, number = record.repo, record.subject
    branch = f"agentflow/{record.pool}/issue-{number}-"
    r = _run(["gh", "pr", "list", "--repo", repo, "--state", "all",
              "--json", "headRefName,url", "--limit", "50"])
    if r.returncode != 0:
        return False
    import json
    return any(str(pr.get("headRefName", "")).startswith(branch)
               for pr in json.loads(r.stdout or "[]"))


def _worktree_ready(record) -> bool:
    """Prepare the record's owned branch/worktree before admission (ADR 0030). An existing
    worktree is reused *as it is* — a continuation must keep its local changes, so it is never
    rebuilt — and an absent one is created fresh off ``origin/main`` on the branch the record
    owns. Any git failure returns False, so admission is skipped with no permit and no attempt
    consumed. Live orchestration, not unit-tested (ADR 0020)."""
    from agentflow.loop import _run
    if not record.source or "/.agentflow/worktrees/" not in record.source:
        return False
    wt = Path(record.source)
    if wt.exists():
        return True  # retained worktree — reuse across the continuation, never recreate it
    workdir, tail = record.source.split("/.agentflow/worktrees/", 1)
    branch = f"agentflow/{tail}"  # {tool}/issue-{n}-{slug}, mirroring the legacy branch scheme
    wt.parent.mkdir(parents=True, exist_ok=True)
    if _run(["git", "-C", workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    have = _run(["git", "-C", workdir, "show-ref", "--quiet",
                 f"refs/heads/{branch}"]).returncode == 0
    add = ["git", "-C", workdir, "worktree", "add"]
    add += [str(wt), branch] if have else ["-b", branch, str(wt), "origin/main"]
    return _run(add).returncode == 0


def reconcile_and_project(coord: Coordinator, *, _log=None) -> list:
    """Reconcile every Build pool and republish the live board as a projection of the running
    records (ADR 0030). Returns the terminal outcomes settled this cycle."""
    from agentflow import live
    outcomes = []
    for pool in BUILD_POOLS:
        outcomes.extend(coord.cycle(pool))
    records = tracer.load_records()
    live.replace_projection(tracer.live_projection(records))
    return outcomes
