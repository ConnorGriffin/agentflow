"""The durable instruction text agentflow hands a provider session (ADR 0018).

Every unattended stage is, in the end, a block of prose: what to build, how to revise,
how to answer a maintainer, how to draw a round of mockups — plus the two park reasons a
human reads when a stage cannot finish. The wording is the product here, so it lives in
one place rather than inside the module that happens to dispatch a stage.

Each prompt is a format template; its ``{{...}}`` fields are filled by the stage that
submits the session, and the two charter gates it states — plain-language PR bodies and
before/after screenshots for a user-facing surface — are enforced elsewhere. Nothing in
this module runs anything.
"""

from __future__ import annotations

from agentflow.runner import MockupScope
from agentflow.screenshot_crib import SCREENSHOT_HARNESS
from agentflow.shell_crib import SHELL_CRIB


# The park reason for the mechanical UI-evidence gap (ADR 0018) — the human needs to
# know the block is the missing screenshot, not the review verdict. Unwaivable, so it
# reads the same whether a reviewed repo hands over or an autonomous one drops out.
UI_GAP_REASON = ("touches a user-facing surface but has no before/after screenshot — the "
                 "charter requires visual proof it matches the locked design, so it can't "
                 "merge unseen (ADR 0018)")


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

MOCKUP_DISCLAIMER = "> *agentflow intake: mockup variants — generated by AI.*"

# Scope-specific draw instructions threaded into PRODUCE_PROMPT (ADR 0048). A `surface` round
# reopens the whole visual world (the classic 3-4 wildly-different tournament); a `local` round
# inherits the shipping surface's identity and varies only the addition. Intake classifies the
# scope; `mockup_scope_from_labels` recovers it from the parked issue's label.
SCOPE_GUIDANCE = {
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

CONFLICT_REASON = (
    "`main` moved since this PR last rebased and it no longer merges cleanly. Rebase it by "
    "hand (or close it) — agentflow won't force a conflicted merge.")

# A survivor that re-rebased clean but still can't auto-merge (review not clean, CI red, or
# an unanswered question) hands off to a human rather than churning a revise here.
