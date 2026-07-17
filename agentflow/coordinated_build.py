"""All six logical stages behind the session coordinator, wired into dispatch
(issues #103–#108).

Every provider stage enters one durable submission. Build, Review, and Revise transfer one change
claim through their convergence loop; Intake, Mockup, and Respond each own their stage-native
boundary and claim. The coordinator owns continuation, admission, and completion, and the live
board is generated from its running records. There is no legacy provider path or bypass mode.

The pure parts — mapping stage inputs to submissions, the Build/Review/Revise transfers, the
``MAX_REVISES``-capped
auto-revise product policy (ADR 0004) that continuation attempts never expand; deriving the phase
and projecting running records — are exercised directly. The production factory wires the
coordinator's stage adapters to the real GitHub PR check, verdict parse, branch head, and
worktrees, following the same stage-native completion contracts the earlier pipeline established.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from agentflow.coordinator import (BuildStageAdapter, ConverseStageAdapter, Coordinator,
                                   IntakeStageAdapter, MockupStageAdapter,
                                   RespondStageAdapter, ReviewStageAdapter, ReviseStageAdapter,
                                   StageRouter, tracer)
from agentflow.balancer import pick_reviewer
from agentflow.coordinator.store import ReservationLimits, StoreUnavailable, default_store_path
from agentflow.gate import MAX_REVISES

BUILD_POOLS = ("claude", "codex")
_ORPHAN_CLAIM_GRACE_SECONDS = 60 * 60
_REVIEW_CI_OBSERVED: dict[str, bool] = {}


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


def mockup_submission(cfg, issue: dict, tool: str):
    """Translate one eligible held issue into its single durable Mockup variant round.

    The stable identity is ``(repo, issue, mockup)``: repeated discovery returns the same record,
    while the pinned pool and owned branch/worktree preserve tool lineage and local progress across
    fresh-session continuations. The durable prompt reconstructs the exact same visual-design job.
    """
    from agentflow.coordinator import Submission
    from agentflow.loop import (PRODUCE_PROMPT, _MOCKUP_DISCLAIMER, _surfaces_phrase,
                                slug, ui_surfaces)

    n = int(issue["number"])
    sl = slug(issue.get("title", ""))
    branch = f"agentflow/{tool}/mockup-{n}-{sl}"
    source = f"{cfg.workdir}/.agentflow/worktrees/{tool}/mockup-{n}-{sl}"
    prompt = PRODUCE_PROMPT.format(
        repo=cfg.repo, n=n, title=issue.get("title", ""), body=issue.get("body") or "",
        branch=branch, surfaces=_surfaces_phrase(ui_surfaces(cfg.workdir)),
        disclaimer=_MOCKUP_DISCLAIMER)
    return Submission(
        repo=cfg.repo, subject=str(n), stage="mockup", pool=tool, complexity="deep",
        source=source, claim=True, input_ptr=prompt, builder_lineage=tool)


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


def survivor_review_submission(cfg, *, issue: int, slug: str, builder_tool: str,
                               head_sha: str, reviewer_tool: str, pr_number: int,
                               acceptance: str):
    """Submit a fresh exact-head Review for an already-open autonomous survivor.

    A survivor has no completed coordinator predecessor to transfer from: its earlier chain has
    already reached an external PR boundary. This mapping therefore creates a cold Review that
    owns the newly-established visible claim directly, while preserving builder lineage and the
    retained branch/worktree naming needed by any later Revise.
    """
    from agentflow.coordinator import Submission
    from agentflow.loop import _surfaces_phrase, ui_surfaces
    from agentflow.reviewer import REVIEW_PROMPT, review_worktree

    if not head_sha or builder_tool not in BUILD_POOLS or reviewer_tool not in BUILD_POOLS:
        return None
    prompt = REVIEW_PROMPT.format(
        pr=pr_number, acceptance=acceptance or "(none provided)",
        surfaces=_surfaces_phrase(ui_surfaces(cfg.workdir)))
    return Submission(
        repo=cfg.repo, subject=str(issue), stage="review", target=head_sha,
        pool=reviewer_tool, complexity="deep",
        source=str(review_worktree(cfg.workdir, reviewer_tool, pr_number, slug)),
        claim=True, input_ptr=prompt, builder_lineage=builder_tool)


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


def respond_submission(cfg, pr_number, branch, comment, target, baseline):
    """Translate one unanswered maintainer comment on an existing agentflow PR into a single
    Respond stage submission — the minimal facts the coordinator needs (ADR 0030). Respond adopts
    the change's *original tool lineage* and its retained PR branch/worktree — both recovered from
    the branch name (``agentflow/<tool>/issue-<n>-<slug>``), so capacity on the other pool can never
    silently switch this code-writing continuation — is bound to the maintainer comment it answers
    (its immutable ``target``, so a later comment opens a genuinely new Respond with a fresh budget),
    and holds the ``building`` change claim while it waits. Pure: the mapping is the test surface
    (ADR 0020). The durable prompt carries the PR head observed before Respond so completion can
    verify a requested push actually advanced that baseline. Returns ``None`` when the branch is
    not an agentflow PR branch or either immutable target is missing."""
    from agentflow.coordinator import Submission
    from agentflow.gate import respond_reply_disclaimer
    from agentflow.loop import _BRANCH_RE, RESPOND_PROMPT, _builder_worktree
    m = _BRANCH_RE.match(branch or "")
    if m is None or not target or not baseline:
        return None
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    brief = RESPOND_PROMPT.format(
        n=pr_number, comment=comment, baseline=baseline,
        disclaimer=respond_reply_disclaimer(str(target)))
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


def owned_issues(cfg, *, store_path=None, lane=None) -> set[int]:
    """The issues in ``cfg.repo`` a coordinator record still owns. Empty when no store exists.

    ``lane`` scopes ownership to one claim type: ``"building"`` (Build/Review/Revise/Respond),
    ``"triaging"`` (Intake), or ``"drawing"`` (Mockup). Each reclamation pass supplies its lane
    so one claim type's live record never shields another type's stale claim."""
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


def reconcile_orphaned_claims(cfg, *, _log=None) -> int:
    """Clear visible claims only after coordinator reconciliation proves them orphaned.

    The durable store is read first and is authoritative. An unreadable store clears nothing.
    For each claim lane, any claim-owning continuation record for the issue keeps the label,
    including a waiting/completed record with no live process. Because every provider family is
    born from a running record, absence of such a record after ``Coordinator.cycle`` also proves
    there is no live family. A one-hour grace protects short deterministic interactive claim
    operations. GitHub listing or verification failures likewise clear nothing.
    """
    from agentflow.coordinator.record import RUNNING
    from agentflow.loop import BUILDING, DRAWING, TRIAGING, _run

    _log = _log or (lambda _line: None)
    try:
        records = tracer.load_records()
    except StoreUnavailable as exc:
        _log(f"{cfg.repo}: claim reconciliation deferred — coordinator state unreadable: {exc}")
        return 0

    lane_labels = (("building", BUILDING), ("triaging", TRIAGING), ("drawing", DRAWING))
    cleared = 0
    for lane, label in lane_labels:
        listed = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
                       "--label", label, "--json", "number,updatedAt", "--limit", "100"])
        if listed.returncode != 0:
            _log(f"{cfg.repo}: {lane} claim reconciliation deferred — GitHub unreadable")
            continue
        try:
            claimed = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError:
            _log(f"{cfg.repo}: {lane} claim reconciliation deferred — GitHub response unreadable")
            continue
        for issue in claimed:
            number = issue.get("number")
            if not isinstance(number, int):
                continue
            try:
                updated = datetime.fromisoformat(
                    str(issue.get("updatedAt", "")).replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if time.time() - updated < _ORPHAN_CLAIM_GRACE_SECONDS:
                continue  # protect short deterministic by-hand claim operations
            related = [
                record for record in records
                if record.repo == cfg.repo and str(record.subject) == str(number)
                and tracer.CLAIM_LANE.get(record.stage) == lane
            ]
            if any((not record.retired and record.claim) or record.state == RUNNING
                   for record in related):
                continue
            removed = _run(["gh", "issue", "edit", str(number), "--repo", cfg.repo,
                            "--remove-label", label])
            if removed.returncode != 0:
                continue
            proved = _run(["gh", "issue", "view", str(number), "--repo", cfg.repo,
                           "--json", "labels"])
            if proved.returncode != 0:
                continue
            try:
                labels = {item.get("name")
                          for item in json.loads(proved.stdout or "{}").get("labels", [])}
            except json.JSONDecodeError:
                continue
            if label not in labels:
                cleared += 1
                _log(f"{cfg.repo}: #{number}: reclaimed orphaned {lane} claim — "
                     "no live family or continuation record")
    return cleared


# --- production wiring (live orchestration; not unit-tested, ADR 0020) -------------------

def build_coordinator(_log=None) -> Coordinator:
    """The daemon's coordinator for all six logical stages (issues #103–#108).
    Its Build adapter verifies the real PR outcome and reuses the retained worktree; its Review
    adapter verifies a durable verdict for the exact PR head SHA and recreates the read-only
    checkout; its Revise adapter verifies a pushed revision on the same branch and reuses that
    retained worktree; its Respond adapter verifies the marked reply plus any pushed change on that
    same branch and releases the change claim on completion; its Mockup adapter verifies one
    pushed visual round and releases its drawing claim at the human-pick boundary. One
    :class:`StageRouter` dispatches each adapter call on the record's stage."""
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
        verdict_ready=_verdict_ready,
        worktree_reset=lambda record: _review_worktree_reset(record, _log=_log),
        handoff=_park_pr,
        settle=_settle_review, prepare_settle=_prepare_review_settlement)
    revise = ReviseStageAdapter(
        revision_ready=_revision_ready, worktree_ready=_worktree_ready, handoff=_park_pr)
    respond = RespondStageAdapter(
        reply_ready=_reply_ready, worktree_ready=_worktree_ready, handoff=_park_respond,
        settle=_settle_respond)
    mockup = MockupStageAdapter(
        outcome_ready=_mockup_outcome_ready,
        worktree_ready=lambda record: (_mockup_claim_ready(record)
                                       and _worktree_ready(record)),
        missing_context=_mockup_missing_context,
        handoff=_hold_mockup,
        settle=_settle_mockup)
    from agentflow import coordinated_converse
    converse = ConverseStageAdapter(
        reply_ready=coordinated_converse._reply_ready,
        adopt=coordinated_converse._adopt_turn,
        park=coordinated_converse._park_ask,
        worktree_ready=coordinated_converse._ask_worktree_ready)
    router = StageRouter({"intake": intake, "build": build, "review": review, "revise": revise,
                          "respond": respond, "mockup": mockup, "converse": converse})
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
        # An interactive turn (an operator-present Ask) is a real-time conversation: it is exempt
        # from the recent-session cooldown, the spend ceiling, and the active-pacing budget (ADR
        # 0034/0025 as amended by #162). Only the reservation ledger in `_begin_start` — true zero
        # capacity — may still defer it. Background stages keep the full clear + pacing gate.
        if record.interactive:
            return True
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
                "respond": "respond", "mockup": "mockup"}
        stage_lane = lane.get(record.stage, record.stage)
        return ReservationLimits(
            machine_ceiling=dispatch.MACHINE_CEILING,
            stage_cap=dispatch.STAGE_CAPS.get(stage_lane, 1),
            stage_lane=stage_lane,
            lane_by_stage=lane,
        )

    def started(self, record) -> None:
        """Charge operator pacing only after the provider start is durable. An interactive turn
        never consumes the background pace budget — even when a background record already marked
        the pool active this cycle, its start is exempt from pacing (ADR 0034/0025 as amended)."""
        if record.interactive:
            return
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
    if not tail.startswith(f"{record.pool}/") or record.lineage != record.pool:
        return None
    name = tail.split("/", 1)[1] if "/" in tail else ""
    valid_name = (name.startswith(f"mockup-{record.subject}-")
                  if record.stage == "mockup" else name.startswith("issue-"))
    if not valid_name:
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
        elif record.stage in {"build", "mockup"}:
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


def _mockup_outcome_ready(record, obs) -> bool:
    """Prove one pushed variant round: committed artifacts/screenshots and one marked comment.

    The worktree is continuation state, never outcome authority. Completion requires its clean
    head to equal the remote branch, at least three branch-only HTML variants and screenshots,
    and exactly one durable issue comment that embeds every committed screenshot. A
    MISSING-CONTEXT comment is a human hold, not a completed visual round.
    """
    from agentflow.loop import MOCKUP_MARK, _issue_comments, _run

    parsed = _source_facts(record)
    if parsed is None:
        return False
    _workdir, branch, wt = parsed
    if not wt.exists():
        return False
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    marked = [comment for comment in _issue_comments(record.repo, number)
              if MOCKUP_MARK in comment.get("body", "")]
    if len(marked) != 1 or "MISSING-CONTEXT:" in marked[0].get("body", ""):
        return False
    fetched = _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", "main", branch])
    if fetched.returncode != 0:
        return False
    local = _run(["git", "-C", str(wt), "rev-parse", "HEAD"])
    remote = _run(["git", "-C", str(wt), "rev-parse", f"origin/{branch}"])
    status = _run(["git", "-C", str(wt), "status", "--porcelain", "--untracked-files=all"])
    if (local.returncode != 0 or remote.returncode != 0 or status.returncode != 0
            or not local.stdout.strip() or local.stdout.strip() != remote.stdout.strip()
            or status.stdout.strip()):
        return False
    changed = _run(["git", "-C", str(wt), "diff", "--name-only", "--diff-filter=ACMRT",
                    "origin/main...HEAD"])
    if changed.returncode != 0:
        return False
    paths = [path for path in changed.stdout.splitlines() if path.startswith("mockups/")]
    variants = [path for path in paths if path.lower().endswith((".html", ".htm"))]
    screenshots = [path for path in paths
                   if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp"))]
    body = marked[0].get("body", "")
    return (len(variants) >= 3 and len(screenshots) >= 3
            and all(path in body for path in screenshots))


def _mockup_missing_context(record) -> bool:
    """Whether this issue carries Mockup's deliberate durable MISSING-CONTEXT boundary."""
    from agentflow.loop import MOCKUP_MARK, _issue_comments

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    return any(MOCKUP_MARK in comment.get("body", "")
               and "MISSING-CONTEXT:" in comment.get("body", "")
               for comment in _issue_comments(record.repo, number))


def _mockup_claim_ready(record) -> bool:
    """Prove Mockup's visible drawing claim immediately before admission."""
    from agentflow.loop import DRAWING, _run

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return False
    viewed = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "labels"])
    if viewed.returncode != 0:
        return False
    try:
        labels = {label.get("name") for label in json.loads(viewed.stdout or "{}").get("labels", [])}
    except json.JSONDecodeError:
        return False
    return DRAWING in labels and "agentflow:needs-mockup" in labels


def _settle_mockup(record) -> str | None:
    """Retire one completed visual round at the human-pick boundary.

    The durable comment and pushed artifacts were already verified by the adapter. Settlement
    removes and proves the drawing claim, keeps ``needs-mockup`` in place for the maintainer's
    choice, and disposes the clean pushed worktree before coordinator ownership disappears.
    Every step is idempotent; an unreadable label or stubborn worktree retries next cycle.
    """
    from agentflow.loop import DRAWING, _run
    from agentflow.runner import remove_worktree_if_safe

    parsed = _source_facts(record)
    if parsed is None:
        return None
    workdir, _branch, wt = parsed
    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    _run(["gh", "issue", "edit", str(number), "--repo", record.repo,
          "--remove-label", DRAWING])
    proved = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "labels,url"])
    if proved.returncode != 0:
        return None
    try:
        state = json.loads(proved.stdout or "{}")
    except json.JSONDecodeError:
        return None
    labels = {label.get("name") for label in state.get("labels", [])}
    if DRAWING in labels or "agentflow:needs-mockup" not in labels:
        return None
    if wt.exists() and not remove_worktree_if_safe(workdir, wt):
        return None
    if wt.exists():
        return None
    return state.get("url") or f"https://github.com/{record.repo}/issues/{number}"


def _hold_mockup(record) -> str | None:
    """Create Mockup's one issue-native handoff while preserving unfinished local work.

    MISSING-CONTEXT already is the durable stage-native handoff; exhaustion posts one stable
    marked comment. Both leave ``needs-mockup`` in place, release and prove the drawing claim,
    retain the worktree, and use a stable notification sequence across crash retries.
    """
    from agentflow.loop import DRAWING, MOCKUP_MARK, _MOCKUP_DISCLAIMER, _run
    from agentflow.notify import notify

    try:
        number = int(record.subject)
    except (TypeError, ValueError):
        return None
    viewed = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "labels,comments,url"])
    if viewed.returncode != 0:
        return None
    try:
        issue = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    comments = issue.get("comments", [])
    marked = next((comment for comment in comments
                   if MOCKUP_MARK in comment.get("body", "")), None)
    missing = next((comment for comment in comments
                    if MOCKUP_MARK in comment.get("body", "")
                    and "MISSING-CONTEXT:" in comment.get("body", "")), None)
    proof = "<!-- agentflow-mockup-hold:" + hashlib.sha256(
        record.identity.encode()).hexdigest()[:24] + " -->"
    explanation = ("Mockup exhausted its continuation budget before completing the visual round. "
                   "The branch and local worktree are retained for a human to continue.")
    existing = marked or next((comment for comment in comments
                               if proof in comment.get("body", "")), None)
    if existing is None:
        body = f"{_MOCKUP_DISCLAIMER}\n{proof}\n\n{explanation}"
        posted = _run(["gh", "issue", "comment", str(number), "--repo", record.repo,
                       "--body", body])
        if posted.returncode != 0:
            return None
    elif missing is None and proof not in existing.get("body", ""):
        comment_id = existing.get("id")
        if not comment_id:
            return None
        body = f"{existing.get('body', '').rstrip()}\n\n{proof}\n\n{explanation}"
        mutation = ("mutation($id:ID!,$body:String!){updateIssueComment("
                    "input:{id:$id,body:$body}){issueComment{id}}}")
        edited = _run(["gh", "api", "graphql", "-f", f"query={mutation}",
                       "-f", f"id={comment_id}", "-f", f"body={body}"])
        if edited.returncode != 0:
            return None
    _run(["gh", "issue", "edit", str(number), "--repo", record.repo,
          "--add-label", "agentflow:needs-mockup", "--remove-label", DRAWING])
    proved = _run(["gh", "issue", "view", str(number), "--repo", record.repo,
                   "--json", "labels,comments,url"])
    if proved.returncode != 0:
        return None
    try:
        state = json.loads(proved.stdout or "{}")
    except json.JSONDecodeError:
        return None
    labels = {label.get("name") for label in state.get("labels", [])}
    final_comments = state.get("comments", [])
    has_proof = any(
        proof in comment.get("body", "")
        or (MOCKUP_MARK in comment.get("body", "")
            and "MISSING-CONTEXT:" in comment.get("body", ""))
        for comment in final_comments)
    if DRAWING in labels or "agentflow:needs-mockup" not in labels or not has_proof:
        return None
    url = state.get("url") or f"https://github.com/{record.repo}/issues/{number}"
    sequence = "mockup-" + hashlib.sha256(record.identity.encode()).hexdigest()[:24]
    reason = ("missing context" if missing is not None
              else "continuation budget exhausted")
    if not notify("agentflow needs you", f"{record.repo} #{number}: Mockup held — {reason}",
                  url, sequence_id=sequence):
        return None
    return str(url)


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


# Consecutive review-prepare failures per source path, so a genuinely stuck
# review (one that never checks out) surfaces once instead of silently no-op'ing
# admission every cycle. Process-local — a daemon restart re-arms it.
_REVIEW_PREPARE_FAILURES: dict[str, int] = {}


def _review_worktree_reset(record, _log=None) -> bool:
    """Recreate the read-only review checkout at the exact PR head SHA before admission (ADR 0030).
    Review holds no local edits, so any stale checkout is discarded and rebuilt detached at the
    record's immutable target SHA — the target is never touched. An orphaned checkout dir (present
    on disk but with its git metadata gone) is self-healed by the detached prepare rather than
    stalling admission forever. Any git failure returns False, so admission is skipped with no
    permit and no attempt; a repeated failure is logged so a stuck review is visible. Live
    orchestration, not unit-tested (ADR 0020)."""
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
        fails = _REVIEW_PREPARE_FAILURES[record.source] = \
            _REVIEW_PREPARE_FAILURES.get(record.source, 0) + 1
        if fails == 2 and _log is not None:
            _log(f"{record.repo}: review checkout keeps failing at {record.source} — "
                 "admission is stuck; the PR will not be reviewed until it is cleared")
        return False
    _REVIEW_PREPARE_FAILURES.pop(record.source, None)
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


def _review_pr_facts(record) -> dict | None:
    """The PR's current head and state, or ``None`` when GitHub is unreadable."""
    from agentflow.loop import _run

    facts = _review_source_facts(record)
    if facts is None:
        return None
    _workdir, pr = facts
    viewed = _run(["gh", "pr", "view", str(pr), "--repo", record.repo,
                   "--json", "headRefOid,state"])
    if viewed.returncode != 0:
        return None
    try:
        data = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    head, state = data.get("headRefOid"), data.get("state")
    if not isinstance(head, str) or not head or state not in {"OPEN", "CLOSED", "MERGED"}:
        return None
    return {"head": head, "state": state}


def _review_pr_head(record) -> str | None:
    facts = _review_pr_facts(record)
    return facts["head"] if facts is not None else None


def _prepare_review_settlement(record) -> bool:
    """Perform slow CI observation outside the coordinator store transaction.

    Only an exact-head, independent, clean autonomous review can merge and therefore needs the
    bounded CI wait. Every park/revise path is immediately ready for its short transactional
    finalization. Settlement rechecks both head and CI immediately before the merge.
    """
    from agentflow.gate import ci_is_green
    from agentflow.loop import repo_profile

    facts = _review_source_facts(record)
    if facts is None:
        return False
    workdir, _pr = facts
    verdict = _review_verdict(record)
    if (not verdict.clean or repo_profile(workdir) != "autonomous"
            or not record.auto_merge_allowed):
        return True
    if _review_pr_head(record) != record.target:
        return True  # short settlement parks the stale exact-head verdict
    _REVIEW_CI_OBSERVED[record.identity] = ci_is_green(record.repo, facts[1])
    return True


def _park_review_settlement(record, verdict, workdir: str, pr: int, comments: list[dict],
                            *, reason: str, autonomous: bool) -> str | None:
    """Idempotently park, prove, clean up, and notify one completed Review."""
    from agentflow.gate import park
    from agentflow.loop import _finish_review, _pr_comments
    from agentflow.notify import notify
    from agentflow import ratchet

    marker = "agentflow: parked for human review"
    already = any(marker in comment.get("body", "") for comment in comments)
    if not already:
        park(record.repo, pr, verdict, reason=reason)
    proved = _pr_comments(record.repo, pr)
    if proved is None or not any(marker in comment.get("body", "") for comment in proved):
        return None
    slug = Path(record.source).name.split(f"pr-{pr}-", 1)[-1]
    _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir), record.pool, pr, slug)
    if autonomous:
        ratchet.record_once(record.repo, "parked", record.identity)
    url = f"https://github.com/{record.repo}/pull/{pr}"
    if not already:
        sequence = "review-" + hashlib.sha256(record.identity.encode()).hexdigest()[:24]
        notify("agentflow needs you", f"{record.repo} PR #{pr}: reviewed — your action", url,
               sequence_id=sequence)
    return url


def _settle_review(record) -> str | None:
    """Consume a parsed exact-head verdict through the established repository merge policy."""
    from agentflow import ratchet
    from agentflow.gate import (MergeDecision, ci_is_green, decide_merge, reply_pending,
                                squash_merge, ui_evidence_gap)
    from agentflow.loop import (_UI_GAP_REASON, _finish_review, _pr_comments, _run,
                                repo_profile, ui_surfaces)

    facts = _review_source_facts(record)
    if facts is None:
        return None
    workdir, pr = facts
    verdict = _review_verdict(record)
    if verdict.blocking:
        return None  # durable opener transfers this claim to Revise
    comments = _pr_comments(record.repo, pr)
    if comments is None:
        return None
    profile = repo_profile(workdir)
    autonomous = profile == "autonomous"
    pr_facts = _review_pr_facts(record)
    if pr_facts is None:
        return None
    head = pr_facts["head"]
    if pr_facts["state"] == "MERGED":
        slug = Path(record.source).name.split(f"pr-{pr}-", 1)[-1]
        _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir),
                       record.pool, pr, slug, merged=True)
        ratchet.record_once(
            record.repo, ratchet.CLEAN_MERGE if record.round == 0 else "merge_after_revise",
            record.identity)
        _run(["gh", "issue", "edit", str(record.subject), "--repo", record.repo,
              "--remove-label", "ready-for-agent"])
        return f"https://github.com/{record.repo}/pull/{pr}"
    if head != record.target:
        return _park_review_settlement(
            record, verdict, workdir, pr, comments,
            reason="PR head changed after the recorded review; a human must re-review",
            autonomous=autonomous)

    surfaces = ui_surfaces(workdir)
    ui_gap = ui_evidence_gap(record.repo, pr, surfaces)
    if not autonomous:
        reason = _UI_GAP_REASON if ui_gap else f"is a `{profile}` repo — a human merges"
        return _park_review_settlement(
            record, verdict, workdir, pr, comments, reason=reason, autonomous=False)
    if not verdict.clean:
        return _park_review_settlement(
            record, verdict, workdir, pr, comments,
            reason="review did not produce an actionable clean verdict", autonomous=True)
    pending_reply = reply_pending(comments)
    if not record.auto_merge_allowed or ui_gap or pending_reply:
        reason = _UI_GAP_REASON if ui_gap else "could not be auto-merged after review"
        return _park_review_settlement(
            record, verdict, workdir, pr, comments, reason=reason, autonomous=True)

    # CI already completed in prepare_completed, outside SQLite's write transaction. Recheck it
    # once without polling, together with the exact head, immediately before merge.
    ci_green = _REVIEW_CI_OBSERVED.pop(record.identity, None)
    if ci_green is None:
        return None
    if not ci_green:
        return _park_review_settlement(
            record, verdict, workdir, pr, comments,
            reason="CI did not complete successfully within the review settlement window",
            autonomous=True)
    decision = decide_merge(
        verdict=verdict, ci_green=True, reviewer_tool=record.pool,
        builder_tool=record.builder_lineage or "", revises_used=record.round,
        ui_evidence_missing=False, reply_pending=False)
    if decision is not MergeDecision.MERGE:
        return _park_review_settlement(
            record, verdict, workdir, pr, comments,
            reason="could not be auto-merged after review", autonomous=True)
    if _review_pr_head(record) != record.target:
        return None
    if not ci_is_green(record.repo, pr, timeout=0, interval=0):
        return None
    if not squash_merge(record.repo, pr):
        return _park_review_settlement(
            record, verdict, workdir, pr, comments,
            reason="could not be squash-merged (branch protection, conflict, or transient error)",
            autonomous=True)
    slug = Path(record.source).name.split(f"pr-{pr}-", 1)[-1]
    _finish_review(SimpleNamespace(repo=record.repo, workdir=workdir),
                   record.pool, pr, slug, merged=True)
    ratchet.record_once(
        record.repo, ratchet.CLEAN_MERGE if record.round == 0 else "merge_after_revise",
        record.identity)
    _run(["gh", "issue", "edit", str(record.subject), "--repo", record.repo,
          "--remove-label", "ready-for-agent"])
    return f"https://github.com/{record.repo}/pull/{pr}"


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
    body = comment.get("body", "") or ""
    if PR_MARK not in body or not has_image_evidence(body):
        return False
    if not opened_at:
        return True
    try:
        created = datetime.fromisoformat(
            str(comment.get("createdAt", "") or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        created = None
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
    from agentflow.gate import respond_reply_change, respond_reply_posted
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
    change = respond_reply_change(comments, record.target or "")
    baseline_match = re.search(r"agentflow-respond-baseline:([^\s>]+)", record.input_ptr or "")
    baseline = baseline_match.group(1) if baseline_match is not None else ""
    if not change or not baseline:
        return False
    # A reply exists. The owned worktree is mandatory evidence: without it there is no way to
    # prove that a requested branch change was either pushed or never left locally. Fail closed and
    # let preparation recover the PR branch before another attempt.
    if not wt.exists():
        return False
    head = pr.get("headRefOid") or ""
    if change != "none" and (change == baseline or change != head):
        return False
    fetched = _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", branch])
    if fetched.returncode != 0:
        return False
    if change != "none":
        ancestry = _run(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                         baseline, head])
        if ancestry.returncode != 0:
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


def _review_context(record) -> tuple[str, str] | None:
    """The issue-anchored acceptance brief and declared UI surfaces for a Review."""
    from agentflow.loop import _run, _surfaces_phrase, ui_surfaces

    parts = _build_source_parts(record)
    if parts is None:
        return None
    workdir, _slug = parts
    acceptance = record.input_ptr if record.stage == "build" and record.input_ptr else None
    if acceptance is None:
        viewed = _run(["gh", "issue", "view", str(record.subject), "--repo", record.repo,
                       "--json", "body"])
        if viewed.returncode != 0:
            return None
        try:
            payload = json.loads(viewed.stdout or "{}")
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        acceptance = payload.get("body")
        if not isinstance(acceptance, str):
            return None
    return acceptance, _surfaces_phrase(ui_surfaces(workdir))


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
    context = _review_context(build)
    if context is None:
        return
    acceptance, surfaces = context
    reviewer_tool = pick_reviewer(build.pool)
    if reviewer_tool is None:
        return  # ADR 0020: no tool free to review this cycle — post nothing; the completed
                # build keeps its claim and this opener re-drives next cycle.
    submission = review_submission(
        build, pr.get("headRefOid", ""), reviewer_tool, pr.get("number"),
        acceptance=acceptance, surfaces=surfaces)
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
    if (review.round >= MAX_REVISES
            or not revise_round_budget_remains(records.values(), review.repo, review.subject)):
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
    context = _review_context(revise)
    if context is None:
        return
    acceptance, surfaces = context
    reviewer_tool = pick_reviewer(revise.builder_lineage)
    if reviewer_tool is None:
        return  # ADR 0020: no tool free to review this cycle — post nothing; the completed
                # revise keeps its claim and this opener re-drives next cycle.
    submission = review_submission(
        revise, pr.get("headRefOid", ""), reviewer_tool, pr.get("number"),
        acceptance=acceptance, surfaces=surfaces)
    if submission is not None:
        coord.submit_stage(submission)


# Each completed stage's claim-transfer opener, keyed by the stage it consumes.
_OPENERS = {"build": _open_review_on_completed_build,
            "review": _open_revise_on_blocking_review,
            "revise": _open_review_on_completed_revise}


def reconcile_and_project(coord: Coordinator, *, _log=None) -> list:
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
    live.replace_projection(tracer.live_projection(records))
    return outcomes
