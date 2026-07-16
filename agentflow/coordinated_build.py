"""Intake, Build, Review, and Revise behind the session coordinator, wired into dispatch
(issues #103–#106).

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
blocking Review to a Revise, and a completed Revise back to a Review; the ``MAX_REVISES``-capped
auto-revise product policy (ADR 0004) that continuation attempts never expand; deriving the phase
without disturbing a never-created store; spotting the current-format sessions a drain must wait
on; and projecting running records — are exercised directly. The production factory wires the
coordinator's stage adapters to the real GitHub PR check, verdict parse, branch head, and
worktrees, following the same live-orchestration path the legacy builder and reviewer use.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

from agentflow.coordinator import (BuildStageAdapter, Coordinator, IntakeStageAdapter, MODE_COORDINATED, Phase,
                                   RespondStageAdapter, ReviewStageAdapter, ReviseStageAdapter, Rollout,
                                   StageRouter, tracer)
from agentflow.coordinator.rollout import COORDINATED, DRAINING, LEGACY
from agentflow.coordinator.store import ReservationLimits, StoreUnavailable, default_store_path
from agentflow.gate import MAX_REVISES

BUILD_POOLS = ("claude", "codex")
LEGACY_SESSION_POOLS = BUILD_POOLS + tuple(f"{tool}-intake" for tool in BUILD_POOLS)


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
    reads it from the durable record instead of a mutable issue label (ADR 0018). A review that
    follows a Revise carries that revise round in its identity, so an evidence-only revision — same
    head SHA, new durable proof — still opens a genuinely new review with a fresh budget, never the
    retired prior review's record. It points at a fresh read-only review worktree the reviewer
    checks out at that SHA. Cross-tool review is always the deep safety net. Pure: the mapping is
    the test surface (ADR 0020). Returns ``None`` if the Build worktree or head SHA is
    unreadable."""
    from agentflow.coordinator import Submission
    from agentflow.reviewer import REVIEW_PROMPT, review_worktree
    parts = _build_source_parts(build_record)
    if parts is None or not head_sha:
        return None
    workdir, slug = parts
    brief = REVIEW_PROMPT.format(
        pr=pr_number, acceptance=acceptance or "(none provided)",
        surfaces=surfaces or "any user-facing surface")
    completed_rounds = (build_record.round + 1 if build_record.stage == "revise"
                        else build_record.round)
    return Submission(
        repo=build_record.repo, subject=build_record.subject, stage="review",
        target=head_sha, pool=reviewer_tool, complexity="deep",
        source=str(review_worktree(workdir, reviewer_tool, pr_number, slug)),
        claim=True, input_ptr=brief, builder_lineage=build_record.pool,
        builder_complexity=build_record.complexity, round=completed_rounds,
        transfer_from=build_record.identity)


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
    the reviewed head SHA it must supersede (its immutable target, together with the review's
    revise round — so a later blocking review, even one re-reviewing an unchanged head SHA, is a
    genuinely fresh revise stage), and assumes the Review's change claim. Pure: the mapping is the
    test surface (ADR 0020). Returns ``None`` if the builder worktree cannot be reconstructed or
    the reviewed SHA is missing."""
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
        round=review_record.round, transfer_from=review_record.identity)


def respond_submission(cfg, pr_number, branch, comment, target):
    """Translate one unanswered maintainer comment on an existing agentflow PR into a single
    Respond stage submission — the minimal facts the coordinator needs (ADR 0030). Respond adopts
    the change's *original tool lineage* and its retained PR branch/worktree — both recovered from
    the branch name (``agentflow/<tool>/issue-<n>-<slug>``), so capacity on the other pool can never
    silently switch this code-writing continuation — is bound to the maintainer comment it answers
    (its immutable ``target``, so a later comment opens a genuinely new Respond with a fresh budget),
    and holds the ``building`` change claim while it waits. Pure: the mapping is the test surface
    (ADR 0020). Returns ``None`` when the branch is not an agentflow PR branch or the comment target
    is missing."""
    from agentflow.coordinator import Submission
    from agentflow.gate import respond_reply_disclaimer
    from agentflow.loop import _BRANCH_RE, RESPOND_PROMPT, _builder_worktree
    m = _BRANCH_RE.match(branch or "")
    if m is None or not target:
        return None
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    brief = RESPOND_PROMPT.format(
        n=pr_number, comment=comment, disclaimer=respond_reply_disclaimer(str(target)))
    return Submission(
        repo=cfg.repo, subject=str(n), stage="respond", target=str(target),
        pool=tool, complexity="deep", source=_builder_worktree(cfg, tool, n, sl),
        claim=True, input_ptr=brief, builder_lineage=tool)


def revise_round_budget_remains(records, repo, subject) -> bool:
    """Whether the auto-revise product cap (ADR 0004's revise round, relaxed to ``MAX_REVISES``
    rounds by ADR 0020's convergence bail) still has room for this issue — fewer than
    ``MAX_REVISES`` *logical* Revise records exist for it, regardless of how many continuation
    attempts each one used. This keeps the per-stage continuation budget separate from the product
    loop: continuation attempts never reset or expand the round cap. Pure — the test surface
    (ADR 0020)."""
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

    The live board is only one fact. A legacy Build or Intake may also have left its GitHub
    claim, active PID marker, or registered/unregistered worktree. Coordinator-owned sources
    and claims are excluded; everything else is named rather than cleared or guessed at.
    """
    from agentflow.loop import BUILDING, TRIAGING, _run
    from agentflow.runner import _active_marker, _registered_worktrees

    sources = {os.path.realpath(r.source) for r in records if r.source and not r.retired}
    evidence = list(legacy_evidence(live_sessions, sources))
    # Ownership is resolved per claim type: an Intake record owns only its issue's `triaging`
    # claim, a Build/Review/Revise record only its issue's `building` claim. Keying the exclusion
    # per lane stops one type's live record from hiding the other type's stale legacy claim.
    owned_by_lane = {(cfg.repo, lane): tracer.owned_issues(records, cfg.repo, lane=lane)
                     for cfg in repos for lane in ("building", "triaging")}
    for cfg in repos:
        for claim_label, lane in ((BUILDING, "building"), (TRIAGING, "triaging")):
            claims = _run(["gh", "api", "--paginate", "--slurp", "-X", "GET",
                           f"repos/{cfg.repo}/issues", "-f", "state=open",
                           "-f", f"labels={claim_label}", "-f", "per_page=100"])
            if claims.returncode != 0:
                evidence.append(f"{cfg.repo} {lane} claims unreadable")
                continue
            try:
                pages = json.loads(claims.stdout or "[]")
            except json.JSONDecodeError:
                evidence.append(f"{cfg.repo} {lane} claims unreadable")
                continue
            if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
                evidence.append(f"{cfg.repo} {lane} claims unreadable")
                continue
            for issue in (item for page in pages for item in page):
                number = issue.get("number")
                if isinstance(number, int) and number not in owned_by_lane[(cfg.repo, lane)]:
                    evidence.append(f"{cfg.repo}#{number} legacy {lane} claim")

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
            if (len(rel.parts) >= 2 and rel.parts[0] in LEGACY_SESSION_POOLS
                    and rel.parts[1].startswith("issue-")):
                candidates[os.path.realpath(path)] = path
        for tool in LEGACY_SESSION_POOLS:
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


def owned_issues(cfg, *, store_path=None, lane=None) -> set[int]:
    """The issues in ``cfg.repo`` a coordinator record still owns — the set legacy claim
    reclamation must never strip (ADR 0028). Empty (and side-effect free) when no store exists.

    ``lane`` scopes ownership to one claim type: ``"building"`` (Build/Review/Revise) or
    ``"triaging"`` (Intake). The build and triage reclamation passes each pass their own lane so
    one claim type's live record never shields the other type's stale claim (issue #106)."""
    path = Path(store_path or default_store_path())
    if not path.exists():
        return set()
    return tracer.owned_issues(tracer.load_records(path), cfg.repo, lane=lane)


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
    """The daemon's coordinator for Intake, Build, Review, Revise, and Respond (issues #103–#107).
    Its Build adapter verifies the real PR outcome and reuses the retained worktree; its Review
    adapter verifies a durable verdict for the exact PR head SHA and recreates the read-only
    checkout; its Revise adapter verifies a pushed revision on the same branch and reuses that
    retained worktree; its Respond adapter verifies the marked reply plus any pushed change on that
    same branch and releases the change claim on completion; and its admission gate keeps Mockup
    queued. One :class:`StageRouter` dispatches each adapter call on the record's stage."""
    from agentflow import coordinated_intake
    intake = IntakeStageAdapter(
        worktree_reset=coordinated_intake.reset_worktree,
        apply_route=coordinated_intake.apply_route,
        claim_ready=coordinated_intake.intake_claim_ready,
        worktree_dispose=coordinated_intake.dispose_worktree,
        handoff=coordinated_intake.hold_intake)
    build = BuildStageAdapter(
        pr_exists=_pr_exists, worktree_ready=_worktree_ready, handoff=_hold_build)
    review = ReviewStageAdapter(
        verdict_ready=_verdict_ready, worktree_reset=_review_worktree_reset, handoff=_park_pr)
    revise = ReviseStageAdapter(
        revision_ready=_revision_ready, worktree_ready=_worktree_ready, handoff=_park_pr)
    respond = RespondStageAdapter(
        reply_ready=_reply_ready, worktree_ready=_worktree_ready, handoff=_park_respond,
        settle=_settle_respond)
    router = StageRouter({"intake": intake, "build": build, "review": review, "revise": revise,
                          "respond": respond})
    return Coordinator(adapter=router, gate=_production_gate(),
                       log=_log or (lambda _line: None))


class _ProductionGate:
    """One dispatch cycle's composed durable admission policy."""

    def __init__(self) -> None:
        from collections import Counter
        self._paced = Counter()
        self._active: dict[str, bool] = {}

    def __call__(self, record) -> bool:
        from agentflow import balancer
        if not tracer.build_review_revise_gate(record):
            return False
        try:
            status = balancer._query_pool(record.pool)
        except Exception:
            return False
        if not status or not status.clear:
            return False
        self._active[record.pool] = status.active
        return not (status.active and self._paced[record.pool] >= balancer.ACTIVE_PACE)

    @staticmethod
    def reservation_limits(record) -> ReservationLimits:
        """The global limits the store enforces with the running-row reservation."""
        from agentflow import dispatch
        lane = {"intake": "triage", "build": "build", "review": "build", "revise": "build",
                "respond": "respond"}
        stage_lane = lane.get(record.stage, record.stage)
        return ReservationLimits(
            machine_ceiling=dispatch.MACHINE_CEILING,
            stage_cap=dispatch.STAGE_CAPS.get(stage_lane, 1),
            stage_lane=stage_lane,
            lane_by_stage=lane,
        )

    def started(self, record) -> None:
        """Charge operator pacing only after the provider start is durable."""
        if self._active.get(record.pool, False):
            self._paced[record.pool] += 1


def _production_gate():
    """Compose stage enablement, headroom, machine/stage caps, and operator pacing.

    Running durable records are the concurrency ledger. The closure lasts one daemon dispatch
    cycle, so its active-pool counter is exactly the per-cycle pacing budget.
    """
    return _ProductionGate()


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
    rebuilt. An absent Build worktree may start a new branch from ``origin/main``; a continuation
    stage may only recover the existing branch from its local or remote PR ref. Any git failure
    returns False, so admission is skipped with no permit and no attempt consumed."""
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
    if have:
        add += [str(wt), branch]
    else:
        remote = _run(["git", "-C", workdir, "show-ref", "--quiet",
                       f"refs/remotes/origin/{branch}"]).returncode == 0
        if remote:
            add += ["-b", branch, str(wt), f"origin/{branch}"]
        elif record.stage == "build":
            add += ["-b", branch, str(wt), "origin/main"]
        else:
            return False
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
    after a daemon crash observes the same comment and does not notify again. Live orchestration;
    exercised with faked GitHub reads in ``tests/test_revise_tracer.py``."""
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


def _park_respond(record) -> str | None:
    """Create Respond's record-specific park proof and idempotent phone notification.

    A generic Review park may already be present on the PR, so it cannot prove that this exact
    maintainer-comment target exhausted its Respond budget. The stable ntfy sequence id closes the
    crash window between posting the durable comment and recording completion locally: a replay
    replaces the same notification instead of multiplying it.
    """
    from agentflow.loop import _pr_comments, _run
    from agentflow.notify import notify

    pr = _park_pr_number(record)
    if pr is None or not record.target:
        return None
    proof = f"<!-- agentflow-respond-park-target:{record.target} -->"
    comments = _pr_comments(record.repo, pr)
    if comments is None:
        return None
    already = any(proof in comment.get("body", "") for comment in comments)
    if not already:
        body = ("> *agentflow: Respond parked for human review.*\n"
                f"{proof}\n\n"
                f"Respond could not finish answering maintainer comment `{record.target}` "
                "within its continuation budget. The PR branch and local work were retained.")
        posted = _run(["gh", "pr", "comment", str(pr), "--repo", record.repo,
                       "--body", body])
        if posted.returncode != 0:
            return None
    proved = _pr_comments(record.repo, pr)
    if proved is None or not any(proof in comment.get("body", "") for comment in proved):
        return None
    url = f"https://github.com/{record.repo}/pull/{pr}"
    sequence = "respond-" + hashlib.sha256(record.identity.encode()).hexdigest()[:24]
    notify("agentflow needs you",
           f"{record.repo} PR #{pr}: Respond parked for maintainer comment {record.target}",
           url, sequence_id=sequence)
    return url


# --- Revise stage: pushed-revision outcome on the retained branch (live; ADR 0020) ------

def _revision_ready(record, obs) -> bool:
    """The Revise outcome is a verified pushed revision **or** the required durable non-code proof,
    read from GitHub independently of how the reviser exited (ADR 0028, issue #105):

    - a pushed revision: the PR branch head SHA has moved past the reviewed SHA the revise was
      opened against (``record.target``) — *descends from it*, so a force-push back to an older
      commit never counts; or
    - the required non-code proof: a durable agentflow-marked PR comment carrying attached evidence
      (e.g. a before/after screenshot), the way a finding that asks to *show* something is answered
      without a code change. The comment must be *created after this revise record was submitted*
      (its durable ``created_at``, which survives a restart) — a marked screenshot left during the
      Build or a prior revise round predates this round and cannot complete it (issue #118).

    A branch whose head still equals the reviewed SHA and carries no such evidence comment pushed
    and proved nothing, so it stays incomplete and continues. Live orchestration; exercised with
    faked GitHub reads in ``tests/test_revise_tracer.py``."""
    from agentflow.loop import _pr_comments, _run
    parsed = _source_facts(record)
    if parsed is None or not record.target:
        return False
    _workdir, branch, wt = parsed
    r = _run(["gh", "pr", "list", "--repo", record.repo, "--head", branch, "--state", "open",
              "--json", "headRefOid,number", "--limit", "1"])
    if r.returncode != 0:
        raise RuntimeError(f"cannot verify Revise outcome for {record.repo}:{branch}")
    prs = json.loads(r.stdout or "[]")
    if not prs:
        return False
    head = prs[0].get("headRefOid", "")
    if head and head != record.target:
        # A different head is the pushed revision only when it descends from the reviewed SHA.
        # The retained builder worktree answers that (fetching the branch so the remote head is
        # local); a rewound or rewritten branch falls through to the evidence check instead of
        # completing. With no worktree to ask, the head comparison stands alone.
        if not wt.exists():
            return True
        _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", branch])
        if _run(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                 record.target, head]).returncode == 0:
            return True
    # No new code, but an evidence-only revision still completes on its durable non-code proof: an
    # agentflow-authored PR comment (our marker, never the maintainer's) that attaches evidence
    # and postdates this revise round.
    comments = _pr_comments(record.repo, prs[0].get("number"))
    if comments is None:
        return False
    return any(_round_evidence(c, record.created_at) for c in comments)


def _round_evidence(comment: dict, opened_at: int) -> bool:
    """Whether one PR comment is the current revise round's durable non-code proof: agentflow-
    marked, carrying attached image evidence, and created strictly after the revise record's
    durable submission time — so evidence left before this round opened can never complete it,
    however many times it is re-observed (issue #118). A record from before submission times were
    stamped carries ``created_at == 0`` and keeps the unanchored behavior; a comment whose
    ``createdAt`` is missing or unparseable cannot be proven to postdate the round, so it fails
    closed."""
    from agentflow.gate import PR_MARK, has_image_evidence
    from agentflow.runner import _iso_to_epoch
    body = comment.get("body", "") or ""
    if PR_MARK not in body or not has_image_evidence(body):
        return False
    if not opened_at:
        return True
    created = _iso_to_epoch(comment.get("createdAt", "") or "")
    return created is not None and created > opened_at


# --- Respond stage: posted-reply outcome on the retained PR branch (live; ADR 0020) ------

def _reply_ready(record, obs) -> bool:
    """The Respond outcome is the marked agentflow reply to the maintainer comment this record
    answers, plus any branch change verified pushed (ADR 0028, issue #107) — read from GitHub
    independently of how the responder exited:

    - the reply: a marked agentflow comment names this record's immutable maintainer-comment
      target, so another reply or generic agentflow comment cannot satisfy it; and
    - verified pushed: the retained PR-branch worktree holds no commit absent from the pushed remote
      branch head *and* no uncommitted change at all. A responder that committed a small fix but
      never pushed it left the remote branch unchanged; one that edited a file but never committed it
      (a modified tracked file, a staged change, or an untracked new file) never turned that change
      into a pushed commit either. Both leave the stage incomplete so it continues on that same
      retained worktree.

    A record without that exact targeted reply stays incomplete. Live orchestration; exercised
    through the Coordinator/Respond adapter seam in ``tests/test_respond_tracer.py``."""
    from agentflow.gate import respond_reply_posted
    from agentflow.loop import _pr_comments, _run
    parsed = _source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, wt = parsed
    pr = _open_pr_for_branch(record.repo, branch)
    if pr is None:
        return False
    comments = _pr_comments(record.repo, pr.get("number"))
    if comments is None or not respond_reply_posted(comments, record.target or ""):
        return False   # no durable reply bound to this record's maintainer-comment target
    # A reply exists. The owned worktree is mandatory evidence: without it there is no way to
    # prove that a requested branch change was either pushed or never left locally. Fail closed and
    # let preparation recover the PR branch before another attempt.
    if not wt.exists():
        return False
    head = pr.get("headRefOid") or ""
    fetched = _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", branch])
    if fetched.returncode != 0:
        return False
    ahead = _run(["git", "-C", str(wt), "rev-list", "--count", f"{head}..HEAD"])
    if not head or ahead.returncode != 0 or ahead.stdout.strip() not in ("", "0"):
        return False
    # An unpushed change need not be a local commit: a responder can post the reply and leave
    # the requested edit uncommitted in the worktree. Any dirty tracked file, staged change, or
    # untracked new file is such a change that never became a pushed commit, so the stage is not
    # complete. A dirty (or unreadable) worktree keeps it incomplete to resume on that worktree.
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if status.returncode != 0 or status.stdout.strip():
        return False
    return True


def _settle_respond(record) -> str | None:
    """Release Respond's change claim once the reply is durable, retiring the record with no
    successor and no human handoff (issue #107). Drops the ``building`` claim label so the answered
    PR returns to the normal merge pipeline, proves the label is gone, then returns the PR (or issue)
    URL as the durable proof. Idempotent and crash-safe: removing an already-removed label is a
    no-op, so a repeat re-proves the same release. Returns ``None`` when the issue is unreadable or
    the label is still present, so settlement retries next cycle rather than retiring over a claim it
    never released. Live orchestration, not unit-tested (ADR 0020)."""
    from agentflow.loop import BUILDING, _run
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    _run(["gh", "issue", "edit", str(number), "--repo", record.repo, "--remove-label", BUILDING])
    proved = _run(["gh", "issue", "view", str(number), "--repo", record.repo, "--json", "labels,url"])
    if proved.returncode != 0:
        return None
    try:
        state = json.loads(proved.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if BUILDING in {label.get("name") for label in state.get("labels", [])}:
        return None   # the claim label is still present — retry rather than retire over it
    pr = _park_pr_number(record)
    if pr is not None:
        return f"https://github.com/{record.repo}/pull/{pr}"
    return state.get("url") or f"https://github.com/{record.repo}/issues/{number}"


def _open_pr_for_branch(repo: str, branch: str) -> dict | None:
    """The one open PR for the owned branch — its ``number`` and ``headRefOid`` — or ``None`` when
    there is none or the read fails. The shared lookup behind every claim-transfer opener; a
    ``None`` leaves the completed record still claimed, so the next reconcile pass retries the
    transfer rather than stranding it."""
    from agentflow.loop import _run
    listed = _run(["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
                   "--json", "number,headRefOid", "--limit", "1"])
    if listed.returncode != 0:
        return None
    try:
        prs = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if (not isinstance(prs, list) or not prs or not isinstance(prs[0], dict)
            or not isinstance(prs[0].get("number"), int)):
        return None
    return prs[0]


def _open_review_on_completed_build(coord: Coordinator, build_identity: str) -> None:
    """A completed Build opens exactly one waiting Review for the exact PR head SHA and transfers
    the change claim before the Build record retires — no ownership gap (ADR 0028). Submission is
    idempotent on the review identity (repo, subject, review, head SHA), so a repeat or restart
    never opens a second review; a new head SHA is a genuinely new stage. Live — its mapping is
    covered through :func:`review_submission`, and the re-drive after a crash or transient failure
    through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    build = records.get(build_identity)
    if build is None or build.stage != "build":
        return
    facts = _source_facts(build)
    if facts is None:
        return
    _workdir, branch, _wt = facts
    pr = _open_pr_for_branch(build.repo, branch)
    if pr is None:
        return
    reviewer_tool = "codex" if build.pool == "claude" else "claude"
    submission = review_submission(
        build, pr.get("headRefOid", ""), reviewer_tool, pr.get("number"))
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
    ownership gap (ADR 0028). A clean verdict is the merge path, not a revise. The auto-revise
    product cap (``MAX_REVISES`` rounds, ADR 0004) is unchanged: once it is spent, a further
    blocking review parks on its own exhaustion rather than looping. Submission is idempotent on
    the revise identity (repo, subject, revise, reviewed SHA, round), so a repeat or restart never
    opens a second revise. Live — its mapping is covered through :func:`revise_submission`, and the
    park and re-drive paths through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    review = records.get(review_identity)
    if review is None or review.stage != "review" or not review.target:
        return
    verdict = _review_verdict(review)
    if verdict.clean or not verdict.blocking:
        return  # a clean (or non-blocking) verdict is the merge path, not a revise
    if not revise_round_budget_remains(records.values(), review.repo, review.subject):
        # The auto-revise rounds are spent and the review still blocks: no revise, review, or
        # merge stage will ever consume this outcome, so park the PR for a human exactly once and
        # release the review's retained claim rather than leaving the PR owned forever (ADR 0028).
        coord.park_completed(review_identity)
        return
    facts = _revise_builder_source(review)
    # The revise runs at the *original builder* complexity, carried durably on the review record
    # since the build opened it (ADR 0018). Re-reading the issue's live label here would let a
    # changed, removed, or unreadable label alter or block the revise; the stage chain owns it.
    complexity = review.builder_complexity
    if facts is None or not complexity:
        # Missing lineage facts — a pre-#105 review record with no durable builder complexity, or
        # an unreadable builder source — are a permanent condition: no revise can ever open from
        # this record, so park the PR for a human exactly once instead of silently stranding the
        # claim (issue #105: a permanent condition creates exactly one parked-PR handoff).
        coord.park_completed(review_identity)
        return
    findings = "\n".join(f"- {f.summary}" for f in verdict.blocking)
    submission = revise_submission(review, complexity, findings)
    if submission is not None:
        coord.submit_stage(submission)


def _open_review_on_completed_revise(coord: Coordinator, revise_identity: str) -> None:
    """A completed Revise opens exactly one waiting Review bound to the current PR head SHA and
    transfers the change claim before the Revise record retires — no ownership gap (ADR 0028). The
    new review carries the revise round in its identity and starts a fresh review budget, so the
    prior review's record is never reused — even for an evidence-only revision whose head SHA never
    moved. Submission is idempotent on that identity, so a repeat or restart never opens a second
    review. Live — its mapping is covered through :func:`review_submission`, and the evidence-only
    and re-drive paths through ``tests/test_revise_tracer.py``."""
    records = {record.identity: record for record in tracer.load_records()}
    revise = records.get(revise_identity)
    if revise is None or revise.stage != "revise":
        return
    facts = _source_facts(revise)  # revise reuses the builder worktree, so this parses its branch
    if facts is None:
        return
    _workdir, branch, _wt = facts
    pr = _open_pr_for_branch(revise.repo, branch)
    if pr is None:
        return
    reviewer_tool = "codex" if revise.builder_lineage == "claude" else "claude"
    submission = review_submission(
        revise, pr.get("headRefOid", ""), reviewer_tool, pr.get("number"))
    if submission is not None:
        coord.submit_stage(submission)


# Each completed stage's claim-transfer opener, keyed by the stage it consumes.
_OPENERS = {"build": _open_review_on_completed_build,
            "review": _open_revise_on_blocking_review,
            "revise": _open_review_on_completed_revise}


def reconcile_and_project(coord: Coordinator, phase: Phase, *, _log=None) -> list:
    """Reconcile every Build/Review/Revise pool and republish the live board as a projection of the
    running records (ADR 0030). A completed Build opens its Review, a blocking Review opens its
    Revise, and a completed Revise opens its next Review — each before the projection, so the claim
    transfers with no ownership gap. The openers are driven from the *durable records*, not this
    cycle's outcomes: any completed record still holding the change claim has no successor yet —
    whether it completed just now, the daemon died between completion and its opener, or a prior
    opener failed on a transient read — so every pass re-drives the transfer idempotently rather
    than stranding the chain (ADR 0028). Returns the terminal outcomes settled this cycle."""
    from agentflow import live
    from agentflow.coordinator.record import COMPLETED
    outcomes = []
    now = int(time.time())
    for pool in BUILD_POOLS:
        outcomes.extend(coord.cycle(pool, now=now))
    # Handoffs are driven from durable state, not only this process's outcomes. A daemon may
    # die after a stage completion is committed but before it consumes the returned outcome.
    # A completed stage keeps its claim until its successor is atomically persisted.
    for record in tracer.load_records():
        if (record.state == COMPLETED and not record.retired and record.claim
                and not record.hold_pending):  # a pending park is already retried by reconcile
            opener = _OPENERS.get(record.stage)
            if opener is not None:
                try:
                    opener(coord, record.identity)
                except StoreUnavailable:
                    # The fail-closed transactional submit refused the transfer — e.g. another
                    # process moved the claim between our durable snapshot and this submit. The
                    # store is the truth; skip and let the next pass re-read it.
                    continue
    records = tracer.load_records()
    owned = {os.path.realpath(r.source) for r in records if r.source and not r.retired}
    live.replace_projection(
        tracer.live_projection(records),
        owned_worktrees=None if phase.name == COORDINATED else owned,
    )
    return outcomes
