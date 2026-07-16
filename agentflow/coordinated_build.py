"""Build, Review, and Revise behind the session coordinator, wired into the daemon's dispatch
(issues #103, #104, #105).

This is the seam that turns the rollout phase into action. In **legacy** phase the daemon's
existing build path is untouched; in **draining** phase no new provider stage of any kind
launches, but the coordinator keeps reconciling the records that still own work; in
**coordinated** phase a ready issue becomes exactly one Build submission, a completed Build opens
exactly one Review bound to the PR head SHA, a blocking Review opens exactly one Revise on the
builder's retained branch, and a completed Revise opens exactly one new Review bound to the new
head SHA — each transferring the change claim before the prior record retires, so there is no
ownership gap. The coordinator owns their continuation, admission, and completion, and the live
board becomes a projection of its running records.

The pure parts — mapping a ready issue to a Build submission, a completed Build to a Review, a
blocking Review to a Revise, and a completed Revise back to a Review; the single-round product
policy that keeps continuation attempts from expanding it; deriving the phase without disturbing a
never-created store; spotting the current-format sessions a drain must wait on; and projecting
running records — are exercised directly. The production factory wires the coordinator's stage
adapters to the real GitHub PR check, verdict parse, branch head, and worktrees, following the same
live-orchestration path the legacy builder and reviewer use (not unit-tested, ADR 0020).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from agentflow.coordinator import (BuildStageAdapter, Coordinator, MODE_COORDINATED, Phase,
                                   ReviewStageAdapter, ReviseStageAdapter, Rollout, StageRouter,
                                   tracer)
from agentflow.coordinator.rollout import COORDINATED, DRAINING, LEGACY
from agentflow.coordinator.store import default_store_path
from agentflow.gate import MAX_REVISES

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


def _build_source_parts(record):
    """The ``(workdir, slug)`` behind a Build record's owned worktree, or ``None``. The slug is
    reused to name the review worktree so both stages of one issue read as a pair on disk."""
    if not record.source or "/.agentflow/worktrees/" not in record.source:
        return None
    workdir, tail = record.source.split("/.agentflow/worktrees/", 1)
    parts = tail.split("/", 1)
    if len(parts) != 2:
        return None
    name = parts[1]
    prefix = f"issue-{record.subject}-"
    return workdir, (name[len(prefix):] if name.startswith(prefix) else name)


def review_submission(build_record, head_sha, reviewer_tool, pr_number,
                      *, acceptance="", surfaces=""):
    """Translate a completed Build (or completed Revise) and its PR head SHA into one Review stage
    submission — the minimal facts the coordinator needs (ADR 0030). The review is bound to the
    *exact* head SHA (its immutable target, so a new head SHA starts a fresh review stage), assumes
    the prior stage's change claim, records the builder's lineage so a same-tool review can finish
    but never auto-merges, and carries the *original builder complexity* forward so a later Revise
    reads it from the durable record instead of a mutable issue label (ADR 0018). It points at a
    fresh read-only review worktree the reviewer checks out at that SHA. Cross-tool review is always
    the deep safety net. Pure: the mapping is the test surface (ADR 0020). Returns ``None`` if the
    Build worktree or head SHA is unreadable."""
    from agentflow.coordinator import Submission
    from agentflow.reviewer import REVIEW_PROMPT, review_worktree
    parts = _build_source_parts(build_record)
    if parts is None or not head_sha:
        return None
    workdir, slug = parts
    brief = REVIEW_PROMPT.format(
        pr=pr_number, acceptance=acceptance or "(none provided)",
        surfaces=surfaces or "any user-facing surface")
    return Submission(
        repo=build_record.repo, subject=build_record.subject, stage="review",
        target=head_sha, pool=reviewer_tool, complexity="deep",
        source=str(review_worktree(workdir, reviewer_tool, pr_number, slug)),
        claim=True, input_ptr=brief, builder_lineage=build_record.pool,
        builder_complexity=build_record.complexity, transfer_from=build_record.identity)


def _revise_builder_source(review_record):
    """The ``(build_worktree, pr_number)`` a Revise adopts from a blocking Review record. The
    revise reuses the *builder's* retained branch/worktree — ``.../<builder_lineage>/issue-<subject>
    -<slug>`` — which the review source (``.../<tool>-review/pr-<pr>-<slug>``) and the record's
    builder lineage together recover, so no second durable field is needed. ``None`` if unreadable."""
    if (not review_record.source or "/.agentflow/worktrees/" not in review_record.source
            or not review_record.builder_lineage):
        return None
    workdir, tail = review_record.source.split("/.agentflow/worktrees/", 1)
    parts = tail.split("/")
    if len(parts) != 2 or not parts[0].endswith("-review"):
        return None
    match = re.match(r"pr-(\d+)-(.+)$", parts[1])
    if match is None:
        return None
    pr_number, slug = int(match.group(1)), match.group(2)
    build_worktree = (f"{workdir}/.agentflow/worktrees/{review_record.builder_lineage}/"
                      f"issue-{review_record.subject}-{slug}")
    return build_worktree, pr_number


def revise_submission(review_record, complexity, findings="", *, surfaces=""):
    """Translate a blocking Review into one Revise stage submission — the minimal facts the
    coordinator needs (ADR 0030). The revise adopts the original builder's retained PR branch and
    worktree, stays pinned to the builder's tool lineage and its original complexity, is bound to
    the reviewed head SHA it must supersede (its immutable target — so a later blocking review at a
    new head SHA is a genuinely fresh revise stage), and assumes the Review's change claim. Pure:
    the mapping is the test surface (ADR 0020). Returns ``None`` if the builder worktree cannot be
    reconstructed or the reviewed SHA is missing."""
    from agentflow.coordinator import Submission
    from agentflow.loop import REVISE_PROMPT
    facts = _revise_builder_source(review_record)
    if facts is None or not review_record.target:
        return None
    build_worktree, pr_number = facts
    brief = REVISE_PROMPT.format(
        n=pr_number, findings=findings or "- (see review)",
        surfaces=surfaces or "any user-facing surface")
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="revise",
        target=review_record.target, pool=review_record.builder_lineage, complexity=complexity,
        source=build_worktree, claim=True, input_ptr=brief,
        builder_lineage=review_record.builder_lineage, builder_complexity=complexity,
        transfer_from=review_record.identity)


def revise_round_budget_remains(records, repo, subject) -> bool:
    """Whether the single auto-revise product round (ADR 0018) is still available for this issue —
    at most ``MAX_REVISES`` *logical* Revise records exist for it, regardless of how many
    continuation attempts each one used. This keeps the per-stage continuation budget separate from
    the product loop: continuation attempts never reset or expand the one-round policy. Pure — the
    test surface (ADR 0020)."""
    rounds = sum(1 for r in records
                 if r.stage == "revise" and r.repo == repo and str(r.subject) == str(subject))
    return rounds < MAX_REVISES


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


def resolve_phase(rollout: Rollout, repos, live_sessions, *, store_path=None,
                  requested_mode: str | None = None) -> Phase:
    """Derive this cycle's phase from the durable rollout mode and the observed world, without
    ever creating a store that never existed. In the steady legacy state (no coordinator has run)
    this is a cheap ``legacy`` with no filesystem or GitHub reads."""
    path = Path(store_path or default_store_path())
    records = tracer.load_records(path) if path.exists() else []
    mode = rollout.mode if requested_mode is None else requested_mode
    if mode == MODE_COORDINATED:
        return rollout.phase(
            legacy_evidence=activation_evidence(repos, live_sessions, records),
            requested_mode=mode,
        )
    return rollout.phase(
        coordinator_active=tracer.coordinator_active(records), requested_mode=mode)


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
    """The daemon's one coordinator for Build, Review, and Revise (issues #103, #104, #105). Its
    Build adapter verifies the real PR outcome and reuses the retained worktree; its Review adapter
    verifies a durable verdict for the exact PR head SHA and recreates the read-only checkout; its
    Revise adapter verifies a pushed revision on the same branch and reuses that retained worktree;
    and its admission gate enables Build, Review, and Revise alone, so every other logical stage
    stays queued. One :class:`StageRouter` dispatches each adapter call on the record's stage."""
    build = BuildStageAdapter(
        pr_exists=_pr_exists, worktree_ready=_worktree_ready, handoff=_hold_build)
    review = ReviewStageAdapter(
        verdict_ready=_verdict_ready, worktree_reset=_review_worktree_reset, handoff=_park_pr)
    revise = ReviseStageAdapter(
        revision_ready=_revision_ready, worktree_ready=_worktree_ready, handoff=_park_pr)
    router = StageRouter({"build": build, "review": review, "revise": revise})
    return Coordinator(adapter=router, gate=tracer.build_review_revise_gate,
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
        raise RuntimeError(f"cannot verify Build PR outcome for {record.repo}:{branch}")
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


# --- Review stage: verdict outcome, read-only checkout, PR park (live; ADR 0020) --------

def _verdict_ready(record, obs) -> bool:
    """The Review outcome is a parsed verdict for the exact reviewed head SHA (``record.target``).
    The reviewer's captured final message is the durable verdict — read by us, never a file in the
    untrusted PR tree — so a parsed verdict naming the target SHA completes review regardless of
    how the reviewer exited; a missing verdict or one for another SHA stays incomplete (ADR 0028)."""
    from agentflow.reviewer import parse_verdict
    if not record.target:
        return False
    return parse_verdict(obs.final_message or "", expected_sha=record.target).parsed


def _review_worktree_reset(record) -> bool:
    """Recreate the read-only review checkout at the exact PR head SHA before admission (ADR 0030).
    Review holds no local edits, so any stale checkout is discarded and rebuilt detached at the
    record's immutable target SHA — the target is never touched. Any git failure returns False, so
    admission is skipped with no permit and no attempt. Live orchestration, not unit-tested (ADR
    0020)."""
    from agentflow.loop import _run
    from agentflow.runner import ClaudeRunner, CodexRunner
    facts = _review_source_facts(record)
    if facts is None or not record.target:
        return False
    workdir, _pr = facts
    wt = Path(record.source)
    if wt.exists():
        _run(["git", "-C", workdir, "worktree", "remove", "--force", str(wt)])
    wt.parent.mkdir(parents=True, exist_ok=True)
    runner = ClaudeRunner() if record.pool == "claude" else CodexRunner()
    try:
        runner.prepare_worktree_detached(workdir, record.target, wt)
        runner.provision(wt)
    except subprocess.CalledProcessError:
        return False
    return True


def _review_source_facts(record):
    """The ``(workdir, pr_number)`` a review worktree encodes, or ``None``. The review source is
    ``.../<tool>-review/pr-<pr>-<slug>``, so the PR number is recoverable for the park handoff
    without a second durable field."""
    if not record.source or "/.agentflow/worktrees/" not in record.source:
        return None
    workdir, tail = record.source.split("/.agentflow/worktrees/", 1)
    parts = tail.split("/")
    if len(parts) != 2 or not parts[0].endswith("-review"):
        return None
    match = re.match(r"pr-(\d+)-", parts[1])
    if match is None:
        return None
    return workdir, int(match.group(1))


def _park_pr_number(record) -> int | None:
    """The PR number to park for a Review or a Revise record. A Review encodes it directly in its
    read-only review worktree path (``.../<tool>-review/pr-<pr>-<slug>``); a Revise instead owns the
    *builder's* branch/worktree (``.../<tool>/issue-<subject>-<slug>``, no PR number), so the open PR
    for that branch is looked up from GitHub. Returns ``None`` when it cannot be resolved, so the
    park handoff stays pending and retries rather than proving a park it never made."""
    facts = _review_source_facts(record)
    if facts is not None:
        return facts[1]
    from agentflow.loop import _run
    parsed = _source_facts(record)
    if parsed is None:
        return None
    _workdir, branch, _wt = parsed
    r = _run(["gh", "pr", "list", "--repo", record.repo, "--head", branch, "--state", "all",
              "--json", "number", "--limit", "1"])
    if r.returncode != 0:
        return None
    prs = json.loads(r.stdout or "[]")
    return prs[0].get("number") if prs else None


def _park_pr(record) -> str | None:
    """Park the reviewed PR for a human and notify once (ADR 0028's exhaustion table). Serves both
    the Review-native park and the Revise-native park — Revise owns a builder worktree, so the PR is
    resolved by branch (:func:`_park_pr_number`). The park comment is the durable proof; a repeat
    after a daemon crash observes the same comment and does not notify again. Live orchestration, not
    unit-tested (ADR 0020)."""
    from agentflow.gate import park
    from agentflow.loop import _pr_comments
    from agentflow.notify import notify
    from agentflow.reviewer import Verdict
    pr = _park_pr_number(record)
    if pr is None:
        return None
    marker = "agentflow: parked for human review"
    comments = _pr_comments(record.repo, pr)
    if comments is None:
        return None
    already = any(marker in comment.get("body", "") for comment in comments)
    if not already:
        park(record.repo, pr, Verdict(clean=False),
             reason="exhausted its review budget without a durable verdict")
    proved = _pr_comments(record.repo, pr)
    if proved is None or not any(marker in comment.get("body", "") for comment in proved):
        return None
    url = f"https://github.com/{record.repo}/pull/{pr}"
    if not already:
        notify("agentflow needs you", f"{record.repo} PR #{pr}: review parked for your action", url)
    return url


# --- Revise stage: pushed-revision outcome on the retained branch (live; ADR 0020) ------

def _revision_ready(record, obs) -> bool:
    """The Revise outcome is a verified pushed revision **or** the required durable non-code proof,
    read from GitHub independently of how the reviser exited (ADR 0028, issue #105):

    - a pushed revision: the PR branch head SHA has moved past the reviewed SHA the revise was
      opened against (``record.target``); or
    - the required non-code proof: a durable agentflow-marked PR comment carrying attached evidence
      (e.g. a before/after screenshot), the way a finding that asks to *show* something is answered
      without a code change.

    A branch whose head still equals the reviewed SHA and carries no such evidence comment pushed and
    proved nothing, so it stays incomplete and continues. Live orchestration, not unit-tested (ADR
    0020)."""
    from agentflow.gate import PR_MARK, has_image_evidence
    from agentflow.loop import _pr_comments, _run
    parsed = _source_facts(record)
    if parsed is None or not record.target:
        return False
    _workdir, branch, _wt = parsed
    r = _run(["gh", "pr", "list", "--repo", record.repo, "--head", branch, "--state", "open",
              "--json", "headRefOid,number", "--limit", "1"])
    if r.returncode != 0:
        raise RuntimeError(f"cannot verify Revise outcome for {record.repo}:{branch}")
    prs = json.loads(r.stdout or "[]")
    if not prs:
        return False
    head = prs[0].get("headRefOid", "")
    if head and head != record.target:
        return True  # a pushed revision advanced the branch past the reviewed SHA
    # No new code, but an evidence-only revision still completes on its durable non-code proof: an
    # agentflow-authored PR comment (our marker, never the maintainer's) that attaches evidence.
    comments = _pr_comments(record.repo, prs[0].get("number"))
    if comments is None:
        return False
    return any(PR_MARK in (c.get("body", "") or "") and has_image_evidence(c.get("body", "") or "")
               for c in comments)


def _open_review_on_completed_build(coord: Coordinator, build_identity: str) -> None:
    """A completed Build opens exactly one waiting Review for the exact PR head SHA and transfers
    the change claim before the Build record retires — no ownership gap (ADR 0028). Submission is
    idempotent on the review identity (repo, subject, review, head SHA), so a repeat or restart
    never opens a second review; a new head SHA is a genuinely new stage. Live, not unit-tested
    (ADR 0020) — its mapping is covered through :func:`review_submission`."""
    from agentflow.loop import _run
    records = {record.identity: record for record in tracer.load_records()}
    build = records.get(build_identity)
    if build is None or build.stage != "build":
        return
    facts = _source_facts(build)
    if facts is None:
        return
    _workdir, branch, _wt = facts
    listed = _run(["gh", "pr", "list", "--repo", build.repo, "--head", branch, "--state", "open",
                   "--json", "number,headRefOid", "--limit", "1"])
    if listed.returncode != 0:
        return
    prs = json.loads(listed.stdout or "[]")
    if not prs:
        return
    reviewer_tool = "codex" if build.pool == "claude" else "claude"
    submission = review_submission(
        build, prs[0].get("headRefOid", ""), reviewer_tool, prs[0].get("number"))
    if submission is not None:
        coord.submit_stage(submission)


def _review_verdict(review):
    """Re-parse the completed Review's durable verdict for its exact reviewed SHA — read from the
    reviewer's captured final message, never a file in the PR tree (ADR 0018/0028). Live, not
    unit-tested (ADR 0020); the parse itself is covered in the reviewer tests."""
    from agentflow.coordinator.providers import ProviderObserver
    from agentflow.reviewer import parse_verdict
    obs = ProviderObserver().observe(review)
    return parse_verdict(obs.final_message or "", expected_sha=review.target)


def _open_revise_on_blocking_review(coord: Coordinator, review_identity: str) -> None:
    """A completed Review whose verdict blocks opens exactly one waiting Revise on the builder's
    retained branch/worktree and transfers the change claim before the Review record retires — no
    ownership gap (ADR 0028). A clean verdict is the merge path, not a revise. The single
    auto-revise product round (ADR 0018) is unchanged: once it is used, a further blocking review
    parks on its own exhaustion rather than looping. Submission is idempotent on the revise identity
    (repo, subject, revise, reviewed SHA), so a repeat or restart never opens a second revise. Live,
    not unit-tested (ADR 0020) — its mapping is covered through :func:`revise_submission`."""
    records = {record.identity: record for record in tracer.load_records()}
    review = records.get(review_identity)
    if review is None or review.stage != "review" or not review.target:
        return
    verdict = _review_verdict(review)
    if verdict.clean or not verdict.blocking:
        return  # a clean (or non-blocking) verdict is the merge path, not a revise
    if not revise_round_budget_remains(records.values(), review.repo, review.subject):
        # The one auto-revise round is spent and the review still blocks: no revise, review, or
        # merge stage will ever consume this outcome, so park the PR for a human exactly once and
        # release the review's retained claim rather than leaving the PR owned forever (ADR 0028).
        coord._park_completed(review_identity)
        return
    facts = _revise_builder_source(review)
    if facts is None:
        return
    # The revise runs at the *original builder* complexity, carried durably on the review record
    # since the build opened it (ADR 0018). Re-reading the issue's live label here would let a
    # changed, removed, or unreadable label alter or block the revise; the stage chain owns it.
    complexity = review.builder_complexity
    if not complexity:
        return
    findings = "\n".join(f"- {f.summary}" for f in verdict.blocking)
    submission = revise_submission(review, complexity, findings)
    if submission is not None:
        coord.submit_stage(submission)


def _open_review_on_completed_revise(coord: Coordinator, revise_identity: str) -> None:
    """A completed Revise opens exactly one waiting Review bound to the *new* PR head SHA and
    transfers the change claim before the Revise record retires — no ownership gap (ADR 0028). The
    new head SHA is a genuinely new review stage with a fresh review budget; the prior SHA's review
    cannot be reused. Submission is idempotent on the new review identity, so a repeat or restart
    never opens a second review. Live, not unit-tested (ADR 0020) — its mapping is covered through
    :func:`review_submission`."""
    from agentflow.loop import _run
    records = {record.identity: record for record in tracer.load_records()}
    revise = records.get(revise_identity)
    if revise is None or revise.stage != "revise":
        return
    facts = _source_facts(revise)  # revise reuses the builder worktree, so this parses its branch
    if facts is None:
        return
    _workdir, branch, _wt = facts
    listed = _run(["gh", "pr", "list", "--repo", revise.repo, "--head", branch, "--state", "open",
                   "--json", "number,headRefOid", "--limit", "1"])
    if listed.returncode != 0:
        return
    prs = json.loads(listed.stdout or "[]")
    if not prs:
        return
    reviewer_tool = "codex" if revise.builder_lineage == "claude" else "claude"
    submission = review_submission(
        revise, prs[0].get("headRefOid", ""), reviewer_tool, prs[0].get("number"))
    if submission is not None:
        coord.submit_stage(submission)


def reconcile_and_project(coord: Coordinator, phase: Phase, *, _log=None) -> list:
    """Reconcile every Build/Review/Revise pool and republish the live board as a projection of the
    running records (ADR 0030). A completed Build opens its Review, a blocking Review opens its
    Revise, and a completed Revise opens its next Review — each before the projection, so the claim
    transfers with no ownership gap. Returns the terminal outcomes settled this cycle."""
    from agentflow import live
    outcomes = []
    now = int(time.time())
    for pool in BUILD_POOLS:
        outcomes.extend(coord.cycle(pool, now=now))
    for outcome in outcomes:
        if outcome.status != "completed":
            continue
        if outcome.stage == "build":
            _open_review_on_completed_build(coord, outcome.identity)
        elif outcome.stage == "review":
            _open_revise_on_blocking_review(coord, outcome.identity)
        elif outcome.stage == "revise":
            _open_review_on_completed_revise(coord, outcome.identity)
    records = tracer.load_records()
    owned = {os.path.realpath(r.source) for r in records if r.source and not r.retired}
    live.replace_projection(
        tracer.live_projection(records),
        owned_worktrees=None if phase.name == COORDINATED else owned,
    )
    return outcomes
