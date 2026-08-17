"""The exact-head merge gate and final public review outcome (ADR 0003, 0004, 0047).

`decide_merge` is pure: auto-merge requires ALL of an independent (cross-tool)
review, green CI, and a clean verdict. Legacy builder-Revise records retain their bounded
compatibility path; ADR 0047 review actions and reviewer-fix chains settle through the coordinator.
The gh actions it dispatches (CI check, squash-merge, park) are thin wrappers around
the pure decision.
"""

from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass
from enum import Enum

from agentflow import github
from agentflow.reviewer import Verdict, parse_verdict

# Merges stay serialized even as builds run concurrently (ADR 0009 collision floor): two
# PRs never squash-merge at the same instant. Concurrent dispatch (ADR 0023) multiplies
# builds, never overlapping merges — this process-wide lock is where that floor is held.
_MERGE_LOCK = threading.Lock()


class MergeDecision(str, Enum):
    MERGE = "merge"
    REVISE = "revise"
    PARK = "park"    # a concrete two-section human decision handoff


# Revise a fixable miss, but bail after this many unproductive rounds rather than
# looping forever (ADR 0020; was a single round under ADR 0004).
MAX_REVISES = 2


def revise_round_budget_remains(records, repo, subject) -> bool:
    """Whether the auto-revise product cap (ADR 0004's revise round, relaxed to ``MAX_REVISES``
    rounds by ADR 0020's convergence bail) still has room for this issue — fewer than
    ``MAX_REVISES`` *logical* Revise records exist for it, regardless of how many continuation
    attempts each one used. This keeps the per-stage continuation budget separate from the product
    loop: continuation attempts never reset or expand the round cap. Conflict Revises (ADR 0038) are
    counted apart and never spend this one; each conflicting head gets its own bounded stage. Pure —
    the test surface (ADR 0020)."""
    rounds = sum(1 for r in records
                 if r.stage == "revise" and not r.conflict_round
                 and r.repo == repo and str(r.subject) == str(subject))
    return rounds < MAX_REVISES


def review_resume_passes(records, repo, subject) -> int:
    """The cumulative mutating-review ledger a manual Review resumes for this PR.

    A completed Revise starts a new review budget boundary. Reviews opened after the newest such
    boundary carry the ledger forward across heads; a submitted but unfinished Revise moves
    nothing. When the boundary has no later Review yet, its own carried ledger is authoritative —
    normally zero, but non-zero for a conflict-decision Revise. A completed Review record counts
    its own push exactly as the automatic successor path does. Pure (test surface, #501).
    """
    matching = [r for r in records
                if r.repo == repo and str(r.subject) == str(subject)]
    boundary = max(
        (r for r in matching if r.stage == "revise" and r.state == "completed"),
        key=lambda r: (r.created_at, r.identity), default=None)
    def after_boundary(record):
        if boundary is None or record.created_at > boundary.created_at:
            return True
        if record.created_at < boundary.created_at:
            return False
        # Coordinator timestamps have one-second precision. An ordinary Revise's automatic
        # successor advances its round, while its predecessor shares the boundary's round. A
        # conflict Revise deliberately keeps its conflict namespace and carried ledger. Manual
        # resumes have their own positive identity dimension. Those durable facts distinguish a
        # same-second successor from an ordinary pre-boundary Review without relying on the clock.
        return (record.resume > 0
                or record.round > boundary.round
                or (bool(boundary.conflict_round)
                    and record.conflict_round == boundary.conflict_round))

    reviews = [r for r in matching if r.stage == "review" and after_boundary(r)]
    newest = max(reviews, key=lambda r: (r.created_at, r.identity), default=None)
    if newest is None:
        return boundary.review_passes if boundary is not None else 0
    if not newest.outcome:
        return newest.review_passes
    verdict = parse_verdict(
        newest.outcome, expected_sha=newest.target,
        expected_depth=newest.review_depth, expected_axis=newest.review_axis,
        expected_author=newest.change_author_tool,
        owned_heads=((newest.review_prior_push,) if newest.review_prior_push else ()))
    return newest.review_passes + int(verdict.parsed and bool(verdict.pushed_sha))


def conflict_revises_used(records, repo, subject) -> list:
    """The conflict Revise records already opened for this PR in its lifetime (ADR 0038), oldest
    first. They determine the next stable conflict-round identity but do not impose a lifetime cap:
    every genuinely new conflicting head gets another bounded Revise attempt. Includes retired
    records and stays separate from the finding-driven revise rounds. Pure."""
    conflicts = [r for r in records
                 if r.stage == "revise" and r.conflict_round
                 and r.repo == repo and str(r.subject) == str(subject)]
    return sorted(conflicts, key=lambda r: r.conflict_round)


# Every agentflow comment on a PR (the park notice, a build-agent reply) carries this
# marker in its disclaimer, so we can tell our own comments from the maintainer's — the
# same discipline intake uses on issues (INTAKE_MARK). The bot posts as the maintainer,
# so we key on the marker, not authorship.
PR_MARK = github.PR_MARK
# The park handoff's own visible disclaimer. It is the durable dedup key for the current park
# comment, and — because a maintainer replies underneath the question they are answering — the
# signal that a following reply is that park's decision rather than PR discussion (#344).
PARK_MARK = "> *agentflow: parked for human review.*"
_RESPOND_TARGET_PREFIX = "agentflow-respond-target:"
_RESPOND_TARGET_RE = re.compile(r"<!--\s*agentflow-respond-target:([^>]+?)\s*-->")
_RESPOND_PARK_TARGET_RE = re.compile(r"<!--\s*agentflow-respond-park-target:([^>]+?)\s*-->")
_RESPOND_CHANGE_RE = re.compile(r"<!--\s*agentflow-respond-change:([^>]+?)\s*-->")


def respond_reply_disclaimer(target: str) -> str:
    """The human-readable Respond marker plus its immutable maintainer-comment target.

    GitHub posts agentflow comments as the maintainer account, so the visible disclaimer keeps
    authorship distinguishable while the hidden target binds completion to the exact comment one
    durable Respond record owns.
    """
    return ("> *agentflow: reply from the build agent.*\n"
            f"<!-- {_RESPOND_TARGET_PREFIX}{target} -->")


def decision_resume_disclaimer(target: str) -> str:
    """Mark the maintainer's decision answered by the review it resumed (#344).

    This reuses the one Respond target protocol — a single hidden marker per answered comment — so
    the reply queue and the merge gate both see the question closed, without inventing a second
    comment contract for the same fact.
    """
    return ("> *agentflow: your decision resumed the parked review.*\n"
            f"<!-- {_RESPOND_TARGET_PREFIX}{target} -->")


def park_awaiting_decision(comments: list[dict], target: str) -> bool:
    """Whether this comment follows the park handoff that asked for a decision. Pure.

    A maintainer comment after the park answers it; one that predates the park is ordinary PR
    discussion, which never resumes a parked review (#344). Later agentflow comments do not close
    the park: a repeat park edits the same comment in place, so it keeps its original position long
    after a resume marker or a build-agent reply followed it, and a second decision round would
    otherwise be unanswerable. What settles a decision is the maintainer's answer recorded on the
    durable review chain, not who spoke last on the thread.
    """
    target_index = next(
        (index for index, comment in enumerate(comments)
         if str(comment.get("id", "")) == str(target)),
        None,
    )
    parked = [index for index, comment in enumerate(comments)
              if PARK_MARK in comment.get("body", "")]
    return target_index is not None and bool(parked) and parked[-1] < target_index


def respond_change_marker(result: str) -> str:
    """Durable Respond outcome claim: ``none`` or the pushed PR head SHA."""
    return f"<!-- agentflow-respond-change:{result} -->"


def _respond_reply_target(body: str) -> str:
    match = _RESPOND_TARGET_RE.search(body)
    return match.group(1).strip() if match is not None else ""


def respond_reply_posted(comments: list[dict], target: str) -> bool:
    """Whether agentflow durably posted the reply for this exact Respond target."""
    return bool(target) and any(
        PR_MARK in comment.get("body", "")
        and _respond_reply_target(comment.get("body", "")) == str(target)
        for comment in comments
    )


def respond_reply_target_repair(
        comments: list[dict], target: str) -> tuple[str, str] | None:
    """Bind one unambiguous change-marked Respond reply to its preceding target.

    The target marker is daemon-owned proof, not prose the model should have to reproduce. Repair
    only the exact live failure shape: one later visible Respond reply, one change marker, no
    existing target binding, and a comment id that can be edited. Anything ambiguous stays
    incomplete.
    """
    target_index = next(
        (index for index, comment in enumerate(comments)
         if str(comment.get("id", "")) == str(target)),
        None,
    )
    if target_index is None:
        return None
    visible = respond_reply_disclaimer(target).splitlines()[0]
    candidates: list[tuple[str, str]] = []
    for comment in comments[target_index + 1:]:
        body = comment.get("body", "")
        raw_id = comment.get("id")
        comment_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if (not comment_id or not body.startswith(visible) or _RESPOND_TARGET_RE.search(body)
                or len(_RESPOND_CHANGE_RE.findall(body)) != 1):
            continue
        candidates.append((
            comment_id,
            body.replace(visible, respond_reply_disclaimer(target), 1),
        ))
    return candidates[0] if len(candidates) == 1 else None


def respond_reply_change(comments: list[dict], target: str) -> str:
    """The unique targeted reply's declared branch outcome, or empty when unproved.

    Requiring exactly one targeted reply makes duplicate posting visible and fail-closed instead
    of accepting whichever duplicate happens to appear last.
    """
    matches = [comment.get("body", "") for comment in comments
               if _respond_reply_target(comment.get("body", "")) == str(target)]
    if len(matches) != 1:
        return ""
    changes = _RESPOND_CHANGE_RE.findall(matches[0])
    return changes[0].strip() if len(changes) == 1 else ""


def _unanswered_maintainer_comments(comments: list[dict]) -> list[tuple[str, str]]:
    """Return unanswered maintainer comments in arrival order, one durable target each.

    A target-aware Respond reply removes only the comment it answered, so a second maintainer
    comment that arrived before that reply remains pending with its own budget. Legacy generic
    agentflow replies retain their old run-level meaning and answer everything before them.
    """
    pending: dict[str, str] = {}
    for index, comment in enumerate(comments):
        body = comment.get("body", "").strip()
        if not body:
            continue
        answered = _respond_reply_target(body)
        if answered:
            pending.pop(answered, None)
            continue
        parked = _RESPOND_PARK_TARGET_RE.search(body)
        if parked is not None:
            pending.pop(parked.group(1).strip(), None)
            continue
        if PR_MARK in body:
            pending.clear()
            continue
        target = str(comment.get("id") or comment.get("url") or "")
        pending.setdefault(target or f"__agentflow_missing_target_{index}", body)
    return list(pending.items())


def reply_pending(comments: list[dict]) -> bool:
    """True when at least one maintainer comment has no matching agentflow reply. Pure
    (test surface).

    On an `autonomous` repo this BLOCKS auto-merge: nothing merges while a maintainer
    question hangs (issue #18). Mirrors intake's `awaiting_recheck`, keyed on our marker."""
    return bool(_unanswered_maintainer_comments(comments))


def maintainer_comment_id(comments: list[dict]) -> str:
    """The oldest unanswered comment id — one immutable coordinated Respond target."""
    pending = _unanswered_maintainer_comments(comments)
    if not pending or pending[0][0].startswith("__agentflow_missing_target_"):
        return ""
    return pending[0][0]


def maintainer_comment(comments: list[dict]) -> str:
    """The oldest unanswered maintainer comment text — exactly what one Respond answers."""
    pending = _unanswered_maintainer_comments(comments)
    return pending[0][1] if pending else ""


def decide_merge(*, verdict: Verdict, ci_green: bool, reviewer_tool: str,
                 builder_tool: str, revises_used: int,
                 ui_evidence_missing: bool,
                 reply_pending: bool) -> MergeDecision:
    """Pure. Merge only on independent review + green CI + clean verdict — and never
    when a change to a declared UI surface carries no screenshot, nor over an
    unanswered maintainer question on the PR (issue #18).

    `ui_evidence_missing` is the mechanical UI-evidence gate (ADR 0018): it is decided
    from the diff and the PR's attachments, NOT from the review verdict, so a reviewer
    who discards a screenshot-less UI change cannot clear it.
    A missing screenshot parks for a human rather than churning revises — the builder
    was already told to attach one."""
    if reply_pending:
        # An open question from the human who merges blocks auto-merge until the
        # responder addresses it — a reply, not a merge, is the next move.
        return MergeDecision.PARK
    independent = bool(reviewer_tool) and reviewer_tool != builder_tool
    if not independent:
        # ADR 0003: a same-tool / missing review never auto-merges.
        return MergeDecision.PARK
    if not verdict.parsed:
        # The review itself failed to produce a usable verdict — a builder revise
        # can't fix that. Park for a human (or a review retry), don't churn the build.
        return MergeDecision.PARK
    if ui_evidence_missing:
        # Mechanical, unwaivable: a declared UI surface changed with no screenshot.
        return MergeDecision.PARK
    if ci_green and verdict.clean:
        return MergeDecision.MERGE
    if revises_used < MAX_REVISES:
        # ADR 0020: revise a fixable miss, bailing after MAX_REVISES rounds.
        return MergeDecision.REVISE
    return MergeDecision.PARK


# --- the mechanical UI-evidence gate (ADR 0018) --------------------------------
# A change under a declared UI surface must ship a before/after screenshot. Both
# predicates are pure (the test surface); `ui_evidence_gap` wires them to live `gh`.

_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]+\)")           # ![alt](url)
_HTML_IMG_RE = re.compile(r"<img[\s>]", re.IGNORECASE)       # <img ...>
# GitHub drag-drop uploads render as a bare link, not a markdown image:
# user-images.githubusercontent.com/... or github.com/<owner>/<repo|user-attachments>/assets/...
_ASSET_URL_RE = re.compile(
    r"https?://(?:[\w.-]*githubusercontent\.com/|github\.com/[^\s)]+/assets/)", re.IGNORECASE)
# The browserless attachment path builders are instructed to use: screenshots committed
# on the branch under docs/screenshots/, viewable in the PR's Files-changed tab.
_EVIDENCE_FILE_RE = re.compile(
    r"(?:^|/)docs/screenshots/.+\.(?:png|jpe?g|gif|webp)$", re.IGNORECASE)


def touches_ui_surface(changed_files: list[str], surfaces: list[str]) -> bool:
    """Pure. True if any changed file lies under a declared UI-surface prefix. With no
    declared surfaces the intersection is empty — the gate is inert for a non-UI repo."""
    prefixes = [s.strip().lstrip("./") for s in surfaces if s.strip()]
    return any(f.startswith(p) for f in changed_files for p in prefixes)


def has_image_evidence(text: str) -> bool:
    """Pure. True if the text carries an image: a markdown image, an `<img>` tag, or a
    GitHub user-asset URL (drag-dropped uploads are bare links, not markdown images)."""
    return bool(_MD_IMAGE_RE.search(text) or _HTML_IMG_RE.search(text)
                or _ASSET_URL_RE.search(text))


def has_committed_evidence(changed_files: list[str]) -> bool:
    """Pure. True if the PR itself commits screenshots under the evidence convention
    (docs/screenshots/**). Agents cannot use GitHub's drag-drop upload (it needs a
    signed-in browser), so committed files are the first-class evidence channel."""
    return any(_EVIDENCE_FILE_RE.search(f) for f in changed_files)


def ui_verification_required(repo: str, pr_number: int, surfaces: list[str]) -> bool | None:
    """Whether this exact PR changes a declared UI surface, or ``None`` when unreadable.

    The Review result records whether browser verification ran; this independent read decides
    whether that declaration was required. It deliberately shares the UI-evidence gate's
    declaration-only policy instead of inferring a UI from arbitrary source paths.
    """
    if not surfaces:
        return False
    content = github.pr_content(repo, pr_number)
    if content is None:
        return None
    return touches_ui_surface(list(content.paths), surfaces)


def ui_evidence_gap(repo: str, pr_number: int, surfaces: list[str]) -> bool:
    """Live: does this PR change a declared UI surface but carry no screenshot in its
    body or an agentflow-marked comment? Fail-safe — a `gh` error with surfaces declared
    returns True (we can't prove a UI change is evidenced, so don't auto-merge it unseen).

    Evidence is anchored to us: it counts only in the PR body or in a comment agentflow
    authored (`PR_MARK`, the same authorship rule `_round_evidence` uses). A maintainer's
    unrelated image, a cross-PR asset URL, or a stray link in someone else's comment no
    longer satisfies the gate (issue #205)."""
    if not surfaces:
        return False   # non-UI repo: gate inert
    content = github.pr_content(repo, pr_number)
    if content is None:
        return True   # couldn't reach GitHub — fail closed to a gap
    files = list(content.paths)
    if not touches_ui_surface(files, surfaces):
        return False
    if has_committed_evidence(files):
        return False
    if has_image_evidence(content.body):
        return False
    for comment in content.comments:
        if PR_MARK in comment.body and has_image_evidence(comment.body):
            return False
    return True


# Paths whose bytes are pinned by digest in `agentflow/capabilities.toml`. A repo-local edit to
# one of these bricks the repo's enrollment the moment it merges (#735), so the mutation must be
# caught here, before merge, not at the next launch.
PINNED_PATHS = ("scripts/screenshots.mjs",)
_PIN_MANIFEST = "agentflow/capabilities.toml"

PINNED_MUTATION_REASON = (
    "changes the shared screenshot harness in place — repo-local capture behavior belongs in a "
    "small local extension that wraps the shared harness (declared with `screenshot-entry:`), so "
    "the shared file's recorded fingerprint stays intact; merged as-is this would break the "
    "repo's fleet enrollment (#735)")


def pinned_path_mutation(paths, *, owns_pin_manifest: bool) -> bool:
    """Pure: does this changed-file set mutate a pinned path without the sanctioned way through?

    The one sanctioned path is the harness's own repository re-pinning deliberately: the pinned
    bytes and the recorded digest in the manifest move in the same PR (#735). Any other mutation —
    a non-owner repo touching the pinned file at all, or the owner touching it without the
    lockstep manifest update — is a gap. The test surface for the pre-merge pin gate."""
    files = set(paths)
    if not files.intersection(PINNED_PATHS):
        return False
    if owns_pin_manifest and _PIN_MANIFEST in files:
        return False
    return True


def pinned_mutation_gap(repo: str, pr_number: int) -> bool | None:
    """Live: does this PR mutate a pinned path without the owner's lockstep re-pin?

    ``None`` when the PR's files can't be read — the caller defers settlement and re-drives,
    the same recovery the head check gate uses, rather than parking on a transient read."""
    content = github.pr_content(repo, pr_number)
    if content is None:
        return None
    return pinned_path_mutation(content.paths, owns_pin_manifest=_owns_pin_manifest(repo))


def _owns_pin_manifest(repo: str) -> bool:
    """Whether ``repo`` is the repository the pin manifest ships from — the only repo whose PRs
    may move pinned bytes, and only together with the recorded digest."""
    from importlib.resources import files
    from pathlib import Path

    from agentflow.provider_skills import _github_repository
    package_repository = _github_repository(Path(str(files("agentflow"))).parent)
    return bool(package_repository) and repo.casefold() == package_repository


# --- gh actions ----------------------------------------------------------------
def ci_is_green(repo: str, pr_number: int, *,
                timeout: int | None = None,
                interval: int | None = None) -> bool:
    """True only if all required checks completed successfully.

    Polls `gh pr checks` every `interval` seconds until all checks pass or
    `timeout` is reached. `timeout` defaults to AGENTFLOW_CI_TIMEOUT (30 min);
    `interval` defaults to AGENTFLOW_CI_INTERVAL (30 s). Returns False at the
    deadline — never hangs indefinitely. Non-zero on fail or on a repo with no
    checks at all is treated as not-green: fail safe, never auto-merges.
    """
    t = timeout if timeout is not None else int(os.environ.get("AGENTFLOW_CI_TIMEOUT", str(30 * 60)))
    iv = interval if interval is not None else int(os.environ.get("AGENTFLOW_CI_INTERVAL", "30"))
    deadline = time.monotonic() + t
    while True:
        if github.pr_checks_passed(repo, pr_number):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(iv, remaining))


def squash_merge(repo: str, pr_number: int) -> bool:
    # The merge lock serializes the actual land across all concurrent build chains and the
    # survivor re-rebase pass, so merges never overlap (ADR 0009). Held only around the
    # merge itself — never during CI polling — so it can't stall other builds.
    with _MERGE_LOCK:
        is_draft = github.pr_is_draft(repo, pr_number)
        if is_draft is None:
            return False
        if is_draft and not github.pr_ready(repo, pr_number):
            return False
        return github.merge_pr(repo, pr_number)


_CLEAN_REVIEW_MARKER = "<!-- agentflow-clean-review-summary -->"
_CLEAN_REVIEW_HEAD_PREFIX = "<!-- agentflow-clean-review-head:"
_CLEAN_REVIEW_HEAD_SUFFIX = " -->"
_SUPERSEDED_REVIEW_MARKER = "<!-- agentflow-superseded-review-summary -->"


@dataclass(frozen=True, slots=True)
class ParkContext:
    """Concrete domain and retained-work facts required in every public PR park."""

    behavior: str
    options: tuple[str, ...]
    consequences: str
    recommendation: str
    locations: tuple[str, ...]
    conflicts: str
    checks: tuple[str, ...]
    retained_work: str
    next_action: str
    decision_needed: bool = False


def _reviewed_head(comment: github.Comment) -> str | None:
    """Return the head recorded on a clean summary, if this is a stamped summary."""
    start = comment.body.find(_CLEAN_REVIEW_HEAD_PREFIX)
    if start < 0:
        return None
    start += len(_CLEAN_REVIEW_HEAD_PREFIX)
    end = comment.body.find(_CLEAN_REVIEW_HEAD_SUFFIX, start)
    return comment.body[start:end] if end >= start else None


def _supersede_summary(comment_id: str, body: str) -> bool:
    """Retire a current summary without losing the evidence it recorded."""
    if not comment_id:
        return False
    return github.edit_comment(
        comment_id, body.replace(_CLEAN_REVIEW_MARKER, _SUPERSEDED_REVIEW_MARKER, 1))


def live_clean_review(comments: list[dict]) -> dict | None:
    """This PR's current clean-review summary — the engine's own "finished, it's yours" hand-off
    — or ``None`` when it carries none. Pure (test surface).

    Retiring a summary rewrites its marker in place (:func:`_supersede_summary`), so a PR the
    engine has taken back stops reading as handed off with no second fact to keep in step. Takes
    GitHub's own comment rows, the shape the snapshot's park classifier already holds; the caller
    reads whatever it needs off the row it gets back."""
    return next((comment for comment in comments
                 if _CLEAN_REVIEW_MARKER in comment.get("body", "")), None)


def supersede_clean_review(comments: list[dict]) -> bool:
    """Retire this PR's merge hand-off because the engine is taking the PR back.

    The same in-place rewrite :func:`park` already performs — evidence preserved, the hand-off
    retired — so a PR under a freshly opened conflict Revise or re-review stops reading as
    finished and yours to merge. Takes GitHub's own comment rows the caller already holds, so
    taking a PR back costs no second read of the thread. Every live summary is attempted even
    when an earlier edit fails, and the answer is ``False`` unless all of them were retired —
    a caller that reports the failure can then retry from rows it re-reads on a later cycle."""
    retired = [_supersede_summary(row.get("id", ""), row.get("body", ""))
               for row in comments if _CLEAN_REVIEW_MARKER in row.get("body", "")]
    return all(retired)


def post_clean_review_summary(repo: str, pr_number: int, verdict: Verdict,
                              reviewed_head: str) -> bool:
    """Publish and prove exactly one current final summary after a clean review chain."""
    status = ("cross-tool review"
              if verdict.reviewer_tool != verdict.change_author_tool
              else "same-tool review; maintainer merge required")
    fixes = "\n".join(f"- {item}" for item in verdict.fixes) or "- None."
    proposal = tuple(item for item in verdict.follow_ups if not item.historic_url)[:1]
    proposed_follow_up = (
        f"Desired outcome: {proposal[0].desired_outcome}\n"
        f"Evidence: {proposal[0].evidence}"
        if proposal else "None.")
    historic_follow_up = ("\n".join(
        f"- Historical follow-up reference: {item}" for item in verdict.follow_up_issues)
        or "- None.")
    checks = "\n".join(f"- {item}" for item in verdict.checks) or "- No proof recorded."
    body = (
        f"> *agentflow: clean review.*\n{_CLEAN_REVIEW_MARKER}\n"
        f"{_CLEAN_REVIEW_HEAD_PREFIX}{reviewed_head}{_CLEAN_REVIEW_HEAD_SUFFIX}\n\n"
        "Outcome: clean.\n\n"
        f"Review depth: {verdict.depth.value.title()} — "
        f"{verdict.depth_reason or 'legacy review assignment'}\n\n"
        f"Fixes shipped:\n{fixes}\n\n"
        f"Proposed follow-up:\n{proposed_follow_up}\n\n"
        f"Historic follow-up references:\n{historic_follow_up}\n\n"
        f"Checks and proof:\n{checks}\n\n"
        f"Review status: {status}.")
    comments = github.pr_comments(repo, pr_number)
    if comments is None:
        return False
    marked = [comment for comment in comments if _CLEAN_REVIEW_MARKER in comment.body]
    matching = [comment for comment in marked if _reviewed_head(comment) == reviewed_head]
    canonical = matching[0] if matching else None
    for comment in marked:
        if comment is canonical:
            continue
        if not _supersede_summary(comment.id, comment.body):
            return False
    if canonical is not None:
        if canonical.body != body and (not canonical.id or not github.edit_comment(canonical.id, body)):
            return False
    elif not github.pr_comment(repo, pr_number, body):
        return False
    proved = github.pr_comments(repo, pr_number)
    current = ([] if proved is None else [
        comment for comment in proved if _CLEAN_REVIEW_MARKER in comment.body])
    return len(current) == 1 and current[0].body == body


def park(repo: str, pr_number: int, verdict: Verdict | None,
         reason: str = "could not be auto-merged after review",
         missing_outcome: str = "No review was completed — do not treat this as a clean review.",
         context: ParkContext | None = None, proof_marker: str = "") -> None:
    """Post one concrete two-section human decision handoff.

    Pass ``verdict=None`` when no review completed (budget exhaustion): the body
    will say so explicitly rather than listing an empty findings section that
    reads as a clean review.
    """
    comments = github.pr_comments(repo, pr_number)
    if comments is None:
        return
    for summary in (comment for comment in comments if _CLEAN_REVIEW_MARKER in comment.body):
        if not _supersede_summary(summary.id, summary.body):
            return
    if context is None:
        context = ParkContext(
            behavior=f"The PR cannot safely complete because it {reason}.",
            options=("Resume the retained agent stage with clarified intent.",
                     "Close the PR without shipping this behavior."),
            consequences="Resuming may ship the intended behavior; closing leaves current behavior unchanged.",
            recommendation="Clarify the unresolved behavior, then resume the retained stage.",
            locations=tuple(
                item.file for item in (verdict.actions if verdict else ()) if item.file)
                or ("PR branch (exact locations were not produced)",),
            conflicts=(missing_outcome if verdict is None
                       else "The grounded review actions listed below remain unresolved."),
            checks=tuple(verdict.checks if verdict else ()) or ("No completed checks were recorded.",),
            retained_work="The PR branch and retained stage worktree remain available.",
            next_action="Choose an option above and resume the retained stage on the same PR.",
            decision_needed=bool(
                verdict and (
                    verdict.uncertainty is not None
                    or any(item.action.value == "ask_maintainer" for item in verdict.actions))))
    from agentflow.review_policy import format_park_comment

    body = format_park_comment(
        context, verdict, proof_marker=proof_marker, park_mark=PARK_MARK)
    if proof_marker:
        parked = [
            comment for comment in comments if PARK_MARK in comment.body]
        if parked:
            if parked[-1].id:
                github.edit_comment(parked[-1].id, body)
            return  # never multiply park comments when the current one cannot be updated
    github.pr_comment(repo, pr_number, body)
