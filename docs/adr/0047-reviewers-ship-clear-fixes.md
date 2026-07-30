# ADR 0047 — Review depth, cross-tool fix review, and conflict decisions

- Status: Accepted
- Date: 2026-07-22

## Context

Review was one deep, report-only pass followed by at most one builder revision. That model made
clear reviewer-discovered fixes wait for another session, then let the same reviewer approve its
own changed head. It also treated all changes alike: wording and evidence cleanup received the same
review as a permission or product decision, while follow-up issue claims were trusted without proof.

The failure was visible in two guarded-project PRs. Review found clear held-reason,
evidence, summary, and helper defects which agents later shipped after maintainer direction. During the first PR's
conflict response, the requested resolution was pushed, the conflict was gone, checks passed, and
the marked explanation was posted; an unrelated local scratch file nevertheless made Respond look
incomplete. Two unnecessary continuations then produced a false park. A separate
reviewed-project PR supplies the different case: the PR was proven, but a reusable browser-walkthrough gap was a necessary follow-up
outside that PR's purpose.

Conflict recovery also counted conflicts across the PR lifetime. That parked normal work when
`main` moved again instead of bounding each genuinely new resolution. Prompt-only
`MISSING-CONTEXT:` comments did not durably represent the narrow two-tool decision path needed when
product intent really was ambiguous.

This decision amends ADRs 0003, 0004, 0015, 0020, 0028, 0038, 0043, and 0044. Exact-head merge
safety, mechanical evidence gates, and autonomy profiles remain in force.

## Decision

### Review depth follows complexity and stakes

Every review is **Focused**, **Targeted**, or **Full**. The change author proposes a depth with one
short reason. Agentflow enforces minimums for sensitive areas. Each later reviewer may escalate but
never downgrade.

- **Focused** covers wording, PR/evidence links, screenshots, styling, formatting, duplicate
  evidence, one-off helpers, and similar housekeeping. It verifies the exact change, its proof, and
  that nothing else moved.
- **Targeted** covers one contained product behavior or user journey. It verifies that behavior and
  only the project rules relevant to it.
- **Full** covers connected behaviors, sensitive information, permissions, safety, or competing
  product decisions. It begins with separate product-outcome and project-standards passes; the
  orchestrator combines those findings and assigns one reviewer to ship clear fixes.

Size does not determine stakes: a one-line permission or safety change is Full, while a large
evidence cleanup may remain Focused. Verification scales with depth: exact proof for Focused,
affected-behavior checks for Targeted, and all required checks for Full. Follow-up reviews examine
the latest changes plus necessary context at Focused or Targeted depth; a Full follow-up examines
the whole PR.

Guidance includes these escalation anchors:

- Focused to Targeted when a screenshot-link correction reveals the screenshot no longer matches
  the app.
- Focused to Full when one line changes the meaning of a destructive or safety-sensitive action.
- Targeted to Full when a contained journey changes a shared decision used elsewhere.
- Conflict to Full when an apparent wording conflict chooses between product behaviors.

### Review actions replace “nit”

Every finding receives one of four actions:

1. **Fix before completion.** Ship a clear, grounded, in-scope correction on the PR branch.
2. **File a necessary follow-up.** Use only for a real improvement outside the PR's purpose, with
   evidence, a clear desired outcome, and proof the repository's issue tracker was searched for a
   duplicate. Agentflow validates the recorded issue against GitHub before accepting it.
3. **Ask the maintainer.** Use only when proceeding would require an unresolved product decision.
4. **Discard as unsupported reviewer preference.** Do not turn ungrounded taste into work.

The repository's `AGENTS.md`, engineering charter, project documentation, explicit maintainer
direction, and established application behavior are grounding, not personal preference. “Personal
preference” means only the reviewer's unsupported taste.

Calibration is concrete. Inconsistent held-reason styling, duplicate screenshots,
jargon-heavy summaries, broken before-evidence, and a committed hardcoded helper are
fix-before-completion work. An absent browser walkthrough is a necessary follow-up
when the current PR was proven and the reusable checking gap was real. A request for different wording
when the existing wording is clear, correct, and project-grounded is discarded.

### Every reviewer-authored head is cross-tool reviewed

Reviewers may push clear fixes to the same PR branch, but never approve their own changed head.
Every completed fix is pushed before the next pass; intermediate coordination and findings remain
private in agentflow's durable state, and no intermediate GitHub comment is posted.

The next agent receives the current pushed PR, the exact reviewed range and changes since the last
pass, what changed and why, assigned depth, completed proof, and unresolved concerns. It verifies
that handoff rather than trusting it. Every new change set in an autonomous repository is approved
by the other tool. A Claude build followed by a Codex fix therefore receives a Claude scoped review;
if Claude changes it, Codex reviews that new head. The chain ends only when the other tool reviews
and makes no change. It parks after three consecutive change-making review passes, which signals
drift or disagreement rather than permission for an infinite loop.

The orchestrator never edits and never runs concurrent editors. Every later agent starts from the
PR's current pushed state. Retained local work belongs only to a continuation of the same interrupted
task; a later Revise or Respond may not resume from the stale builder state after a reviewer push.

Durable review state records depth and reason, Full-review axis when applicable, change-author tool,
reviewed start and final heads/range, consecutive change-making pass count, cross-tool coverage,
same-tool taint, private handoff, shipped fixes, validated follow-ups, and unresolved uncertainty.
Any reviewer-authored pushed head therefore remains unmergeable until another tool reviews that exact
head cleanly. Empty or missing push provenance can never prove a mutating review.

### Availability and taint are explicit

Cross-tool review is required for autonomous repositories and preferred elsewhere. An autonomous PR
holds open indefinitely without consuming capacity when the other tool is unavailable; it neither
fails nor parks.

`/agentflow review <pr>` may force a same-tool review only after a warning and maintainer
confirmation. That PR becomes **human-merge-only**, or **tainted**. If the other tool returns while
the PR is still open, agentflow automatically performs the proper cross-tool review; a clean review
clears the taint.

Reviewed repositories retain immediate progress: use cross-tool review when the other tool is
available now, otherwise run a fresh same-tool review. The maintainer still merges, and the final
summary says exactly: “same-tool review; maintainer merge required.”

### Conflict resolution preserves compatible outcomes

Current `main` and the PR both carry intended behavior. A resolver preserves both whenever they are
compatible; neither side is discarded merely because it is newer. When they encode incompatible
product intent, the resolver does not privilege `main`: it records both options for the private
second opinion. Each genuinely new
conflict caused by `main` moving receives a new bounded resolution stage. There is no PR-lifetime
conflict count. The same conflict continues only after interruption or real retained progress; the
same unresolved state is never blindly repeated.

When product intent is genuinely ambiguous, the first resolver durably records both options, the
exact missing guidance, and its recommendation in private state. Agentflow performs one narrow,
in-flow handoff to the other tool without a GitHub comment. If that agent resolves the choice, the
pipeline continues normally. If both remain unsure, agentflow parks once with the exact maintainer
decision needed.

Conflict/Respond completion is proved from the PR: targeted reply, pushed resolution, conflict gone,
checks green, and explanation posted. Relevant unpushed work blocks. Unrelated temporary local files
do not. Each resolution is then reviewed at the depth of the actual choice: wording may be Focused;
competing behaviors are Full.

### Only the final outcome is public

Existing dashboard `reviewing` status is sufficient; no new user-facing dashboard state is added.
Maintainer guidance received during work enters at the next safe boundary; explicit `stop` or `park`
interrupts immediately.

Agentflow posts exactly one final clean review summary or one final park. A clean summary contains
the outcome; depth and reason; shipped fixes; necessary follow-ups; checks and proof; and cross-tool
or same-tool status.

Every park has two sections and begins in domain language. The first heading names the real
boundary rather than inventing product ambiguity:

1. **Maintainer decision needed** only for unresolved product intent; otherwise **Action needed:**
   affected application behavior, choices, consequences, and the agents' recommendation.
2. **Agent handoff:** code locations, conflicting changes, checks, retained work, and the exact next
   action.

## Alternatives considered

- **Let a mutating reviewer approve its own final diff.** Rejected: re-reading one's work is not an
  independent review, and it loses exact-head authorship provenance.
- **Return all findings to the original builder.** Rejected: it wastes a session on clear fixes and
  can resume a stale builder checkout after the reviewer has advanced the branch.
- **Treat all reviews as Full.** Rejected: proof and attention should scale with actual choices and
  stakes, not line count or a universal maximum.
- **Keep “blocking” and “nit.”** Rejected: severity alone cannot distinguish fix-now work, a proven
  necessary follow-up, an actual maintainer decision, and unsupported taste.
- **Trust follow-up URLs emitted by an agent.** Rejected: an assertion is not proof that the issue
  exists in the repository or was deduplicated.
- **Make a dirty local checkout fail Respond completion.** Rejected: the PR is authoritative; only
  relevant unpushed work belongs in that completion boundary.
- **Keep a PR-lifetime conflict cap.** Rejected: it counts normal movement of `main`, not repeated
  failure to resolve the current conflict. Per-stage attempts and the one narrow uncertainty handoff
  are the bounded mechanisms.

## Consequences

- Clear fixes ship without maintainer round trips, while every changed head retains independent
  cross-tool approval.
- Review becomes a depth-aware orchestration chain rather than a single verdict. Full review has two
  read passes before one assigned editor; all later editing remains serialized.
- Autonomous work waits safely through cross-tool outages. Maintainer-confirmed same-tool review is
  visible and can never auto-merge while tainted.
- Follow-ups are real repository issues with evidence and duplicate-search proof, not output strings.
- New conflicts remain bounded per actual resolution, and genuine ambiguity has one durable
  cross-tool decision handoff before a single precise park.
- Respond stops wasting continuations on unrelated scratch files while still rejecting relevant
  unpushed resolution work.
