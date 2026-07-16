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
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agentflow import live, ratchet
from agentflow.balancer import pick_pair
from agentflow.gate import (MAX_REVISES, MergeDecision, ci_is_green, decide_merge,
                            maintainer_comment, park, reply_pending, squash_merge,
                            ui_evidence_gap)
from agentflow.intake import (INTAKE_MARK, Intake, IntakeResult, IntakeRoute, STATE_LABELS,
                              _DISCLAIMER, _strip_quoted_lines,
                              apply_intake, awaiting_recheck, intake_result_is_durable,
                              replies_since_intake)
from agentflow.notify import notify
from agentflow.reviewer import Reviewer, Verdict, review_worktree
from agentflow.runner import (BuildStatus, BuildTask, Complexity, Effort, _run,
                              _worktree_is_disposable, _worktree_is_registered,
                              recover_stale_worktrees,
                              remove_worktree_if_safe, worktree_session)


def _pr_url(repo: str, pr: int) -> str:
    return f"https://github.com/{repo}/pull/{pr}"


def _pr_comments(repo: str, pr: int) -> list[dict] | None:
    """The PR's comments, or None when they cannot be verified."""
    r = _run(["gh", "pr", "view", str(pr), "--repo", repo, "--json", "comments"])
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout or "{}").get("comments", [])
    except json.JSONDecodeError:
        return None


def _issue_comments(repo: str, n: int) -> list[dict]:
    """The issue's comments, or [] if they can't be read. Impure."""
    r = _run(["gh", "issue", "view", str(n), "--repo", repo, "--json", "comments"])
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
_ALLOWLIST_RE = re.compile(r"^intake-allowlist:\s*(.+)", re.MULTILINE)

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


def intake_allowlist(repo: str, workdir: str) -> set[str]:
    """Authors whose comments can trigger a resume or re-intake. Always includes the repo
    owner; extend via an `intake-allowlist: alice, bob` line in AGENTS.md/CLAUDE.md."""
    owner = repo.split("/")[0]
    for name in ("AGENTS.md", "CLAUDE.md"):
        p = Path(workdir) / name
        if p.exists():
            m = _ALLOWLIST_RE.search(p.read_text(errors="replace"))
            if m:
                extra = {s.strip() for s in m.group(1).split(",") if s.strip()}
                return {owner} | extra
    return {owner}


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
_BLOCKED_BY_RE = re.compile(r"^Blocked by #(\d+)\s*$", re.MULTILINE)


def _free_to_dispatch(cfg: RepoConfig, issue: dict, in_flight: set[int], _log=None) -> bool:
    """A ready issue is free only if no agent owns it and every declared blocker is closed.

    Blocker state is read from the same repository on every selection pass. Unknown is not
    closed: a missing issue, `gh` failure, or malformed response skips the dependent until a
    later pass can verify it safely. Shared by queue selection and by-hand builds."""
    if (issue["number"] in in_flight
            or BUILDING in {lbl["name"] for lbl in issue.get("labels", [])}):
        return False

    blockers = dict.fromkeys(int(n) for n in _BLOCKED_BY_RE.findall(issue.get("body") or ""))
    for blocker in blockers:
        r = _run(["gh", "issue", "view", str(blocker), "--repo", cfg.repo,
                  "--json", "state"])
        try:
            data = json.loads(r.stdout or "{}") if r.returncode == 0 else {}
            state = data.get("state") if isinstance(data, dict) else None
        except json.JSONDecodeError:
            state = None
        if state != "CLOSED":
            if _log:
                reason = f"open blocker #{blocker}" if state == "OPEN" \
                    else f"blocker #{blocker} whose state could not be determined"
                _log(f"{cfg.repo}: #{issue['number']}: skipped — {reason}")
            return False
    return True


def _next_ready_issue(cfg: RepoConfig, _log=None) -> dict | None:
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", "ready-for-agent", "--json", "number,title,body,labels",
              "--limit", "50"])
    if r.returncode != 0:
        return None
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return None   # can't see what's in flight — fail closed, dispatch next cycle
    issues = sorted(json.loads(r.stdout or "[]"), key=lambda i: i["number"])
    return next((i for i in issues if _free_to_dispatch(cfg, i, in_flight, _log)), None)


def _issues_with_live_session(repo: str) -> set[int]:
    """Issue numbers with a session running *right now* on this repo (the live board). With
    concurrent dispatch (ADR 0023) a build is live at the top of a cycle before it has opened
    its PR — so "nothing is live at cycle start" no longer holds, and reclaim must key on real
    session evidence, not just the open-PR check (issue #74)."""
    return {s["number"] for s in live.running()
            if s.get("repo") == repo and isinstance(s.get("number"), int)}


def reclaim_claims(cfg: RepoConfig, coordinator_owned: set[int] = frozenset()) -> int:
    """Drop `agentflow:building` claims orphaned by a crash. A claim is stale only if no
    session is building it right now (the live board) AND it has no open agentflow PR — under
    concurrent dispatch a live build holds its claim before its PR exists, so we must not strip
    it. A stale claim is fail-safe (the issue is skipped, never duplicated) but blocks that
    issue until cleared. Returns how many it cleared.

    `coordinator_owned` are the issues a session-coordinator record still owns (ADR 0028): a
    claim can legitimately outlive its provider process now, so reclamation must never strip one
    — that would clear coordinator ownership and let a duplicate build in (issue #103)."""
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", BUILDING, "--json", "number", "--limit", "100"])
    if r.returncode != 0:
        return 0
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return 0   # can't see what's in flight — a live claim must never be stripped
    live_now = _issues_with_live_session(cfg.repo)
    stale = [i["number"] for i in json.loads(r.stdout or "[]")
             if i["number"] not in in_flight and i["number"] not in live_now
             and i["number"] not in coordinator_owned]
    for n in stale:
        _release(cfg.repo, n)
    return len(stale)


TRIAGING = "agentflow:triaging"   # dispatch claim — a grounding session owns this issue

# Out of the intake queue: a resolved state label, or a live triaging claim.
_TRIAGE_SKIP = set(STATE_LABELS) | {TRIAGING}


def reclaim_triage_claims(cfg: RepoConfig, coordinator_owned: set[int] = frozenset()) -> int:
    """Drop `agentflow:triaging` claims stranded when a daemon is killed mid-grounding (issue
    #74). Intake opens no PR, so its only outcome signal is a state label — a claim is stale
    only when intake has stamped none of them AND no session is grounding it right now (the
    live board), symmetric to the build path keying on the open-PR check plus live sessions.
    If the listing fails, reclaim nothing — a live claim must never be stripped. Returns how
    many it cleared."""
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", TRIAGING, "--json", "number,labels", "--limit", "100"])
    if r.returncode != 0:
        return 0
    live_now = _issues_with_live_session(cfg.repo)
    stale = [i["number"] for i in json.loads(r.stdout or "[]")
             if i["number"] not in live_now
             and i["number"] not in coordinator_owned
             and not ({lbl["name"] for lbl in i.get("labels", [])} & set(STATE_LABELS))]
    for n in stale:
        _release_triage(cfg.repo, n)
    return len(stale)


def _untriaged(issue: dict) -> bool:
    """An issue is in the intake queue only if it is not a wayfinder planning artifact and
    nothing has resolved or claimed it — none of intake's state labels and no
    `agentflow:triaging` claim (set before the grounding session, closing intake's
    no-label-yet window, symmetric to `_free_to_dispatch`). Pure (test surface)."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if any(label.startswith("wayfinder:") for label in labels):
        return False
    return not (labels & _TRIAGE_SKIP)


def _next_untriaged_issue(cfg: RepoConfig, reserved: set[int] = frozenset()) -> dict | None:
    """The oldest open issue in the intake queue — excluding wayfinder planning artifacts,
    with none of intake's state labels and unclaimed by a live grounding session (ADR 0016).
    `reserved` skips issues a concurrent triage fan-out already claimed this cycle, before
    their `agentflow:triaging` label is visible."""
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--json", "number,title,body,labels", "--limit", "50"])
    if r.returncode != 0:
        return None
    untriaged = [i for i in json.loads(r.stdout or "[]")
                 if _untriaged(i) and i["number"] not in reserved]
    return min(untriaged, key=lambda i: i["number"]) if untriaged else None


def _builder_worktree(cfg: RepoConfig, tool: str, n: int, sl: str) -> str:
    return str(Path(cfg.workdir) / ".agentflow" / "worktrees" / tool / f"issue-{n}-{sl}")


def _finish_review(cfg: RepoConfig, reviewer_tool: str, pr: int, sl: str,
                   merged: bool = False) -> None:
    comments = _pr_comments(cfg.repo, pr)
    durable = merged or (comments is not None and any(
        "agentflow: parked for human review" in c.get("body", "") for c in comments))
    if durable:
        remove_worktree_if_safe(
            cfg.workdir, review_worktree(cfg.workdir, reviewer_tool, pr, sl))


def _launch_revise(builder, cfg: RepoConfig, pr: int, n: int, sl: str,
                   complexity: Complexity, verdict: Verdict, title: str = "") -> None:
    """One builder pass addressing the blocking findings on the PR branch (ADR 0020)."""
    findings = "\n".join(f"- {f.summary}" for f in verdict.blocking) or "- (see review)"
    surfaces = _surfaces_phrase(ui_surfaces(cfg.workdir))
    branch = f"agentflow/{builder.tool}/issue-{n}-{sl}"
    wt = Path(_builder_worktree(cfg, builder.tool, n, sl))
    if not _checkout_pr_branch(cfg, branch, wt):
        return
    builder.provision(wt)
    model = builder.model_for(complexity)
    session = live.Session(repo=cfg.repo, number=n, title=title, stage="building",
                           tool=builder.tool, model=model, branch=branch)
    with worktree_session(wt, session):
        ok, _ = builder.launch(REVISE_PROMPT.format(n=pr, findings=findings, surfaces=surfaces),
                               cwd=str(wt), model=model)
    if ok:
        remove_worktree_if_safe(cfg.workdir, wt)


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


def run_once(cfg: RepoConfig, _log=None, slot=None) -> str:
    """Pull the next ready issue and run it end to end. Returns a one-line result. `slot`
    (the dispatch governor's handle) bounds build concurrency — a build defers when the
    machine is at capacity or an active pool's pace is spent."""
    issue = _next_ready_issue(cfg, _log=_log)
    if not issue:
        return "no ready-for-agent issues"
    return _dispatch_build(cfg, issue, _log=_log, slot=slot)


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
    if not _free_to_dispatch(cfg, issue, in_flight):
        return f"#{n}: not dispatchable — already claimed, in flight, or waiting on a blocker"
    return _dispatch_build(cfg, issue, operator=True)


def _dispatch_build(cfg: RepoConfig, issue: dict, operator: bool = False,
                    _log=None, slot=None) -> str:
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

    builder, reviewer_runner, block_msg = pick_pair(operator=operator)   # ADR 0006: more headroom builds; other reviews
    if builder is None:
        return f"#{n}: no pool has headroom ({block_msg}) — deferring"
    if slot is not None and not slot.admit("build", builder.tool):
        return f"#{n}: build deferred — machine at session capacity"
    if _log:
        _log(f"{cfg.repo}: #{n}: routing → {getattr(builder, 'tool', '?')} (build)")
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
        if slot is not None:
            slot.release("build")


def _claim(repo: str, n: int) -> bool:
    """Mark issue n as owned by an agent *before* its build runs, so a concurrent or
    next-cycle dispatch skips it (closes the no-PR-yet window). Ensures the label first and
    reports whether ownership was actually made visible; coordinated Build refuses submission
    when it cannot establish this guard."""
    created = _run(["gh", "label", "create", BUILDING, "--repo", repo, "--color", "fbca04",
                    "--description", "An agent is building this issue", "--force"])
    if created.returncode != 0:
        return False
    claimed = _run(["gh", "issue", "edit", str(n), "--repo", repo,
                    "--add-label", BUILDING])
    return claimed.returncode == 0


def _release(repo: str, n: int) -> None:
    """Drop the build claim when the build is done, whatever the outcome. A parked PR
    stays skipped via the open-PR check; a failed build is free to retry next cycle."""
    _run(["gh", "issue", "edit", str(n), "--repo", repo, "--remove-label", BUILDING])


def _claim_triage(repo: str, n: int) -> bool:
    """Claim issue n for intake *before* its grounding session, so a concurrent or next-cycle
    dispatch skips it — closing intake's no-label-yet window (the state label is only stamped
    once the session finishes). Symmetric to `_claim`; ensures the label first."""
    created = _run(["gh", "label", "create", TRIAGING, "--repo", repo, "--color", "d4c5f9",
                    "--description", "A grounding session is triaging this issue", "--force"])
    claimed = _run(["gh", "issue", "edit", str(n), "--repo", repo, "--add-label", TRIAGING])
    return created.returncode == 0 and claimed.returncode == 0


def _release_triage(repo: str, n: int) -> bool:
    """Drop the intake claim once routing is written (the state label dedups from here) or the
    session ended. A crash *before* this strands the claim: fail-safe (the issue is skipped,
    never double-triaged), auto-reclaimed next cycle by `reclaim_triage_claims` — the missing
    state label is intake's stale signal, standing in for the open-PR check builds have.
    Returns durable proof that GitHub no longer carries the claim."""
    removed = _run(["gh", "issue", "edit", str(n), "--repo", repo,
                    "--remove-label", TRIAGING])
    if removed.returncode != 0:
        return False
    viewed = _run(["gh", "issue", "view", str(n), "--repo", repo, "--json", "labels"])
    if viewed.returncode != 0:
        return False
    try:
        labels = json.loads(viewed.stdout or "{}").get("labels", [])
    except ValueError:
        return False
    return TRIAGING not in {label.get("name") for label in labels if isinstance(label, dict)}


def _build_review_merge(cfg: RepoConfig, issue: dict, n: int, sl: str, complexity: Complexity,
                        effort: Effort, builder, reviewer_runner, profile: str,
                        build_prompt: str) -> str:
    """Build the issue, then cross-review -> merge/park. Runs under run_once's
    `agentflow:building` claim (dispatch dedup)."""
    task = BuildTask(cfg.repo, cfg.workdir, n, sl, complexity, effort, prompt=build_prompt,
                     title=issue.get("title", ""))
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
        remove_worktree_if_safe(cfg.workdir, Path(_builder_worktree(cfg, builder.tool, n, sl)))
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
    title = issue.get("title", "")

    if profile != "autonomous":
        # reviewed / guarded: revise until the review is clean (or we bail), then a
        # HUMAN merges (ADR 0002, 0020) — hand over a clean PR when we can.
        revises_used = 0
        while True:
            verdict = reviewer.review(cfg.repo, cfg.workdir, pr, head_branch, sl,
                                      acceptance=acceptance, surfaces=surfaces_phrase,
                                      issue_number=n, title=title)
            if verdict.clean or not (verdict.parsed and verdict.blocking) or revises_used >= MAX_REVISES:
                # A human merges either way, but the mechanical UI-evidence gate still runs
                # (ADR 0018): a screenshot-less UI change must park with the missing-screenshot
                # reason, so the human isn't handed one that silently fails the charter gate.
                ui_gap = ui_evidence_gap(cfg.repo, pr, surfaces)
                reason = _UI_GAP_REASON if ui_gap else f"is a `{profile}` repo — a human merges"
                park(cfg.repo, pr, verdict, reason=reason)
                _finish_review(cfg, reviewer_runner.tool, pr, sl)
                notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} reviewed ({profile}) — your merge",
                       _pr_url(cfg.repo, pr))
                return f"#{n}: PR #{pr} reviewed ({profile}) — awaiting human merge"
            _launch_revise(builder, cfg, pr, n, sl, complexity, verdict, title=title)
            revises_used += 1

    revises_used = 0
    while True:
        verdict = reviewer.review(cfg.repo, cfg.workdir, pr, head_branch, sl,
                                  acceptance=acceptance, surfaces=surfaces_phrase,
                                  issue_number=n, title=title)
        # The UI-evidence gate is read from the diff + attachments, AFTER the review, so a
        # reviewer's "not blocking" cannot clear a screenshot-less UI change (ADR 0018).
        ui_gap = ui_evidence_gap(cfg.repo, pr, surfaces)
        decision = decide_merge(verdict=verdict, ci_green=ci_is_green(cfg.repo, pr),
                                reviewer_tool=reviewer_runner.tool, builder_tool=builder.tool,
                                revises_used=revises_used, ui_evidence_missing=ui_gap,
                                reply_pending=reply_pending(_pr_comments(cfg.repo, pr) or []))
        if decision is MergeDecision.MERGE:
            ok = squash_merge(cfg.repo, pr)
            if ok:
                _finish_review(cfg, reviewer_runner.tool, pr, sl, merged=True)
                ratchet.record(cfg.repo, ratchet.CLEAN_MERGE if revises_used == 0
                               else "merge_after_revise")
                _run(["gh", "issue", "edit", str(n), "--repo", cfg.repo,
                      "--remove-label", "ready-for-agent"])
                return f"#{n}: MERGED PR #{pr}"
            park(cfg.repo, pr, verdict,
                 reason="could not be squash-merged (branch protection, conflict, or transient error)")
            _finish_review(cfg, reviewer_runner.tool, pr, sl)
            ratchet.record(cfg.repo, "parked")
            notify("agentflow needs you",
                   f"{cfg.repo} #{n}: PR #{pr} merge failed — your action needed",
                   _pr_url(cfg.repo, pr))
            return f"#{n}: merge failed on PR #{pr}"
        if decision is MergeDecision.PARK:
            reason = _UI_GAP_REASON if ui_gap else "could not be auto-merged after review"
            park(cfg.repo, pr, verdict, reason=reason)
            _finish_review(cfg, reviewer_runner.tool, pr, sl)
            ratchet.record(cfg.repo, "parked")
            notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} parked after review",
                   _pr_url(cfg.repo, pr))
            return f"#{n}: parked PR #{pr} for human review"
        _launch_revise(builder, cfg, pr, n, sl, complexity, verdict, title=title)
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
        allowlist = intake_allowlist(cfg.repo, cfg.workdir)
        if awaiting_recheck(comments, allowlist):
            qualifying = [c for c in comments
                          if c.get("author", {}).get("login", "") in allowlist
                          and INTAKE_MARK not in _strip_quoted_lines(c.get("body", ""))]
            if qualifying:
                latest = qualifying[-1]
                issue["_intake_target"] = str(latest.get("id") or latest.get("createdAt") or "")
            return issue, replies_since_intake(comments, allowlist)
    return None


def _handle_intake_infra_failure(cfg: RepoConfig, n: int, result: IntakeResult) -> str:
    """A legacy Intake setup failure stays quiet and free until coordinated mode owns it.

    The durable coordinator is the sole retry/hold budget. The temporary rollback path must
    not keep a second in-memory streak whose count resets on restart.
    """
    return f"#{n}: intake couldn't start ({result.detail}) — retrying silently"


def _next_intake_candidate(cfg: RepoConfig,
                           reserved: set[int] = frozenset()) -> tuple[dict, str] | None:
    """The next issue to triage — a held issue the maintainer just answered (resume, ADR
    0019) or the oldest un-triaged one (ADR 0016) — with its resume text. Skips issues a
    concurrent fan-out already claimed this cycle (`reserved`). None when the queue is empty."""
    resumable = _next_resumable_issue(cfg)
    if resumable and resumable[0]["number"] not in reserved:
        return resumable
    issue = _next_untriaged_issue(cfg, reserved)
    return (issue, "") if issue else None


def intake_once(cfg: RepoConfig, _log=None, slot=None) -> str:
    """Triage the next issue: a held issue the maintainer just answered (resume, ADR
    0019) or the oldest un-triaged one (ADR 0016). Ground, route, write to GitHub.

    `slot` (the dispatch governor's admission handle) bounds triage concurrency: when the
    machine is at capacity or an active pool's pace is spent, the triage defers to a later
    cycle instead of starting."""
    picked = _next_intake_candidate(cfg)
    if not picked:
        return "no un-triaged issues"
    issue, extra = picked
    n = issue["number"]
    builder, _, block_msg = pick_pair()   # intake needs one available tool, not a pair
    if builder is None:
        return f"#{n}: no pool has headroom for intake ({block_msg}) — deferring"
    if slot is not None and not slot.admit("triage", builder.tool):
        return f"#{n}: intake deferred — machine at session capacity"
    if _log:
        _log(f"{cfg.repo}: #{n}: routing → {getattr(builder, 'tool', '?')} (intake)")
    _claim_triage(cfg.repo, n)   # own the issue before the long session (dispatch dedup)
    try:
        return _run_intake_session(cfg, issue, extra, builder)
    finally:
        if slot is not None:
            slot.release("triage")


def _run_intake_session(cfg: RepoConfig, issue: dict, extra: str, builder) -> str:
    """Run one grounding session on an already-claimed issue: ground, route, write to GitHub,
    drop the claim. Shared by `intake_once` and the daemon's concurrent triage fan-out."""
    n = issue["number"]
    try:
        result = Intake(builder).intake(cfg.repo, cfg.workdir, issue, extra=extra)
        if result.infra_failed:
            return _handle_intake_infra_failure(cfg, n, result)
        current_labels = [lbl["name"] for lbl in issue.get("labels", [])]
        summary = apply_intake(cfg.repo, n, issue.get("title", ""), current_labels, result)
        tool = getattr(builder, "tool", None)
        if tool and intake_result_is_durable(cfg.repo, n, result):
            wt = Path(cfg.workdir) / ".agentflow" / "worktrees" / f"{tool}-intake" / f"issue-{n}"
            remove_worktree_if_safe(cfg.workdir, wt)
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
        if not _worktree_is_registered(cfg.workdir, wt):
            return False
        if not _worktree_is_disposable(cfg.workdir, wt):
            return False
        return _run(["git", "-C", str(wt), "reset", "--hard", f"origin/{branch}"]).returncode == 0
    wt.parent.mkdir(parents=True, exist_ok=True)
    return _run(["git", "-C", cfg.workdir, "worktree", "add", "-B", branch,
                 str(wt), f"origin/{branch}"]).returncode == 0


def respond_once(cfg: RepoConfig, _log=None, slot=None) -> str:
    """Answer the next parked PR whose latest comment is an unanswered maintainer question:
    spawn a responder in the PR-branch worktree that replies in-thread, attaches requested
    evidence, and pushes small fixes to the same branch (issue #18). Never merges, never a
    new PR — same contract as a revise. `slot` bounds responder concurrency."""
    pending = _next_pr_awaiting_reply(cfg)
    if not pending:
        return "no parked PRs awaiting reply"
    pr, branch, comment = pending
    m = _BRANCH_RE.match(branch)
    if not m:
        return f"PR #{pr}: unrecognized branch {branch}"
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    builder, _, block_msg = pick_pair()   # a reply needs one available tool, not a pair
    if builder is None:
        return f"PR #{pr}: no pool has headroom to respond ({block_msg}) — deferring"
    if slot is not None and not slot.admit("respond", builder.tool):
        return f"PR #{pr}: respond deferred — machine at session capacity"
    if _log:
        _log(f"{cfg.repo}: PR #{pr}: routing → {getattr(builder, 'tool', '?')} (respond)")
    try:
        wt = Path(_builder_worktree(cfg, tool, n, sl))
        if not _checkout_pr_branch(cfg, branch, wt):
            return f"PR #{pr}: could not check out {branch} to respond — retry next cycle"
        builder.provision(wt)
        with worktree_session(wt):
            ok, _ = builder.launch(
                RESPOND_PROMPT.format(n=pr, comment=comment, disclaimer=_RESPOND_DISCLAIMER),
                cwd=str(wt), model=builder.model_for(Complexity.DEEP))
        comments = _pr_comments(cfg.repo, pr)
        replied = ok and comments is not None and not reply_pending(comments)
        if replied:
            remove_worktree_if_safe(cfg.workdir, wt)
        if replied:
            return f"PR #{pr}: replied to the maintainer"
        if ok:
            return f"PR #{pr}: responder exited without a confirmed reply — retaining its worktree"
        return f"PR #{pr}: responder session errored"
    finally:
        if slot is not None:
            slot.release("respond")


# --- mockup phase: draw variants on a parked needs-mockup issue (issue #29) ------------
# The daemon draws a parked UI issue so it advances on its own: 3-4 wildly-different variants,
# screenshots posted in ONE issue comment, and the maintainer picks (or tweaks) in one reply.
# Nobody enters an agent session. The pick resumes through intake (ADR 0019), which locks the
# chosen variant and promotes to ready.

DRAWING = "agentflow:drawing-mockup"   # dispatch claim — a session is drawing this issue's variants

# The produced-variants comment carries INTAKE_MARK ("agentflow intake") so `awaiting_recheck`
# reads it as OURS — never as a maintainer reply, which would self-trigger the resume path (the
# TRAP in the brief). MOCKUP_MARK additionally distinguishes it from intake's park/kickoff
# comment, so the produce phase draws exactly one round and never re-draws.
MOCKUP_MARK = "mockup variants"
_MOCKUP_DISCLAIMER = "> *agentflow intake: mockup variants — generated by AI.*"

PRODUCE_PROMPT = """You are agentflow's mockup phase for {repo} issue #{n}: {title}

This UI issue is parked waiting for a locked visual design. Your job is to DRAW it — produce
3-4 wildly-different design variants, screenshot them, and post ONE comment on the issue so the
maintainer can pick in a single reply. You do NOT choose the winner and you do NOT implement the
feature — you give the maintainer something concrete to react to.

The issue as filed / scoped:
---
{body}
---

Run the `/ui-mockups` skill for this repo's user-facing surface(s): {surfaces}. Follow it
headless — ground every variant in the shipping app's own theme, library, and data; make the
variants diverge at the CONCEPT level (layout metaphor, interaction model, hierarchy), not just
colors; and screenshot each one. 3-4 variants is the sweet spot.

You are on branch `{branch}` in this worktree. PRESERVE the work so it is not lost with the
worktree: commit every variant's HTML (and any forked render/screenshot files) AND the
screenshots to this branch, then `git push -u origin {branch}`.

Then post EXACTLY ONE comment on the ISSUE (`gh issue comment {n}`), and nothing else — do not
edit the issue title, body, or labels; do not open a PR. The comment MUST:
- START with this exact line, verbatim (it marks the comment as ours so the daemon never mistakes
  it for the maintainer's reply):
  {disclaimer}
- Embed each variant's screenshot as a markdown image from its committed path on this branch,
  using the `github.com/.../raw/refs/heads/` host (NOT `raw.githubusercontent.com`, which 404s on
  private repos because it ignores the viewer's login):
  `![Variant A](https://github.com/{repo}/raw/refs/heads/{branch}/mockups/<file>.png)`.
- Name the variants A, B, C (and D), each with a ONE-LINE description of its concept.
- End with a clear ask: reply on this issue with a pick ("B", "the second one", "A but with C's
  header") or an adjustment, and agentflow will lock the chosen design and start the build.

One variant round is enough to unblock — do not loop. If you genuinely cannot produce the
variants (no runnable surface, missing grounding), post one comment starting with {disclaimer}
and prefixed `MISSING-CONTEXT:`, then stop."""


def _claim_mockup(repo: str, n: int) -> None:
    """Claim issue n for the produce phase *before* its long drawing session, so a concurrent or
    next-cycle pass skips it (no double-draw). Symmetric to `_claim_triage`; ensures the label
    first. A crash before release strands the claim — fail-safe (skipped, never double-drawn),
    cleared by hand, since the session opens no PR to check liveness against."""
    _run(["gh", "label", "create", DRAWING, "--repo", repo, "--color", "fef2c0",
          "--description", "A session is drawing mockup variants for this issue", "--force"])
    _run(["gh", "issue", "edit", str(n), "--repo", repo, "--add-label", DRAWING])


def _release_mockup(repo: str, n: int) -> None:
    """Drop the drawing claim once the variant round is posted (the mockup comment dedups from
    here via MOCKUP_MARK) or the session ended."""
    _run(["gh", "issue", "edit", str(n), "--repo", repo, "--remove-label", DRAWING])


def _has_mockup_variants(comments: list[dict]) -> bool:
    """Pure. True once we've posted the variant round on this issue (a comment carrying
    MOCKUP_MARK) — so the produce phase draws exactly one round, never a re-draw. Distinct from
    intake's park/kickoff comment, which carries INTAKE_MARK but not this marker."""
    return any(MOCKUP_MARK in c.get("body", "") for c in comments)


def _mockup_eligible(issue: dict, comments: list[dict], allowlist: set[str] | None) -> bool:
    """Pure (test surface). A `needs-mockup` issue is ready to draw when no variant round has
    been posted yet, no maintainer reply is pending (that belongs to the resume path, ADR 0019),
    and no drawing claim is live (dispatch dedup — no double-draw across cycles)."""
    if DRAWING in {lbl["name"] for lbl in issue.get("labels", [])}:
        return False
    if _has_mockup_variants(comments):
        return False
    return not awaiting_recheck(comments, allowlist)


def _next_mockup_issue(cfg: RepoConfig) -> dict | None:
    """The oldest open `needs-mockup` issue ready for a variant round — none drawn yet, no
    pending maintainer reply, unclaimed. None on a `gh` blip (fail closed — try next cycle)."""
    r = _run(["gh", "issue", "list", "--repo", cfg.repo, "--state", "open",
              "--label", "agentflow:needs-mockup", "--json", "number,title,body,labels",
              "--limit", "50"])
    if r.returncode != 0:
        return None
    allowlist = intake_allowlist(cfg.repo, cfg.workdir)
    for issue in sorted(json.loads(r.stdout or "[]"), key=lambda i: i["number"]):
        cr = _run(["gh", "issue", "view", str(issue["number"]), "--repo", cfg.repo, "--json", "comments"])
        if cr.returncode != 0:
            continue
        comments = json.loads(cr.stdout or "{}").get("comments", [])
        if _mockup_eligible(issue, comments, allowlist):
            return issue
    return None


def produce_once(cfg: RepoConfig, _log=None, slot=None) -> str:
    """Draw the next parked UI issue's variant round so it advances without a human session
    (issue #29). Pick the oldest `needs-mockup` issue with nothing drawn and no pending reply,
    claim it, and spawn a session that runs `/ui-mockups` headless for the repo's surfaces:
    3-4 wildly-different variants, screenshots posted in ONE issue comment carrying the intake
    marker (so it never reads as a maintainer reply), variant HTML committed to a branch. The
    maintainer's pick reply resumes through intake, which locks the choice and promotes to ready.
    Returns a one-line result. Never opens a PR, never chooses the winner."""
    issue = _next_mockup_issue(cfg)
    if not issue:
        return "no needs-mockup issues to draw"
    n = issue["number"]
    builder, _, block_msg = pick_pair()   # drawing needs one available tool, not a pair
    if builder is None:
        return f"#{n}: no pool has headroom to draw mockups ({block_msg}) — deferring"
    if slot is not None and not slot.admit("mockup", builder.tool):
        return f"#{n}: mockup deferred — machine at session capacity"
    if _log:
        _log(f"{cfg.repo}: #{n}: routing → {getattr(builder, 'tool', '?')} (mockup)")
    sl = slug(issue["title"])
    branch = f"agentflow/{builder.tool}/mockup-{n}-{sl}"
    wt = Path(cfg.workdir) / ".agentflow" / "worktrees" / builder.tool / f"mockup-{n}-{sl}"
    surfaces = ui_surfaces(cfg.workdir)
    prompt = PRODUCE_PROMPT.format(repo=cfg.repo, n=n, title=issue["title"],
                                   body=issue.get("body") or "", branch=branch,
                                   surfaces=_surfaces_phrase(surfaces), disclaimer=_MOCKUP_DISCLAIMER)
    _claim_mockup(cfg.repo, n)   # own the issue before the long session — no double-draw
    try:
        try:
            builder.prepare_worktree(cfg.workdir, branch, wt, cfg.repo)
            builder.provision(wt)
        except subprocess.CalledProcessError as e:
            return f"#{n}: mockup worktree/provision failed ({e})"
        model = builder.model_for(Complexity.DEEP)
        session = live.Session(repo=cfg.repo, number=n, title=issue["title"], stage="triaging",
                               tool=builder.tool, model=model, branch=branch)
        with worktree_session(wt, session):
            ok, _ = builder.launch(prompt, cwd=str(wt), model=model)
        if not ok:
            return f"#{n}: mockup session errored"
        # Confirm what was actually posted — a successful exit alone doesn't prove a comment landed.
        issue_url = f"https://github.com/{cfg.repo}/issues/{n}"
        comments = _issue_comments(cfg.repo, n)
        posted = next((c for c in comments if MOCKUP_MARK in c.get("body", "")), None)
        if posted is None:
            return f"#{n}: drew mockup variants"
        remove_worktree_if_safe(cfg.workdir, wt)
        if "MISSING-CONTEXT:" in posted.get("body", ""):
            notify("agentflow needs you", f"{cfg.repo} #{n}: mockup is stuck — MISSING-CONTEXT",
                   issue_url)
            return f"#{n}: mockup stuck (MISSING-CONTEXT)"
        notify("agentflow needs you", f"{cfg.repo} #{n}: mockup variants ready", issue_url)
        return f"#{n}: drew mockup variants"
    finally:
        _release_mockup(cfg.repo, n)
        if slot is not None:
            slot.release("mockup")


# --- ADR 0009 merge-time floor: re-rebase survivors after main advances (issue #45) ---
# When one PR merges, `main` moves and every other open agentflow PR that was parked
# clean can silently go CONFLICTING. This pass re-rebases those survivors each cycle so a
# conflicted one is never discovered by hand at merge time with no signal on the PR.

# Our conflict notice on a survivor carries the PR marker so it reads as ours (not a
# maintainer question, issue #18) and lets us ping a conflicted survivor once, not every
# cycle — see `conflict_already_flagged`.
_CONFLICT_MARK = "agentflow: parked — conflicts after main advanced"
_CONFLICT_REASON = (
    "`main` moved since this PR last rebased and it no longer merges cleanly. Rebase it by "
    "hand (or close it) — agentflow won't force a conflicted merge.")

# A survivor that re-rebased clean but still can't auto-merge (review not clean, CI red, or
# an unanswered question) hands off to a human rather than churning a revise here.
_SURVIVOR_PARK_REASON = ("was re-rebased after `main` advanced but still can't auto-merge — "
                         "a human takes it from here")


class RebaseResult(str, Enum):
    CLEAN = "clean"        # re-rebased onto main and force-pushed to the same branch
    NOOP = "noop"          # rebase replayed nothing — no force-push
    CONFLICT = "conflict"  # rebase hit conflicts and was aborted; branch untouched
    ERROR = "error"        # checkout / rebase / push plumbing failed


def base_advanced(main_tip: str, merge_base: str) -> bool:
    """Pure. True when `origin/main` has moved past the point the branch last rebased onto
    — the merge-base is no longer main's tip, so the branch must re-rebase. False when the
    merge-base already IS the tip (the branch contains current main; rebasing would be a
    needless force-push) or when either SHA is missing (a git blip → don't churn). This is
    ADR 0009's 'base advanced since last rebase' check, kept pure so a survivor whose base
    hasn't moved is left untouched."""
    return bool(main_tip) and bool(merge_base) and main_tip != merge_base


def conflict_already_flagged(comments: list[dict]) -> bool:
    """Pure. True when our conflict notice is the most recent comment on the PR — so a
    still-conflicting survivor is pinged once, not re-pinged every cycle while its base
    stays behind. A newer comment (a maintainer engaging) flips this False; from there the
    comment responder owns the reply (issue #18)."""
    for c in reversed(comments):
        body = c.get("body", "")
        if _CONFLICT_MARK in body:
            return True
        if body.strip():
            return False
    return False


def _open_agentflow_prs(cfg: RepoConfig) -> list[tuple[int, str]] | None:
    """Open agentflow PRs as (number, head_branch), oldest first. None on a `gh` blip —
    unknown is not empty, so a listing failure defers the whole pass rather than reading as
    'no survivors to re-rebase'."""
    r = _run(["gh", "pr", "list", "--repo", cfg.repo, "--state", "open",
              "--json", "number,headRefName", "--limit", "100"])
    if r.returncode != 0:
        return None
    prs = [(pr["number"], pr.get("headRefName", ""))
           for pr in json.loads(r.stdout or "[]")
           if issue_of_branch(pr.get("headRefName", "")) is not None]
    return sorted(prs, key=lambda p: p[0])


def _base_advanced_for(workdir: str, branch: str) -> bool | None:
    """Live: has `origin/main` advanced past this branch's last rebase? Compares the
    merge-base against main's tip (both read from the just-fetched remote refs). None when a
    git command fails — caller skips rather than blind-rebases."""
    tip = _run(["git", "-C", workdir, "rev-parse", "origin/main"])
    mb = _run(["git", "-C", workdir, "merge-base", "origin/main", f"origin/{branch}"])
    if tip.returncode != 0 or mb.returncode != 0:
        return None
    return base_advanced(tip.stdout.strip(), mb.stdout.strip())


def _rebase_branch(cfg: RepoConfig, branch: str, wt: Path) -> RebaseResult:
    """Re-rebase the PR branch onto `origin/main` in its worktree and force-push it back
    (same branch, never a new one). A rebase that replays nothing is a no-op, not a
    force-push; a conflicting rebase is aborted so the branch is left exactly as it was.
    Live orchestration, not unit-tested (mirrors `_checkout_pr_branch`)."""
    if not _checkout_pr_branch(cfg, branch, wt):
        return RebaseResult.ERROR
    before = _run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    if _run(["git", "-C", str(wt), "rebase", "origin/main"]).returncode != 0:
        _run(["git", "-C", str(wt), "rebase", "--abort"])
        return RebaseResult.CONFLICT
    after = _run(["git", "-C", str(wt), "rev-parse", "HEAD"]).stdout.strip()
    if after and after == before:
        return RebaseResult.NOOP
    if _run(["git", "-C", str(wt), "push", "--force-with-lease", "origin", branch]).returncode != 0:
        return RebaseResult.ERROR
    return RebaseResult.CLEAN


def _park_conflicted_survivor(cfg: RepoConfig, pr: int, n: int) -> None:
    """A survivor that no longer rebases clean: post one conflict notice (carrying our
    marker) and ping, so a conflicted survivor is never silent."""
    body = f"> *{_CONFLICT_MARK}.*\n\n{_CONFLICT_REASON}"
    _run(["gh", "pr", "comment", str(pr), "--repo", cfg.repo, "--body", body])
    ratchet.record(cfg.repo, "parked")
    notify("agentflow needs you",
           f"{cfg.repo} #{n}: PR #{pr} conflicts after main advanced — rebase by hand",
           _pr_url(cfg.repo, pr))


def _issue_meta(cfg: RepoConfig, n: int) -> dict:
    """The originating issue's body + labels, for re-reviewing a survivor. {} on error."""
    r = _run(["gh", "issue", "view", str(n), "--repo", cfg.repo, "--json", "body,labels"])
    if r.returncode != 0:
        return {}
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _merge_autonomous_survivor(cfg: RepoConfig, pr: int, n: int, sl: str,
                               branch_tool: str, branch: str) -> str:
    """A clean-rebased survivor on an `autonomous` repo: rerun the full merge gate (fresh
    cross-tool review + green CI + clean verdict, ADR 0003/0009) and land it, or park for a
    human. Parks rather than churning a revise here (that's the build loop's job); never
    less safe than `reviewed`, since the same `decide_merge` gate governs the merge."""
    builder, reviewer_runner, _ = pick_pair()
    reviewer_runner = reviewer_runner or builder
    if reviewer_runner is None:
        return "deferred"   # no headroom to re-review — try again next cycle
    meta = _issue_meta(cfg, n)
    surfaces = ui_surfaces(cfg.workdir)
    verdict = Reviewer(reviewer_runner).review(cfg.repo, cfg.workdir, pr, branch, sl,
                                               acceptance=meta.get("body") or "",
                                               surfaces=_surfaces_phrase(surfaces))
    ui_gap = ui_evidence_gap(cfg.repo, pr, surfaces)
    decision = decide_merge(verdict=verdict, ci_green=ci_is_green(cfg.repo, pr),
                            reviewer_tool=reviewer_runner.tool, builder_tool=branch_tool,
                            revises_used=MAX_REVISES,   # park a still-imperfect survivor, don't churn
                            ui_evidence_missing=ui_gap,
                            reply_pending=reply_pending(_pr_comments(cfg.repo, pr) or []))
    if decision is MergeDecision.MERGE and squash_merge(cfg.repo, pr):
        _finish_review(cfg, reviewer_runner.tool, pr, sl, merged=True)
        ratchet.record(cfg.repo, ratchet.CLEAN_MERGE)
        _run(["gh", "issue", "edit", str(n), "--repo", cfg.repo, "--remove-label", "ready-for-agent"])
        return "merged"
    park(cfg.repo, pr, verdict, reason=_SURVIVOR_PARK_REASON)
    _finish_review(cfg, reviewer_runner.tool, pr, sl)
    ratchet.record(cfg.repo, "parked")
    notify("agentflow needs you", f"{cfg.repo} #{n}: PR #{pr} re-rebased but parked for you",
           _pr_url(cfg.repo, pr))
    return "parked"


def _rebase_survivor(cfg: RepoConfig, pr: int, branch: str, profile: str) -> str:
    """Re-rebase one survivor and route the outcome by the repo's profile. On conflict,
    park-and-ping (every profile). On a clean re-rebase: `autonomous` reruns the merge gate
    and lands one; `reviewed`/`guarded` just leave the PR mergeable again for the human —
    never a merge (ADR 0002)."""
    m = _BRANCH_RE.match(branch)
    if not m:
        return f"#{pr}: unrecognized branch {branch}"
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    wt = Path(_builder_worktree(cfg, tool, n, sl))
    with worktree_session(wt):
        result = _rebase_branch(cfg, branch, wt)
    if result is RebaseResult.CONFLICT:
        _park_conflicted_survivor(cfg, pr, n)
        comments = _pr_comments(cfg.repo, pr)
        if comments is not None and any(_CONFLICT_MARK in c.get("body", "") for c in comments):
            remove_worktree_if_safe(cfg.workdir, wt)
        return f"#{pr}: conflict — parked for human"
    if result is RebaseResult.ERROR:
        return f"#{pr}: rebase plumbing failed — retry next cycle"
    if result is RebaseResult.NOOP:
        remove_worktree_if_safe(cfg.workdir, wt)
        return f"#{pr}: nothing to replay"
    remove_worktree_if_safe(cfg.workdir, wt)
    if profile != "autonomous":
        return f"#{pr}: re-rebased clean — mergeable for the human"
    return f"#{pr}: {_merge_autonomous_survivor(cfg, pr, n, sl, tool, branch)}"


def recheck_once(cfg: RepoConfig) -> str:
    """Re-rebase every open agentflow PR whose base advanced since a sibling merged, and
    reroute by profile (ADR 0009 merge-time floor; issue #45). Merges serialize — at most
    one lands per cycle, so surviving siblings re-rebase against the new `main` before the
    next is eligible. Skips a survivor whose base hasn't moved (no needless force-push) and
    one already flagged / awaiting a maintainer reply (no re-ping, no fighting the responder
    — issue #18). Never opens a new PR; never merges on a `reviewed`/`guarded` repo."""
    prs = _open_agentflow_prs(cfg)
    if prs is None:
        return "couldn't list open PRs — deferring"
    if _run(["git", "-C", cfg.workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return "couldn't fetch origin — deferring"
    profile = repo_profile(cfg.workdir)
    results: list[str] = []
    for pr, branch in prs:
        if not _base_advanced_for(cfg.workdir, branch):
            continue   # False or None — base hasn't moved (or unknown): leave it untouched
        comments = _pr_comments(cfg.repo, pr)
        if comments is None:
            continue
        if conflict_already_flagged(comments) or reply_pending(comments):
            continue   # already pinged, or a maintainer question the responder owns
        out = _rebase_survivor(cfg, pr, branch, profile)
        results.append(out)
        if out.endswith(": merged"):
            break   # one merge per cycle — survivors re-rebase against the new main first
    return "; ".join(results) if results else "no survivors to re-rebase"


def pipeline_once(cfg: RepoConfig, _log=None) -> str:
    """One full pass for a repo: triage one un-triaged issue, build one ready issue, draw one
    parked UI issue's mockup variants, answer one parked PR the maintainer commented on, and
    re-rebase any survivor whose base moved when a sibling merged (ADR 0016 — intake runs ahead
    of the build queue; issue #29 — parked UI issues get their variants drawn; issue #18 — parked
    PRs stay answered; ADR 0009 / issue #45 — survivors never go silently conflicting)."""
    recovery = recover_stale_worktrees(cfg.repo, cfg.workdir)
    if _log and (recovery.removed or recovery.retained):
        _log(f"{cfg.repo}: worktree recovery removed {len(recovery.removed)}, "
             f"retained {len(recovery.retained)} for recovery")
    return (f"intake: {intake_once(cfg, _log=_log)} · build: {run_once(cfg, _log=_log)} · "
            f"mockup: {produce_once(cfg, _log=_log)} · respond: {respond_once(cfg, _log=_log)} · "
            f"recheck: {recheck_once(cfg)}")


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
