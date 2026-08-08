# ADR 540 — Bounded review follow-up proposals

- Status: Accepted
- Date: 2026-08-08

## Context

ADR 0047 required a reviewer to create, deduplicate, and prove a new GitHub issue before a
necessary follow-up could settle. That made a review run manufacture tracker work, coupled the
current PR's completion to a separate issue-write operation, and preserved unbounded review
evidence in its public park comment.

The operator needs a review outcome that can be acted on without copied logs or an agent-created
issue. Already-filed follow-ups remain useful historical references, but they are not current
review work and must not prevent a later review from carrying its one current proposal.

## Decision

This amends ADR 0047's **Review actions replace “nit”**, durable-state, final-summary, and park
sections.

1. A `necessary_follow_up` finding carries exactly one concise proposal with evidence and a
   desired outcome. A reviewer does not create, file, search for, or validate a GitHub issue as
   part of this action. The proposal never blocks the current PR's clean verdict.
2. A review session's terminal schema contains no issue URL or issue-creation field. Its durable
   session events are audited before completion: a captured `gh issue create` command makes the
   verdict unaccepted. Git transport and ordinary tracker reads remain available for
   reviewer-authored in-scope fixes and evidence gathering.
3. Durable review state retains any already-filed follow-up URLs as historical references and at
   most one non-historical proposal. A later pass replaces the current proposal while retaining
   historical references; it never accumulates multiple live proposals.
4. A clean summary names the one proposed follow-up and separately labels historical references.
   It does not imply that either was filed by the current review.
5. A park is a fixed, operator-sized two-section envelope capped at 2,000 characters. It always
   states the affected behavior, options, consequences, recommendation, a bounded code-location
   reference, unresolved fact or conflict, completed check, retained work, and exact next action.
   Each dynamic field is compacted before interpolation; accumulated findings, logs, fixes, and
   historical URLs are intentionally excluded.

## Consequences

- Review no longer creates or has to prove new tracker work in order to finish a PR.
- Maintainers receive one actionable outcome and concise decision context; they decide whether a
  proposed follow-up deserves a separately created issue.
- Historical references survive compatible record rewrites without turning into new proposals.
- ADR 0047's issue-filing and unbounded-public-handoff requirements no longer apply.
