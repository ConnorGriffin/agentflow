"""The M0 loop — one ready-for-agent issue through the whole pipeline, serially.

Ephemeral hands, single issue (ADR 0011); the persistent daemon and real two-pool
balancing are M1. For M0 the pair is fixed — Claude builds, Codex reviews — so
cross-tool independence holds and swapping in the headroom balancer is the M1 change.
Every ready issue must carry an `agentflow:complexity:*` label (the hard gate,
ADR 0018) — intake stamps it; the loop reads it and skips an issue that has none.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agentflow import github, ratchet
from agentflow.balancer import pick_pair, pick_reviewer
from agentflow.gate import (maintainer_comment, maintainer_comment_id, park, reply_pending)
from agentflow.intake import (INTAKE_MARK, IntakeResult, IntakeRoute, STATE_LABELS,
                              _DISCLAIMER, _strip_quoted_lines, awaiting_recheck,
                              replies_since_intake)
from agentflow.notify import notify
from agentflow.reviewer import Verdict, review_worktree
from agentflow.screenshot_crib import SCREENSHOT_HARNESS
from agentflow.shell_crib import SHELL_CRIB
from agentflow.runner import (Complexity, Effort, MockupScope, _commit_is_on_origin, _run,
                              _worktree_is_disposable, _worktree_is_registered,
                              remove_worktree_if_safe, resettable_head,
                              retain_stranded_commit, worktree_session)
from agentflow.worktree_ref import WorktreeRef


def _pr_url(repo: str, pr: int) -> str:
    return f"https://github.com/{repo}/pull/{pr}"


def _pr_comments(repo: str, pr: int) -> list[dict] | None:
    """The PR's comments, or None when they cannot be verified. The comment rows flow on to
    gate/intake predicates that read GitHub's own `body`/`author` keys, so this keeps the raw
    row shape while routing the read through the shared module's escape hatch."""
    data = github.api(["pr", "view", str(pr), "--repo", repo, "--json", "comments"],
                      parse_json=True)
    if not isinstance(data, dict):
        return None
    return data.get("comments", [])


def _issue_comments(repo: str, n: int) -> list[dict]:
    """The issue's comments, or [] if they can't be read. Impure."""
    data = github.api(["issue", "view", str(n), "--repo", repo, "--json", "comments"],
                      parse_json=True)
    if not isinstance(data, dict):
        return []
    return data.get("comments", [])


def _issue_comments_or_none(repo: str, n: int) -> list[dict] | None:
    """The issue's comment rows, or None when they can't be read — the fail-closed variant a
    per-issue selection pass needs, so an unreadable thread skips the issue instead of reading
    as an empty (and therefore 'eligible') one."""
    data = github.api(["issue", "view", str(n), "--repo", repo, "--json", "comments"],
                      parse_json=True)
    if not isinstance(data, dict):
        return None
    return data.get("comments", [])


_COMPLEXITY_LABEL = re.compile(r"^agentflow:complexity:(standard|deep)$")
_EFFORT_LABEL = re.compile(r"^agentflow:effort:(low|medium|high|extra)$")
_MOCKUP_SCOPE_LABEL = re.compile(r"^agentflow:mockup:(local|surface)$")
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


def mockup_scope_from_labels(labels: list[str]) -> MockupScope:
    """The parked mockup issue's scope from its `agentflow:mockup:*` label; defaults to
    `local` when absent so a legacy park (or a dropped label) draws the safer, narrower
    round rather than reopening the whole surface (ADR 0048). Pure (test surface)."""
    for name in labels:
        m = _MOCKUP_SCOPE_LABEL.match(name)
        if m:
            return MockupScope(m.group(1))
    return MockupScope.LOCAL


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
CSS/API specifics (ADR 0018). End it with `Review depth: Focused|Targeted|Full — <one short
reason>`: Focused for exact housekeeping/evidence, Targeted for one contained behavior or
journey, Full for connected behavior, sensitive information, permissions, safety, or competing
product decisions. Small safety/permission changes are Full. If the change touches a user-facing surface (this repo's are:
{surfaces}), you MUST ship before/after screenshots as proof it matches the locked mockup —
both light and dark themes where the app has them. If this brief carries a `## LOCKED visual
contract`, that contract is the spec you build and screenshot against: satisfy every visual rule
and interaction it states, and ship a screenshot of EVERY state it names must be screenshotted
(the reviewer compares your screenshots to those exact contract lines). """ + SCREENSHOT_HARNESS + """

Attach the PNGs the committed way — no browser is involved and none may be used: save them under
`docs/screenshots/issue-{n}/<short-sha>/` (namespace each round by the branch's current short
commit SHA so a later revision can never overwrite the files an earlier comment points at),
commit and push them FIRST, then embed each in the PR body as a markdown image hosted from the
immutable commit that added it:
`https://github.com/{repo}/raw/<commit-sha>/docs/screenshots/issue-{n}/<short-sha>/<name>.png`,
where `<commit-sha>` is the pushed commit's full hash. NEVER host from a mutable branch ref
(never `refs/heads/...`, never `blob/<branch>/...`): GitHub serves those from the current branch
head, so after any later push every historical screenshot silently repaints to the new head's
files or 404s. NEVER try to upload images through a web browser or GitHub's drag-drop attachments
— this host cannot do that, and an apology comment does not satisfy the gate; the committed files
do. A UI change with no screenshot cannot auto-merge (a mechanical gate parks it, ADR 0018),
and a body full of jargon blocks at review. Both are charter gates, not style points.

Keep the change minimal and match the surrounding code. If you hit a blocker you
cannot safely resolve, post a comment prefixed `MISSING-CONTEXT:` and stop instead
of guessing.""" + SHELL_CRIB

REVISE_PROMPT = """Address the blocking review findings on PR #{n} in this worktree,
push to the same branch, and keep the test suite green. Do NOT open a new PR.

Make the implementation judgment yourself when the issue brief, current `main`, surrounding code,
and tests establish a safe answer. A merge conflict is not by itself missing context: reconcile
both intended behaviors wherever they are compatible; neither side wins merely because it is newer.
When the choices encode genuinely incompatible product intent, do not silently preserve either
side. Stop and post no intermediate PR comment, then
return one private final line `CONFLICT-UNCERTAINTY: {{"options":["option A","option B"],
"missing_guidance":"exact missing rule","recommendation":"your grounded recommendation"}}`.
Agentflow gives that decision one narrow in-flow handoff to the other tool.

Do not degrade the two charter gates while revising (ADR 0018):
- If the PR touches a user-facing surface (this repo's are: {surfaces}), keep before/after
  screenshots attached — both light and dark themes where the app has them. A UI change with
  no screenshot cannot auto-merge; a mechanical gate parks it regardless of the review.
  If screenshots are missing or stale, capture them this way — """ + SCREENSHOT_HARNESS + """
  Commit the PNGs under `docs/screenshots/issue-{n}/<short-sha>/` — namespace each round by the branch's
  current short commit SHA so this round's files never overwrite the ones an earlier comment
  points at. Push FIRST, then embed them in the PR body as markdown images hosted from the
  immutable commit that added them
  (`https://github.com/{repo}/raw/<commit-sha>/docs/screenshots/issue-{n}/<short-sha>/<name>.png`,
  `<commit-sha>` = the pushed commit's full hash). NEVER host from a mutable branch ref (never
  `refs/heads/...`, never `blob/<branch>/...`): GitHub serves those from the current branch head,
  so a later push repaints every historical screenshot to the new files or 404s. NEVER try to
  upload images through a web browser or GitHub's drag-drop attachments — this host cannot do
  that; the committed files are the evidence.
- Keep the PR body in plain app language for the human who merges — no file/function/test
  names or CSS/API specifics.

Blocking findings:
{findings}""" + SHELL_CRIB

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
- Relevant work must be committed and pushed before the reply. Unrelated local scratch files are
  outside the completion proof; do not delete user files merely to make the worktree globally clean.

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
  your comment, against the locked mockup for a UI surface, the same way a build does.
  """ + SCREENSHOT_HARNESS + """
- If they asked for a small change, make it, commit, and push to THIS SAME branch.

Same contract as a revision: never open a new PR, never merge. If you genuinely can't
address it, say so plainly in the reply.""" + SHELL_CRIB


def _row_dict(row: github.IssueRow) -> dict:
    """Adapt a typed issue row back to the raw mapping the coordinator's build/mockup submission
    builders and the pure dispatch predicates still read (they consume GitHub's own
    `labels[].name` shape). The discovery listings return typed rows now; this is the single
    bridge to the not-yet-migrated consumers, so the wire shape is reassembled once here rather
    than at every call site."""
    return {"number": row.number, "title": row.title, "body": row.body,
            "labels": [{"name": name} for name in row.labels]}


def _issues_in_flight(cfg: RepoConfig) -> set[int] | None:
    """Issues that already have an OPEN agentflow PR — an agent is on them, so don't
    re-dispatch a duplicate. Dispatch dedup, distinct from ADR 0009's merge-time floor:
    an issue stays `ready-for-agent` while its PR is in review, so without this the loop
    would re-build it every cycle (a second PR on a different tool).

    Returns None when the listing itself failed — unknown is NOT empty. Treating a `gh`
    blip as "nothing in flight" would re-dispatch every in-review issue; callers fail closed
    (skip, retry next cycle)."""
    # closingIssuesReferences isn't part of the typed PR row, so this discovery listing goes
    # through the module's escape hatch rather than `list_open_prs`.
    data = github.api(["pr", "list", "--repo", cfg.repo, "--state", "open",
                       "--json", "headRefName,closingIssuesReferences", "--limit", "100"],
                      parse_json=True)
    if not isinstance(data, list):
        return None
    in_flight: set[int] = set()
    for pr in data:
        # A PR's declared closing-issue reference marks that issue in-flight regardless of
        # how its head branch is named — a hand-driven build on an off-convention branch
        # (e.g. `codex/40-foo`) still dedups. The field is same-repo scoped, so it can't be
        # fooled by a `Closes #N` meaning another repo.
        for ref in pr.get("closingIssuesReferences") or []:
            if (n := ref.get("number")) is not None:
                in_flight.add(n)
        # Fallback: recognize the conventional branch even if no closing reference is declared.
        if (n := issue_of_branch(pr.get("headRefName", ""))) is not None:
            in_flight.add(n)
    return in_flight


BUILDING = "agentflow:building"   # dispatch claim — an agent is building this issue
_BLOCKED_BY_RE = re.compile(r"^Blocked by #(\d+)\s*$", re.MULTILINE)


def _native_blockers(cfg: RepoConfig, number: int) -> set[int] | None:
    """Same-repo issue numbers from an issue's native GitHub `blocked_by` edges.

    A second recognized source of blockers alongside `Blocked by #N` body prose (ADR 0024):
    planning tools express dependencies as native GitHub relationships. Returns None when the
    edge set cannot be read (a `gh` failure or malformed response) so the caller can fail
    closed — an unreadable dependency graph is never mistaken for "no blockers". Cross-repo
    edges are ignored: only blockers in `cfg.repo` join the same-repo gate."""
    # A REST dependency read — one of the escape hatch's named uses (ADR 0040).
    edges = github.api(["api", f"repos/{cfg.repo}/issues/{number}/dependencies/blocked_by"],
                       parse_json=True)
    if not isinstance(edges, list):
        return None
    numbers: set[int] = set()
    for edge in edges:
        if not isinstance(edge, dict):
            return None
        if (edge.get("repository") or {}).get("full_name") == cfg.repo:
            numbers.add(int(edge["number"]))
    return numbers


def _free_to_dispatch(cfg: RepoConfig, issue: dict, in_flight: set[int], _log=None) -> bool:
    """A ready issue is free only if no agent owns it and every declared blocker is closed.

    The blocker set is the union of the issue's native GitHub `blocked_by` edges and its
    `Blocked by #N` body prose (ADR 0024). Blocker state is read from the same repository on
    every selection pass. Unknown is not closed: a missing issue, `gh` failure, or malformed
    response — from either source — skips the dependent until a later pass can verify it
    safely. Shared by queue selection and by-hand builds."""
    if (issue["number"] in in_flight
            or BUILDING in {lbl["name"] for lbl in issue.get("labels", [])}):
        return False

    native = _native_blockers(cfg, issue["number"])
    if native is None:
        if _log:
            _log(f"{cfg.repo}: #{issue['number']}: skipped — native blocked-by edges "
                 "could not be determined")
        return False

    prose = (int(n) for n in _BLOCKED_BY_RE.findall(issue.get("body") or ""))
    blockers = dict.fromkeys([*prose, *sorted(native)])
    for blocker in blockers:
        state = github.issue_state(cfg.repo, blocker)
        if state != "CLOSED":
            if _log:
                reason = f"open blocker #{blocker}" if state == "OPEN" \
                    else f"blocker #{blocker} whose state could not be determined"
                _log(f"{cfg.repo}: #{issue['number']}: skipped — {reason}")
            return False
    return True


def _next_ready_issue(cfg: RepoConfig, reserved: set[int] = frozenset(),
                      _log=None) -> dict | None:
    """The oldest ready issue free to dispatch. `reserved` skips candidates this pass has
    already found conclusively undispatchable, so one bad queue head cannot starve later
    runnable work (#327)."""
    rows = github.list_issues(cfg.repo, label="ready-for-agent", limit=50)
    if rows is None:
        return None
    in_flight = _issues_in_flight(cfg)
    if in_flight is None:
        return None   # can't see what's in flight — fail closed, dispatch next cycle
    issues = sorted((_row_dict(r) for r in rows), key=lambda i: i["number"])
    return next((i for i in issues if i["number"] not in reserved
                 and _free_to_dispatch(cfg, i, in_flight, _log)), None)


TRIAGING = "agentflow:triaging"   # dispatch claim — a grounding session owns this issue
DRAWING = "agentflow:drawing-mockup"   # dispatch claim — a session is drawing this issue's variants

# Out of the intake queue: a resolved state label, a live triaging claim, or a live
# build/mockup claim. An issue already owned by a downstream lane (`agentflow:building`,
# `agentflow:drawing-mockup`) is mid-pipeline, not un-triaged, so intake never re-claims it —
# even after the reconciler strips its stale `triaging` label (#201).
_TRIAGE_SKIP = set(STATE_LABELS) | {TRIAGING, BUILDING, DRAWING}


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
    rows = github.list_issues(cfg.repo, limit=50)
    if rows is None:
        return None
    untriaged = [i for i in (_row_dict(r) for r in rows)
                 if _untriaged(i) and i["number"] not in reserved]
    return min(untriaged, key=lambda i: i["number"]) if untriaged else None


def _builder_worktree(cfg: RepoConfig, tool: str, n: int, sl: str) -> str:
    return WorktreeRef.for_build(cfg.workdir, tool, n, sl).path


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
    # A single-issue read that pulls state alongside the build fields; the row is handed to the
    # coordinator's build submission, which reads GitHub's own keys, so it goes through the hatch.
    issue = github.api(["issue", "view", str(n), "--repo", cfg.repo,
                        "--json", "number,title,body,labels,state"], parse_json=True)
    if not isinstance(issue, dict):
        return f"#{n}: not found in {cfg.repo}"
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
    from agentflow.coordinator.record import WAITING
    builder, _reviewer, block_msg = pick_pair(operator=True)
    if builder is None:
        return f"#{n}: no pool has headroom ({block_msg}) — deferring"
    submission = coordinated_build.build_submission(cfg, issue, builder.tool)
    if submission is None:
        return f"#{n}: skipped — no agentflow:complexity:* label (ADR 0018 hard gate)"
    # A `build <N>` on an issue whose latest Build exhausted its budget and `held` is the explicit,
    # durable maintainer resume (#245): open a fresh bounded execution at the next resume identity
    # instead of silently reusing the terminal held record.
    records = coordinated_build.tracer.load_records()
    resumed = coordinated_build.resume_if_held(submission, records)
    coordinator = coordinated_build.build_coordinator()
    identity = coordinator.submit_stage(resumed)
    record = coordinator.stage_record(identity)
    # Claim the issue and report a launch only when admission actually produced runnable work. An
    # ordinary resubmission that reused a terminal (held/completed) record leaves nothing to run —
    # never stamp `agentflow:building` on it or claim success; redirect the maintainer to `pickup`.
    if record is None or record.state != WAITING or record.hold_pending or record.retired:
        if coordinated_build.resume_in_flight(resumed, records):
            return f"#{n}: a resume is already running — let it finish or reply what's missing"
        return f"#{n}: still held — resume it with `/agentflow pickup {n}`, reply what's missing"
    if not _claim(cfg.repo, n):
        # We submitted a runnable record but never established the GitHub claim: withdraw it so no
        # orphaned WAITING build is left for a later cycle to launch unguarded (#245).
        coordinator.withdraw_stage(identity)
        return f"#{n}: could not claim Build — withdrew the coordinator submission"
    coordinated_build.reconcile_and_project(coordinator)
    verb = "resumed" if resumed.resume else "submitted"
    return f"#{n}: {verb} to coordinator → {resumed.pool} (build)"


def _claim(repo: str, n: int) -> bool:
    """Mark issue n as owned by an agent *before* its build runs, so a concurrent or
    next-cycle dispatch skips it (closes the no-PR-yet window). Ensures the label first and
    reports whether ownership was actually made visible; coordinated Build refuses submission
    when it cannot establish this guard."""
    # The typed `create_label` write carries no description; these claim labels want one, so the
    # create goes through the module's escape hatch while the add-label is the typed write.
    if github.api(["label", "create", BUILDING, "--repo", repo, "--color", "fbca04",
                   "--description", "An agent is building this issue", "--force"]) is None:
        return False
    return github.add_label(repo, n, BUILDING)


def _release(repo: str, n: int) -> None:
    """Drop the build claim when the build is done, whatever the outcome. A parked PR
    stays skipped via the open-PR check; a failed build is free to retry next cycle."""
    github.remove_label(repo, n, BUILDING)


def _claim_triage(repo: str, n: int) -> bool:
    """Claim issue n for intake *before* its grounding session, so a concurrent or next-cycle
    dispatch skips it — closing intake's no-label-yet window (the state label is only stamped
    once the session finishes). Symmetric to `_claim`; ensures the label first."""
    created = github.api(["label", "create", TRIAGING, "--repo", repo, "--color", "d4c5f9",
                          "--description", "Intake ownership claim — prevents duplicate dispatch",
                          "--force"]) is not None
    claimed = github.add_label(repo, n, TRIAGING)
    return created and claimed


def _release_triage(repo: str, n: int) -> bool:
    """Drop the intake claim once routing is written (the state label dedups from here) or the
    session ended. Only the owning Intake finalizer releases it; an unreadable coordinator store
    therefore clears nothing.
    Returns durable proof that GitHub no longer carries the claim."""
    def claim_present() -> bool | None:
        labels = github.issue_labels(repo, n)
        if labels is None:
            return None
        return TRIAGING in labels

    before = claim_present()
    if before is None:
        return False
    if not before:
        return True  # an earlier settlement released it before interruption
    if not github.remove_label(repo, n, TRIAGING):
        return False
    return claim_present() is False


# --- research stage: dispatch an unattended session for an AFK-able planning ticket (ADR 0037) ---
# Build intake still walls out the whole `wayfinder:*` namespace (`_untriaged`); this is the *only*
# path that lets the daemon see and run a planning ticket, and it may run `wayfinder:research` and
# nothing else. The shared `wayfinder:resolving` label is the claim — set before the session, so a
# human session and the daemon never both grab the same ticket.

RESEARCH_TICKET = "wayfinder:research"   # the one AFK-able planning ticket type the daemon may run
RESOLVING = "wayfinder:resolving"        # shared claim — a session (human or daemon) owns this ticket

# The wayfinder type labels the daemon must never dispatch: grilling/prototype/task stay
# human-in-the-loop (ADR 0037). A ticket carrying any of these is refused even if it is also
# mislabeled `wayfinder:research`, so the AFK boundary never leaks.
_WAYFINDER_NON_RESEARCH = frozenset({"wayfinder:grilling", "wayfinder:prototype", "wayfinder:task"})


def _research_eligible(issue: dict) -> bool:
    """Pure (test surface). A ticket is dispatchable research only if it carries the
    `wayfinder:research` type label, is not already claimed by `wayfinder:resolving`, and carries no
    other `wayfinder:*` type label the daemon must never run (ADR 0037). Its blocker state is
    verified separately against the live dependency graph."""
    labels = {lbl["name"] for lbl in issue.get("labels", [])}
    if RESEARCH_TICKET not in labels or RESOLVING in labels:
        return False
    return not (labels & _WAYFINDER_NON_RESEARCH)


def _research_unblocked(cfg: RepoConfig, number: int, _log=None) -> bool:
    """Whether every native `blockedBy` edge of a research ticket is closed (ADR 0037). Unreadable
    edges (`_native_blockers` returns None) or a blocker whose state cannot be read fail closed — an
    unknown dependency graph is never mistaken for 'unblocked', the same discipline the build queue
    uses."""
    native = _native_blockers(cfg, number)
    if native is None:
        if _log:
            _log(f"{cfg.repo}: #{number}: skipped research — native blocked-by edges "
                 "could not be determined")
        return False
    for blocker in sorted(native):
        state = github.issue_state(cfg.repo, blocker)
        if state != "CLOSED":
            if _log:
                _log(f"{cfg.repo}: #{number}: skipped research — blocker #{blocker} not closed")
            return False
    return True


def _next_research_ticket(cfg: RepoConfig, _log=None) -> dict | None:
    """The oldest open, unblocked, unclaimed `wayfinder:research` ticket the daemon may resolve
    unattended (ADR 0037). Selection is by the research type label, so no other `wayfinder:*` ticket
    is ever admitted; eligibility and blocker state are re-read on every pass and fail closed. None
    on a `gh` blip (retry next cycle)."""
    rows = github.list_issues(cfg.repo, label=RESEARCH_TICKET, limit=50)
    if rows is None:
        return None
    for issue in sorted((_row_dict(r) for r in rows), key=lambda i: i["number"]):
        if _research_eligible(issue) and _research_unblocked(cfg, issue["number"], _log):
            return issue
    return None


def _claim_resolving(repo: str, n: int) -> bool:
    """Claim a research ticket with the shared `wayfinder:resolving` label *before* dispatching its
    session, so a concurrent or next-cycle pass — human or daemon — skips it (ADR 0037). Ensures the
    label exists first; symmetric to `_claim_triage`."""
    created = github.api(["label", "create", RESOLVING, "--repo", repo, "--color", "5319e7",
                          "--description", "A session is resolving this planning ticket",
                          "--force"]) is not None
    claimed = github.add_label(repo, n, RESOLVING)
    return created and claimed


def _release_resolving(repo: str, n: int) -> bool:
    """Drop the shared research claim once the ticket is resolved or the run gave up (ADR 0037).
    Mirrors `_release_triage`: only the owning research finalizer releases it, and it returns durable
    proof that GitHub no longer carries the claim so a finalizer never retires over a claim it never
    released."""
    def claim_present() -> bool | None:
        labels = github.issue_labels(repo, n)
        if labels is None:
            return None
        return RESOLVING in labels

    before = claim_present()
    if before is None:
        return False
    if not before:
        return True  # an earlier settlement released it before interruption
    if not github.remove_label(repo, n, RESOLVING):
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
        rows = github.list_issues(cfg.repo, label=label, limit=50)
        if rows is None:
            return None
        issues.extend(_row_dict(r) for r in rows)
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
        comments = _issue_comments_or_none(cfg.repo, issue["number"])
        if comments is None:
            continue
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
    prs = github.list_open_prs(cfg.repo, limit=100)
    if prs is None:
        return None
    for pr in sorted(prs, key=lambda p: p.number):
        branch = pr.head_ref_name
        baseline = pr.head_ref_oid
        if issue_of_branch(branch) is None or not baseline:
            continue   # not an agentflow PR — a human's own branch
        comments = _pr_comments(cfg.repo, pr.number)
        if comments is None:
            continue
        if reply_pending(comments):
            return (pr.number, branch, maintainer_comment(comments),
                    maintainer_comment_id(comments), baseline)
    return None


def _checkout_pr_branch(cfg: RepoConfig, branch: str, wt: Path) -> bool:
    """Put the PR branch in a worktree so a responder can push fixes to it. Reuses the
    builder's worktree when it's still there (freshened to the PR head), else cuts a fresh
    one tracking `origin/<branch>`. Returns success. Live orchestration, not unit-tested.

    Reuse is gated on what the freshening actually overwrites: tracked content. A build session
    routinely leaves untracked files behind (a screenshot config, a draft PR body), and a reset
    leaves those untouched — so they must not veto the reuse. A commit that never reached the
    remote is anchored under a recovery ref before the reset, exactly as a review checkout's is."""
    if _run(["git", "-C", cfg.workdir, "fetch", "origin", "--quiet"]).returncode != 0:
        return False
    if wt.exists():
        head = resettable_head(cfg.workdir, wt)
        if not head:
            return False
        if not _commit_is_on_origin(cfg.workdir, head) \
                and not retain_stranded_commit(cfg.workdir, wt, head):
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

# The produced-variants comment carries INTAKE_MARK ("agentflow intake") so `awaiting_recheck`
# reads it as OURS — never as a maintainer reply, which would self-trigger the resume path (the
# TRAP in the brief). MOCKUP_MARK additionally distinguishes it from intake's park/kickoff
# comment, so the produce phase draws exactly one round and never re-draws.
MOCKUP_MARK = "mockup variants"
_MOCKUP_DISCLAIMER = "> *agentflow intake: mockup variants — generated by AI.*"

# Scope-specific draw instructions threaded into PRODUCE_PROMPT (ADR 0048). A `surface` round
# reopens the whole visual world (the classic 3-4 wildly-different tournament); a `local` round
# inherits the shipping surface's identity and varies only the addition. Intake classifies the
# scope; `mockup_scope_from_labels` recovers it from the parked issue's label.
_SCOPE_GUIDANCE = {
    MockupScope.SURFACE: (
        "SCOPE: surface — this is a whole-surface replacement (or a brand-new surface with no "
        "incumbent to inherit). Produce 3-4 genuinely-different WHOLE-SURFACE concepts that "
        "diverge at the CONCEPT level (layout metaphor, interaction model, hierarchy), not just "
        "colors. Ground every one in the shipping app's own theme, library, and data. 3-4 "
        "variants is the sweet spot."),
    MockupScope.LOCAL: (
        "SCOPE: local — this is an ADDITION inside an already-shipping surface. INHERIT that "
        "surface's exact identity: its theme, layout, components, spacing, and data. Do NOT "
        "re-theme it or reopen the whole visual world. Produce 2-3 variants that differ only in "
        "how the ADDITION works — its purpose, hierarchy, interaction model, states, and fit with "
        "its surroundings. Every variant must look like it already lives in the shipping app."),
}

PRODUCE_PROMPT = """You are agentflow's mockup phase for {repo} issue #{n}: {title}

This UI issue is parked waiting for a locked visual design. Your job is to DRAW it — produce
design variants, screenshot them, and post ONE comment on the issue so the maintainer can pick
in a single reply. You do NOT choose the winner and you do NOT implement the feature — you give
the maintainer something concrete to react to.

The issue as filed / scoped:
---
{body}
---

{scope_guidance}

Run the `/ui-mockups` skill for this repo's user-facing surface(s): {surfaces}, following it
headless. Screenshot each variant this way — """ + SCREENSHOT_HARNESS + """

Every variant MUST carry a `LOCKED` contract of AT MOST 150 words — the durable spec the build
and the independent review are judged against after these mockups are archived. State, for that
variant: its thesis; the user's path through it; what the first viewport shows; the visual rules
that bind the build; the interactions and states it requires; EXACTLY which states must be
screenshotted as proof; and its explicit out-of-scope boundaries. Put each variant's `LOCKED`
contract inline in the issue comment beside that variant (verbatim, not a link), so the pick can
copy the winner's contract word-for-word into the build brief.

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
- Commit and push the variant HTML and screenshots FIRST, then embed each screenshot as a markdown
  image hosted from the immutable commit that added it — the pushed commit's full hash, never the
  mutable `{branch}` ref. GitHub serves a branch-ref host from the current branch head, so a later
  push would silently repaint or 404 every image the maintainer is looking at. Do NOT use
  `raw.githubusercontent.com` either (it 404s on private repos because it ignores the viewer's login):
  `![Variant A](https://github.com/{repo}/raw/<commit-sha>/mockups/<file>.png)`.
- Name the variants A, B, C (and D), each with a ONE-LINE description of its concept AND its
  full `LOCKED` contract inline (≤150 words each), so the winner's contract is durably captured
  in this comment and can be copied verbatim into the build brief.
- End with a clear ask: reply on this issue with a pick ("B", "the second one", "A but with C's
  header") or an adjustment, and agentflow will lock the chosen design and start the build.

One variant round is enough to unblock — do not loop. If you genuinely cannot produce the
variants (no runnable surface, missing grounding), post one comment starting with {disclaimer}
and prefixed `MISSING-CONTEXT:`, then stop.""" + SHELL_CRIB


def _claim_mockup(repo: str, n: int) -> bool:
    """Claim issue n for the produce phase *before* its long drawing session, so a concurrent or
    next-cycle pass skips it (no double-draw). Symmetric to `_claim_triage`; ensures the label
    first. A crash before release strands the claim — fail-safe (skipped, never double-drawn),
    cleared by hand, since the session opens no PR to check liveness against."""
    created = github.api(["label", "create", DRAWING, "--repo", repo, "--color", "fef2c0",
                          "--description", "A session is drawing mockup variants for this issue",
                          "--force"]) is not None
    claimed = github.add_label(repo, n, DRAWING)
    return created and claimed


def _release_mockup(repo: str, n: int) -> None:
    """Drop the drawing claim once the variant round is posted (the mockup comment dedups from
    here via MOCKUP_MARK) or the session ended."""
    github.remove_label(repo, n, DRAWING)


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
    rows = github.list_open_prs(cfg.repo, limit=100)
    if rows is None:
        return None
    prs = [(row.number, row.head_ref_name)
           for row in rows
           if issue_of_branch(row.head_ref_name) is not None]
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
    body = (
        f"> *{_CONFLICT_MARK}.*\n\n"
        "## Maintainer decision needed\n\n"
        "Affected behavior: the PR and current `main` cannot be combined automatically.\n\n"
        "Options:\n"
        "- Clarify how the two intended behaviors coexist and resume conflict resolution.\n"
        "- Close the PR and retain current `main` behavior only.\n\n"
        "Consequences: resolving may ship both compatible outcomes; closing drops the PR outcome.\n\n"
        "Recommendation: state the intended behavior at the conflict, then resume this PR.\n\n"
        "## Agent handoff\n\n"
        f"Code locations: PR #{pr}'s conflicting diff against current `main`.\n\n"
        f"Conflicting changes or unresolved facts: {_CONFLICT_REASON}\n\n"
        "Checks: the rebase was attempted and aborted after Git reported conflicts; no conflicted "
        "state was pushed.\n\n"
        f"Retained work: issue #{n}'s existing PR branch remains unchanged.\n\n"
        "Exact next action: record the intended behavior, then resume conflict resolution on this "
        "same PR.")
    github.pr_comment(cfg.repo, pr, body)
    ratchet.record(cfg.repo, "parked")
    notify("agentflow needs you",
           f"{cfg.repo} #{n}: PR #{pr} conflicts after main advanced — rebase by hand",
           _pr_url(cfg.repo, pr))


def _issue_acceptance(cfg: RepoConfig, number: int) -> str | None:
    """The current issue body anchoring a survivor re-review; unreadable fails closed."""
    return github.issue_body(cfg.repo, number)


_SAME_TOOL_REVIEW_WARNING = (
    "Warning: same-tool review is not independent. This PR will be human-merge-only and marked "
    "tainted until the other tool reviews the exact open head. Re-run with maintainer_confirmed=True "
    "only after the maintainer explicitly confirms this trade-off.")


def review_pr(cfg: RepoConfig, pr: int, *, force_same_tool: bool = False,
              maintainer_confirmed: bool = False) -> str:
    """Submit `/agentflow review <pr>` through the durable coordinator.

    A forced same-tool review is never implicit: the first call returns the warning, and only an
    explicit confirmed call submits the human-merge-only tainted review. Existing running review
    work is never preempted; a waiting exact-head review may transfer its claim atomically.
    """
    from agentflow import coordinated_build
    from agentflow.coordinator.store import StoreUnavailable

    if force_same_tool and not maintainer_confirmed:
        return _SAME_TOOL_REVIEW_WARNING
    data = github.api([
        "pr", "view", str(pr), "--repo", cfg.repo,
        "--json", "headRefName,headRefOid,closingIssuesReferences,state",
    ], parse_json=True)
    if not isinstance(data, dict) or data.get("state") != "OPEN":
        return "open PR facts unreadable"
    branch, head = str(data.get("headRefName") or ""), str(data.get("headRefOid") or "")
    match = _BRANCH_RE.match(branch)
    if match is None or not head:
        return "PR is not on a recognized agentflow issue branch"
    builder_tool, branch_issue, slug = match.group(1), int(match.group(2)), match.group(3)
    if builder_tool not in {"claude", "codex"}:
        return "PR builder tool is unreadable"
    closing = [
        int(item["number"]) for item in data.get("closingIssuesReferences") or []
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    ]
    issue = closing[0] if closing else branch_issue
    acceptance = _issue_acceptance(cfg, issue)
    if acceptance is None:
        return "issue acceptance unreadable"
    try:
        records = coordinated_build.tracer.load_records()
    except StoreUnavailable:
        return "coordinator state unreadable"
    same_head = [
        record for record in records
        if record.stage == "review" and record.repo == cfg.repo
        and str(record.subject) == str(issue) and record.target == head
    ]
    latest = max(
        same_head,
        key=lambda record: (
            record.review_sequence, record.review_passes, record.created_at, record.identity),
        default=None)
    # Branch naming is durable builder lineage, not current authorship. A reviewer that pushed a
    # fix became the author of the exact open head; manual re-review must preserve that provenance.
    current_author = (
        latest.change_author_tool if latest and latest.change_author_tool else builder_tool)
    profile = repo_profile(cfg.workdir)
    if force_same_tool:
        reviewer_tool = current_author
    else:
        reviewer_tool = pick_reviewer(
            current_author, allow_same_tool=profile != "autonomous")
        if reviewer_tool is None:
            return "no eligible reviewer pool available — deferring"
    assignment, _changed_files = coordinated_build._review_assignment_facts(
        cfg.repo, pr, profile=profile)
    active = next((record for record in same_head if not record.retired), None)
    if active is not None and active.state == "running":
        return "exact-head review is already running; it was not preempted"
    sequence = max((record.review_sequence for record in same_head), default=-1) + 1
    from agentflow.review_policy import ReviewState
    review = ReviewState(
        assignment=assignment, change_author_tool=current_author,
        reviewed_from_sha=head, sequence=sequence, tainted=force_same_tool)
    submission = coordinated_build.survivor_review_submission(
        cfg, issue=issue, slug=slug, builder_tool=builder_tool, head_sha=head,
        reviewer_tool=reviewer_tool, pr_number=pr, acceptance=acceptance,
        review=review, transfer_from=active.identity if active else None,
        supersede=active is not None)
    if submission is None:
        return "review submission unavailable"
    if active is None and not _claim(cfg.repo, issue):
        return "could not claim PR review"
    coordinator = coordinated_build.build_coordinator()
    coordinator.submit_stage(submission)
    coordinated_build.reconcile_and_project(coordinator)
    status = "same-tool review submitted; maintainer merge required" if force_same_tool \
        else "review submitted"
    return status


def _merge_autonomous_survivor(cfg: RepoConfig, pr: int, n: int, sl: str,
                               branch_tool: str, branch: str) -> str:
    """Submit the rebased exact head as a fresh durable Review; never launch one directly."""
    from agentflow import coordinated_build

    head = _run(["git", "-C", cfg.workdir, "rev-parse", f"origin/{branch}"])
    if head.returncode != 0 or not head.stdout.strip():
        return "review head unreadable"
    # Route the re-review through the same reviewer choice the autonomous openers use: require the
    # cross-tool reviewer and defer while it cannot launch. Same-tool fallback would create a
    # result that the autonomous profile is forbidden to merge.
    reviewer_tool = pick_reviewer(branch_tool, allow_same_tool=False)
    if reviewer_tool is None:
        return "no reviewer pool available — deferring"
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


def _conflict_revise_survivor(cfg: RepoConfig, pr: int, n: int, sl: str, tool: str,
                              branch: str) -> str | None:
    """A survivor's re-rebase no longer applies: open a conflict Revise on the builder's own lineage
    to resolve it (ADR 0038) instead of parking. The Revise adopts the retained PR-branch worktree,
    is bound to the conflicting head SHA it must supersede, and is admitted ahead of cold build work.
    Returns a status string once a conflict Revise is opened (or is already open for this head).
    There is no PR-lifetime conflict cap: each genuinely new conflicting head gets its own bounded
    stage attempts. ``None`` is reserved for an unreconstructable submission, where the caller uses
    the human fallback. Never parks or force-merges here."""
    from agentflow import coordinated_build
    from agentflow.coordinator.store import StoreUnavailable

    head = _run(["git", "-C", cfg.workdir, "rev-parse", f"origin/{branch}"])
    if head.returncode != 0 or not head.stdout.strip():
        return f"#{pr}: conflict — head unreadable, retry next cycle"
    head_sha = head.stdout.strip()
    try:
        records = coordinated_build.tracer.load_records()
    except StoreUnavailable:
        return f"#{pr}: conflict — coordinator state unreadable, retry next cycle"
    priors = coordinated_build.conflict_revises_used(records, cfg.repo, n)
    if any(r.target == head_sha for r in priors):
        return f"#{pr}: conflict — revise already open"   # idempotent under re-reconcile
    conflict_round = len(priors) + 1
    submission = coordinated_build.survivor_conflict_revise_submission(
        cfg, issue=n, slug=sl, builder_tool=tool, head_sha=head_sha, pr_number=pr,
        conflict_round=conflict_round)
    if submission is None:
        return None                                       # unreconstructable → caller parks
    if not _claim(cfg.repo, n):
        return f"#{pr}: conflict — could not claim conflict revise"
    coordinator = coordinated_build.build_coordinator()
    coordinator.submit_stage(submission)
    coordinated_build.reconcile_and_project(coordinator)
    return f"#{pr}: conflict — revise round {conflict_round} opened"


def _rebase_survivor(cfg: RepoConfig, pr: int, branch: str, profile: str) -> str:
    """Re-rebase one survivor and route the outcome by the repo's profile. On conflict, open a
    conflict Revise to resolve it (ADR 0038); park only when that stage cannot be reconstructed or
    genuinely fails its bounded attempts. On a clean re-rebase: `autonomous` reruns the merge gate
    and lands one; `reviewed`/`guarded` just leave the PR mergeable again for the human — never a
    merge (ADR 0002)."""
    m = _BRANCH_RE.match(branch)
    if not m:
        return f"#{pr}: unrecognized branch {branch}"
    tool, n, sl = m.group(1), int(m.group(2)), m.group(3)
    wt = Path(_builder_worktree(cfg, tool, n, sl))
    result = _rebase_branch(cfg, branch, wt)
    if result is RebaseResult.CONFLICT:
        revised = _conflict_revise_survivor(cfg, pr, n, sl, tool, branch)
        if revised is not None:
            return revised
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
