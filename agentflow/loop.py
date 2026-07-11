"""The M0 loop — one ready-for-agent issue through the whole pipeline, serially.

Ephemeral hands, single issue (ADR 0011); the persistent daemon and real two-pool
balancing are M1. For M0 the pair is fixed — Claude builds, Codex reviews — so
cross-tool independence holds and swapping in the headroom balancer is the M1 change.
Every ready issue must carry an `agentflow:complexity:*` label (the hard gate,
ADR 0018) — intake stamps it; the loop reads it and skips an issue that has none.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path

from agentflow import ratchet
from agentflow.balancer import pick_pair
from agentflow.gate import (MAX_REVISES, MergeDecision, ci_is_green, decide_merge,
                            maintainer_comment, park, reply_pending, squash_merge,
                            ui_evidence_gap)
from agentflow.intake import (Intake, IntakeResult, IntakeRoute, STATE_LABELS, _DISCLAIMER,
                              apply_intake, awaiting_recheck, replies_since_intake)
from agentflow.notify import notify
from agentflow.reviewer import Reviewer, Verdict
from agentflow.runner import BuildStatus, BuildTask, Complexity, Effort, _run


def _pr_url(repo: str, pr: int) -> str:
    return f"https://github.com/{repo}/pull/{pr}"


def _pr_comments(repo: str, pr: int) -> list[dict]:
    """The PR's comments, or [] if they can't be read. Impure. A read failure reads as
    'no maintainer question' — but `decide_merge` still needs independent review + green
    CI + a clean verdict, so a `gh` blip never turns into an unsafe merge."""
    r = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "comments"])
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout or "{}").get("comments", [])
    except json.JSONDecodeError:
        return []


_COMPLEXITY_LABEL = re.compile(r"^agentflow:complexity:(standard|deep)$")
_EFFORT_LABEL = re.compile(r"^agentflow:effort:(low|medium|high|extra)$")
_PROFILE_RE = re.compile(r"^profile:\s*(autonomous|reviewed|guarded)", re.MULTILINE)
_UI_SURFACES_RE = re.compile(r"^ui-surfaces:\s*(.+)$", re.MULTILINE)

# The park reason for the mechanical UI-evidence gap (ADR 0018) — the human needs to
# know the block is the missing screenshot, not the review verdict. Unwaivable, so it
# reads the same whether a reviewed repo hands over or an autonomous one drops out.
_UI_GAP_REASON = ("touches a user-facing surface but has no before/after screenshot — the "
                  "charter requires visual proof it matches the locked design, so it can't "
                  "merge unseen (ADR 0018)")


def repo_profile(workdir: str) -> str:
    """The repo's autonomy profile from its AGENTS.md/CLAUDE.md `profile:` line.
    Defaults to `reviewed` (ADR 0002) — the safe middle, never auto-merge by accident."""
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = Path(workdir) / name
        if p.exists():
            m = _PROFILE_RE.search(p.read_text(errors="replace"))
            if m:
                return m.group(1)
    return "reviewed"


def ui_surfaces(workdir: str) -> list[str]:
    """The repo's declared user-facing surfaces from its AGENTS.md/CLAUDE.md
    `ui-surfaces:` line — a comma-separated list of path prefixes (e.g.
    `agentflow/static/, frontend/`), or `[]` when none is declared. A change under
    one of these needs a before/after screenshot: the charter's UI-evidence gate
    (ADR 0018) reads this per repo instead of a hardcoded example."""
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = Path(workdir) / name
        if p.exists():
            m = _UI_SURFACES_RE.search(p.read_text(errors="replace"))
            if m:
                return [s.strip() for s in m.group(1).split(",") if s.strip()]
    return []


def _surfaces_phrase(surfaces: list[str]) -> str:
    """How to name the repo's UI surfaces to a builder/reviewer prompt."""
    return ", ".join(f"`{s}`" for s in surfaces) if surfaces \
        else "any user-facing surface (frontend, UI templates, etc.)"


@dataclass(frozen=True)
class RepoConfig:
    repo: str        # "owner/name" on GitHub
    workdir: str     # local main checkout


def complexity_from_labels(labels: list[str]) -> Complexity | None:
    """The issue's model-size dial from its `agentflow:complexity:*` label. Hard gate
    — no build without one (ADR 0018)."""
    for name in labels:
        m = _COMPLEXITY_LABEL.match(name)
        if m:
            return Complexity(m.group(1))
    return None


def effort_from_labels(labels: list[str]) -> Effort:
    """The issue's effort dial from its `agentflow:effort:*` label; defaults to
    `medium` when absent (guidance, not a hard gate — ADR 0018)."""
    for name in labels:
        m = _EFFORT_LABEL.match(name)
        if m:
            return Effort(m.group(1))
    return Effort.MEDIUM


def slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40].strip("-") or "issue"


def pr_number(url: str) -> int:
    return int(url.rstrip("/").rsplit("/", 1)[-1])


_BRANCH_ISSUE_RE = re.compile(r"^agentflow/[^/]+/issue-(\d+)-")
_BRANCH_RE = re.compile(r"^agentflow/([^/]+)/issue-(\d+)-(.+)$")


def issue_of_branch(branch: str) -> int | None:
    """The issue number an agentflow PR branch is working, or None. Pure — the signal
    that an agent already owns an issue (dispatch dedup, beyond ADR 0009's merge floor)."""
    m = _BRANCH_ISSUE_RE.match(branch or "")
    return int(m.group(1)) if m else None


BUILD_PROMPT = """Implement {repo} issue #{n}: {title}

{body}

Effort budget: {effort}. Scope your work to match — don't gold-plate a low-effort
issue or under-invest a high-effort one.

You are in a fresh worktree on a new branch off origin/main. Implement the change and
commit your work. Cover the new behavior with a test that **exercises it through the
public interface** — and, where it fits, one that **failed first for the right reason**
(the charter test standard) — then make the suite green.

Before opening the PR, `git fetch origin` and rebase once onto `origin/main`, then
rerun the tests. If the rebase conflicts (or tests fail post-rebase for a reason not
your own), stop and post a comment prefixed `INTEGRATION-COLLISION:` instead of
forcing it. Otherwise push the branch and open a PR with `Closes #{n}` in the body.

Write the PR body for the human who merges it — plain language: what changed, why, and
what to check, in the app's own domain terms. No jargon: no file/function/test names or
CSS/API specifics (ADR 0018). If the change touches a user-facing surface (this repo's are:
{surfaces}), you MUST attach before/after screenshots (headless Playwright) as proof it
matches the locked mockup — both light and dark themes where the app has them. A UI change
with no screenshot cannot auto-merge (a mechanical gate parks it, ADR 0018), and a body full
of jargon blocks at review. Both are charter gates, not style points.

Keep the change minimal and match the surrounding code. If you hit a blocker you
cannot safely resolve, post a comment prefixed `MISSING-CONTEXT:` and stop instead
of guessing."""

REVISE_PROMPT = """Address the blocking review findings on PR #{n} in this worktree,
push to the same branch, and keep the test suite green. Do NOT open a new PR.

Do not degrade the two charter gates while revising (ADR 0018):
- If the PR touches a user-facing surface (this repo's are: {surfaces}), keep before/after
  screenshots attached — both light and dark themes where the app has them. A UI change with
  no screenshot cannot auto-merge; a mechanical gate parks it regardless of the review.
- Keep the PR body in plain app language for the human who merges — no file/function/test
  names or CSS/API specifics.

Blocking findings:
{findings}"""

# The responder's reply disclaimer — carries the PR marker (`agentflow:`) so the next
# cycle can tell it apart from the maintainer's comment (see gate.reply_pending).
_RESPOND_DISCLAIMER = "> *agentflow: reply from the build agent.*"

RESPOND_PROMPT = """A maintainer left a comment on PR #{n} in this worktree and it is
still unanswered. Read the full conversation first (`gh pr view {n} --json comments`),
then answer their latest comment. This is a REPLY, not a fresh review — answer what they
actually asked, nothing more.

Their comment:
---
{comment}
---

Reply conversationally in a PR comment that STARTS with this exact line, so we can tell
your reply apart from theirs:
{disclaimer}

- Answer in plain language, in the app's own terms — no code symbols or file paths.
- If they asked for evidence (e.g. "show me a screenshot"), produce it and ATTACH it to
  your comment — headless Playwright against the locked mockup for a UI surface, the same
  way a build does.
- If they asked for a small change, make it, commit, and push to THIS SAME branch.

Same contract as a revision: never open a new PR, never merge. If you genuinely can't
address it, say so plainly in the reply."""


def _issues_in_flight(cfg: RepoConfig) -> set[int] | None:
    """Issues that already have an OPEN agentflow PR — an agent is on them, so don't
    re-dispatch a duplicate. Dispatch dedup, distinct from ADR 0009's merge-time floor:
    an issue stays `ready-for-agent` while its PR is in review, so without this the loop
    would re-build it every cycle (a second PR on a different tool).

    Returns None when the listing itself failed — unknown is NOT empty. Treating a `gh`
    blip as "nothing in flight" would both re-dispatch every in-review issue and let
    `reclaim_claims` strip live claims; callers fail closed (skip, retry next cycle)."""
    r = _run(["gh", "pr", "list", "--repo", cfg.repo, "--state", "open",
              "--json", "headRefName", "--limit", "100"])
    if r.returncode != 0:
        return None
    return {n for pr in json.loads(r.stdout or "[]")
            if (n := issue_of_branch(pr.get("headRefName", ""))) is not None}


BUILDING = "agentflow:building"   # dispatch claim — an agent is building this issue


def _free_to_dispatch(issue: dict, in_flight: set[int]) -> bool:
    """A ready issue is free only if no agent already owns it — no `agentflow:building`
    claim (set before the build, closing the no-PR-yet window) and no open agentflow PR
    (the parked-in-review window, which outlives the claim). Pure (test surface)."""
    return (issue["number"] not in in_flight
            and BUILDING not in {lbl["name"] for lbl in issue.get("labels", [])})


def _next_ready_issue(cfg: RepoConfig) -> dict | None:
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", "ready-for-agent", "--json", "number,title,body,labels",
              "--limit", "50"])
    if r.returncode != 0:
        return None
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return None   # can't see what's in flight — fail closed, dispatch next cycle
    issues = sorted((i for i in json.loads(r.stdout or "[]") if _free_to_dispatch(i, in_flight)),
                    key=lambda i: i["number"])
    return issues[0] if issues else None


def reclaim_claims(cfg: RepoConfig) -> int:
    """Drop `agentflow:building` claims orphaned by a crash — a freshly-started daemon has
    no live builds, so any claim without an open agentflow PR is stale. A stale claim is
    fail-safe (the issue is skipped, never duplicated) but blocks that issue until cleared.
    Returns how many it cleared."""
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", BUILDING, "--json", "number", "--limit", "100"])
    if r.returncode != 0:
        return 0
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return 0   # can't see what's in flight — a live claim must never be stripped
    stale = [i["number"] for i in json.loads(r.stdout or "[]") if i["number"] not in in_flight]
    for n in stale:
        _release(cfg.repo, n)
    return len(stale)


TRIAGING = "agentflow:triaging"   # dispatch claim — a grounding session owns this issue

# Out of the intake queue: a resolved state label, or a live triaging claim.
_TRIAGE_SKIP = set(STATE_LABELS) | {TRIAGING}


def _untriaged(issue: dict) -> bool:
    """An issue is in the intake queue only if nothing has resolved or claimed it — none of
    intake's state labels and no `agentflow:triaging` claim (set before the grounding session,
    closing intake's no-label-yet window, symmetric to `_free_to_dispatch`). Pure (test surface)."""
    return not ({lbl["name"] for lbl in issue.get("labels", [])} & _TRIAGE_SKIP)


def _next_untriaged_issue(cfg: RepoConfig) -> dict | None:
    """The oldest open issue in the intake queue — none of intake's state labels and unclaimed
    by a live grounding session (ADR 0016)."""
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--json", "number,title,body,labels", "--limit", "50"])
    if r.returncode != 0:
        return None
    untriaged = [i for i in json.loads(r.stdout or "[]") if _untriaged(i)]
    return min(untriaged, key=lambda i: i["number"]) if untriaged else None


def _builder_worktree(cfg: RepoConfig, tool: str, n: int, sl: str) -> str:
    return str(Path(cfg.workdir) / ".agentflow" / "worktrees" / tool / f"issue-{n}-{sl}")


def _launch_revise(builder, cfg: RepoConfig, pr: int, n: int, sl: str,
                   complexity: Complexity, verdict: Verdict) -> None:
    """One builder pass addressing the blocking findings on the PR branch (ADR 0020)."""
    findings = "\n".join(f"- {f.summary}" for f in verdict.blocking) or "- (see review)"
    surfaces = _surfaces_phrase(ui_surfaces(cfg.workdir))
    builder.launch(REVISE_PROMPT.format(n=pr, findings=findings, surfaces=surfaces),
                   cwd=_builder_worktree(cfg, builder.tool, n, sl),
                   model=builder.model_for(complexity))


def held_build_result(status: str, where: str) -> IntakeResult:
    """The hold a stuck build hands back — routes the issue to `needs-grilling` instead of
    leaving it `ready-for-agent`, where the loop would re-pick it every cycle: a fresh build
    session, a duplicate bail comment, and a duplicate ping per cycle, with the rest of the
    queue stalled behind it (ADR 0021's claim only covers a *live* build). The body carries
    the intake marker, so a maintainer reply resumes it through the normal re-intake path
    (ADR 0019). Pure (test surface)."""
    body = (f"{_DISCLAIMER}\n\nThe build stopped before opening a PR ({status}) — details in "
            f"{where}. I've held this rather than retrying blind. Reply here with what's "
            "missing (or run `/agentflow pickup` to drive it live) and I'll re-scope and retry.")
    return IntakeResult(IntakeRoute.GRILL, body)


def _preserve_progress(cfg: RepoConfig, tool: str, n: int, sl: str) -> str | None:
    """A stuck build's commits live in its worktree but aren't on GitHub. If there are
    any, push the branch and open a DRAFT PR so nothing is lost; else None. Returns the
    draft PR url. Live orchestration, not unit-tested (ADR 0020)."""
    wt = _builder_worktree(cfg, tool, n, sl)
    branch = f"agentflow/{tool}/issue-{n}-{sl}"
    ahead = _run(["git", "-C", wt, "rev-list", "--count", "origin/main..HEAD"])
    if ahead.returncode != 0 or ahead.stdout.strip() in ("", "0"):
        return None
    if _run(["git", "-C", wt, "push", "-u", "origin", branch]).returncode != 0:
        return None
    existing = _run(["gh", "pr", "list", "--repo", cfg.repo, "--head", branch,
                     "--state", "all", "--json", "url", "-q", '.[0].url // ""']).stdout.strip()
    if existing:
        return existing
    r = _run(["gh", "pr", "create", "--repo", cfg.repo, "--draft", "--head", branch,
              "--title", f"[draft] #{n} — handed back (build did not finish)",
              "--body", f"> *agentflow: build stopped early; progress saved for you.*\n\nCloses #{n} when finished."])
    return r.stdout.strip() or None


def run_once(cfg: RepoConfig) -> str:
    """Pull the next ready issue and run it end to end. Returns a one-line result."""
    issue = _next_ready_issue(cfg)
    if not issue:
        return "no ready-for-agent issues"
    return _dispatch_build(cfg, issue)


HELD_LABELS = {"agentflow:needs-grilling", "agentflow:needs-mockup"}


def build_issue(cfg: RepoConfig, n: int) -> str:
    """By-hand build of a *specific* ready issue (ADR 0022's `build <N>`). Fetches issue N,
    **refuses and redirects** anything that isn't `ready-for-agent` (a held issue → `pickup`;
    an un-triaged one → `triage`/`scope`), refuses one already claimed or in flight, then
    drives the same build path as the daemon — one builder path, one `agentflow:building`
    claim, cross-review and merge/park per the repo's profile."""
    r = _run(["gh", "issue", "view", str(n), "--repo", cfg.repo,
              "--json", "number,title,body,labels,state"])
    if r.returncode != 0:
        return f"#{n}: not found in {cfg.repo}"
    issue = json.loads(r.stdout)
    if issue.get("state") != "OPEN":
        return f"#{n}: closed — nothing to build"
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if "ready-for-agent" not in labels:
        held = labels & HELD_LABELS
        if held:
            return f"#{n}: held — resume it with `/agentflow pickup {n}`, not build"
        return f"#{n}: not ready — run `/agentflow triage {n}` (or `scope {n}`) first"
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return f"#{n}: can't see what's in flight (gh error) — refusing to risk a duplicate; retry"
    if not _free_to_dispatch(issue, in_flight):
        return f"#{n}: already claimed or in flight — a build already owns it"
    return _dispatch_build(cfg, issue, operator=True)


def _dispatch_build(cfg: RepoConfig, issue: dict, operator: bool = False) -> str:
    """Build one already-selected ready issue end to end: gate on the complexity label,
    claim it, then build → cross-review → merge/park under the claim. Shared by the daemon's
    next-ready pull (`run_once`) and the by-hand `build <N>` (`build_issue`) so there is one
    builder path, not two. Every profile builds from the Agent Brief in the issue body (ADR
    0022) — there is no separate work-order comment.

    `operator=True` (by-hand `build <N>`) skips the pools' recent-activity guard — the
    operator is the live session and asked for this — while still deferring on a genuinely
    rate-limited pool. The daemon's `run_once` leaves it False and keeps the full guard."""
    n = issue["number"]
    labels = [lbl["name"] for lbl in issue["labels"]]
    complexity = complexity_from_labels(labels)
    if complexity is None:
        return f"#{n}: skipped — no agentflow:complexity:* label (ADR 0018 hard gate)"
    effort = effort_from_labels(labels)

    builder, reviewer_runner = pick_pair(operator=operator)   # ADR 0006: more headroom builds; other reviews
    if builder is None:
        return f"#{n}: no pool has headroom right now — deferring"
    profile = repo_profile(cfg.workdir)
    surfaces = ui_surfaces(cfg.workdir)
    sl = slug(issue["title"])
    build_prompt = BUILD_PROMPT.format(repo=cfg.repo, n=n, title=issue["title"],
                                       body=issue.get("body") or "", effort=effort.value,
                                       surfaces=_surfaces_phrase(surfaces))
    _claim(cfg.repo, n)   # an agent now owns this issue — no duplicate dispatch (dedup)
    try:
        return _build_review_merge(cfg, issue, n, sl, complexity, effort,
                                   builder, reviewer_runner, profile, build_prompt)
    finally:
        _release(cfg.repo, n)


def _claim(repo: str, n: int) -> None:
    """Mark issue n as owned by an agent *before* its build runs, so a concurrent or
    next-cycle dispatch skips it (closes the no-PR-yet window). Ensures the label first."""
    _run(["gh", "label", "create", BUILDING, "--repo", repo, "--color", "fbca04",
          "--description", "An agent is building this issue", "--force"])
    _run(["gh", "issue", "edit", str(n), "--repo", repo, "--add-label", BUILDING])


def _release(repo: str, n: int) -> None:
    """Drop the build claim when the build is done, whatever the outcome. A parked PR
    stays skipped via the open-PR check; a failed build is free to retry next cycle."""
    _run(["gh", "issue", "edit", str(n), "--repo", repo, "--remove-label", BUILDING])


def _claim_triage(repo: str, n: int) -> None:
    """Claim issue n for intake *before* its grounding session, so a concurrent or next-cycle
    dispatch skips it — closing intake's no-label-yet window (the state label is only stamped
    once the session finishes). Symmetric to `_claim`; ensures the label first."""
    _run(["gh", "label", "create", TRIAGING, "--repo", repo, "--color", "d4c5f9",
          "--description", "A grounding session is triaging this issue", "--force"])
    _run(["gh", "issue", "edit", str(n), "--repo", repo, "--add-label", TRIAGING])


def _release_triage(repo: str, n: int) -> None:
    """Drop the intake claim once routing is written (the state label dedups from here) or the
    session ended. A crash *before* this strands the claim: fail-safe (the issue is skipped,
    never double-triaged), cleared by hand — intake opens no PR, so there's no liveness signal
    for a safe auto-reclaim like builds have."""
    _run(["gh", "issue", "edit", str(n), "--repo", repo, "--remove-label", TRIAGING])


def _build_review_merge(cfg: RepoConfig, issue: dict, n: int, sl: str, complexity: Complexity,
                        effort: Effort, builder, reviewer_runner, profile: str,
                        build_prompt: str) -> str:
    """Build the issue, then cross-review -> merge/park. Runs under run_once's
    `agentflow:building` claim (dispatch dedup)."""
    task = BuildTask(cfg.repo, cfg.workdir, n, sl, complexity, effort, prompt=build_prompt)
    outcome = builder.build(task)
    if outcome.status is not BuildStatus.PR_OPENED:
        # Stuck (a bail marker, or ran with no PR). Preserve the work as a draft PR so
        # nothing is lost, then hand the issue back HELD — still-`ready` would be
        # re-dispatched every cycle (see held_build_result).
        draft = _preserve_progress(cfg, builder.tool, n, sl)
        where = f"draft PR {draft}" if draft else "the build session's comment on the issue"
        apply_intake(cfg.repo, n, issue.get("title", ""),
                     [lbl["name"] for lbl in issue.get("labels", [])],
                     held_build_result(outcome.status.value, where))
        notify("agentflow needs you", f"{cfg.repo} #{n}: build {outcome.status.value} — {where}",
               draft or f"https://github.com/{cfg.repo}/issues/{n}")
        return f"#{n}: build {outcome.status.value} — {outcome.detail}; held for you ({where})"

    pr = pr_number(outcome.pr_url)
    head_branch = f"agentflow/{builder.tool}/issue-{n}-{sl}"
    surfaces = ui_surfaces(cfg.workdir)
    surfaces_phrase = _surfaces_phrase(surfaces)
    # Prefer cross-tool; if only one tool is free, review same-tool rather than stall
    # (ADR 0020). Same-tool never auto-merges — decide_merge parks it.
    reviewer_runner = reviewer_runner or builder
    reviewer = Reviewer(reviewer_runner)
    acceptance = issue.get("body") or ""

    if profile != "autonomous":
        # reviewed / guarded: revise until the review is clean (or we bail), then a
        # HUMAN merges (ADR 0002, 0020) — hand over a clean PR when we can.
        revises_used = 0
        while True:
            verdict = reviewer.review(cfg.repo, cfg.workdir, pr, head_branch, sl, complexity,
                                      acceptance=acceptance, surfaces=surfaces_phrase)
            if verdict.clean or not (verdict.parsed and verdict.blocking) or revises_used >= MAX_REVISES:
                # A human merges either way, but the mechanical UI-evidence gate still runs
                # (ADR 0018): a screenshot-less UI change must park with the missing-screenshot
                # reason, so the human isn't handed one that silently fails the charter gate.
                ui_gap = ui_evidence_gap(cfg.repo, pr, surfaces)
                reason = _UI_GAP_REASON if ui_gap else f"is a `{profile}` repo — a human merges"
                park(cfg.repo, pr, verdict, reason=reason)
                notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} reviewed ({profile}) — your merge",
                       _pr_url(cfg.repo, pr))
                return f"#{n}: PR #{pr} reviewed ({profile}) — awaiting human merge"
            _launch_revise(builder, cfg, pr, n, sl, complexity, verdict)
            revises_used += 1

    revises_used = 0
    while True:
        verdict = reviewer.review(cfg.repo, cfg.workdir, pr, head_branch, sl, complexity,
                                  acceptance=acceptance, surfaces=surfaces_phrase)
        # The UI-evidence gate is read from the diff + attachments, AFTER the review, so a
        # reviewer's "not blocking" cannot clear a screenshot-less UI change (ADR 0018).
        ui_gap = ui_evidence_gap(cfg.repo, pr, surfaces)
        decision = decide_merge(verdict=verdict, ci_green=ci_is_green(cfg.repo, pr),
                                reviewer_tool=reviewer_runner.tool, builder_tool=builder.tool,
                                revises_used=revises_used, ui_evidence_missing=ui_gap,
                                reply_pending=reply_pending(_pr_comments(cfg.repo, pr)))
        if decision is MergeDecision.MERGE:
            ok = squash_merge(cfg.repo, pr)
            if ok:
                ratchet.record(cfg.repo, ratchet.CLEAN_MERGE if revises_used == 0
                               else "merge_after_revise")
                return f"#{n}: MERGED PR #{pr}"
            park(cfg.repo, pr, verdict,
                 reason="could not be squash-merged (branch protection, conflict, or transient error)")
            ratchet.record(cfg.repo, "parked")
            notify("agentflow needs you",
                   f"{cfg.repo} #{n}: PR #{pr} merge failed — your action needed",
                   _pr_url(cfg.repo, pr))
            return f"#{n}: merge failed on PR #{pr}"
        if decision is MergeDecision.PARK:
            reason = _UI_GAP_REASON if ui_gap else "could not be auto-merged after review"
            park(cfg.repo, pr, verdict, reason=reason)
            ratchet.record(cfg.repo, "parked")
            notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} parked after review",
                   _pr_url(cfg.repo, pr))
            return f"#{n}: parked PR #{pr} for human review"
        _launch_revise(builder, cfg, pr, n, sl, complexity, verdict)
        revises_used += 1


def _next_resumable_issue(cfg: RepoConfig) -> tuple[dict, str] | None:
    """A `needs-grilling` or `needs-mockup` issue whose latest comment is the maintainer's
    reply — return it with their answer text so intake can resolve it (ADR 0019). A waiver
    reply on a mockup-held issue ("skip the mockup, build it") promotes to ready; a locked-spec
    reply ("here is the visual spec") does the same. Use `/agentflow pickup <N>` to drive
    either interactively instead."""
    issues: list[dict] = []
    for label in ("agentflow:needs-grilling", "agentflow:needs-mockup"):
        r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
                  "--label", label, "--json", "number,title,body,labels",
                  "--limit", "50"])
        if r.returncode != 0:
            return None
        issues.extend(json.loads(r.stdout or "[]"))
    seen: set[int] = set()
    deduped = []
    for issue in sorted(issues, key=lambda i: i["number"]):
        if issue["number"] not in seen:
            seen.add(issue["number"])
            deduped.append(issue)
    for issue in deduped:
        if TRIAGING in {lbl["name"] for lbl in issue["labels"]}:
            continue   # a re-intake already owns this held issue
        cr = _run(["gh", "issue", "view", str(issue["number"]), "--repo", cfg.repo, "--json", "comments"])
        if cr.returncode != 0:
            continue
        comments = json.loads(cr.stdout or "{}").get("comments", [])
        if awaiting_recheck(comments):
            return issue, replies_since_intake(comments)
    return None


# Consecutive intake infra failures per (repo, issue), in-memory in the daemon. Infra
# failures leave no GitHub trace by design (no comment, no label), so the retry streak
# lives here; a restart resets it — fine for a bounded safety backstop, not state of record.
_intake_infra_failures: dict[tuple[str, int], int] = {}
INTAKE_MAX_INFRA_FAILURES = 3   # after this many in a row on one issue, post one held comment


def _handle_intake_infra_failure(cfg: RepoConfig, n: int, result: IntakeResult) -> str:
    """An intake that fell over before the model weighed in (worktree/provision/launch).
    Post nothing, change no labels, leave the issue for next cycle — until the streak hits
    the backstop, when we post exactly one held comment so a truly broken issue isn't
    retried forever in silence (ADR 0019)."""
    key = (cfg.repo, n)
    fails = _intake_infra_failures.get(key, 0) + 1
    _intake_infra_failures[key] = fails
    if fails < INTAKE_MAX_INFRA_FAILURES:
        return f"#{n}: intake couldn't start ({result.detail}) — retrying silently ({fails}/{INTAKE_MAX_INFRA_FAILURES})"
    del _intake_infra_failures[key]
    apply_intake(cfg.repo, n, "", [], replace(result, infra_failed=False))  # a real, human-visible hold
    notify("agentflow needs you", f"{cfg.repo} #{n}: intake keeps failing to start",
           f"https://github.com/{cfg.repo}/issues/{n}")
    return f"#{n}: intake couldn't start ({result.detail}) — held after {INTAKE_MAX_INFRA_FAILURES} tries"


def intake_once(cfg: RepoConfig) -> str:
    """Triage the next issue: a held issue the maintainer just answered (resume, ADR
    0019) or the oldest un-triaged one (ADR 0016). Ground, route, write to GitHub."""
    resumable = _next_resumable_issue(cfg)
    issue, extra = resumable if resumable else (_next_untriaged_issue(cfg), "")
    if not issue:
        return "no un-triaged issues"
    n = issue["number"]
    builder, _ = pick_pair()   # intake needs one available tool, not a pair
    if builder is None:
        return f"#{n}: no pool has headroom for intake — deferring"
    _claim_triage(cfg.repo, n)   # own the issue before the long session (dispatch dedup)
    try:
        result = Intake(builder).intake(cfg.repo, cfg.workdir, issue, extra=extra)
        if result.infra_failed:
            return _handle_intake_infra_failure(cfg, n, result)
        _intake_infra_failures.pop((cfg.repo, n), None)   # a clean run ends the streak
        current_labels = [lbl["name"] for lbl in issue.get("labels", [])]
        summary = apply_intake(cfg.repo, n, issue.get("title", ""), current_labels, result)
    finally:
        _release_triage(cfg.repo, n)   # the state label dedups from here; drop the claim
    if result.route in (IntakeRoute.GRILL, IntakeRoute.MOCKUP):
        notify("agentflow needs you", f"{cfg.repo} #{n}: {result.route.value}",
               f"https://github.com/{cfg.repo}/issues/{n}")
    return f"#{n}: {summary}{' (resumed)' if extra else ''}"


def _next_pr_awaiting_reply(cfg: RepoConfig) -> tuple[int, str, str] | None:
    """The next open agentflow PR whose latest comment is the maintainer's unanswered
    question — returns (pr_number, head_branch, their_comment). Skips a PR where our own
    marker had the last word (a park notice, or a reply we already posted) so the responder
    never wakes on its own comments (issue #18)."""
    r = _run(["gh", "pr", "list", "--repo", cfg.repo, "--state", "open",
              "--json", "number,headRefName", "--limit", "100"])
    if r.returncode != 0:
        return None
    for pr in sorted(json.loads(r.stdout or "[]"), key=lambda p: p.get("number", 0)):
        branch = pr.get("headRefName", "")
        if issue_of_branch(branch) is None:
            continue   # not an agentflow PR — a human's own branch
        cr = _run(["gh", "pr", "view", str(pr["number"]), "--repo", cfg.repo, "--json", "comments"])
        if cr.returncode != 0:
            continue
        comments = json.loads(cr.stdout or "{}").get("comments", [])
        if reply_pending(comments):
            return pr["number"], branch, maintainer_comment(comments)
    return None


def _checkout_pr_branch(cfg: RepoConfig, branch: str, wt: Path) -> bool:
    """Put the PR branch in a worktree so a responder can push fixes to it. Reuses the
    builder's worktree when it's still there (freshened to the PR head), else cuts a fresh
    one tracking `origin/<branch>`. Returns success. Live orchestration, not unit-tested."""
    if _run(["git", "-C", cfg.workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    if wt.exists():
        return _run(["git", "-C", str(wt), "reset", "--hard", f"origin/{branch}"]).returncode == 0
    wt.parent.mkdir(parents=True, exist_ok=True)
    return _run(["git", "-C", cfg.workdir, "worktree", "add", "-B", branch,
                 str(wt), f"origin/{branch}"]).returncode == 0


def respond_once(cfg: RepoConfig) -> str:
    """Answer the next parked PR whose latest comment is an unanswered maintainer question:
    spawn a responder in the PR-branch worktree that replies in-thread, attaches requested
    evidence, and pushes small fixes to the same branch (issue #18). Never merges, never a
    new PR — same contract as a revise."""
    pending = _next_pr_awaiting_reply(cfg)
    if not pending:
        return "no parked PRs awaiting reply"
    pr, branch, comment = pending
    m = _BRANCH_RE.match(branch)
    if not m:
        return f"PR #{pr}: unrecognized branch {branch}"
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    builder, _ = pick_pair()   # a reply needs one available tool, not a pair
    if builder is None:
        return f"PR #{pr}: no pool has headroom to respond — deferring"
    wt = Path(_builder_worktree(cfg, tool, n, sl))
    if not _checkout_pr_branch(cfg, branch, wt):
        return f"PR #{pr}: could not check out {branch} to respond — retry next cycle"
    builder.provision(wt)
    ok, _ = builder.launch(RESPOND_PROMPT.format(n=pr, comment=comment, disclaimer=_RESPOND_DISCLAIMER),
                           cwd=str(wt), model=builder.model_for(Complexity.DEEP))
    return f"PR #{pr}: replied to the maintainer" if ok else f"PR #{pr}: responder session errored"


def pipeline_once(cfg: RepoConfig) -> str:
    """One full pass for a repo: triage one un-triaged issue, build one ready issue, and
    answer one parked PR the maintainer commented on (ADR 0016 — intake runs ahead of the
    build queue; issue #18 — parked PRs stay answered)."""
    return f"intake: {intake_once(cfg)} · build: {run_once(cfg)} · respond: {respond_once(cfg)}"


def _main_config(argv: list[str]) -> RepoConfig:
    """Parse CLI args: <owner/repo> [workdir]. Workdir defaults to ~/Code/<owner>/<name>."""
    if not argv:
        raise SystemExit("usage: python -m agentflow.loop <owner/repo> [workdir]")
    repo = argv[0]
    if len(argv) > 1:
        workdir = argv[1]
    else:
        owner, name = (repo.split("/", 1) + ["repo"])[:2]
        workdir = str(Path.home() / "Code" / owner / name)
    return RepoConfig(repo=repo, workdir=workdir)


if __name__ == "__main__":
    import sys
    print(pipeline_once(_main_config(sys.argv[1:])))
