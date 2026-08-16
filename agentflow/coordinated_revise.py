"""The Revise stage behind the durable session coordinator (issue #105, ADR 0038).

A blocking Review hands its change claim to one durable ``revise`` submission that runs on the
*builder's* retained branch and worktree, bound to the reviewed head SHA it must supersede. A
merge conflict against `main` gets its own separately-budgeted conflict round. This module is the
daemon-side glue, mirroring its stage siblings:

- **submission mapping** — the finding-driven revise, the conflict revise for an already-open
  survivor PR, and the resume of a retained conflict once the other tool supplied a grounded
  decision.
- **stage collaborators** — the pushed-revision (or durable non-code proof) outcome and the
  private conflict-ambiguity outcome. These are the production wiring :mod:`agentflow.pipeline`
  injects into :class:`ReviseStageAdapter`; the branch/worktree preparation Revise shares with
  Build, Respond and Mockup is :mod:`agentflow.stage_worktree`, and the PR park it shares with
  Review is :mod:`agentflow.pr_park`.

The mappings are pure and exercised directly; the live GitHub/git reads are exercised through the
adapter seam (ADR 0020).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from agentflow import github
from agentflow.balancer import BUILD_POOLS
from agentflow.prompts import stage_prompt_spec
from agentflow.pool_control import POOLS, pool_paused
from agentflow.review_policy import CONFLICT_UNCERTAINTY_PREFIX
from agentflow.repo_facts import surface_declaration, surfaces_phrase
from agentflow.routing import routing
from agentflow.runner import _run, codex_spent_at_render
from agentflow.stage_worktree import worktree_owns_head
from agentflow.worktree_ref import WorktreeKind, WorktreeRef, source_facts


# ADR 0038: rebase, resolve, stay green, and preserve every compatible behavior. Genuine product
# competition is a decision handoff, not a reason to privilege whichever side merged first.
_CONFLICT_REVISE_FINDING = (
    "Rebase this PR's branch onto the current `origin/main` and resolve the merge conflicts, then "
    "keep the full test suite green. Preserve both sides wherever their intended behavior is "
    "compatible; neither side wins merely because it is newer. If they encode incompatible product "
    "intent, return the private two-option conflict uncertainty instead of choosing silently.")


def _session_lead_prompt(prompt: str, effort: str | None, parent_pool: str) -> str:
    """Render the provider-neutral session-lead brief."""
    brief = prompt + routing.session_lead_instructions(
        "revise", effort, parent_provider=parent_pool, codex_spent=codex_spent_at_render(),
        unavailable_providers=frozenset(pool for pool in POOLS if pool_paused(pool)))
    return brief


def survivor_conflict_revise_submission(cfg, *, issue: int, slug: str, builder_tool: str,
                                        head_sha: str, pr_number: int, conflict_round: int,
                                        parent_pool: str = "claude"):
    """Submit a conflict Revise for an open survivor whose re-rebase no longer applies (ADR 0038).

    Like a survivor re-review, a survivor has no completed coordinator predecessor to transfer from —
    its chain already reached the PR boundary — so this creates a Revise that owns the visible claim
    directly. It runs on the builder's own tool lineage in the retained PR-branch worktree (recovered
    the way any Revise recovers it), bound to the conflicting head SHA it must supersede, carrying the
    conflict round so it is budgeted apart from the finding-driven revise rounds and admitted ahead of
    cold build work. Returns ``None`` when the tool is unknown or the head SHA is missing.
    """
    from agentflow.coordinator import Submission
    if not head_sha or builder_tool not in BUILD_POOLS:
        return None
    declaration = surface_declaration(cfg.workdir)
    ui = bool(declaration.surfaces)
    brief = _session_lead_prompt(stage_prompt_spec("revise").render(
        n=pr_number, repo=cfg.repo, findings=f"- {_CONFLICT_REVISE_FINDING}",
        surfaces=surfaces_phrase(declaration)), None, parent_pool)
    return Submission(
        repo=cfg.repo, subject=str(issue), stage="revise", target=head_sha,
        subject_revision=head_sha,
        pool=parent_pool, complexity="deep", conflict_round=conflict_round,
        source=WorktreeRef.for_build(cfg.workdir, builder_tool, issue, slug).path,
        claim=True, input_ptr=brief,
        builder_lineage=parent_pool, branch_lineage=builder_tool,
        builder_complexity="deep", continuation=True, session_lead=True,
        capability_root=cfg.workdir, capability_context={"ui": ui})


def _revise_builder_source(review_record):
    """The ``(build_worktree, pr_number)`` a Revise adopts from a blocking Review record. The
    revise reuses the *builder's* retained branch/worktree — ``.../<builder_lineage>/issue-<subject>
    -<slug>`` — which the review source (``.../<tool>-review/pr-<pr>-<slug>``) and the record's
    retained branch lineage together recover. Older records predate that distinct fact, so their
    builder lineage remains the migration fallback. ``None`` if unreadable."""
    review = WorktreeRef.parse(review_record.source)
    branch_tool = review_record.branch_lineage or review_record.builder_lineage
    if review is None or review.kind is not WorktreeKind.REVIEW or not branch_tool:
        return None
    build = WorktreeRef.for_build(
        review.workdir, branch_tool, int(review_record.subject), review.slug)
    return build.path, review.number


def revise_submission(review_record, complexity, findings="", *, surfaces="", target_sha="",
                      parent_pool: str = "claude"):
    """Translate a blocking Review into one Revise stage submission — the minimal facts the
    coordinator needs (ADR 0030). The revise adopts the original builder's retained PR branch and
    worktree, stays pinned to the builder's tool lineage and its original complexity and effort,
    is bound to the reviewed head SHA it must supersede (its immutable target, together with the review's
    revise round — so a later blocking review, even one re-reviewing an unchanged head SHA, is a
    genuinely fresh revise stage), and assumes the Review's change claim. Pure: the mapping is the
    test surface (ADR 0020). Returns ``None`` if the builder worktree cannot be reconstructed or
    the reviewed SHA is missing."""
    from agentflow.coordinator import Submission
    from agentflow.review_policy import ReviewState
    facts = _revise_builder_source(review_record)
    reviewed_head = target_sha or review_record.target
    if facts is None or not reviewed_head:
        return None
    build_worktree, pr_number = facts
    brief = _session_lead_prompt(stage_prompt_spec("revise").render(
        n=pr_number, repo=review_record.repo, findings=findings or "- (see review)",
        surfaces=surfaces or "any user-facing surface"), review_record.builder_effort, parent_pool)
    review = ReviewState.from_record(review_record)
    if review is not None:
        review = replace(review, change_author_tool=parent_pool)
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="revise",
        target=reviewed_head, subject_revision=reviewed_head,
        pool=parent_pool, complexity=complexity,
        effort=review_record.builder_effort, source=build_worktree, claim=True, input_ptr=brief,
        builder_lineage=parent_pool,
        branch_lineage=review_record.branch_lineage or review_record.builder_lineage,
        builder_complexity=complexity,
        builder_effort=review_record.builder_effort,
        round=review_record.round, transfer_from=review_record.identity, session_lead=True,
        review=review,
        capability_root=review_record.capability_root,
        capability_context=review_record.capability_context or "{}")


def record_evidence(producer, record, _obs=None):
    """Reconcile a verified Revise to its complete classified Review finding set."""
    from agentflow import coordinated_review
    from agentflow.evidence_pipeline import FixFact
    from agentflow.review_policy import ReviewState

    state = ReviewState.from_record(record)
    parsed = source_facts(record)
    if state is None or parsed is None or not record.target:
        return None
    _workdir, branch, _worktree = parsed
    pr = github.open_pr_for_branch(record.repo, branch)
    if pr is None or not pr.head_ref_oid or pr.head_ref_oid == record.target:
        return None
    review_id = coordinated_review._evidence_identity(record)
    source = producer.review_source(
        review_id, record.target, locator=f"pulls/{pr.number}",
        observed_at=max(0, record.started_at))
    findings = coordinated_review.record_evidence(producer, record, source=source)
    finding_ids = tuple(item.finding_id for item in findings)
    if not finding_ids:
        return None
    return producer.fix(FixFact(
        review_id, record.target, pr.head_ref_oid, finding_ids, source,
        coordinated_review._evidence_digest(
            "review-revise-v1", review_id, record.target, pr.head_ref_oid),
        "model_judged", max(0, record.started_at) + 1))


def conflict_decision_revise_submission(review_record, verdict, *, parent_pool: str = "claude"):
    """Resume the same retained conflict after the other tool supplies a grounded decision."""
    from agentflow.coordinator import Submission
    from agentflow.review_policy import (
        ReviewAssignment, ReviewAxis, ReviewDepth, ReviewState)

    facts = _revise_builder_source(review_record)
    if (facts is None or review_record.review_axis != "decision"
            or not review_record.conflict_round or not verdict.decision):
        return None
    build_worktree, pr_number = facts
    decision = (
        "The other tool resolved the private conflict decision. Apply this choice while preserving "
        f"all compatible behavior: {verdict.decision}"
    )
    prompt = _session_lead_prompt(stage_prompt_spec("revise").render(
        n=pr_number, repo=review_record.repo, findings=f"- {decision}",
        surfaces="any user-facing surface"), review_record.builder_effort, parent_pool)
    prior = ReviewState.from_record(review_record)
    if prior is None:
        return None
    review = replace(
        prior,
        assignment=ReviewAssignment(
            ReviewDepth.FULL, review_record.depth_reason, ReviewAxis.PRODUCT),
        change_author_tool=parent_pool, handoff=decision)
    return Submission(
        repo=review_record.repo, subject=review_record.subject, stage="revise",
        target=review_record.target, subject_revision=review_record.target or "", pool=parent_pool,
        complexity=review_record.builder_complexity or "deep",
        effort=review_record.builder_effort, source=build_worktree,
        claim=True, input_ptr=prompt, builder_lineage=parent_pool,
        branch_lineage=review_record.branch_lineage or review_record.builder_lineage,
        builder_complexity=review_record.builder_complexity or "deep",
        builder_effort=review_record.builder_effort,
        round=review_record.round, conflict_round=review_record.conflict_round,
        transfer_from=review_record.identity, continuation=True, session_lead=True,
        review=review, capability_root=review_record.capability_root,
        capability_context=review_record.capability_context or "{}")


def _conflict_uncertainty_outcome(record, obs) -> str | None:
    """Turn a resolver's private structured ambiguity into the Revise stage outcome."""
    import json
    from agentflow.review_policy import conflict_uncertainty_from_message

    if not record.conflict_round:
        return None
    value = conflict_uncertainty_from_message(obs.final_message or "")
    if value is None:
        return None
    return CONFLICT_UNCERTAINTY_PREFIX + json.dumps({
        "options": list(value.options), "missing_guidance": value.missing_guidance,
        "recommendation": value.recommendation}, sort_keys=True)


def _revision_ready(record, obs) -> bool:
    """The Revise outcome is a verified pushed revision **or** the required durable non-code proof,
    read from GitHub independently of how the reviser exited (ADR 0028, issue #105):

    - a pushed revision: the PR branch head SHA has moved off the reviewed SHA the revise was opened
      against (``record.target``). A head that *descends from* the reviewed SHA is such a revision;
      so is a rewritten head (e.g. a rebase a finding demanded) that the retained builder worktree
      owns — its ``HEAD`` equals the pushed head and its tree is clean after fetching the branch. A
      force-push back to a commit that is an *ancestor* of the reviewed SHA is a rewind and never
      counts, even when the worktree sits on it; or
    - the required non-code proof: a durable agentflow-marked PR comment carrying attached evidence
      (e.g. a before/after screenshot), the way a finding that asks to *show* something is answered
      without a code change. The comment must be *created after this revise record was submitted*
      (its durable ``created_at``, which survives a restart) — a marked screenshot left during the
      Build or a prior revise round predates this round and cannot complete it (issue #118).

    A branch whose head still equals the reviewed SHA and carries no such evidence comment pushed
    and proved nothing, so it stays incomplete and continues — as a typed
    :class:`~agentflow.coordinator.verification.Verification` naming the first failed check.
    Live orchestration; exercised with faked GitHub reads in ``tests/test_revise_tracer.py``."""
    from agentflow.coordinator.verification import VERIFIED, unverified
    parsed = source_facts(record)
    if parsed is None or not record.target:
        return unverified("pr-branch-facts",
                          f"the record's source {record.source!r} does not parse as an owned "
                          f"PR-branch worktree (target {str(record.target or 'missing')[:12]})")
    _workdir, branch, wt = parsed
    prs = github.list_open_prs(record.repo, head=branch)
    if prs is None:
        raise RuntimeError(f"cannot verify Revise outcome for {record.repo}:{branch}")
    if not prs:
        return unverified("open-pr", f"no open PR found for branch {branch!r}")
    head = prs[0].head_ref_oid
    moved_but_unproven = ""
    if head and head != record.target:
        # A moved head is the pushed revision when it descends from the reviewed SHA, or — when the
        # history was rewritten (a rebase the finding asked for) — when the retained builder worktree
        # proves the reviser owns that exact head. The worktree answers both (fetching the branch so
        # the remote head is local). A rewind to an *ancestor* of the reviewed SHA still never counts,
        # and with no worktree to ask, the head comparison stands alone.
        if not wt.exists():
            return VERIFIED
        _run(["git", "-C", str(wt), "fetch", "--quiet", "origin", branch])
        if _run(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                 record.target, head]).returncode == 0:
            return VERIFIED
        rewound = _run(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                        head, record.target]).returncode == 0
        if not rewound and worktree_owns_head(wt, head):
            return VERIFIED
        moved_but_unproven = (
            f"the PR head moved to {head[:12]!r} but it is a rewind of the reviewed SHA"
            if rewound else
            f"the PR head moved to {head[:12]!r} but neither descends from the reviewed SHA "
            "nor is owned by the retained worktree")
    # No new code, but an evidence-only revision still completes on its durable non-code proof: an
    # agentflow-authored PR comment (our marker, never the maintainer's) that attaches evidence
    # and postdates this revise round.
    comments = github.pr_comment_rows(record.repo, prs[0].number)
    if comments is None:
        return unverified("pr-comments-read",
                          f"the PR #{prs[0].number} comment list is unreadable from GitHub")
    if any(_round_evidence(c, record.created_at) for c in comments):
        return VERIFIED
    return unverified(
        "pushed-revision",
        (moved_but_unproven or
         f"the PR head still equals the reviewed SHA {record.target[:12]!r}")
        + f", and none of the {len(comments)} PR comments is an agentflow evidence comment "
          "created after this revise round opened")


def _retire_dead_revises(coord) -> None:
    """Retire a Revise whose PR is merged or closed — or whose owned branch no longer carries any
    PR — before another attempt or continuation is charged against a change nothing can land
    (#432), the same disposition :func:`coordinated_review._resettle_diverged_reviews` already
    takes for a Review of a gone PR.

    A Revise resolves its PR by its owned builder branch (the park's own resolution,
    :func:`agentflow.pr_park.park_pr_number`), so an operator who closes or supersedes the PR and
    deletes the branch otherwise leaves the record re-driving interrupted continuations and
    finally parking against a PR number that no longer resolves — a park that stays pending and
    silently retries forever. This runs every reconcile pass, before admission, over the durable
    records, and deliberately includes a record whose park is already pending: retiring is the
    only exit that unresolvable park has.

    Trust boundary: an unreadable GitHub answer never retires — ``None`` means "couldn't check",
    never "gone". Only a definite CLOSED/MERGED latest PR, or a definite empty PR-for-branch
    answer, retires the record: every Revise descends from a blocking Review of a real PR on its
    own branch, so a definitely empty answer means that PR and its branch are gone."""
    from agentflow.coordinated_review import _kill_running_family
    from agentflow.coordinator import tracer
    from agentflow.coordinator.record import RUNNING
    for record in tracer.load_records():
        if (not coord._manages_repository(record.repo)
                or record.stage != "revise" or record.retired or not record.claim):
            continue
        parsed = source_facts(record)
        if parsed is None:
            continue  # not this record's own checkout — no owned branch to resolve a PR from
        _workdir, branch, _wt = parsed
        prs = github.prs_for_branch(record.repo, branch, limit=1)
        if prs is None:
            continue  # GitHub unreadable — fail closed; a later pass retries
        if prs and prs[0].state not in {"CLOSED", "MERGED"}:
            continue  # an open (or unrecognized-state) PR is live revise work
        if record.state == RUNNING:
            _kill_running_family(record)
        coord.retire_stale_revise(record.identity)


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
