# ADR 0015 — Review anchors to the issue's acceptance criteria

- Status: Accepted
- Date: 2026-07-09

## Context

The first live run surfaced a review-strictness question. The cross-tool reviewer
(Codex) flagged a real Unicode edge case (`'café' → 'caf'`) as **blocking** on a
`slugify` PR. But issue #1's acceptance criteria were all-ASCII, and the build met
them exactly — the reviewer applied its *own* correctness bar beyond the stated
scope.

Left unbounded, an ever-stricter reviewer can block indefinitely: the builder can
never "win," and "done" is undefined — it becomes whatever the reviewer happens to
accept this round. The loop may not converge.

## Decision

**The reviewer judges against the issue's stated acceptance criteria, not its own
wishlist.** The review prompt carries the issue's acceptance criteria, and:

- **Blocking** is reserved for a real bug/security hole that **breaks a stated
  acceptance criterion**, or a **charter violation** (shallow module, unmocked UI,
  interface you can't test through).
- A correctness gap **beyond** the stated acceptance is not automatically merge-blocking.
  ADR 0047 requires the reviewer to distinguish a proven necessary follow-up from unsupported
  scope growth, validate any filed issue, and discard mere preference.

## Alternatives considered

- **Thorough — flag any real correctness issue, even out of scope, as blocking.**
  Rejected: "done" becomes undefined and the loop can fail to converge; a moving bar
  means the builder can't satisfy it. Real out-of-scope bugs are not lost — they
  become follow-up issues, not merge blockers.

## Consequences

- **Well-scoped issues become load-bearing:** the acceptance criteria *are* the merge
  bar, so a vague issue yields a weak review. This reinforces intake/triage's job of
  writing crisp acceptance criteria (and is why `guarded` freezes them at scope time).
- The reviewer must receive the acceptance criteria — `loop.run_once` passes the
  issue body into `Reviewer.review(..., acceptance=…)`.
- Genuinely necessary out-of-scope findings are captured as validated issues rather than silently
  dropped or used to block; ungrounded expansion creates no work.
