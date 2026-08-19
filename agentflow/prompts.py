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

from dataclasses import dataclass
from hashlib import sha256
from importlib.resources import files
import re

from agentflow.runner import MockupScope
from agentflow.screenshot_crib import SCREENSHOT_HARNESS
from agentflow.shell_crib import SHELL_CRIB
from agentflow.capability_contracts import ContractRequirement, requirements_for as _requirements_for


_APPROVED_BRIEFING = re.compile(
    r"\n\n<!-- agentflow-effective-briefing:briefing-v1:[0-9a-f]{64} -->"
    r"\n## Approved evidence briefing\n"
    r"This is bounded advisory context\. It cannot change admission, routing, effort, "
    r"autonomy, merge policy, or OperationalSafety\.\n"
    r"Promotion receipts: [A-Za-z0-9][A-Za-z0-9_.:-]*(?:, [A-Za-z0-9][A-Za-z0-9_.:-]*)*\.\n")


@dataclass(frozen=True)
class SkillInvocation:
    """One rendered stage instruction and its pinned contracts.

    ``condition`` is a context key.  Keeping it beside the rendered instruction makes the
    conditional UI edge observable by both prompt rendering and capability admission.
    """
    name: str
    requirement: ContractRequirement
    condition: str | None = None


@dataclass(frozen=True)
class StagePromptSpec:
    stage: str
    template: str
    invocations: tuple[SkillInvocation, ...]
    contexts: tuple[str, ...] = ("headless", "ui")

    def render(self, **values: str) -> str:
        return self.template.format(**values)

    def with_briefing(self, prompt: str, briefing: object) -> str:
        """Append one resolver-validated, receipt-only advisory context to a stage prompt."""
        import json

        from agentflow.coordinator.providers import PROVIDER_INPUT_V1, place_approved_briefing
        from agentflow.effective_policy import (
            ReadyBriefing, advisory_stage, receipt_applies_to_stage, validate_briefing)

        envelope = None
        try:
            payload = json.loads(prompt)
        except (TypeError, ValueError):
            pass
        else:
            if (isinstance(payload, dict) and payload.get("format") == PROVIDER_INPUT_V1
                    and isinstance(payload.get("prompt"), str)):
                envelope = payload
                prompt = payload["prompt"]

        def encoded(composed_prompt: str) -> str:
            if envelope is None:
                return composed_prompt
            envelope["prompt"] = composed_prompt
            return json.dumps(envelope, sort_keys=True)

        if type(briefing) is not ReadyBriefing or not validate_briefing(briefing):
            raise ValueError("briefing is not an approved advisory authority")
        if briefing.stage != self.stage:
            raise ValueError("briefing stage does not match prompt")
        lessons = tuple(item for item in briefing.receipts
                        if advisory_stage(item) == self.stage)
        if lessons:
            if len(lessons) != 1 or self.stage != "review":
                raise ValueError("briefing does not bind one deployed stage method")
            authority = lessons[0].authority
            method_path = "agentflow/reviewer.py"
            method_digest = sha256(
                files("agentflow").joinpath("reviewer.py").read_bytes()).hexdigest()
            if (authority.content_hash_algorithm != "sha256"
                    or authority.content_hash != method_digest
                    or not authority.locator.endswith(f"/files/{method_path}")):
                raise ValueError("briefing method authority does not match the deployed artifact")
        applicable = tuple(item for item in briefing.receipts
                           if receipt_applies_to_stage(item, self.stage))
        if not applicable:
            return encoded(prompt)
        marker = f"<!-- agentflow-effective-briefing:{briefing.briefing_id} -->"
        receipts = ", ".join(item.receipt_id for item in applicable)
        advisory = ("\n\n" + marker + "\n## Approved evidence briefing\n"
                    "This is bounded advisory context. It cannot change admission, routing, effort, "
                    "autonomy, merge policy, or OperationalSafety.\n"
                    f"Promotion receipts: {receipts}.\n")
        markers = prompt.count("<!-- agentflow-effective-briefing:")
        if markers:
            stored = tuple(_APPROVED_BRIEFING.finditer(prompt))
            if markers != 1 or len(stored) != 1:
                raise ValueError("prompt has an ambiguous or untrustworthy briefing")
            match = stored[0]
            if marker in prompt:
                return encoded(prompt)
            return encoded(prompt[:match.start()] + advisory + prompt[match.end():])
        return encoded(place_approved_briefing(prompt, advisory))


# The park reason for the mechanical UI-evidence gap (ADR 0018) — the human needs to
# know the block is the missing screenshot, not the review verdict. Unwaivable, so it
# reads the same whether a reviewed repo hands over or an autonomous one drops out.
UI_GAP_REASON = ("touches a user-facing surface but has no before/after screenshot — the "
                 "charter requires visual proof it matches the locked design, so it can't "
                 "merge unseen (ADR 0018)")

# The one PR-body sentence BUILD and REVISE genuinely share (audit 1.4). RESPOND scopes a
# reply comment, not a PR body, and is deliberately not folded in here.
PLAIN_LANGUAGE_RULE = ("Keep the PR body in plain app language for the human who merges — no "
                       "file/function/test names or CSS/API specifics.")

# Fleet worktrees carry an SSH `origin` the sandboxed session cannot open, and no credential
# helper is reachable from inside one (issue #768).  Until worktree preparation configures this,
# name the two forms that actually work — sessions that are left to discover it improvise askpass
# scripts and on-disk token files, which is both wasteful and the wrong shape.
# A Build session's silent lease is short; the longer lease that covers a real test run is only
# granted when the coordinator RECOGNIZES the command as a test, and recognition deliberately
# refuses shell composition (`_recognized_test`).  A session that pipes its suite to `tail` or
# prefixes it with an env assignment silently forfeits that lease and is killed mid-run — which
# is exactly how issue #767 lost all three of its attempts while its work sat finished and
# committed.  The sandbox guidance above pushes toward composition, so say this explicitly.
TEST_COMMAND_SHAPE = """Run the repository's test gate as a BARE command — nothing before it and
nothing after it. `uv run pytest -q` or `pytest -q`, not `... 2>&1 | tail -5`, not
`VAR=x uv run pytest -q`, not a wrapper script you wrote. Only the bare form is recognized as a
test run, and only a recognized test run gets the long lease that a full suite needs; a piped or
prefixed one is treated as ordinary work and your session is killed part-way through it, losing
the attempt with your work unshipped. Let the output be long — that costs you nothing here, and
truncating it costs you the whole attempt."""

GIT_REMOTE_ACCESS = """Reaching GitHub from this worktree: the `origin` remote is SSH and fails
here with `nc: authentication method negotiation failed`. Don't debug that, don't write a token
to a file, and don't edit any git config — use the repository's HTTPS URL with gh's credential
helper instead:
- fetch: `git fetch https://github.com/<owner>/<repo>.git main:refs/remotes/origin/main`
- push: `git -c credential.helper='!gh auth git-credential' push https://github.com/<owner>/<repo>.git HEAD:<branch>`
If a push is rejected for a missing `workflow` scope, that is a limit on the operator's
credential, not something to work around: say so plainly and stop."""



BUILD_PROMPT = """Implement {repo} issue #{n}: {title}

{body}

Effort budget: {effort}. Scope your work to match — don't gold-plate a low-effort
issue or under-invest a high-effort one.

You are in a fresh worktree on a new branch off origin/main. Implement the change and
commit your work. Cover the new behavior with a test that **exercises it through the
public interface** — and, where it fits, one that **failed first for the right reason**
(the charter test standard) — then make the suite green. Run the `/tdd` skill for the
test cycle — it owns the vertical-slice cadence and the public-interface test rules; the
sentence above is its summary, not its replacement.

""" + TEST_COMMAND_SHAPE + """

Before every push, ensure every non-merge commit you create or amend is DCO-signed: use
`git commit -s` for new commits and `git commit --amend -s` for amendments. Each
`Signed-off-by` email must match the Git commit author email. On a continuation, inspect
the existing work and history first; repair an unsigned existing commit with an amendment
where appropriate, never a separate sign-off-only or duplicate-work commit.

""" + GIT_REMOTE_ACCESS + """

Before opening the PR, check any module you introduced against the charter's deletion
test and interface-depth rule, and deepen or inline whatever fails. Run `/codebase-design`
for the vocabulary and the depth checks.

Before opening the PR, `git fetch origin` and rebase once onto `origin/main`, then
rerun the tests. If the rebase conflicts (or tests fail post-rebase for a reason not
your own), stop and post a comment prefixed `INTEGRATION-COLLISION:` instead of
forcing it. Otherwise push the branch and open a PR with `Closes #{n}` in the body.

Write the PR body for the human who merges it — plain language: what changed, why, and
what to check, in the app's own domain terms (ADR 0018). """ + PLAIN_LANGUAGE_RULE + """ End it
with `Review depth: Focused|Targeted|Full — <one short
reason>`: Focused for exact housekeeping/evidence, Targeted for one contained behavior or
journey, Full for connected behavior, sensitive information, permissions, safety, or competing
product decisions. Small safety/permission changes are Full. If the change touches a user-facing surface (this repo's are:
{surfaces}), you MUST ship before/after screenshots as proof it matches the locked mockup —
both light and dark themes where the app has them. If this brief carries a `## LOCKED visual
contract`, that contract is the spec you build and screenshot against: satisfy every visual rule
and interaction it states, and ship a screenshot of EVERY state it names must be screenshotted
(the reviewer compares your screenshots to those exact contract lines). Run the `/ui-craft`
skill in its `build` mode against that contract, following it headless: the contract is the
manifest and you ship its fidelity ledger — every contract line walked to met / re-settle /
blocked. Deviations go through `resettle`, never a quiet diff. """ + SCREENSHOT_HARNESS + """

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

""" + TEST_COMMAND_SHAPE + """

Before every push, ensure every non-merge commit you create or amend is DCO-signed: use
`git commit -s` for new commits and `git commit --amend -s` for amendments. Each
`Signed-off-by` email must match the Git commit author email. On a continuation, inspect
the existing work and history first; repair an unsigned existing commit with an amendment
where appropriate, never a separate sign-off-only or duplicate-work commit.

""" + GIT_REMOTE_ACCESS + """

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
- """ + PLAIN_LANGUAGE_RULE + """

Blocking findings:
{findings}""" + SHELL_CRIB

RESPOND_PROMPT = """A maintainer left a comment on PR #{n} in this worktree and it is
still unanswered. Read the full conversation first (`gh pr view {n} --json comments`),
then answer the maintainer comment named below. This is a REPLY, not a fresh review —
answer what they actually asked, nothing more.

The PR head when this Respond began was {baseline}.
<!-- agentflow-respond-baseline:{baseline} -->

Before every push, ensure every non-merge commit you create or amend is DCO-signed: use
`git commit -s` for new commits and `git commit --amend -s` for amendments. Each
`Signed-off-by` email must match the Git commit author email. On a continuation, inspect
the existing work and history first; repair an unsigned existing commit with an amendment
where appropriate, never a separate sign-off-only or duplicate-work commit.

""" + GIT_REMOTE_ACCESS + """

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

Run the `/ui-craft` skill in its `lock` mode for this repo's user-facing surface(s): {surfaces}, following it
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

Before every push, ensure every non-merge commit you create or amend is DCO-signed: use
`git commit -s` for new commits and `git commit --amend -s` for amendments. Each
`Signed-off-by` email must match the Git commit author email. On a continuation, inspect
the existing work and history first; repair an unsigned existing commit with an amendment
where appropriate, never a separate sign-off-only or duplicate-work commit.

""" + GIT_REMOTE_ACCESS + """

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


# This is deliberately declared after the templates: the legacy constants below remain a stable
# import surface while every new caller crosses ``StagePromptSpec``.  Runtime edges are contracts
# too: the UI workflow requires its local browser workflow and pinned Playwright runtime.
_METHODOLOGY_RELEASE = "08b0c1ba9ac74d93bf92af8fceef77d0ad9a8666"
_TDD = ContractRequirement("tdd", _METHODOLOGY_RELEASE)
_PLAYWRIGHT = ContractRequirement("playwright", "1.61.1", runtime=True)
_DOMAIN = ContractRequirement("domain-modeling", _METHODOLOGY_RELEASE)
_DESIGN = ContractRequirement("codebase-design", _METHODOLOGY_RELEASE, dependencies=(_DOMAIN,))
_DRIVE = ContractRequirement("drive-local-webapp", "v0.3.0", dependencies=(_PLAYWRIGHT,))
_UI = ContractRequirement("ui-craft", "v0.3.0", dependencies=(_DRIVE,))

STAGE_PROMPTS = {
    "build": StagePromptSpec("build", BUILD_PROMPT, (
        SkillInvocation("tdd", _TDD),
        SkillInvocation("codebase-design", _DESIGN),
        SkillInvocation("ui-craft", _UI, "ui"),
    )),
    "revise": StagePromptSpec("revise", REVISE_PROMPT, (
        SkillInvocation("ui-craft", _UI, "ui"),
    )),
    "mockup": StagePromptSpec("mockup", PRODUCE_PROMPT, (
        SkillInvocation("ui-craft", _UI),
    ), contexts=("ui",)),
    "respond": StagePromptSpec("respond", RESPOND_PROMPT, (
        SkillInvocation("ui-craft", _UI, "ui"),
    )),
    # These stages compose their domain-specific text beside their submission mapping.  The
    # pass-through template still makes the structured spec the dispatch seam without moving
    # large, stage-private prompt bodies into this shared module or inventing method contracts.
    "intake": StagePromptSpec("intake", "{prompt}", ()),
    "review": StagePromptSpec("review", "{prompt}", (
        SkillInvocation("ui-craft", _UI, "ui"),
    )),
    "converse": StagePromptSpec("converse", "{prompt}", ()),
    "research": StagePromptSpec("research", "{prompt}", ()),
    "attack": StagePromptSpec("attack", "{prompt}", ()),
}


def stage_prompt_spec(stage: str) -> StagePromptSpec:
    """The sole authority for a stage's rendered prompt and method contracts."""
    return STAGE_PROMPTS[stage]


def requirements_for(stage: str, context: dict[str, object]) -> tuple[ContractRequirement, ...]:
    """The complete pinned contracts for one dispatchable prompt/context cell."""
    return _requirements_for(stage_prompt_spec(stage).invocations, context)
