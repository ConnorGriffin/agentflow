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
from dataclasses import dataclass
from pathlib import Path

from agentflow import ratchet
from agentflow.balancer import pick_pair
from agentflow.gate import MAX_REVISES, MergeDecision, ci_is_green, decide_merge, park, squash_merge
from agentflow.intake import (Intake, IntakeRoute, STATE_LABELS, apply_intake,
                              awaiting_recheck, replies_since_intake)
from agentflow.notify import notify
from agentflow.reviewer import Reviewer, Verdict
from agentflow.runner import BuildStatus, BuildTask, Complexity, Effort, _run


def _pr_url(repo: str, pr: int) -> str:
    return f"https://github.com/{repo}/pull/{pr}"


_COMPLEXITY_LABEL = re.compile(r"^agentflow:complexity:(standard|deep)$")
_EFFORT_LABEL = re.compile(r"^agentflow:effort:(low|medium|high|extra)$")
_PROFILE_RE = re.compile(r"^profile:\s*(autonomous|reviewed|guarded)", re.MULTILINE)


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
CSS/API specifics (ADR 0018). If the change touches a user-facing surface, you MUST attach
before/after screenshots (headless Playwright) as proof it matches the locked mockup — the
cross-review blocks a UI PR that has none. Both are charter gates, not style points.

Keep the change minimal and match the surrounding code. If you hit a blocker you
cannot safely resolve, post a comment prefixed `MISSING-CONTEXT:` and stop instead
of guessing."""

REVISE_PROMPT = """Address the blocking review findings on PR #{n} in this worktree,
push to the same branch, and keep the test suite green. Do NOT open a new PR.

Blocking findings:
{findings}"""


def _issues_in_flight(cfg: RepoConfig) -> set[int]:
    """Issues that already have an OPEN agentflow PR — an agent is on them, so don't
    re-dispatch a duplicate. Dispatch dedup, distinct from ADR 0009's merge-time floor:
    an issue stays `ready-for-agent` while its PR is in review, so without this the loop
    would re-build it every cycle (a second PR on a different tool)."""
    r = _run(["gh", "pr", "list", "--repo", cfg.repo, "--state", "open",
              "--json", "headRefName", "--limit", "100"])
    if r.returncode != 0:
        return set()
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
    builder.launch(REVISE_PROMPT.format(n=pr, findings=findings),
                   cwd=_builder_worktree(cfg, builder.tool, n, sl),
                   model=builder.model_for(complexity))


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
    if not _free_to_dispatch(issue, _issues_in_flight(cfg)):
        return f"#{n}: already claimed or in flight — a build already owns it"
    return _dispatch_build(cfg, issue)


def _dispatch_build(cfg: RepoConfig, issue: dict) -> str:
    """Build one already-selected ready issue end to end: gate on the complexity label,
    claim it, then build → cross-review → merge/park under the claim. Shared by the daemon's
    next-ready pull (`run_once`) and the by-hand `build <N>` (`build_issue`) so there is one
    builder path, not two. Every profile builds from the Agent Brief in the issue body (ADR
    0022) — there is no separate work-order comment."""
    n = issue["number"]
    labels = [lbl["name"] for lbl in issue["labels"]]
    complexity = complexity_from_labels(labels)
    if complexity is None:
        return f"#{n}: skipped — no agentflow:complexity:* label (ADR 0018 hard gate)"
    effort = effort_from_labels(labels)

    builder, reviewer_runner = pick_pair()   # ADR 0006: more headroom builds; other reviews
    if builder is None:
        return f"#{n}: no pool has headroom right now — deferring"
    profile = repo_profile(cfg.workdir)
    sl = slug(issue["title"])
    build_prompt = BUILD_PROMPT.format(repo=cfg.repo, n=n, title=issue["title"],
                                       body=issue.get("body") or "", effort=effort.value)
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
        # nothing is lost, then hand back to a human (ADR 0020).
        draft = _preserve_progress(cfg, builder.tool, n, sl)
        where = f"draft PR {draft}" if draft else "the build session's comment"
        notify("agentflow needs you", f"{cfg.repo} #{n}: build {outcome.status.value} — {where}",
               draft or f"https://github.com/{cfg.repo}/issues/{n}")
        return f"#{n}: build {outcome.status.value} — {outcome.detail}; progress in {where}"

    pr = pr_number(outcome.pr_url)
    head_branch = f"agentflow/{builder.tool}/issue-{n}-{sl}"
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
            verdict = reviewer.review(cfg.repo, cfg.workdir, pr, head_branch, sl, complexity, acceptance=acceptance)
            if verdict.clean or not (verdict.parsed and verdict.blocking) or revises_used >= MAX_REVISES:
                park(cfg.repo, pr, verdict, reason=f"is a `{profile}` repo — a human merges")
                notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} reviewed ({profile}) — your merge",
                       _pr_url(cfg.repo, pr))
                return f"#{n}: PR #{pr} reviewed ({profile}) — awaiting human merge"
            _launch_revise(builder, cfg, pr, n, sl, complexity, verdict)
            revises_used += 1

    revises_used = 0
    while True:
        verdict = reviewer.review(cfg.repo, cfg.workdir, pr, head_branch, sl, complexity, acceptance=acceptance)
        decision = decide_merge(verdict=verdict, ci_green=ci_is_green(cfg.repo, pr),
                                reviewer_tool=reviewer_runner.tool, builder_tool=builder.tool,
                                revises_used=revises_used)
        if decision is MergeDecision.MERGE:
            ok = squash_merge(cfg.repo, pr)
            if ok:
                ratchet.record(cfg.repo, ratchet.CLEAN_MERGE if revises_used == 0
                               else "merge_after_revise")
            return f"#{n}: MERGED PR #{pr}" if ok else f"#{n}: merge failed on PR #{pr}"
        if decision is MergeDecision.PARK:
            park(cfg.repo, pr, verdict)
            ratchet.record(cfg.repo, "parked")
            notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} parked after review",
                   _pr_url(cfg.repo, pr))
            return f"#{n}: parked PR #{pr} for human review"
        _launch_revise(builder, cfg, pr, n, sl, complexity, verdict)
        revises_used += 1


def _next_resumable_issue(cfg: RepoConfig) -> tuple[dict, str] | None:
    """A `needs-grilling` issue whose latest comment is the maintainer's reply — return
    it with their answer text so intake can resolve it (ADR 0019). `needs-mockup` resumes
    via `/agentflow mockup`, not an unattended re-triage."""
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", "agentflow:needs-grilling", "--json", "number,title,body,labels",
              "--limit", "50"])
    if r.returncode != 0:
        return None
    for issue in sorted(json.loads(r.stdout or "[]"), key=lambda i: i["number"]):
        if TRIAGING in {lbl["name"] for lbl in issue["labels"]}:
            continue   # a re-intake already owns this held issue
        cr = _run(["gh", "issue", "view", str(issue["number"]), "--repo", cfg.repo, "--json", "comments"])
        if cr.returncode != 0:
            continue
        comments = json.loads(cr.stdout or "{}").get("comments", [])
        if awaiting_recheck(comments):
            return issue, replies_since_intake(comments)
    return None


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
        current_labels = [lbl["name"] for lbl in issue.get("labels", [])]
        summary = apply_intake(cfg.repo, n, issue.get("title", ""), current_labels, result)
    finally:
        _release_triage(cfg.repo, n)   # the state label dedups from here; drop the claim
    if result.route in (IntakeRoute.GRILL, IntakeRoute.MOCKUP):
        notify("agentflow needs you", f"{cfg.repo} #{n}: {result.route.value}",
               f"https://github.com/{cfg.repo}/issues/{n}")
    return f"#{n}: {summary}{' (resumed)' if extra else ''}"


def pipeline_once(cfg: RepoConfig) -> str:
    """One full pass for a repo: triage one un-triaged issue, then build one ready
    issue (ADR 0016 — intake runs ahead of the build queue)."""
    return f"intake: {intake_once(cfg)} · build: {run_once(cfg)}"


if __name__ == "__main__":  # entrypoint — the sandbox dogfood target
    cfg = RepoConfig(repo="ConnorGriffin/agentflow-sandbox",
                     workdir=str(Path.home() / "Code" / "ConnorGriffin" / "agentflow-sandbox"))
    print(pipeline_once(cfg))
