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

from agentflow import ratchet
from agentflow.balancer import pick_pair
from agentflow.gate import (maintainer_comment, maintainer_comment_id, park, reply_pending)
from agentflow.intake import (INTAKE_MARK, IntakeResult, IntakeRoute, STATE_LABELS,
                              _DISCLAIMER, _strip_quoted_lines, awaiting_recheck,
                              replies_since_intake)
from agentflow.notify import notify
from agentflow.reviewer import Verdict, review_worktree
from agentflow.runner import (Complexity, Effort, _run,
                              _worktree_is_disposable, _worktree_is_registered,
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

RESPOND_PROMPT = """A maintainer left a comment on PR #{n} in this worktree and it is
still unanswered. Read the full conversation first (`gh pr view {n} --json comments`),
then answer the maintainer comment named below. This is a REPLY, not a fresh review —
answer what they actually asked, nothing more.

The PR head when this Respond began was {baseline}.
<!-- agentflow-respond-baseline:{baseline} -->

Before doing anything, inspect both the conversation and the branch. This prompt may be a
continuation after a partial outcome:
- If the exact targeted reply marker below already exists, do not post the reply again. If its
  outcome marker is missing or stale after you finish the branch work, edit that existing reply in
  place to carry the final marker.
- If the requested branch change is already committed and pushed beyond the baseline, do not
  make or push it again; finish only the still-missing reply.
- If the reply exists but local work remains, finish and push that work without replying again.

Their comment:
---
{comment}
---

Reply conversationally in a PR comment that STARTS with this exact line, so we can tell
your reply apart from theirs:
{disclaimer}

The same comment must contain exactly one durable outcome marker:
- `<!-- agentflow-respond-change:none -->` only when the maintainer requested no branch change.
- `<!-- agentflow-respond-change:<pushed-head-sha> -->` when a branch change was requested,
  after pushing it. Use the PR's current pushed head SHA; it must differ from the baseline.

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
    blip as "nothing in flight" would re-dispatch every in-review issue; callers fail closed
    (skip, retry next cycle)."""
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


TRIAGING = "agentflow:triaging"   # dispatch claim — a grounding session owns this issue

# Out of the intake queue: a resolved state label, or a live triaging claim.
_TRIAGE_SKIP = set(STATE_LABELS) | {TRIAGING}


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


HELD_LABELS = {"agentflow:needs-grilling", "agentflow:needs-mockup"}


def build_issue(cfg: RepoConfig, n: int) -> str:
    """By-hand build of a *specific* ready issue (ADR 0022's `build <N>`). Fetches issue N,
    **refuses and redirects** anything that isn't `ready-for-agent` (a held issue → `pickup`;
    an un-triaged one → `triage`/`scope`), refuses one already claimed or in flight, then
    submits the same durable Build record as the daemon. Provider launch, review, continuation,
    and permits remain behind the coordinator."""
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
    from agentflow import coordinated_build
    builder, _reviewer, block_msg = pick_pair(operator=True)
    if builder is None:
        return f"#{n}: no pool has headroom ({block_msg}) — deferring"
    submission = coordinated_build.build_submission(cfg, issue, builder.tool)
    if submission is None:
        return f"#{n}: skipped — no agentflow:complexity:* label (ADR 0018 hard gate)"
    if not _claim(cfg.repo, n):
        return f"#{n}: could not claim Build — refusing coordinator submission"
    coordinator = coordinated_build.build_coordinator()
    coordinator.submit_stage(submission)
    coordinated_build.reconcile_and_project(coordinator)
    return f"#{n}: submitted to coordinator → {builder.tool} (build)"


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
                    "--description", "Intake ownership claim — prevents duplicate dispatch", "--force"])
    claimed = _run(["gh", "issue", "edit", str(n), "--repo", repo, "--add-label", TRIAGING])
    return created.returncode == 0 and claimed.returncode == 0


def _release_triage(repo: str, n: int) -> bool:
    """Drop the intake claim once routing is written (the state label dedups from here) or the
    session ended. Only the owning Intake finalizer releases it; an unreadable coordinator store
    therefore clears nothing.
    Returns durable proof that GitHub no longer carries the claim."""
    def claim_present() -> bool | None:
        viewed = _run(["gh", "issue", "view", str(n), "--repo", repo, "--json", "labels"])
        if viewed.returncode != 0:
            return None
        try:
            labels = json.loads(viewed.stdout or "{}").get("labels", [])
        except ValueError:
            return None
        return TRIAGING in {
            label.get("name") for label in labels if isinstance(label, dict)
        }

    before = claim_present()
    if before is None:
        return False
    if not before:
        return True  # an earlier settlement released it before interruption
    removed = _run(["gh", "issue", "edit", str(n), "--repo", repo,
                    "--remove-label", TRIAGING])
    if removed.returncode != 0:
        return False
    return claim_present() is False


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
        claims = {lbl["name"] for lbl in issue["labels"]}
        if TRIAGING in claims or DRAWING in claims:
            continue   # Intake or the current Mockup round already owns this held issue
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


def _next_pr_awaiting_reply(cfg: RepoConfig) -> tuple[int, str, str, str, str] | None:
    """The next open agentflow PR with an unanswered maintainer comment.

    Returns ``(pr_number, head_branch, comment, comment_target, baseline_head)`` for the oldest
    unanswered target. A target-aware reply removes only that comment, so later comments become
    fresh Respond stages instead of being collapsed into one run. Generic legacy markers retain
    their old run-level meaning, and the responder never wakes on its own comments (#18/#107)."""
    r = _run(["gh", "pr", "list", "--repo", cfg.repo, "--state", "open",
              "--json", "number,headRefName,headRefOid", "--limit", "100"])
    if r.returncode != 0:
        return None
    for pr in sorted(json.loads(r.stdout or "[]"), key=lambda p: p.get("number", 0)):
        branch = pr.get("headRefName", "")
        baseline = pr.get("headRefOid", "")
        if issue_of_branch(branch) is None or not baseline:
            continue   # not an agentflow PR — a human's own branch
        cr = _run(["gh", "pr", "view", str(pr["number"]), "--repo", cfg.repo, "--json", "comments"])
        if cr.returncode != 0:
            continue
        comments = json.loads(cr.stdout or "{}").get("comments", [])
        if reply_pending(comments):
            return (pr["number"], branch, maintainer_comment(comments),
                    maintainer_comment_id(comments), baseline)
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

This may be a continuation after an interrupted session. Before changing anything, inspect the
branch, worktree, and issue comments. Reuse committed or local variants and finish only missing
artifacts; never create a second variant round or duplicate already-pushed work. If the marked
issue comment already exists, NEVER post another: finish and push any missing work, then edit that
existing comment in place if its links or descriptions need correction.

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


def _claim_mockup(repo: str, n: int) -> bool:
    """Claim issue n for the produce phase *before* its long drawing session, so a concurrent or
    next-cycle pass skips it (no double-draw). Symmetric to `_claim_triage`; ensures the label
    first. A crash before release strands the claim — fail-safe (skipped, never double-drawn),
    cleared by hand, since the session opens no PR to check liveness against."""
    created = _run(["gh", "label", "create", DRAWING, "--repo", repo, "--color", "fef2c0",
                    "--description", "A session is drawing mockup variants for this issue",
                    "--force"])
    claimed = _run(["gh", "issue", "edit", str(n), "--repo", repo, "--add-label", DRAWING])
    return created.returncode == 0 and claimed.returncode == 0


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
    # Claim the worktree session only AFTER the checkout has run: `_checkout_pr_branch` reuses an
    # existing worktree only when its own idle/disposability guard passes, and that guard rejects
    # an *active* worktree. Marking the session before the checkout makes the guard see our own
    # mark and refuse — every survivor with a live builder worktree then fails plumbing forever.
    # Held across rebase+push so a concurrent cleanup can't remove the worktree mid-rebase.
    with worktree_session(wt):
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


def _issue_acceptance(cfg: RepoConfig, number: int) -> str | None:
    """The current issue body anchoring a survivor re-review; unreadable fails closed."""
    viewed = _run(["gh", "issue", "view", str(number), "--repo", cfg.repo, "--json", "body"])
    if viewed.returncode != 0:
        return None
    try:
        body = json.loads(viewed.stdout or "{}").get("body")
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, str) else None


def _merge_autonomous_survivor(cfg: RepoConfig, pr: int, n: int, sl: str,
                               branch_tool: str, branch: str) -> str:
    """Submit the rebased exact head as a fresh durable Review; never launch one directly."""
    from agentflow import coordinated_build

    head = _run(["git", "-C", cfg.workdir, "rev-parse", f"origin/{branch}"])
    if head.returncode != 0 or not head.stdout.strip():
        return "review head unreadable"
    reviewer_tool = "codex" if branch_tool == "claude" else "claude"
    acceptance = _issue_acceptance(cfg, n)
    if acceptance is None:
        return "issue acceptance unreadable"
    submission = coordinated_build.survivor_review_submission(
        cfg, issue=n, slug=sl, builder_tool=branch_tool, head_sha=head.stdout.strip(),
        reviewer_tool=reviewer_tool, pr_number=pr, acceptance=acceptance)
    if submission is None:
        return "review submission unavailable"
    if not _claim(cfg.repo, n):
        return "could not claim survivor Review"
    coordinator = coordinated_build.build_coordinator()
    coordinator.submit_stage(submission)
    coordinated_build.reconcile_and_project(coordinator)
    return "review submitted"


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
